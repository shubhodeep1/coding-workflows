#!/usr/bin/env bash
# Unblock judge (docs/plans/replace-claude-sessions-with-cli-engine-plan.md
# Phase 7, Q10-Q13).
#
# One run judges one blocked item: an issue, a pull request or an orchestrator
# project (its tracking issue). The poller's unblock scan dispatches it through
# unblock_judge_dispatch.yml. The run:
#   1. reads the item, its comments and, for a project item, the tracking
#      issue's comments, and works out the stop id (scripts/unblock_ledger.py);
#   2. if an earlier `descope` / `operator_step` verdict is waiting on its
#      fix-up issue, checks the fix-up instead: still open, it refreshes the
#      wait marker and stops; merged, it posts the stop's resume command
#      (scripts/unblock_actions.py followup) and stops;
#   3. fingerprints the failure, asks the ledger what is still allowed
#      (never repeat a verdict per fingerprint, 2 rounds per item, 6 per
#      project, the terminal close), and, unless only the close is left, asks
#      the UNBLOCK_JUDGE role (prompts/mode-judge-unblock.txt) for a verdict;
#   4. validates the verdict against the ledger and the hard limits, posts it
#      with its <!-- ai:unblock:v1 ... --> marker on the item (and on the
#      tracking issue for a project's item), and runs the operations
#      scripts/unblock_actions.py plans for it.
#
# Usage: unblock_judge.sh            Env: REPOSITORY, ITEM.
# Optional env: SUPPORT_DIR (checkout with scripts/ and prompts/, default this
# script's repository root), TARGET_DIR (read-only checkout of the default
# branch for the model; default SUPPORT_DIR), RUNTIME_DIR,
# UNBLOCK_JUDGE_ENABLED (default true), UNBLOCK_JUDGE_MODEL,
# UNBLOCK_JUDGE_REASONING, UNBLOCK_JUDGE_TIMEOUT_SECS (default 1500),
# UNBLOCK_JUDGE_FIXUP_WAIT_HOURS (default 72), MOCK_UNBLOCK_JUDGE_JSON (tests
# only: used instead of the model), MOCK_UNBLOCK_JUDGE_NOW (tests only).
#
# Both engines run in a network-isolated container with a host-side provider relay;
# its output is data, and verdicts containing literal or encoded credentials
# are rejected before only ledger-approved operations are acted on.
# A PR binds to a project only with a same-repository head, an ai/issue-<n>
# head branch, and <n> in pipeline-authored project state.
#
# Never fails its caller: every problem is logged and the exit code is 0.
# API budget (CLAUDE.md §15), per run: one `user` read, the item, its
# comments (paginated; fresh reads before recording a `project-failed`
# verdict and before its terminal label), the tracking issue's comments for a project's item,
# at most one linked-issue read, one fix-up read while waiting, the PR diff,
# at most three run-metadata reads to bind a run cited in a pipeline-authored
# comment to this item, then that run's log, one verdict comment (two for a
# project's item) and
# the planned operations (at most about six writes). A failed project marker
# is reconciled if the item stays blocked; actuation used the item ledger.
# Log: UNBLOCK_JUDGE item= kind= stop= fingerprint= verdict= round= outcome= reason=
# Project-failed fallback skips: project_state_unverified, project_not_failed,
# project_state_recheck_unavailable, project_resumed.
set -uo pipefail

UNBLOCK_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPPORT_DIR="${SUPPORT_DIR:-$(cd "${UNBLOCK_SELF_DIR}/.." && pwd)}"
TARGET_DIR="${TARGET_DIR:-${SUPPORT_DIR}}"
RUNTIME_DIR="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}/unblock-judge}"
mkdir -p "${RUNTIME_DIR}"
UNBLOCK_SOURCE_REPO="shubhodeep1/coding-workflows"
if [ -f "${SUPPORT_DIR}/scripts/label_helpers.sh" ]; then
	# shellcheck source=/dev/null
	source "${SUPPORT_DIR}/scripts/label_helpers.sh"
fi

unblock_log()
{
	echo "UNBLOCK_JUDGE $*"
}

unblock_py()
{
	PYTHONDONTWRITEBYTECODE=1 python3 "$@"
}

unblock_now()
{
	if [ -n "${MOCK_UNBLOCK_JUDGE_NOW:-}" ]; then
		printf '%s\n' "${MOCK_UNBLOCK_JUDGE_NOW}"
	else
		date -u +%Y-%m-%dT%H:%M:%SZ
	fi
}

# All comments of an issue or PR, oldest first, as one JSON array.
unblock_fetch_comments()
{
	local number="$1" out="$2"
	if ! gh api --paginate "repos/${REPOSITORY}/issues/${number}/comments?per_page=100" 2>/dev/null | jq -s 'add // []' > "${out}" 2>/dev/null; then
		echo '[]' > "${out}"
		return 1
	fi
	jq -e 'type == "array"' "${out}" >/dev/null 2>&1 || { echo '[]' > "${out}"; return 1; }
}

# Only the pipeline login's V2 state can authorize the unlabeled project-failed
# fallback. No V1 or untrusted-comment fallback is safe for this decision.
unblock_trusted_project_status()
{
	local comments_file="$1" out_state_file="$2"
	jq -e --arg login "${UNBLOCK_LOGIN}" '
		if type == "array" then [.[] | select(.user.login == $login)] else empty end
	' "${comments_file}" > "${RUNTIME_DIR}/project_state_comments.json" 2>/dev/null || return 1
	unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract \
		--require-latest --comments-json "${RUNTIME_DIR}/project_state_comments.json" > "${out_state_file}" 2>/dev/null || return 1
	jq -er 'if type == "object" and (.status | type == "string") then .status else empty end' \
		"${out_state_file}" 2>/dev/null
}

unblock_tg()
{
	local level="$1" text="$2"
	if [ -f "${SUPPORT_DIR}/scripts/tg_helpers.sh" ]; then
		# shellcheck source=/dev/null
		source "${SUPPORT_DIR}/scripts/tg_helpers.sh" 2>/dev/null || true
		if declare -F tg_send_msg >/dev/null 2>&1; then
			tg_send_msg "${text}" "${level}" >/dev/null 2>&1 || true
		fi
	fi
}

# The newest trusted wait marker: `<comment id> <fixup> <done|open> <iso time>`.
unblock_latest_wait()
{
	jq -r --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" '
		[.[] | select((.user.login // "") == $login)
			| . as $c
			| ((.body // "") | split("\n") | map(select(test("\\S"))) | last // "" | rtrimstr("\r")) as $last
			| ($last | capture("^<!-- ai:unblock-wait:v1 item=(?<item>[0-9]+) fixup=(?<fixup>[0-9]+)(?<done> done)? -->$")?) as $m
			| select($m.item == $item)
			| {id: $c.id, fixup: $m.fixup, done: (if ($m.done // "") != "" then "done" else "open" end), at: ($c.created_at // ""), updated: ($c.updated_at // $c.created_at // "")}]
		| sort_by(.at) | last // empty
		| "\(.id) \(.fixup) \(.done) \(.at) \(.updated)"
	' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null || true
}

unblock_patch_comment()
{
	local comment_id="$1" body="$2"
	gh api -X PATCH "repos/${REPOSITORY}/issues/comments/${comment_id}" -f body="${body}" >/dev/null 2>&1
}

unblock_hours_since()
{
	python3 - "$1" "$(unblock_now)" <<'PY'
import datetime as dt, sys
def parse(v):
	t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
	return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
try:
	print(int((parse(sys.argv[2]) - parse(sys.argv[1])).total_seconds() // 3600))
except ValueError:
	print(0)
PY
}

# Runs the operations of unblock_actions.py output, in order. A failed
# operation is logged and the rest still run, except a failed prerequisite
# must not close an item, and a failed close must not add the terminal label.
unblock_run_ops()
{
	local ops_file="$1" project_failed_guard="${2:-false}" count idx op issue number created body label close_failed="false" close_succeeded="false" ops_failed="false" terminal_project_status unblock_removed_guard=""
	count="$(jq '.ops | length' "${ops_file}" 2>/dev/null || echo 0)"
	for ((idx = 0; idx < count; idx++)); do
		op="$(jq -r ".ops[${idx}].op" "${ops_file}")"
		issue="$(jq -r ".ops[${idx}].issue // empty" "${ops_file}")"
		if [ "${ops_failed}" = "true" ] && [ "${op}" != "close" ] && [ "${op}" != "telegram" ] \
			&& ! { [ "${op}" = "add_labels" ] && [ "${close_succeeded}" = "true" ] && [ "${ITEM_KIND}" = "pr" ] \
				&& jq -e '.ops[0].op == "comment" and .ops[1].op == "close" and .ops[3].op == "telegram" and .ops[3].level == "WARNING"' "${ops_file}" >/dev/null 2>&1; }; then
			continue
		fi
		case "${op}" in
			comment)
				body="$(jq -r ".ops[${idx}].body" "${ops_file}")"
				if ! gh api "repos/${REPOSITORY}/issues/${issue}/comments" -f body="${body}" >/dev/null 2>&1; then
					ops_failed="true"
					unblock_log "item=${ITEM} op=comment issue=${issue} outcome=failed"
					if [ "${body}" = "/approved" ] && [ -n "${unblock_removed_guard}" ]; then
						# Keep a failed approval discoverable after its trigger label was cleared.
						if ! gh api -X POST "repos/${REPOSITORY}/issues/${issue}/labels" -f "labels[]=${unblock_removed_guard}" >/dev/null 2>&1; then
							unblock_log "item=${ITEM} op=restore_label issue=${issue} label=${unblock_removed_guard} outcome=failed"
							unblock_tg "CRITICAL" "Unblock judge could not restore ${unblock_removed_guard} on #${issue} after /approved failed (${REPOSITORY})."
						fi
					fi
				fi
				;;
			add_labels)
				while IFS= read -r label; do
					[ -n "${label}" ] || continue
					if [ "${close_failed}" = "true" ] && [ "${label}" = "ai:unblock-closed" ]; then
						continue
					fi
					if declare -F ensure_label_exists >/dev/null 2>&1; then
						ensure_label_exists "${label}" "${REPOSITORY}" >/dev/null 2>&1 || true
					fi
					if [ "${project_failed_guard}" = "true" ] && [ "${label}" = "ai:unblock-closed" ]; then
						if ! unblock_fetch_comments "${ITEM}" "${RUNTIME_DIR}/item_comments_terminal.json"; then
							unblock_log "item=${ITEM} kind=project op=add_labels outcome=skip reason=project_state_terminal_recheck_unavailable"
							return 1
						fi
						if ! terminal_project_status="$(unblock_trusted_project_status "${RUNTIME_DIR}/item_comments_terminal.json" "${RUNTIME_DIR}/project_state_trusted.json")"; then
							unblock_log "item=${ITEM} kind=project op=add_labels outcome=skip reason=project_state_terminal_unverified"
							return 1
						fi
						if [ "${terminal_project_status}" != "failed" ]; then
							unblock_log "item=${ITEM} kind=project op=add_labels outcome=skip reason=project_state_changed_before_terminal_label status=$(printf '%s' "${terminal_project_status}" | tr -cd 'a-z_-' | cut -c1-40)"
							return 1
						fi
					fi
					gh api -X POST "repos/${REPOSITORY}/issues/${issue}/labels" -f "labels[]=${label}" >/dev/null 2>&1 \
						|| { ops_failed="true"; unblock_log "item=${ITEM} op=add_labels issue=${issue} label=${label} outcome=failed"; }
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				;;
			remove_label)
				label="$(jq -r ".ops[${idx}].label" "${ops_file}")"
				if gh api -X DELETE "repos/${REPOSITORY}/issues/${issue}/labels/$(jq -rn --arg l "${label}" '$l | @uri')" >/dev/null 2> "${RUNTIME_DIR}/remove_label_error.txt"; then
					unblock_removed_guard="${label}"
				else
					if ! grep -q 'HTTP 404' "${RUNTIME_DIR}/remove_label_error.txt"; then
						ops_failed="true"
						unblock_log "item=${ITEM} op=remove_label issue=${issue} label=${label} outcome=failed"
					else
						unblock_removed_guard="${label}"
					fi
				fi
				;;
			create_issue)
				local -a create_args=(-f "title=$(jq -r ".ops[${idx}].title" "${ops_file}")" -f "body=$(jq -r ".ops[${idx}].body" "${ops_file}")")
				while IFS= read -r label; do
					[ -n "${label}" ] && create_args+=(-f "labels[]=${label}")
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				created="$(gh api "repos/${REPOSITORY}/issues" "${create_args[@]}" --jq '[.number, ([.labels[].name] | join(","))] | @tsv' 2>/dev/null || true)"
				local created_labels=""
				created_labels="${created#*$'\t'}"
				created="${created%%$'\t'*}"
				if ! [[ "${created}" =~ ^[0-9]+$ ]]; then
					ops_failed="true"
					unblock_log "item=${ITEM} op=create_issue outcome=failed"
					continue
				fi
				while IFS= read -r label; do
					[ -n "${label}" ] || continue
					if [[ ",${created_labels}," != *",${label},"* ]]; then
						ops_failed="true"
						unblock_log "item=${ITEM} op=create_issue outcome=labels_missing issue=${created}"
						break
					fi
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				[ "${ops_failed}" != "true" ] || continue
				number="$(jq -r ".ops[${idx}].wait_on // empty" "${ops_file}")"
				if [[ "${number}" =~ ^[0-9]+$ ]]; then
					gh api "repos/${REPOSITORY}/issues/${number}/comments" \
						-f body="Waiting for fix-up #${created} to merge; the unblock judge resumes this item afterwards.

<!-- ai:unblock-wait:v1 item=${number} fixup=${created} -->" >/dev/null 2>&1 \
						|| {
							ops_failed="true"
							unblock_log "item=${ITEM} op=wait_marker outcome=failed fixup=${created}"
							# Without the parent marker, a later verdict could create another fix-up.
							gh api -X PATCH "repos/${REPOSITORY}/issues/${created}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1 \
								|| unblock_log "item=${ITEM} op=orphan_fixup_close outcome=failed fixup=${created}"
						}
				fi
				;;
			edit_files_touched)
				jq -r '.body // ""' "${RUNTIME_DIR}/item.json" > "${RUNTIME_DIR}/item_body.txt"
				jq -c ".ops[${idx}].paths" "${ops_file}" > "${RUNTIME_DIR}/override_paths.json"
				if unblock_py - "${RUNTIME_DIR}/item_body.txt" "${RUNTIME_DIR}/override_paths.json" > "${RUNTIME_DIR}/item_body_new.txt" <<'PY'
import json, sys
body = open(sys.argv[1], encoding="utf-8").read().rstrip("\n")
paths = json.load(open(sys.argv[2], encoding="utf-8"))
lines = body.split("\n")
for idx, raw in enumerate(lines):
	if raw.strip() not in ("files_touched:", "- files_touched:"):
		continue
	base = len(raw) - len(raw.lstrip(" "))
	end = idx + 1
	indent = base + 2
	existing = set()
	for pos in range(idx + 1, len(lines)):
		candidate = lines[pos]
		if not candidate.strip():
			if existing:
				break
			continue
		cand_indent = len(candidate) - len(candidate.lstrip(" "))
		if cand_indent <= base or not candidate.strip().startswith("- "):
			break
		indent = cand_indent
		existing.add(candidate.strip()[2:].strip())
		end = pos + 1
	new = [" " * indent + "- " + path for path in paths if path not in existing]
	lines[end:end] = new
	print("\n".join(lines))
	break
else:
	print("\n".join(lines + ["", "files_touched:"] + ["  - " + path for path in paths]))
PY
				then
					gh api -X PATCH "repos/${REPOSITORY}/issues/${issue}" -f body="$(cat "${RUNTIME_DIR}/item_body_new.txt")" >/dev/null 2>&1 \
						|| { ops_failed="true"; unblock_log "item=${ITEM} op=edit_files_touched outcome=failed"; }
				else
					ops_failed="true"
					unblock_log "item=${ITEM} op=edit_files_touched outcome=failed"
				fi
				;;
			operator_step)
				jq -c ".ops[${idx}].steps" "${ops_file}" > "${RUNTIME_DIR}/operator_steps.json"
				unblock_py "${SUPPORT_DIR}/scripts/operator_step_issue.py" upsert --repo "${REPOSITORY}" \
					--key "$(jq -r ".ops[${idx}].key" "${ops_file}")" --source "$(jq -r ".ops[${idx}].source" "${ops_file}")" \
					--steps-file "${RUNTIME_DIR}/operator_steps.json" >/dev/null 2>&1 \
					|| { ops_failed="true"; unblock_log "item=${ITEM} op=operator_step outcome=failed"; }
				;;
			auto_decision)
				jq -c "{decisions: [.ops[${idx}].decision]}" "${ops_file}" > "${RUNTIME_DIR}/decisions.json"
				if unblock_py "${SUPPORT_DIR}/scripts/auto_decisions.py" render --comments-file "${RUNTIME_DIR}/item_comments.json" \
					--decisions-file "${RUNTIME_DIR}/decisions.json" --source "unblock judge" > "${RUNTIME_DIR}/ad.json" 2>/dev/null; then
					body="$(jq -r '.body' "${RUNTIME_DIR}/ad.json")"
					number="$(jq -r '.comment_id // empty' "${RUNTIME_DIR}/ad.json")"
					if [[ "${number}" =~ ^[0-9]+$ ]]; then
						unblock_patch_comment "${number}" "${body}" || { ops_failed="true"; unblock_log "item=${ITEM} op=auto_decision outcome=failed"; }
					else
						gh api "repos/${REPOSITORY}/issues/${issue}/comments" -f body="${body}" >/dev/null 2>&1 \
							|| { ops_failed="true"; unblock_log "item=${ITEM} op=auto_decision outcome=failed"; }
					fi
				else
					ops_failed="true"
					unblock_log "item=${ITEM} op=auto_decision outcome=failed"
				fi
				;;
			close)
				if [ "${ops_failed}" = "true" ] && ! { [ "${ITEM_KIND}" = "pr" ] \
					&& jq -e '.ops[0].op == "comment" and .ops[1].op == "close" and .ops[3].op == "telegram" and .ops[3].level == "WARNING"' "${ops_file}" >/dev/null 2>&1; }; then
					unblock_log "item=${ITEM} op=close issue=${issue} outcome=skip reason=prerequisite_failed"
					continue
				fi
				if [ "$(jq -r ".ops[${idx}].pr" "${ops_file}")" = "true" ]; then
					gh api -X PATCH "repos/${REPOSITORY}/pulls/${issue}" -f state=closed >/dev/null 2>&1 \
						&& close_succeeded="true" \
						|| { close_failed="true"; ops_failed="true"; unblock_log "item=${ITEM} op=close issue=${issue} outcome=failed"; }
				else
					gh api -X PATCH "repos/${REPOSITORY}/issues/${issue}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1 \
						&& close_succeeded="true" \
						|| { close_failed="true"; ops_failed="true"; unblock_log "item=${ITEM} op=close issue=${issue} outcome=failed"; }
				fi
				;;
			dispatch_review)
				number="$(jq -r ".ops[${idx}].pr" "${ops_file}")"
				local review_workflow="ai-review.yml"
				[ "${REPOSITORY}" = "${UNBLOCK_SOURCE_REPO}" ] && review_workflow="internal-review.yml"
				gh workflow run "${review_workflow}" -R "${REPOSITORY}" -f pr_number="${number}" >/dev/null 2>&1 \
					|| { ops_failed="true"; unblock_log "item=${ITEM} op=dispatch_review pr=${number} outcome=failed"; }
				;;
			telegram)
				if [ "${ops_failed}" = "true" ] && [ "${close_succeeded}" != "true" ]; then
					if [ "${ITEM_KIND}" = "project" ] && [ "$(jq -r ".ops[${idx}].level" "${ops_file}")" = "CRITICAL" ]; then
						unblock_tg "CRITICAL" "Unblock judge could not complete closure of project #${ITEM}; inspect the failed operation in the workflow log (${REPOSITORY})."
					elif [ "${ITEM_KIND}" = "issue" ] && [ "$(jq -r ".ops[${idx}].level" "${ops_file}")" = "CRITICAL" ] \
						&& jq -e 'index("ai:security") != null' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
						# A failed terminal-label write cannot hide an unresolved finding.
						unblock_tg "CRITICAL" "$(jq -r ".ops[${idx}].text" "${ops_file}") (${REPOSITORY})"
					elif [ "${ITEM_KIND}" = "pr" ] && jq -e '.ops[0].op == "comment" and .ops[1].op == "close" and .ops[3].op == "telegram" and .ops[3].level == "WARNING"' "${ops_file}" >/dev/null 2>&1; then
						unblock_tg "WARNING" "Unblock judge could not close untrusted PR #${ITEM}; a write failed (${REPOSITORY})."
					fi
					continue
				fi
				unblock_tg "$(jq -r ".ops[${idx}].level" "${ops_file}")" "$(jq -r ".ops[${idx}].text" "${ops_file}") (${REPOSITORY})"
				;;
			*)
				unblock_log "item=${ITEM} op=${op} outcome=skip reason=unknown_op"
				;;
		esac
	done
	[ "${ops_failed}" != "true" ]
}

# Normalised evidence for the fingerprint: the block's reason line and, for a
# pull request, its failing checks. Run ids, SHAs and long numbers are dropped
# so the same failure keeps the same fingerprint across runs.
unblock_evidence()
{
	unblock_py - "${RUNTIME_DIR}/item_comments.json" "${UNBLOCK_LOGIN}" "${RUNTIME_DIR}/failing_checks.json" "${ITEM_KIND}" "${ITEM}" <<'PY'
import json, re, sys
comments = json.load(open(sys.argv[1], encoding="utf-8"))
login, kind, item = sys.argv[2], sys.argv[4], sys.argv[5]
try:
	checks = json.load(open(sys.argv[3], encoding="utf-8"))
except (OSError, ValueError):
	checks = []
def norm(text):
	text = re.sub(r"https?://\S+", "", text)
	text = re.sub(r"\b[0-9a-f]{7,40}\b", "", text)
	text = re.sub(r"\b\d{6,}\b", "", text)
	return " ".join(text.split())[:300]
reason = ""
for comment in reversed(comments if isinstance(comments, list) else []):
	body = str(comment.get("body") or "")
	if (comment.get("user") or {}).get("login") != login or "ai:unblock" in body or "ORCHESTRATOR_STATE" in body:
		continue
	first = next((line for line in body.splitlines() if line.strip()), "")
	reason = norm(first.lstrip("#").strip())
	if reason:
		break
evidence = {"reason": reason or "no pipeline comment"}
if kind == "pr":
	evidence["pr"] = item
	if isinstance(checks, list) and checks:
		evidence["checks"] = sorted({str(name) for name in checks if name})[:30]
print(json.dumps(evidence))
PY
}

# Item/comments/check-runs and the optional run-log read do not include Actions
# artifact metadata. Only a destructive override needs these three reads.
unblock_rejection_snapshot()
{
	local artifact_id rejection_run_id rejection_artifact_file="${RUNTIME_DIR}/rejection_artifact.json"
	rm -f "${RUNTIME_DIR}/rejected_deletions.json"
	rejection_run_id="$(jq -r 'if .status == "ok" and (.run | type) == "string" then .run else empty end' "${RUNTIME_DIR}/rejection.json")"
	if ! [[ "${rejection_run_id}" =~ ^[1-9][0-9]*$ ]]; then
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=rejection_run_unbound"
		return 1
	fi
	if ! gh api "repos/${REPOSITORY}/actions/runs/${rejection_run_id}/artifacts?per_page=100" > "${RUNTIME_DIR}/rejection_artifacts.json" 2>/dev/null; then
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=artifact_list_unavailable"
		return 1
	fi
	if ! jq -e --arg name "destructive-rejection-issue-${ITEM}" --argjson run "${rejection_run_id}" '
		[.artifacts[]? | select(.name == $name and .expired == false and .workflow_run.id == $run)]
		| sort_by(.created_at) | last // empty
	' "${RUNTIME_DIR}/rejection_artifacts.json" > "${rejection_artifact_file}" 2>/dev/null; then
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=artifact_missing"
		return 1
	fi
	artifact_id="$(jq -r '.id' "${rejection_artifact_file}")"
	rejection_run_id="$(jq -r '.workflow_run.id' "${rejection_artifact_file}")"
	if ! [[ "${artifact_id}" =~ ^[1-9][0-9]*$ && "${rejection_run_id}" =~ ^[1-9][0-9]*$ ]] \
		|| ! gh api "repos/${REPOSITORY}/actions/runs/${rejection_run_id}" > "${RUNTIME_DIR}/rejection_run.json" 2>/dev/null \
		|| ! gh api "repos/${REPOSITORY}/actions/artifacts/${artifact_id}/zip" > "${RUNTIME_DIR}/rejection.zip" 2>/dev/null \
		|| ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" rejection-snapshot \
			--run-file "${RUNTIME_DIR}/rejection_run.json" --artifact-file "${rejection_artifact_file}" \
			--zip-file "${RUNTIME_DIR}/rejection.zip" --item "${ITEM}" --repo "${REPOSITORY}" \
			> "${RUNTIME_DIR}/rejected_deletions.json"; then
		rm -f "${RUNTIME_DIR}/rejected_deletions.json"
		unblock_log "item=${ITEM} op=rejection_snapshot outcome=failed reason=unverified"
		return 1
	fi
}

# A project may only claim an issue that its authenticated V2 state lists.
unblock_state_lists_issue()
{
	local bound_issue="$1" binding_json="$2"
	jq -e --argjson issue "${bound_issue}" 'any((.issue_number_map // {})[]; . == $issue) or any(.waves[]?.issues[]?; .github_issue == $issue) or any(.security_pass_active_fix_issues[]?; . == $issue) or any(.validation_active_fix_issues[]?; . == $issue)' "${binding_json}" >/dev/null 2>&1
}

unblock_main()
{
	local now stop_json fp verdict_name round marker_line comment_body terminal ops_file wait
	local project_failed_substituted="false" project_status=""
	if [ "${UNBLOCK_JUDGE_ENABLED:-true}" = "false" ]; then
		unblock_log "item=${ITEM:-} outcome=skip reason=disabled"
		return 0
	fi
	if [ -z "${REPOSITORY:-}" ] || ! [[ "${ITEM:-}" =~ ^[1-9][0-9]*$ ]]; then
		unblock_log "item=${ITEM:-} outcome=skip reason=missing_input"
		return 0
	fi
	now="$(unblock_now)"
	UNBLOCK_LOGIN="$(gh api user --jq '.login' 2>/dev/null || true)"
	if ! [[ "${UNBLOCK_LOGIN}" =~ ^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$ ]]; then
		unblock_log "item=${ITEM} outcome=skip reason=login_unavailable"
		return 0
	fi
	if ! gh api "repos/${REPOSITORY}/issues/${ITEM}" > "${RUNTIME_DIR}/item.json" 2>/dev/null \
		|| ! jq -e '.number' "${RUNTIME_DIR}/item.json" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} outcome=skip reason=item_unreadable"
		return 0
	fi
	if [ "$(jq -r '.state' "${RUNTIME_DIR}/item.json")" != "open" ]; then
		unblock_log "item=${ITEM} outcome=skip reason=closed"
		return 0
	fi
	jq -c '[.labels[]?.name]' "${RUNTIME_DIR}/item.json" > "${RUNTIME_DIR}/labels.json"
	if jq -e 'index("ai:orchestrator-tracking")' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		ITEM_KIND="project"
	elif jq -e '.pull_request' "${RUNTIME_DIR}/item.json" >/dev/null 2>&1; then
		ITEM_KIND="pr"
	else
		ITEM_KIND="issue"
	fi
	if ! unblock_fetch_comments "${ITEM}" "${RUNTIME_DIR}/item_comments.json"; then
		unblock_log "item=${ITEM} outcome=skip reason=comments_unavailable"
		return 0
	fi

	# The project an item belongs to, and a PR's linked issue.
	local tracking="" linked="" finding="" body_text pr_json="" head_sha="" head_ref="" head_repo="" binding_issue=""
	local pr_trusted="false" pr_author="" pr_assoc="" pr_head_repo="" untrusted_project_pr="false"
	body_text="$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json")"
	if [ "${ITEM_KIND}" = "issue" ]; then
		finding="$(jq -r '(.body // "") | [match("<!-- ai:security-finding:([^>]+) -->").captures[0].string] | first // "" | gsub("^\\s+|\\s+$"; "")' "${RUNTIME_DIR}/item.json")"
	fi
	if [ "${ITEM_KIND}" = "project" ]; then
		tracking="${ITEM}"
	elif [ "${ITEM_KIND}" = "issue" ] && jq -e 'index("ai:orchestrator-managed")' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		tracking="$(printf '%s\n' "${body_text}" | sed -n 's/^[[:space:]]*-\{0,1\}[[:space:]]*\(\*\*\)\{0,1\}Tracking issue:\(\*\*\)\{0,1\}[[:space:]]*#\([0-9][0-9]*\)[[:space:]]*$/\3/p' | head -n1)"
		binding_issue="${ITEM}"
	fi
	if [ "${ITEM_KIND}" = "pr" ]; then
		# PR body references are untrusted; project binding also requires a
		# same-repository head and trusted project-state membership below.
		pr_json="$(gh api "repos/${REPOSITORY}/pulls/${ITEM}" 2>/dev/null || true)"
		if ! jq -e --argjson item "${ITEM}" '.number == $item and (.base.ref | type == "string")' <<< "${pr_json}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} outcome=skip reason=pr_unreadable"
			return 0
		fi
		head_sha="$(jq -r '.head.sha // ""' <<< "${pr_json}")"
		head_ref="$(jq -r '.head.ref // ""' <<< "${pr_json}")"
		pr_author="$(jq -r '.user.login // ""' <<< "${pr_json}")"
		pr_assoc="$(jq -r '.author_association // ""' <<< "${pr_json}")"
		pr_head_repo="$(jq -r '.head.repo.full_name // ""' <<< "${pr_json}")"
		if jq -e --arg repo "${REPOSITORY}" --arg login "${UNBLOCK_LOGIN}" '
			(.head.repo.full_name | type == "string")
			and ((.head.repo.full_name | ascii_downcase) == ($repo | ascii_downcase))
			and (.user.login | strings | test("^[A-Za-z0-9][A-Za-z0-9-]*(\\[bot\\])?$"))
			and (.head.sha | strings | test("^[0-9a-f]{40}([0-9a-f]{24})?$"))
			and ((.author_association | IN("OWNER", "MEMBER", "COLLABORATOR"))
				or .user.login == $login or .user.login == "github-actions[bot]")
		' <<< "${pr_json}" >/dev/null 2>&1; then
			pr_trusted="true"
		fi
		unblock_log "item=${ITEM} kind=pr op=pr_provenance trusted=${pr_trusted} head_repo=$(printf '%s' "${pr_head_repo}" | tr '\r\n' '  ') assoc=$(printf '%s' "${pr_assoc}" | tr '\r\n' '  ')"
		if [[ "$(jq -r '.base.ref' <<< "${pr_json}")" =~ ^orchestrator/project-([1-9][0-9]*)$ ]]; then
			if [ "${pr_trusted}" != "true" ]; then
				# Never bind an untrusted PR to project state; only its rejection may proceed.
				untrusted_project_pr="true"
			else
				tracking="${BASH_REMATCH[1]}"
				head_repo="$(jq -r '.head.repo.full_name // ""' <<< "${pr_json}")"
				if [ -z "${head_repo}" ] || [ "${head_repo,,}" != "${REPOSITORY,,}" ]; then
					unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified detail=fork_head"
					return 0
				fi
				if [[ ! "${head_ref}" =~ ^ai/issue-([1-9][0-9]*)$ ]]; then
					unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified detail=head_ref"
					return 0
				fi
				binding_issue="${BASH_REMATCH[1]}"
			fi
		fi
		linked="$(printf '%s\n' "${body_text}" | grep -oiE '\b(refs|closes|close|closed|fixes|fix|fixed|resolves|resolve|resolved)[[:space:]]+#[0-9]+' | head -n1 | grep -oE '[0-9]+' || true)"
	fi

	if ! stop_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" stop --labels-json "$(cat "${RUNTIME_DIR}/labels.json")")"; then
		if [ "${ITEM_KIND}" = "project" ]; then
			if ! project_status="$(unblock_trusted_project_status "${RUNTIME_DIR}/item_comments.json" "${RUNTIME_DIR}/project_state_trusted.json")"; then
				unblock_log "item=${ITEM} kind=project outcome=skip reason=project_state_unverified"
				return 0
			fi
			if [ "${project_status}" != "failed" ]; then
				unblock_log "item=${ITEM} kind=project outcome=skip reason=project_not_failed status=$(printf '%s' "${project_status}" | tr -cd 'a-z_-' | cut -c1-40)"
				return 0
			fi
			project_failed_substituted="true"
			stop_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" stop --project-failed)"
		else
			unblock_log "item=${ITEM} kind=${ITEM_KIND} outcome=skip reason=not_blocked"
			return 0
		fi
	fi
	ITEM_STOP="$(jq -r '.stop' <<< "${stop_json}")"

	local has_plan="false"
	if jq -e --arg login "${UNBLOCK_LOGIN}" 'any(.[]; ((.body // "") | test("(?im)^\\s*implementation\\s+plan\\b")) and (((.user.type // "") == "Bot") or ((.author_association // "") as $a | ["OWNER", "MEMBER", "COLLABORATOR"] | index($a)) or ((.user.login // "") == $login)))' \
		"${RUNTIME_DIR}/item_comments.json" >/dev/null 2>&1; then
		has_plan="true"
	fi
	jq -n --arg repo "${REPOSITORY}" --arg kind "${ITEM_KIND}" --argjson item "${ITEM}" --arg stop "${ITEM_STOP}" \
		--slurpfile labels "${RUNTIME_DIR}/labels.json" --arg tracking "${tracking}" --arg linked "${linked}" --arg finding "${finding}" \
		--argjson has_plan "${has_plan}" --arg title "$(jq -r '.title // ""' "${RUNTIME_DIR}/item.json")" \
		--arg security_body "${body_text}" \
		--argjson pr_trusted "${pr_trusted}" --arg pr_author "${pr_author}" --arg pr_head_repo "${pr_head_repo}" --arg pr_head_sha "${head_sha}" \
		'{repo: $repo, kind: $kind, item: $item, stop: $stop, labels: $labels[0], tracking: (if $tracking == "" then null else ($tracking | tonumber) end), linked_issue: (if $linked == "" then null else ($linked | tonumber) end), has_plan: $has_plan, title: $title, security_finding_id: (if $finding == "" then null else $finding end), security_source_body: (if $kind == "issue" and ($labels[0] | index("ai:security")) != null then $security_body else null end), pr_trusted: $pr_trusted, pr_author: $pr_author, pr_head_repo: $pr_head_repo, pr_head_sha: $pr_head_sha}' \
		> "${RUNTIME_DIR}/context.json"

	# A pending fix-up comes first (Q11): wait for it, or run its follow-up.
	local last_activity="" wait_id wait_fixup wait_state wait_at wait_updated fixup_json
	wait=""
	[ "${untrusted_project_pr}" = "true" ] || wait="$(unblock_latest_wait)"
	if [ -n "${wait}" ]; then
		read -r wait_id wait_fixup wait_state wait_at wait_updated <<< "${wait}"
		if [ "${wait_state}" = "done" ]; then
			last_activity="${wait_updated}"
		else
			fixup_json="$(gh api "repos/${REPOSITORY}/issues/${wait_fixup}" --jq '{state, state_reason, labels: [.labels[]?.name]}' 2>/dev/null || echo '{}')"
			if ! jq -e '.state == "open" or .state == "closed"' <<< "${fixup_json}" >/dev/null 2>&1; then
				unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=fixup_unavailable"
				return 0
			fi
			if jq -e '.state == "closed" and .state_reason == "completed" and (.labels | index("ai:merged") != null)' <<< "${fixup_json}" >/dev/null 2>&1; then
				ops_file="${RUNTIME_DIR}/followup_ops.json"
				if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" followup --context-file "${RUNTIME_DIR}/context.json" --fixup "${wait_fixup}" > "${ops_file}" \
					|| ! unblock_run_ops "${ops_file}"; then
					unblock_log "item=${ITEM} fixup=${wait_fixup} outcome=skip reason=followup_failed"
					return 0
				fi
				unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} merged; the unblock judge posted this item's resume command.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} outcome=followup"
				return 0
			fi
			if [ "$(jq -r '.state // ""' <<< "${fixup_json}")" = "open" ] \
				&& [ "$(unblock_hours_since "${wait_at}")" -lt "${UNBLOCK_JUDGE_FIXUP_WAIT_HOURS:-72}" ]; then
				unblock_patch_comment "${wait_id}" "Waiting for fix-up #${wait_fixup} to merge; the unblock judge resumes this item afterwards. Last checked ${now}.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} -->" || true
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fixup=${wait_fixup} outcome=waiting"
				return 0
			fi
			# Closed without merging, or waited too long: judge again.
			unblock_patch_comment "${wait_id}" "Fix-up #${wait_fixup} did not merge in time; the unblock judge decides again.

<!-- ai:unblock-wait:v1 item=${ITEM} fixup=${wait_fixup} done -->" || true
			last_activity="${now}"
		fi
	fi

	# Evidence and fingerprint.
	echo '[]' > "${RUNTIME_DIR}/failing_checks.json"
	if [ "${ITEM_KIND}" = "pr" ]; then
		if [[ "${head_sha}" =~ ^[0-9a-f]{40}$ ]]; then
			gh api "repos/${REPOSITORY}/commits/${head_sha}/check-runs?per_page=100" \
				--jq '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out") | .name]' \
				> "${RUNTIME_DIR}/failing_checks.json" 2>/dev/null || echo '[]' > "${RUNTIME_DIR}/failing_checks.json"
		fi
	fi
	unblock_evidence > "${RUNTIME_DIR}/evidence.json" 2>/dev/null || echo '{"reason": "unreadable"}' > "${RUNTIME_DIR}/evidence.json"
	fp="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" fingerprint --stop "${ITEM_STOP}" --evidence-file "${RUNTIME_DIR}/evidence.json" | jq -r '.fingerprint // empty')"
	if ! [[ "${fp}" =~ ^[0-9a-f]{12}$ ]]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=skip reason=fingerprint_failed"
		return 0
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" rejection --item "${ITEM}" --stop "${ITEM_STOP}" \
		--comments-file "${RUNTIME_DIR}/item_comments.json" --trusted-login "${UNBLOCK_LOGIN}" > "${RUNTIME_DIR}/rejection.json"; then
		echo '{"status":"none","reason":"unreadable"}' > "${RUNTIME_DIR}/rejection.json"
	fi

	local -a decide_args=(decide --item "${ITEM}" --stop "${ITEM_STOP}" --fingerprint "${fp}" --comments-file "${RUNTIME_DIR}/item_comments.json"
		--trusted-login "${UNBLOCK_LOGIN}" --now "${now}" --kind "${ITEM_KIND}" --rejection-file "${RUNTIME_DIR}/rejection.json")
	[ -n "${last_activity}" ] && decide_args+=(--last-activity "${last_activity}")
	if [[ "${tracking}" =~ ^[0-9]+$ ]]; then
		if [ "${tracking}" = "${ITEM}" ]; then
			cp "${RUNTIME_DIR}/item_comments.json" "${RUNTIME_DIR}/project_comments.json"
		else
			if ! unblock_fetch_comments "${tracking}" "${RUNTIME_DIR}/project_comments.json"; then
				unblock_log "item=${ITEM} outcome=skip reason=project_comments_unavailable"
				return 0
			fi
		fi
		if ! jq -e --arg login "${UNBLOCK_LOGIN}" '[.[] | select((.user.login // "") == $login)]' \
			"${RUNTIME_DIR}/project_comments.json" > "${RUNTIME_DIR}/project_state_comments.json"; then
			unblock_log "item=${ITEM} outcome=skip reason=project_ledger_unreadable detail=state_filter"
			return 0
		fi
		if [ "${tracking}" != "${ITEM}" ]; then
			if [ "${ITEM_KIND}" = "issue" ] || [ "${ITEM_KIND}" = "pr" ]; then
				if ! unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract --comments-json "${RUNTIME_DIR}/project_state_comments.json" > "${RUNTIME_DIR}/project_binding.json" 2>/dev/null \
					|| ! unblock_state_lists_issue "${binding_issue}" "${RUNTIME_DIR}/project_binding.json"; then
					unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified"
					return 0
				fi
			fi
			# Restore any item verdict whose project copy failed to post. Do not
			# decide from an incomplete project-wide round ledger this run.
			local missing_project_markers unblock_marker_entry
			if ! missing_project_markers="$(jq -r --arg login "${UNBLOCK_LOGIN}" --arg item "${ITEM}" \
				--slurpfile project "${RUNTIME_DIR}/project_comments.json" '
				[$project[0][]? | select((.user.login // "") == $login) | ((.body // "") | split("\n") | map(select(length > 0)) | last // "" | rtrimstr("\r"))] as $known
				| [.[] | select((.user.login // "") == $login)
				   | ((.body // "") | split("\n") | map(select(length > 0)) | last // "" | rtrimstr("\r"))
				   | select(startswith("<!-- ai:unblock:v1 item=" + $item + " ") and endswith(" -->"))
				   | select(. as $entry | ($known | index($entry)) == null)] | unique | .[]
			' "${RUNTIME_DIR}/item_comments.json")"; then
				unblock_log "item=${ITEM} outcome=skip reason=project_ledger_unreadable"
				return 0
			fi
			if [ -n "${missing_project_markers}" ]; then
				while IFS= read -r unblock_marker_entry; do
					if ! gh api "repos/${REPOSITORY}/issues/${tracking}/comments" -f body="Reconciled unblock judge verdict on #${ITEM}.

${unblock_marker_entry}" >/dev/null 2>&1; then
						unblock_log "item=${ITEM} outcome=skip reason=project_ledger_repair_failed"
						return 0
					fi
				done <<< "${missing_project_markers}"
				unblock_log "item=${ITEM} outcome=skip reason=project_ledger_repaired"
				return 0
			fi
		fi
		decide_args+=(--project-comments-file "${RUNTIME_DIR}/project_comments.json")
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${decide_args[@]}" > "${RUNTIME_DIR}/decision.json"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} outcome=skip reason=decide_failed"
		return 0
	fi
	terminal="$(jq -r '.terminal' "${RUNTIME_DIR}/decision.json")"

	if [ "${terminal}" = "true" ]; then
		jq -n --arg reason "$(jq -r '"Terminal: " + .terminal_reason + " (" + (.item_rounds | tostring) + " round(s) on this item)."' "${RUNTIME_DIR}/decision.json")" \
			'{verdict: "close", reason: ($reason + " The unblock judge has no verdict left that could clear this block.")}' > "${RUNTIME_DIR}/verdict_raw.json"
	else
		unblock_ask_model || return 0
	fi
	local -a validate_args=(validate --verdict-file "${RUNTIME_DIR}/verdict_raw.json" --decision-file "${RUNTIME_DIR}/decision.json" --repo "${REPOSITORY}" --rejection-file "${RUNTIME_DIR}/rejection.json")
	if [ "${ITEM_STOP}" = "destructive-blocked" ] && [ "${ITEM_KIND}" = "issue" ] \
		&& jq -e '.verdict == "override_guard"' "${RUNTIME_DIR}/verdict_raw.json" >/dev/null 2>&1; then
		if unblock_rejection_snapshot; then
			validate_args+=(--rejected-deletions-file "${RUNTIME_DIR}/rejected_deletions.json")
		fi
	fi
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${validate_args[@]}" > "${RUNTIME_DIR}/verdict.json"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} outcome=skip reason=invalid_verdict detail=$(jq -r '.error // ""' "${RUNTIME_DIR}/verdict.json" | tr ' ' '_' | cut -c1-120)"
		gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="The unblock judge could not reach a valid verdict this time and will try again later.

<!-- ai:unblock-wait:v1 item=${ITEM} reason=invalid_verdict -->" >/dev/null 2>&1 || true
		return 0
	fi
	verdict_name="$(jq -r '.verdict' "${RUNTIME_DIR}/verdict.json")"
	if [ "${untrusted_project_pr}" = "true" ] && [[ ! "${verdict_name}" =~ ^(reissue|descope|operator_step|accept_with_followup|close)$ ]]; then
		unblock_log "item=${ITEM} outcome=skip reason=project_binding_unverified detail=untrusted_pr_verdict"
		return 0
	fi
	round="$(jq -r '.round' "${RUNTIME_DIR}/verdict.json")"
	local -a marker_args=(marker --item "${ITEM}" --stop "${ITEM_STOP}" --fingerprint "${fp}" --verdict "${verdict_name}" --round "${round}")
	[ "$(jq -r '.override // ""' "${RUNTIME_DIR}/verdict.json")" = "bulk_delete" ] && marker_args+=(--override bulk_delete)
	marker_line="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${marker_args[@]}" | jq -r '.marker // empty')"
	if [ -z "${marker_line}" ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=marker_failed"
		return 0
	fi
	if [ "${project_failed_substituted}" = "true" ]; then
		# The model can take minutes; never record or act on a stale verdict.
		if ! unblock_fetch_comments "${ITEM}" "${RUNTIME_DIR}/item_comments_recheck.json"; then
			unblock_log "item=${ITEM} kind=project outcome=skip reason=project_state_recheck_unavailable"
			return 0
		fi
		if ! project_status="$(unblock_trusted_project_status "${RUNTIME_DIR}/item_comments_recheck.json" "${RUNTIME_DIR}/project_state_trusted.json")"; then
			unblock_log "item=${ITEM} kind=project outcome=skip reason=project_state_unverified stage=recheck"
			return 0
		fi
		if [ "${project_status}" != "failed" ]; then
			unblock_log "item=${ITEM} kind=project outcome=skip reason=project_resumed status=$(printf '%s' "${project_status}" | tr -cd 'a-z_-' | cut -c1-40)"
			return 0
		fi
	fi
	comment_body="$(jq -r --arg marker "${marker_line}" '
		"## Unblock judge: `" + .verdict + "` (round " + (.round | tostring) + ")\n\nReason: " + .reason + "\n\n"
		+ (if (.instructions // "") != "" then "Instructions: " + .instructions + "\n\n" else "" end)
		+ (if (.answer // "") != "" then "Answer: " + .answer + "\n\n" else "" end)
		+ (if (.paths // []) | length > 0 then "Paths: " + ((.paths // []) | map("`" + . + "`") | join(", ")) + "\n\n" else "" end)
		+ (if (.override // "") == "bulk_delete" then "Approved deletions: " + (.paths | tojson) + "\n\n" else "" end)
		+ (if (.override // "") == "bulk_delete" then "Rejected run: " + (.rejected_run | tostring) + "\n\n" else "" end)
		+ (if (.rejection_run // "") != "" then "Bound to guard rejection from run " + .rejection_run + ".\n\n" else "" end)
		+ (if (.placeholder // "") != "" then "Stays off behind `" + .placeholder + "` until the operator step is done.\n\n" else "" end)
		+ $marker
	' "${RUNTIME_DIR}/verdict.json")"
	# The ledger record goes first, so a failed operation below can never
	# make the same verdict available again.
	if ! gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="${comment_body}" >/dev/null 2>&1; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=skip reason=record_failed"
		return 0
	fi
	ops_file="${RUNTIME_DIR}/ops.json"
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_actions.py" plan --verdict-file "${RUNTIME_DIR}/verdict.json" \
		--context-file "${RUNTIME_DIR}/context.json" > "${ops_file}"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=skip reason=plan_failed"
		return 0
	fi
	local actuation_failed="false"
	if ! unblock_run_ops "${ops_file}" "${project_failed_substituted}"; then
		actuation_failed="true"
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=skip reason=actuation_failed"
	fi
	if [[ "${tracking}" =~ ^[0-9]+$ ]] && [ "${tracking}" != "${ITEM}" ]; then
		if ! gh api "repos/${REPOSITORY}/issues/${tracking}/comments" -f body="Unblock judge verdict on #${ITEM}: \`${verdict_name}\`.

${marker_line}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} op=project_record outcome=failed"
			return 0
		fi
	fi
	if [ "${actuation_failed}" = "true" ]; then
		return 0
	fi
	unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} round=${round} outcome=acted"
}

# Attach failed logs only when a pipeline-authored citation belongs to this
# item. Missing or unverifiable metadata leaves the prompt's log section empty.
unblock_select_run_log()
{
	local candidate_id run_json checked=0 unreadable="false" reason="run_unbound"
	local -a candidates=()
	: > "${RUNTIME_DIR}/run_log_tail.txt"
	mapfile -t candidates < <(jq -r --arg login "${UNBLOCK_LOGIN}" '
		[.[] | select((.user.login // "") == $login and (((.body // "") | test("ai:unblock|ORCHESTRATOR_STATE")) | not))
		 | (.body // "") | scan("/actions/runs/([0-9]+)") | .[0]]
		| reverse | reduce .[] as $id ([]; if index($id) == null then . + [$id] else . end)
		| .[:3][]
	' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null)
	if [ "${#candidates[@]}" -eq 0 ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} op=run_log outcome=omitted reason=no_trusted_run candidates=0"
		return 0
	fi
	for candidate_id in "${candidates[@]}"; do
		[[ "${candidate_id}" =~ ^[0-9]+$ ]] || continue
		checked=$((checked + 1))
		# The existing item, comment, PR and check-runs reads contain no run
		# repository/branch metadata; this bounded read is needed to bind the ID.
		if ! run_json="$(gh api "repos/${REPOSITORY}/actions/runs/${candidate_id}" 2>/dev/null)"; then
			unreadable="true"
			continue
		fi
		if ! jq -e 'type == "object"' <<< "${run_json}" >/dev/null 2>&1; then
			unreadable="true"
			continue
		fi
		if ! jq -e --arg id "${candidate_id}" --arg repo "${REPOSITORY}" --arg kind "${ITEM_KIND}" \
				--arg item "${ITEM}" \
				--arg sha "${head_sha:-}" --arg ref "${head_ref:-}" '
				(.id == ($id | tonumber) and .repository.full_name == $repo and .head_repository.full_name == $repo)
				and (if $kind == "pr" then
					((.display_title // "") | endswith("[pr:" + $item + "]"))
					or (($sha | test("^[0-9a-f]{40}$")) and .head_sha == $sha)
					or ($ref != "" and .head_branch == $ref)
					or any(.pull_requests[]?; .number == ($item | tonumber))
				elif $kind == "issue" then
					.head_branch == ("ai/issue-" + $item)
				else
					.head_branch == ("orchestrator/project-" + $item)
					or ((.display_title // "") | endswith("[tracking:" + $item + "]"))
				end)
			' <<< "${run_json}" >/dev/null 2>&1; then
			continue
		fi
		gh run view "${candidate_id}" -R "${REPOSITORY}" --log-failed 2>/dev/null | tail -n 400 | tail -c 120000 > "${RUNTIME_DIR}/run_log_tail.txt" || true
		unblock_log "item=${ITEM} kind=${ITEM_KIND} op=run_log outcome=attached run=${candidate_id}"
		return 0
	done
	[ "${unreadable}" = "false" ] || reason="run_unreadable"
	unblock_log "item=${ITEM} kind=${ITEM_KIND} op=run_log outcome=omitted reason=${reason} candidates=${checked}"
}

# Builds the evidence prompt, runs the UNBLOCK_JUDGE role and writes the raw
# verdict JSON to verdict_raw.json. Returns 1 (after logging) when there is
# no usable answer.
unblock_ask_model()
{
	local prompt_file="${RUNTIME_DIR}/prompt.txt" output_file="${RUNTIME_DIR}/model_output.txt" model reasoning engine rc iso_rc parse_rc
	unblock_select_run_log
	: > "${RUNTIME_DIR}/pr_diff.txt"
	if [ "${ITEM_KIND}" = "pr" ]; then
		gh api "repos/${REPOSITORY}/pulls/${ITEM}" -H "Accept: application/vnd.github.diff" 2>/dev/null | head -c 120000 > "${RUNTIME_DIR}/pr_diff.txt" || true
	fi
	echo '{}' > "${RUNTIME_DIR}/state_slice.json"
	local spec=""
	if [ -n "${tracking:-}" ] && [ -s "${RUNTIME_DIR}/project_state_comments.json" ]; then
		if unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract --comments-json "${RUNTIME_DIR}/project_state_comments.json" > "${RUNTIME_DIR}/state_full.json" 2>/dev/null; then
			jq '{status, current_wave, integration_branch, judge_cycle, recovery_count, validation_last_raw_status, validation_failure_reason, security_pass_status, final_merge_status, waves: [(.waves // [])[] | {issues: [(.issues // [])[] | {id, github_issue, status}]}]}' \
				"${RUNTIME_DIR}/state_full.json" > "${RUNTIME_DIR}/state_slice.json" 2>/dev/null || echo '{}' > "${RUNTIME_DIR}/state_slice.json"
		fi
		if [ "${ITEM_KIND}" = "project" ]; then
			spec="$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json" | head -c 8000)"
		fi
	fi
	jq -n \
		--slurpfile context "${RUNTIME_DIR}/context.json" \
		--slurpfile decision "${RUNTIME_DIR}/decision.json" \
		--slurpfile evidence "${RUNTIME_DIR}/evidence.json" \
		--slurpfile rejection "${RUNTIME_DIR}/rejection.json" \
		--slurpfile comments "${RUNTIME_DIR}/item_comments.json" \
		--slurpfile state "${RUNTIME_DIR}/state_slice.json" \
		--arg login "${UNBLOCK_LOGIN}" \
		--arg item_body "$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json" | head -c 6000)" \
		--arg spec "${spec}" \
		'{
			block: {item: $context[0].item, kind: $context[0].kind, stop: $context[0].stop, labels: $context[0].labels, tracking_issue: $context[0].tracking, title: $context[0].title},
			fingerprint: $decision[0].fingerprint,
			evidence: $evidence[0],
			allowed: $decision[0].allowed,
			used_for_this_fingerprint: $decision[0].used,
			guard_rejection: (if $rejection[0].status == "ok" then ($rejection[0] | {guard, paths, run}) else null end),
			rounds: {item: $decision[0].item_rounds, project: $decision[0].project_rounds},
			prior_unblock_verdicts: [$comments[0][] | select((.user.login // "") == $login and ((.body // "") | test("<!-- ai:unblock:v1 "))) | {created_at, body: ((.body // "")[0:1500])}] | .[-5:],
			last_comments: [$comments[0][-20:][] | {author: (.user.login // ""), created_at, body: ((.body // "")[0:2000])}],
			item_body_untrusted: $item_body,
			project_state: $state[0],
			project_spec_untrusted: $spec
		}' > "${RUNTIME_DIR}/judge_context.json" 2>/dev/null || {
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=skip reason=context_failed"
		return 1
	}
	{
		bash "${SUPPORT_DIR}/scripts/render_prompt.sh" "${SUPPORT_DIR}/prompts/mode-judge-unblock.txt" 2>/dev/null \
			|| cat "${SUPPORT_DIR}/prompts/mode-judge-unblock.txt"
		echo
		echo "=== UNBLOCK CONTEXT JSON (untrusted text inside) ==="
		cat "${RUNTIME_DIR}/judge_context.json"
		echo
		echo "=== FAILING RUN LOG TAIL (untrusted) ==="
		cat "${RUNTIME_DIR}/run_log_tail.txt"
		echo
		echo "=== PULL REQUEST DIFF (untrusted, at most 120 KB) ==="
		cat "${RUNTIME_DIR}/pr_diff.txt"
	} > "${prompt_file}"
	: > "${output_file}"
	if [ -n "${MOCK_UNBLOCK_JUDGE_JSON:-}" ]; then
		printf '%s\n' "${MOCK_UNBLOCK_JUDGE_JSON}" > "${output_file}"
	else
		model="${UNBLOCK_JUDGE_MODEL:-${WORKFLOW_EDITOR_MODEL:-openai/gpt-6-sol}}"
		reasoning="${UNBLOCK_JUDGE_REASONING:-high}"
		if [[ ! "${model}" =~ ^[a-zA-Z0-9/_.-]+$ ]] || [[ ! "${reasoning}" =~ ^(xhigh|high|medium|low|none)$ ]]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=invalid_model_config"
			echo '{}' > "${RUNTIME_DIR}/verdict_raw.json"
			return 0
		fi
		UNBLOCK_JUDGE_TIMEOUT_SECS="${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}"
		if [[ ! "${UNBLOCK_JUDGE_TIMEOUT_SECS}" =~ ^[1-9][0-9]*$ ]]; then
			unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=timeout_fallback reason=invalid_timeout default=1500"
			UNBLOCK_JUDGE_TIMEOUT_SECS="1500"
		fi
		engine="codex"
		if [ -f "${SUPPORT_DIR}/scripts/ai_engine.sh" ]; then
			# shellcheck source=/dev/null
			source "${SUPPORT_DIR}/scripts/ai_engine.sh"
			engine="$(AI_ENGINE_LABELS="$(cat "${RUNTIME_DIR}/labels.json")" AI_ENGINE_MODEL_HINT="${model}" AI_ENGINE_EFFORT_HINT="${reasoning}" \
				ai_engine_for_role UNBLOCK_JUDGE 2>/dev/null || echo codex)"
		fi
		rc=75
		if [ "${engine}" = "claude" ]; then
			rc=0
			# The OAuth credential stays in the host relay; the model runs only in the container.
			(cd "${TARGET_DIR}" && env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET -u OPENROUTER_API_KEY \
				MODEL_EDITOR="${model}" MODEL_REASONING_EFFORT="${reasoning}" \
				CLARIFY_ISOLATION_SUPPORT_DIR="${SUPPORT_DIR}/scripts" \
				CLARIFY_ISOLATION_TIMEOUT_SECS="${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}" \
				bash "${SUPPORT_DIR}/scripts/clarify_isolated_run.sh" "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/claude.err" claude UNBLOCK_JUDGE) || rc=$?
			if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 75 ]; then
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=isolation_failed rc=${rc}"
				: > "${output_file}"
			fi
		fi
		if [ "${rc}" -eq 75 ]; then
			iso_rc=0
			(cd "${TARGET_DIR}" && env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET \
				MODEL_EDITOR="${model}" MODEL_REASONING_EFFORT="${reasoning}" \
				CLARIFY_ISOLATION_SUPPORT_DIR="${SUPPORT_DIR}/scripts" \
				CLARIFY_ISOLATION_TIMEOUT_SECS="${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}" \
				bash "${SUPPORT_DIR}/scripts/clarify_isolated_run.sh" "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/codex.err" codex UNBLOCK_JUDGE) || iso_rc=$?
			if [ "${iso_rc}" -ne 0 ]; then
				unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} outcome=model_failed reason=isolation_failed rc=${iso_rc}"
				: > "${output_file}"
			fi
		fi
	fi
	parse_rc=0
	unblock_py - "${output_file}" > "${RUNTIME_DIR}/verdict_raw.json" <<'PY' || parse_rc=$?
import base64, json, os, re, sys, urllib.parse
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
separators = re.compile(r'''[\s\-_.:/,"']''')
def strings(value):
	if isinstance(value, str):
		yield value
	elif isinstance(value, list):
		for item in value:
			yield from strings(item)
	elif isinstance(value, dict):
		for key, item in value.items():
			yield from strings(key)
			yield from strings(item)
def contains_secret(value):
	text = "".join(strings(value))
	compact = separators.sub("", text).lower()
	if re.search(r"sk-or-v1-[0-9a-fA-F]{32,}|sk-or-[A-Za-z0-9_-]{20,}", text):
		return True
	if len(api_key) < 16:
		return False
	key_bytes = api_key.encode("utf-8")
	literal_needles = {api_key, api_key[::-1], urllib.parse.quote(api_key, safe="")}
	compact_needles = {separators.sub("", api_key).lower(), key_bytes.hex(), base64.b32encode(key_bytes).decode("ascii").rstrip("=").lower()}
	for shift in range(3):
		shifted = b"\0" * shift + key_bytes
		for encode in (base64.b64encode, base64.urlsafe_b64encode):
			encoded = encode(shifted).decode("ascii")
			if shift == 0:
				literal_needles.add(encoded)
				literal_needles.add(encoded.rstrip("="))
			else:
				# Ignore the prefix group and incomplete tail; the middle depends
				# entirely on credential bytes, regardless of adjacent data.
				middle = encoded[4:(len(encoded.rstrip("=")) // 4) * 4]
				if len(middle) >= 16:
					literal_needles.add(middle)
	return any(needle and needle in text for needle in literal_needles) or any(
		len(needle) >= 16 and needle in compact for needle in compact_needles
	)
def redact_verdict_value(value):
	if isinstance(value, str):
		return value.replace(api_key, "[redacted]") if api_key else value
	if isinstance(value, list):
		return [redact_verdict_value(item) for item in value]
	if isinstance(value, dict):
		return {key: redact_verdict_value(item) for key, item in value.items()}
	return value
for start in [m.start() for m in re.finditer(r"\{", raw)]:
	try:
		value = json.JSONDecoder().raw_decode(raw[start:])[0]
	except ValueError:
		continue
	if isinstance(value, dict) and "verdict" in value:
		if contains_secret(value):
			sys.exit(3)
		print(json.dumps(redact_verdict_value(value)))
		sys.exit(0)
sys.exit(1)
PY
	if [ "${parse_rc}" -ne 0 ]; then
		[ "${parse_rc}" -ne 3 ] || unblock_log "item=${ITEM} outcome=skip reason=verdict_secret_rejected"
		echo '{}' > "${RUNTIME_DIR}/verdict_raw.json"
	fi
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	unblock_main "$@"
	exit 0
fi
