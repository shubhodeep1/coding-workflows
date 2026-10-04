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
# The model runs with GH_TOKEN, GITHUB_TOKEN and TG_BOT_SECRET removed from
# its environment, and its output is data: only verdicts the ledger accepts
# are acted on, and only through the operations unblock_actions.py plans.
#
# Never fails its caller: every problem is logged and the exit code is 0.
# API budget (CLAUDE.md §15), per run: one `user` read, the item, its
# comments (paginated), the tracking issue's comments for a project's item,
# at most one linked-issue read, one fix-up read while waiting, the run log
# and diff for evidence, one verdict comment (two for a project's item) and
# the planned operations (at most about six writes). A failed project marker
# is reconciled if the item stays blocked; actuation used the item ledger.
# Log: UNBLOCK_JUDGE item= kind= stop= fingerprint= verdict= round= outcome= reason=
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
	local ops_file="$1" count idx op issue number created body label close_failed="false" ops_failed="false"
	count="$(jq '.ops | length' "${ops_file}" 2>/dev/null || echo 0)"
	for ((idx = 0; idx < count; idx++)); do
		op="$(jq -r ".ops[${idx}].op" "${ops_file}")"
		issue="$(jq -r ".ops[${idx}].issue // empty" "${ops_file}")"
		if [ "${ops_failed}" = "true" ] && [ "${op}" != "close" ] && [ "${op}" != "telegram" ]; then
			continue
		fi
		case "${op}" in
			comment)
				body="$(jq -r ".ops[${idx}].body" "${ops_file}")"
				gh api "repos/${REPOSITORY}/issues/${issue}/comments" -f body="${body}" >/dev/null 2>&1 \
					|| { ops_failed="true"; unblock_log "item=${ITEM} op=comment issue=${issue} outcome=failed"; }
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
					gh api -X POST "repos/${REPOSITORY}/issues/${issue}/labels" -f "labels[]=${label}" >/dev/null 2>&1 \
						|| { ops_failed="true"; unblock_log "item=${ITEM} op=add_labels issue=${issue} label=${label} outcome=failed"; }
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				;;
			remove_label)
				label="$(jq -r ".ops[${idx}].label" "${ops_file}")"
				gh api -X DELETE "repos/${REPOSITORY}/issues/${issue}/labels/$(jq -rn --arg l "${label}" '$l | @uri')" >/dev/null 2>&1 \
					|| { ops_failed="true"; unblock_log "item=${ITEM} op=remove_label issue=${issue} label=${label} outcome=failed"; }
				;;
			create_issue)
				local -a create_args=(-f "title=$(jq -r ".ops[${idx}].title" "${ops_file}")" -f "body=$(jq -r ".ops[${idx}].body" "${ops_file}")")
				while IFS= read -r label; do
					[ -n "${label}" ] && create_args+=(-f "labels[]=${label}")
				done < <(jq -r ".ops[${idx}].labels[]" "${ops_file}")
				created="$(gh api "repos/${REPOSITORY}/issues" "${create_args[@]}" --jq '.number' 2>/dev/null || true)"
				if ! [[ "${created}" =~ ^[0-9]+$ ]]; then
					ops_failed="true"
					unblock_log "item=${ITEM} op=create_issue outcome=failed"
					continue
				fi
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
				if [ "${ops_failed}" = "true" ]; then
					unblock_log "item=${ITEM} op=close issue=${issue} outcome=skip reason=prerequisite_failed"
					continue
				fi
				if [ "$(jq -r ".ops[${idx}].pr" "${ops_file}")" = "true" ]; then
					gh api -X PATCH "repos/${REPOSITORY}/pulls/${issue}" -f state=closed >/dev/null 2>&1 \
						|| { close_failed="true"; ops_failed="true"; unblock_log "item=${ITEM} op=close issue=${issue} outcome=failed"; }
				else
					gh api -X PATCH "repos/${REPOSITORY}/issues/${issue}" -f state=closed -f state_reason=not_planned >/dev/null 2>&1 \
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
				[ "${ops_failed}" = "true" ] && continue
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

unblock_main()
{
	local now stop_json fp verdict_name round marker_line comment_body terminal ops_file wait
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
	local tracking="" linked="" body_text pr_json="" head_sha=""
	body_text="$(jq -r '.body // ""' "${RUNTIME_DIR}/item.json")"
	if [ "${ITEM_KIND}" = "project" ]; then
		tracking="${ITEM}"
	elif [ "${ITEM_KIND}" = "issue" ] && jq -e 'index("ai:orchestrator-managed")' "${RUNTIME_DIR}/labels.json" >/dev/null 2>&1; then
		tracking="$(printf '%s\n' "${body_text}" | sed -n 's/^[[:space:]]*-\{0,1\}[[:space:]]*\(\*\*\)\{0,1\}Tracking issue:\(\*\*\)\{0,1\}[[:space:]]*#\([0-9][0-9]*\)[[:space:]]*$/\3/p' | head -n1)"
	fi
	if [ "${ITEM_KIND}" = "pr" ]; then
		# PR body references are untrusted; only its GitHub-reported base can
		# bind a fix-up and its verdict ledger to an orchestrator project.
		pr_json="$(gh api "repos/${REPOSITORY}/pulls/${ITEM}" 2>/dev/null || true)"
		if ! jq -e --argjson item "${ITEM}" '.number == $item and (.base.ref | type == "string")' <<< "${pr_json}" >/dev/null 2>&1; then
			unblock_log "item=${ITEM} outcome=skip reason=pr_unreadable"
			return 0
		fi
		if [[ "$(jq -r '.base.ref' <<< "${pr_json}")" =~ ^orchestrator/project-([1-9][0-9]*)$ ]]; then
			tracking="${BASH_REMATCH[1]}"
		fi
		head_sha="$(jq -r '.head.sha // ""' <<< "${pr_json}")"
		linked="$(printf '%s\n' "${body_text}" | grep -oiE '\b(refs|closes|close|closed|fixes|fix|fixed|resolves|resolve|resolved)[[:space:]]+#[0-9]+' | head -n1 | grep -oE '[0-9]+' || true)"
	fi

	if ! stop_json="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" stop --labels-json "$(cat "${RUNTIME_DIR}/labels.json")")"; then
		if [ "${ITEM_KIND}" = "project" ]; then
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
		--slurpfile labels "${RUNTIME_DIR}/labels.json" --arg tracking "${tracking}" --arg linked "${linked}" \
		--argjson has_plan "${has_plan}" --arg title "$(jq -r '.title // ""' "${RUNTIME_DIR}/item.json")" \
		'{repo: $repo, kind: $kind, item: $item, stop: $stop, labels: $labels[0], tracking: (if $tracking == "" then null else ($tracking | tonumber) end), linked_issue: (if $linked == "" then null else ($linked | tonumber) end), has_plan: $has_plan, title: $title}' \
		> "${RUNTIME_DIR}/context.json"

	# A pending fix-up comes first (Q11): wait for it, or run its follow-up.
	local last_activity="" wait_id wait_fixup wait_state wait_at wait_updated fixup_json
	wait="$(unblock_latest_wait)"
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

	local -a decide_args=(decide --item "${ITEM}" --stop "${ITEM_STOP}" --fingerprint "${fp}" --comments-file "${RUNTIME_DIR}/item_comments.json"
		--trusted-login "${UNBLOCK_LOGIN}" --now "${now}" --kind "${ITEM_KIND}")
	[ -n "${last_activity}" ] && decide_args+=(--last-activity "${last_activity}")
	if [[ "${tracking}" =~ ^[0-9]+$ ]]; then
		if [ "${tracking}" = "${ITEM}" ]; then
			cp "${RUNTIME_DIR}/item_comments.json" "${RUNTIME_DIR}/project_comments.json"
		else
			if ! unblock_fetch_comments "${tracking}" "${RUNTIME_DIR}/project_comments.json"; then
				unblock_log "item=${ITEM} outcome=skip reason=project_comments_unavailable"
				return 0
			fi
			if [ "${ITEM_KIND}" = "issue" ]; then
				if ! unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract --comments-json "${RUNTIME_DIR}/project_comments.json" > "${RUNTIME_DIR}/project_binding.json" 2>/dev/null \
					|| ! jq -e --argjson issue "${ITEM}" 'any((.issue_number_map // {})[]; . == $issue) or any(.waves[]?.issues[]?; .github_issue == $issue) or any(.security_pass_active_fix_issues[]?; . == $issue) or any(.validation_active_fix_issues[]?; . == $issue)' "${RUNTIME_DIR}/project_binding.json" >/dev/null 2>&1; then
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
	if ! unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" validate --verdict-file "${RUNTIME_DIR}/verdict_raw.json" \
		--decision-file "${RUNTIME_DIR}/decision.json" --repo "${REPOSITORY}" > "${RUNTIME_DIR}/verdict.json"; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} outcome=skip reason=invalid_verdict detail=$(jq -r '.error // ""' "${RUNTIME_DIR}/verdict.json" | tr ' ' '_' | cut -c1-120)"
		gh api "repos/${REPOSITORY}/issues/${ITEM}/comments" -f body="The unblock judge could not reach a valid verdict this time and will try again later.

<!-- ai:unblock-wait:v1 item=${ITEM} reason=invalid_verdict -->" >/dev/null 2>&1 || true
		return 0
	fi
	verdict_name="$(jq -r '.verdict' "${RUNTIME_DIR}/verdict.json")"
	round="$(jq -r '.round' "${RUNTIME_DIR}/verdict.json")"
	local -a marker_args=(marker --item "${ITEM}" --stop "${ITEM_STOP}" --fingerprint "${fp}" --verdict "${verdict_name}" --round "${round}")
	[ "$(jq -r '.override // ""' "${RUNTIME_DIR}/verdict.json")" = "bulk_delete" ] && marker_args+=(--override bulk_delete)
	marker_line="$(unblock_py "${SUPPORT_DIR}/scripts/unblock_ledger.py" "${marker_args[@]}" | jq -r '.marker // empty')"
	if [ -z "${marker_line}" ]; then
		unblock_log "item=${ITEM} kind=${ITEM_KIND} stop=${ITEM_STOP} fingerprint=${fp} verdict=${verdict_name} outcome=skip reason=marker_failed"
		return 0
	fi
	comment_body="$(jq -r --arg marker "${marker_line}" '
		"## Unblock judge: `" + .verdict + "` (round " + (.round | tostring) + ")\n\nReason: " + .reason + "\n\n"
		+ (if (.instructions // "") != "" then "Instructions: " + .instructions + "\n\n" else "" end)
		+ (if (.answer // "") != "" then "Answer: " + .answer + "\n\n" else "" end)
		+ (if (.paths // []) | length > 0 then "Paths: " + ((.paths // []) | map("`" + . + "`") | join(", ")) + "\n\n" else "" end)
		+ (if (.override // "") == "bulk_delete" then "Approved deletions: " + (.paths | tojson) + "\n\n" else "" end)
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
	if ! unblock_run_ops "${ops_file}"; then
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

# Builds the evidence prompt, runs the UNBLOCK_JUDGE role and writes the raw
# verdict JSON to verdict_raw.json. Returns 1 (after logging) when there is
# no usable answer.
unblock_ask_model()
{
	local prompt_file="${RUNTIME_DIR}/prompt.txt" output_file="${RUNTIME_DIR}/model_output.txt" run_id model reasoning engine rc
	run_id="$(jq -r '[.[] | (.body // "") | scan("/actions/runs/([0-9]+)") | .[0]] | last // empty' "${RUNTIME_DIR}/item_comments.json" 2>/dev/null || true)"
	: > "${RUNTIME_DIR}/run_log_tail.txt"
	if [[ "${run_id}" =~ ^[0-9]+$ ]]; then
		gh run view "${run_id}" -R "${REPOSITORY}" --log-failed 2>/dev/null | tail -n 400 | tail -c 120000 > "${RUNTIME_DIR}/run_log_tail.txt" || true
	fi
	: > "${RUNTIME_DIR}/pr_diff.txt"
	if [ "${ITEM_KIND}" = "pr" ]; then
		gh api "repos/${REPOSITORY}/pulls/${ITEM}" -H "Accept: application/vnd.github.diff" 2>/dev/null | head -c 120000 > "${RUNTIME_DIR}/pr_diff.txt" || true
	fi
	echo '{}' > "${RUNTIME_DIR}/state_slice.json"
	local spec=""
	if [ -s "${RUNTIME_DIR}/project_comments.json" ] 2>/dev/null; then
		if unblock_py "${SUPPORT_DIR}/scripts/orchestrate_state_v2.py" extract --comments-json "${RUNTIME_DIR}/project_comments.json" > "${RUNTIME_DIR}/state_full.json" 2>/dev/null; then
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
			( unset GH_TOKEN GITHUB_TOKEN TG_BOT_SECRET OPENROUTER_API_KEY; AI_ENGINE_MODEL_HINT="${model}" AI_ENGINE_EFFORT_HINT="${reasoning}" \
				claude_run UNBLOCK_JUDGE "${prompt_file}" "${output_file}" "${TARGET_DIR}" ) || rc=$?
		fi
		if [ "${rc}" -eq 75 ]; then
			if bash "${SUPPORT_DIR}/scripts/write_codex_config.sh" --model "${model}" --reasoning "${reasoning}" >/dev/null 2>&1; then
				(cd "${TARGET_DIR}" && env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET timeout "${UNBLOCK_JUDGE_TIMEOUT_SECS:-1500}" \
					codex --ask-for-approval never -c model_verbosity=low exec --skip-git-repo-check --model "${model}" --sandbox read-only \
					< "${prompt_file}" > "${output_file}" 2>"${RUNTIME_DIR}/codex.err") || true
			fi
		fi
	fi
	if ! unblock_py - "${output_file}" > "${RUNTIME_DIR}/verdict_raw.json" <<'PY'
import json, os, re, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
api_key = os.environ.get("OPENROUTER_API_KEY", "")
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
		print(json.dumps(redact_verdict_value(value)))
		sys.exit(0)
sys.exit(1)
PY
	then
		echo '{}' > "${RUNTIME_DIR}/verdict_raw.json"
	fi
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	unblock_main "$@"
	exit 0
fi
