#!/usr/bin/env bash
# alert_self_fix_break_glass.sh — the hourly break-glass scan for alert
# self-fix PRs (alert triage plan Phase 3, Refs #9394;
# docs/plans/alert-triage-auto-heal-plan.md, "Self-fix and break-glass").
#
# `ai-review/head-gate` is a required status on the default branch and the
# ruleset has no bypass actors, so a fix for a broken review pipeline cannot
# merge through that pipeline. This script posts `ai-review/head-gate=success`
# on a self-fix PR head ONLY when all six conditions hold:
#   1. ownership: the PR carries ai:alert-self-fix, is not cross-repository,
#      its branch is ai/alert-self-fix-<fp8>-<run_id>, the ledger record
#      self_fix/pr-<n>.json on ai/alert-inbox (written by alert_self_fix.sh
#      with the job token) names the same branch, run id and head, and the PR
#      has exactly one commit, that head, whose only parent is the recorded
#      base (no foreign commits);
#   2. head gate: no ai-review/head-gate status on the head 60 minutes after
#      the recorded push, or the latest one is `error`. A `failure` is never
#      overridden: it is escalated once per (PR, head) instead;
#   3. paths: every changed file (and every rename source) matches
#      break_glass_allowed_paths and none matches break_glass_denied_paths of
#      the trusted .github/ai/alert_inbox.json (a denied match wins);
#   4. review: a separate read-only ALERT_BREAK_GLASS_REVIEWER engine call,
#      which sees only the diff and the diagnosis, replies exactly one JSON
#      object with "verdict": "approve";
#   5. tests: every tests/test_*.py passes on the head in the credential-free
#      gate container (scripts/prepush_test_gate_run.sh --tests-only), with
#      no failures and no inherited failures;
#   6. cap: fewer than ALERT_TRIAGE_BREAK_GLASS_MAX_PER_DAY passes in the last
#      24 hours (break_glass/pass-*.json records).
#
# Subcommands (one workflow step each; state in ALERT_BREAK_GLASS_STATE_DIR):
#   scan    switch, cap (6), one PR list, conditions 1-3 per PR (at most 20,
#           oldest first, stop at the first that qualifies), the diff and the
#           diagnosis for the candidate, and its head fetched into a separate
#           repository. Token: GITHUB_TOKEN.
#   review  condition 4, no GitHub token, OIDC variable or Telegram secret.
#   test    condition 5, no token at all (the container gets no environment).
#   post    re-check head, status and cap; write the pass record (ledger
#           first: a failed write posts nothing); POST the status with
#           GH_TOKEN (GH_PAT, the identity scripts/review_head_gate.sh posts
#           with); one direct Telegram notice.
#
# Variables (every one has a default): ALERT_TRIAGE_BREAK_GLASS_ENABLED=false,
# ALERT_TRIAGE_BREAK_GLASS_MAX_PER_DAY=2 (0-10; invalid -> 0, fail closed),
# ALERT_TRIAGE_BREAK_GLASS_TEST_TIMEOUT_MINUTES=90,
# ALERT_BREAK_GLASS_ENGINE_TIMEOUT_SECS=900, ALERT_BREAK_GLASS_MODEL=openai/gpt-6-sol,
# ALERT_BREAK_GLASS_STATE_DIR / ALERT_BREAK_GLASS_HEAD_DIR / ALERT_TRIAGE_INBOX_DIR
# under $RUNNER_TEMP, ALERT_BREAK_GLASS_REMOTE_URL (this repository).
#
# Log lines (stdout, stable prefix ALERT_BREAK_GLASS):
#   ALERT_BREAK_GLASS skip reason=<token>
#   ALERT_BREAK_GLASS evaluate pr=<n> head=<sha12> condition=<1-6> outcome=true|false reason=<token>
#   ALERT_BREAK_GLASS escalate pr=<n> head=<sha12> outcome=sent|failed|deduplicated reason=<token>
#   ALERT_BREAK_GLASS pass pr=<n> head=<sha12> outcome=posted|post_failed|ledger_failed|recheck_failed reason=<token>
#   ALERT_BREAK_GLASS notice pr=<n> outcome=sent|failed

set -euo pipefail

ABG_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ABG_REPO_ROOT="$(dirname "${ABG_SCRIPT_DIR}")"
ABG_SELF_REPO="${GITHUB_REPOSITORY:-shubhodeep1/coding-workflows}"
ABG_SERVER="${GITHUB_SERVER_URL:-https://github.com}"
ABG_TMP="${RUNNER_TEMP:-/tmp}"
ABG_RUN_ID="${GITHUB_RUN_ID:-0}"
ABG_RUN_URL="${ABG_SERVER}/${ABG_SELF_REPO}/actions/runs/${ABG_RUN_ID}"
ABG_STATE_DIR="${ALERT_BREAK_GLASS_STATE_DIR:-${ABG_TMP}/alert-break-glass}"
ABG_REVIEW_DIR="${ABG_STATE_DIR}/review"
ABG_HEAD_DIR="${ALERT_BREAK_GLASS_HEAD_DIR:-${ABG_TMP}/break-glass-head}"
ABG_INBOX_DIR="${ALERT_TRIAGE_INBOX_DIR:-${ABG_TMP}/alert-inbox}"
ABG_REMOTE_URL="${ALERT_BREAK_GLASS_REMOTE_URL:-${ABG_SERVER}/${ABG_SELF_REPO}.git}"
ABG_CONFIG="${ABG_REPO_ROOT}/.github/ai/alert_inbox.json"
ABG_LABEL="ai:alert-self-fix"
ABG_CONTEXT="ai-review/head-gate"
ABG_MAX_CANDIDATES=20
ABG_WAIT_SECS=3600
ABG_DIFF_MAX_BYTES=262144
export PYTHONDONTWRITEBYTECODE=1

_abg_log()
{
	printf 'ALERT_BREAK_GLASS %s\n' "$*"
}

_abg_output()
{
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		printf '%s\n' "$1" >> "${GITHUB_OUTPUT}"
	fi
}

if ! [[ "${ABG_SELF_REPO}" =~ ^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$ ]] || ! [[ "${ABG_RUN_ID}" =~ ^[0-9]{1,20}$ ]]; then
	_abg_log "skip reason=bad_run_context"
	exit 1
fi

_abg_enabled()
{
	[ "$(printf '%s' "${ALERT_TRIAGE_BREAK_GLASS_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]
}

# The daily cap: an integer 0-10, default 2. Anything else is 0 (no pass).
_abg_cap()
{
	local raw="${ALERT_TRIAGE_BREAK_GLASS_MAX_PER_DAY:-2}"
	if [[ "${raw}" =~ ^[0-9]{1,2}$ ]] && [ "$((10#${raw}))" -le 10 ]; then
		printf '%s\n' "$((10#${raw}))"
		return 0
	fi
	echo "::warning::ALERT_TRIAGE_BREAK_GLASS_MAX_PER_DAY is not an integer 0-10; break-glass stays off (cap 0)." >&2
	printf '0\n'
}

_abg_gh_read()
{
	GH_TOKEN="${GITHUB_TOKEN:-}" gh "$@"
}

_abg_eval()
{
	local pr="$1" head="$2" condition="$3" outcome="$4" reason="$5"
	_abg_log "evaluate pr=${pr} head=${head:0:12} condition=${condition} outcome=${outcome} reason=${reason}"
}

_abg_fetch_inbox()
{
	local present="" rc=0 out_file
	out_file="$(mktemp "${ABG_TMP}/alert-break-glass-fetch-XXXXXX")"
	ALERT_TRIAGE_INBOX_DIR="${ABG_INBOX_DIR}" GITHUB_OUTPUT="${out_file}" \
		bash "${ABG_SCRIPT_DIR}/alert_triage_route.sh" fetch >/dev/null 2>&1 || rc=$?
	present="$(sed -n 's/^present=//p' "${out_file}" | tail -n 1)"
	rm -f -- "${out_file}"
	[ "${rc}" -eq 0 ] && [ "${present}" = "true" ]
}

# Passes recorded in the last 24 hours (condition 6).
_abg_recent_passes()
{
	python3 -I -B - "${ABG_INBOX_DIR}/break_glass" <<'PY'
import datetime
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
now = datetime.datetime.now(datetime.timezone.utc)
count = 0
for path in sorted(root.glob("pass-*.json")) if root.is_dir() else []:
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
		posted = datetime.datetime.strptime(data["posted_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
	except Exception:
		# An unreadable record still counts: fail closed on the cap.
		count += 1
		continue
	if now - posted < datetime.timedelta(hours=24):
		count += 1
print(count)
PY
}

# _abg_c1 PR_JSON COMMITS_JSON RECORD_FILE -> "true <reason>" | "false <reason>"
_abg_c1()
{
	python3 -I -B - "$1" "$2" "$3" "${ABG_LABEL}" <<'PY'
import json
import pathlib
import re
import sys

pr = json.loads(sys.argv[1])
label = sys.argv[4]


def verdict(ok, reason):
	print(("true " if ok else "false ") + reason)
	sys.exit(0)


labels = {item.get("name") for item in pr.get("labels") or [] if isinstance(item, dict)}
if label not in labels:
	verdict(False, "label_missing")
if pr.get("isCrossRepository") is not False:
	verdict(False, "cross_repository")
branch = pr.get("headRefName") or ""
match = re.fullmatch(r"ai/alert-self-fix-([0-9a-f]{8})-([0-9]{1,20})", branch)
if not match:
	verdict(False, "branch_name")
head = pr.get("headRefOid") or ""
record_path = pathlib.Path(sys.argv[3])
try:
	record = json.loads(record_path.read_text(encoding="utf-8"))
except Exception:
	verdict(False, "no_ledger_record")
if not isinstance(record, dict) or record.get("schema") != "alert_self_fix_record.v1":
	verdict(False, "ledger_schema")
if record.get("pr") != pr.get("number") or record.get("branch") != branch or record.get("run_id") != match.group(2):
	verdict(False, "ledger_mismatch")
if not str(record.get("fingerprint") or "").startswith(match.group(1)):
	verdict(False, "ledger_mismatch")
if not re.fullmatch(r"[0-9a-f]{40}", head) or record.get("head_sha") != head:
	verdict(False, "head_not_recorded")
base = record.get("base_sha") or ""
if not re.fullmatch(r"[0-9a-f]{40}", base):
	verdict(False, "ledger_schema")
try:
	commits = json.loads(sys.argv[2])
except Exception:
	verdict(False, "commits_unreadable")
if not isinstance(commits, list) or len(commits) != 1:
	verdict(False, "foreign_commits")
commit = commits[0] if isinstance(commits[0], dict) else {}
parents = [item.get("sha") for item in commit.get("parents") or [] if isinstance(item, dict)]
if commit.get("sha") != head or parents != [base]:
	verdict(False, "foreign_commits")
verdict(True, "owned")
PY
}

# _abg_c2 STATUSES_JSON PUSHED_AT -> "true|false|failure <reason>"
_abg_c2()
{
	python3 -I -B - "$1" "$2" "${ABG_CONTEXT}" "${ABG_WAIT_SECS}" <<'PY'
import datetime
import json
import sys

context = sys.argv[3]
wait = int(sys.argv[4])
try:
	statuses = json.loads(sys.argv[1])
except Exception:
	print("false statuses_unreadable")
	sys.exit(0)
if not isinstance(statuses, list):
	print("false statuses_unreadable")
	sys.exit(0)
mine = [item for item in statuses if isinstance(item, dict) and item.get("context") == context]
# The API lists statuses newest first; sort defensively by created_at.
mine.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
if mine:
	state = mine[0].get("state")
	if state == "error":
		print("true status_error")
	elif state == "failure":
		print("failure status_failure")
	elif state in ("pending", "success"):
		print(f"false status_{state}")
	else:
		print("false status_unknown")
	sys.exit(0)
try:
	pushed = datetime.datetime.strptime(sys.argv[2], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
except Exception:
	print("false no_pushed_at")
	sys.exit(0)
age = (datetime.datetime.now(datetime.timezone.utc) - pushed).total_seconds()
print("true status_missing" if age >= wait else "false waiting")
PY
}

# _abg_c3 FILES_JSON -> "true|false <reason>"
_abg_c3()
{
	python3 -I -B - "$1" "${ABG_CONFIG}" <<'PY'
import json
import re
import sys


def glob_re(pattern):
	# Full-path anchored; `**` crosses `/`, `*` and `?` do not.
	out = []
	index = 0
	while index < len(pattern):
		if pattern.startswith("**", index):
			out.append(".*")
			index += 2
		elif pattern[index] == "*":
			out.append("[^/]*")
			index += 1
		elif pattern[index] == "?":
			out.append("[^/]")
			index += 1
		else:
			out.append(re.escape(pattern[index]))
			index += 1
	return re.compile("".join(out))


try:
	config = json.load(open(sys.argv[2], encoding="utf-8"))
	allowed = [glob_re(item) for item in config["break_glass_allowed_paths"] if isinstance(item, str) and item]
	denied = [glob_re(item) for item in config["break_glass_denied_paths"] if isinstance(item, str) and item]
except Exception:
	print("false config_missing")
	sys.exit(0)
if not allowed:
	print("false config_missing")
	sys.exit(0)
try:
	files = json.loads(sys.argv[1])
except Exception:
	print("false files_unreadable")
	sys.exit(0)
if not isinstance(files, list) or not files:
	print("false no_files")
	sys.exit(0)
if len(files) >= 100:
	print("false too_many_files")
	sys.exit(0)
for entry in files:
	if not isinstance(entry, dict):
		print("false files_unreadable")
		sys.exit(0)
	for key in ("filename", "previous_filename"):
		path = entry.get(key)
		if key == "previous_filename" and not path:
			continue
		if not isinstance(path, str) or not path or ".." in path.split("/") or path.startswith("/"):
			print("false bad_path")
			sys.exit(0)
		if any(rule.fullmatch(path) for rule in denied):
			print("false denied_path")
			sys.exit(0)
		if not any(rule.fullmatch(path) for rule in allowed):
			print("false not_allowed_path")
			sys.exit(0)
print("true paths_allowed")
PY
}

_abg_escalate_failure()
{
	local pr="$1" head="$2" marker="${ABG_STATE_DIR}/escalation.json" rc=0 msg_id=""
	jq -n --argjson pr "${pr}" --arg head "${head}" --arg run_id "${ABG_RUN_ID}" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
		'{schema: "alert_break_glass_escalation.v1", pr: $pr, head_sha: $head, run_id: $run_id, escalated_at: $at, reason: "status_failure"}' > "${marker}"
	# At most once per (PR, head): the marker is written before the send.
	bash "${ABG_SCRIPT_DIR}/alert_self_fix.sh" ledger-put "break_glass/escalated-pr${pr}-${head:0:12}.json" "${marker}" >/dev/null 2>&1 || rc=$?
	if [ "${rc}" -eq 3 ]; then
		_abg_log "escalate pr=${pr} head=${head:0:12} outcome=deduplicated reason=status_failure"
		return 0
	fi
	if [ "${rc}" -ne 0 ]; then
		_abg_log "escalate pr=${pr} head=${head:0:12} outcome=failed reason=ledger_failed"
		return 0
	fi
	msg_id="$(_abg_send "Alert self-fix PR ${pr}: ai-review/head-gate reported failure on ${head:0:12}. Break-glass never overrides a failure; a person must look.
Run: ${ABG_RUN_URL}" ERROR)" || msg_id=""
	if [ -n "${msg_id}" ]; then
		_abg_log "escalate pr=${pr} head=${head:0:12} outcome=sent reason=status_failure"
	else
		_abg_log "escalate pr=${pr} head=${head:0:12} outcome=failed reason=status_failure"
	fi
}

_abg_send()
{
	local text="$1" level="$2"
	[ -f "${ABG_SCRIPT_DIR}/tg_helpers.sh" ] || return 1
	(
		# shellcheck disable=SC1091
		source "${ABG_SCRIPT_DIR}/tg_helpers.sh" && tg_send_msg_direct "${text}" "${level}" 2>/dev/null
	)
}

# _abg_git_auth ARGS — git with GITHUB_TOKEN as an extra header (env only).
_abg_git_auth()
{
	local token="${GITHUB_TOKEN:-}"
	if [ -n "${token}" ] && [[ "${ABG_REMOTE_URL}" == https://* ]]; then
		GIT_CONFIG_COUNT=1 \
		GIT_CONFIG_KEY_0="http.${ABG_SERVER}/.extraheader" \
		GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${token}" | base64 | tr -d '\n')" \
			git "$@"
	else
		git "$@"
	fi
}

# --- scan ------------------------------------------------------------------------------

abg_scan()
{
	local cap=0 passes=0 prs="" pr="" head="" record="" commits="" statuses="" files="" verdict="" reason="" result=""
	local pushed_at="" base="" candidate="" pr_json=""
	rm -rf -- "${ABG_STATE_DIR}"
	mkdir -p "${ABG_STATE_DIR}"
	_abg_output "candidate="
	if ! _abg_enabled; then
		_abg_log "skip reason=disabled"
		return 0
	fi
	cap="$(_abg_cap)"
	if [ "${cap}" -eq 0 ]; then
		_abg_eval - - 6 false cap_zero
		return 0
	fi
	if ! _abg_fetch_inbox; then
		# No ledger, no proof of ownership: nothing can pass.
		_abg_log "skip reason=no_inbox"
		return 0
	fi
	passes="$(_abg_recent_passes)"
	if [ "${passes}" -ge "${cap}" ]; then
		_abg_eval - - 6 false cap_reached
		return 0
	fi
	if ! prs="$(_abg_gh_read pr list --repo "${ABG_SELF_REPO}" --label "${ABG_LABEL}" --state open --limit 100 \
		--json number,headRefName,headRefOid,isCrossRepository,labels,createdAt 2>/dev/null)"; then
		_abg_log "skip reason=pr_list_failed"
		return 1
	fi
	while IFS= read -r pr_json; do
		pr="$(jq -r '.number' <<< "${pr_json}")"
		head="$(jq -r '.headRefOid // ""' <<< "${pr_json}")"
		[[ "${pr}" =~ ^[0-9]{1,10}$ ]] || continue
		record="${ABG_INBOX_DIR}/self_fix/pr-${pr}.json"
		commits="$(_abg_gh_read api "repos/${ABG_SELF_REPO}/pulls/${pr}/commits?per_page=2" 2>/dev/null)" || commits="null"
		result="$(_abg_c1 "${pr_json}" "${commits}" "${record}")"
		read -r verdict reason <<< "${result}"
		_abg_eval "${pr}" "${head}" 1 "${verdict}" "${reason}"
		[ "${verdict}" = "true" ] || continue
		pushed_at="$(jq -r '.pushed_at // ""' "${record}")"
		base="$(jq -r '.base_sha' "${record}")"
		statuses="$(_abg_gh_read api "repos/${ABG_SELF_REPO}/commits/${head}/statuses?per_page=100" 2>/dev/null)" || statuses="null"
		result="$(_abg_c2 "${statuses}" "${pushed_at}")"
		read -r verdict reason <<< "${result}"
		if [ "${verdict}" = "failure" ]; then
			_abg_eval "${pr}" "${head}" 2 false "${reason}"
			_abg_escalate_failure "${pr}" "${head}"
			continue
		fi
		_abg_eval "${pr}" "${head}" 2 "${verdict}" "${reason}"
		[ "${verdict}" = "true" ] || continue
		files="$(_abg_gh_read api "repos/${ABG_SELF_REPO}/pulls/${pr}/files?per_page=100" 2>/dev/null)" || files="null"
		result="$(_abg_c3 "${files}")"
		read -r verdict reason <<< "${result}"
		_abg_eval "${pr}" "${head}" 3 "${verdict}" "${reason}"
		[ "${verdict}" = "true" ] || continue
		candidate="${pr}"
		break
	done < <(jq -c --argjson max "${ABG_MAX_CANDIDATES}" 'sort_by(.createdAt) | .[:$max] | .[]' <<< "${prs}")
	if [ -z "${candidate}" ]; then
		_abg_log "skip reason=no_candidate"
		return 0
	fi
	# Review input: only the diff and the diagnosis.
	mkdir -p "${ABG_REVIEW_DIR}"
	if ! _abg_gh_read api -H "Accept: application/vnd.github.diff" "repos/${ABG_SELF_REPO}/pulls/${candidate}" > "${ABG_REVIEW_DIR}/diff.patch" 2>/dev/null; then
		_abg_eval "${candidate}" "${head}" 4 false diff_unavailable
		return 0
	fi
	if [ "$(wc -c < "${ABG_REVIEW_DIR}/diff.patch")" -gt "${ABG_DIFF_MAX_BYTES}" ]; then
		_abg_eval "${candidate}" "${head}" 4 false diff_too_large
		return 0
	fi
	jq '.diagnosis // {}' "${record}" > "${ABG_REVIEW_DIR}/diagnosis.json"
	# The head tree for condition 5, in its own repository (never the
	# trusted checkout). Depth 2 keeps the recorded base reachable.
	rm -rf -- "${ABG_HEAD_DIR}"
	git init -q "${ABG_HEAD_DIR}"
	if ! _abg_git_auth -C "${ABG_HEAD_DIR}" fetch -q --no-tags --depth=2 "${ABG_REMOTE_URL}" "${head}" >/dev/null 2>&1 \
		|| ! git -C "${ABG_HEAD_DIR}" -c advice.detachedHead=false checkout -q --detach "${head}" >/dev/null 2>&1 \
		|| [ "$(git -C "${ABG_HEAD_DIR}" rev-parse HEAD^ 2>/dev/null)" != "${base}" ]; then
		_abg_eval "${candidate}" "${head}" 5 false head_fetch_failed
		return 0
	fi
	jq -n --argjson pr "${candidate}" --arg head "${head}" --arg base "${base}" \
		'{pr: $pr, head_sha: $head, base_sha: $base}' > "${ABG_STATE_DIR}/candidate.json"
	_abg_output "candidate=${candidate}"
	_abg_log "candidate pr=${candidate} head=${head:0:12}"
}

_abg_candidate()
{
	local field="$1"
	[ -f "${ABG_STATE_DIR}/candidate.json" ] || return 1
	jq -er --arg f "${field}" '.[$f] | tostring' "${ABG_STATE_DIR}/candidate.json"
}

# --- review (condition 4) -------------------------------------------------------------

_abg_parse_review()
{
	python3 -I -B - "$1" <<'PY'
import json
import sys

try:
	text = open(sys.argv[1], encoding="utf-8").read()
except Exception:
	print("reject reply_missing")
	sys.exit(0)
decoder = json.JSONDecoder()
objects = []
index = 0
while True:
	start = text.find("{", index)
	if start < 0:
		break
	try:
		value, end = decoder.raw_decode(text, start)
	except ValueError:
		index = start + 1
		continue
	if isinstance(value, dict):
		objects.append(value)
	index = end
if len(objects) != 1:
	print("reject malformed")
	sys.exit(0)
reply = objects[0]
reasons = reply.get("reasons")
if set(reply) != {"verdict", "reasons"} or not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
	print("reject malformed")
elif reply.get("verdict") == "approve":
	print("approve approved")
elif reply.get("verdict") == "reject":
	print("reject rejected")
else:
	print("reject malformed")
PY
}

abg_review()
{
	local pr="" head="" engine_root="${CLAUDE_ENGINE_SUPPORT_DIR:-}" prompt="${ABG_STATE_DIR}/review_prompt.md" reply="${ABG_STATE_DIR}/review_reply.txt"
	local template="${ABG_REPO_ROOT}/prompts/mode-alert-break-glass-review.txt" call_timeout="${ALERT_BREAK_GLASS_ENGINE_TIMEOUT_SECS:-900}"
	local model="${ALERT_BREAK_GLASS_MODEL:-openai/gpt-6-sol}" rc=0 verdict="reject" reason="engine_failed"
	_abg_output "approved=false"
	pr="$(_abg_candidate pr)" || { _abg_log "skip reason=no_candidate"; return 0; }
	head="$(_abg_candidate head_sha)"
	[[ "${call_timeout}" =~ ^[0-9]{1,5}$ ]] && [ "$((10#${call_timeout}))" -ge 30 ] || call_timeout=900
	call_timeout=$((10#${call_timeout}))
	[[ "${model}" =~ ^[a-zA-Z0-9/_.:-]+$ ]] || model="openai/gpt-6-sol"
	rm -f -- "${reply}"
	if [ ! -f "${template}" ] || [ -z "${engine_root}" ] || [ ! -f "${engine_root}/scripts/ai_engine.sh" ] || [ -L "${engine_root}/scripts/ai_engine.sh" ]; then
		_abg_eval "${pr}" "${head}" 4 false engine_unavailable
		return 0
	fi
	{
		sed -e "s/{{PR_NUMBER}}/${pr}/g" -e "s/{{HEAD_SHA}}/${head}/g" "${template}"
		printf '\n=== BEGIN UNTRUSTED DIAGNOSIS ===\n'
		# A line starting with `===` cannot close a fence early.
		sed -e 's/^===/= ==/' "${ABG_REVIEW_DIR}/diagnosis.json"
		printf '\n=== END UNTRUSTED DIAGNOSIS ===\n\n=== BEGIN UNTRUSTED DIFF ===\n'
		sed -e 's/^===/= ==/' "${ABG_REVIEW_DIR}/diff.patch"
		printf '\n=== END UNTRUSTED DIFF ===\n'
	} > "${prompt}"
	(
		# The reviewer never gets a GitHub token, the OIDC request variables
		# or the Telegram secret, and sees only the review directory.
		unset GH_TOKEN GITHUB_TOKEN GH_PAT TG_BOT_SECRET ACTIONS_ID_TOKEN_REQUEST_TOKEN ACTIONS_ID_TOKEN_REQUEST_URL
		unset AI_ENGINE_INCLUDE_PATHS
		export AI_ENGINE_READ_ONLY=true
		# shellcheck disable=SC2016  # $0 / $@ expand in the child bash, not here.
		timeout -k 30 "${call_timeout}" \
			bash -c 'source "$0" || exit 2; claude_run_selected "$@"' "${engine_root}/scripts/ai_engine.sh" \
			ALERT_BREAK_GLASS_REVIEWER "${prompt}" "${reply}" "${ABG_REVIEW_DIR}" --codex-stdio -- \
			bash "${engine_root}/scripts/codex_isolated_exec.sh" run --mode read-only --workdir "${ABG_REVIEW_DIR}" -- \
				--ask-for-approval never \
				exec \
				--skip-git-repo-check \
				--model "${model}" \
				--sandbox read-only
	) >/dev/null 2>&1 || rc=$?
	case "${rc}" in
		0) read -r verdict reason <<< "$(_abg_parse_review "${reply}")" ;;
		124|137) reason="timeout" ;;
		*) reason="engine_failed" ;;
	esac
	if [ "${verdict}" = "approve" ]; then
		_abg_eval "${pr}" "${head}" 4 true "${reason}"
		printf 'approve\n' > "${ABG_STATE_DIR}/review_verdict"
		_abg_output "approved=true"
	else
		_abg_eval "${pr}" "${head}" 4 false "${reason}"
	fi
}

# --- test (condition 5) ---------------------------------------------------------------

abg_test()
{
	local pr="" head="" base="" minutes="${ALERT_TRIAGE_BREAK_GLASS_TEST_TIMEOUT_MINUTES:-90}" image="" prep="" status=""
	local exec_helper="${ABG_REPO_ROOT}/scripts/codex_isolated_exec.sh" wrapper="${ABG_REPO_ROOT}/scripts/prepush_test_gate_run.sh"
	local module="" tests_file="${ABG_STATE_DIR}/tests.txt" gate_dir="${ABG_STATE_DIR}/gate"
	_abg_output "tests_passed=false"
	pr="$(_abg_candidate pr)" || { _abg_log "skip reason=no_candidate"; return 0; }
	head="$(_abg_candidate head_sha)"
	base="$(_abg_candidate base_sha)"
	if [ "$(cat "${ABG_STATE_DIR}/review_verdict" 2>/dev/null)" != "approve" ]; then
		_abg_eval "${pr}" "${head}" 5 false not_reviewed
		return 0
	fi
	[[ "${minutes}" =~ ^[0-9]{1,3}$ ]] && [ "$((10#${minutes}))" -ge 1 ] || minutes=90
	minutes=$((10#${minutes}))
	if [ "$(git -C "${ABG_HEAD_DIR}" rev-parse HEAD 2>/dev/null)" != "${head}" ]; then
		_abg_eval "${pr}" "${head}" 5 false head_tree_mismatch
		return 0
	fi
	for module in "${ABG_REPO_ROOT}/.claude/hooks/prepush_affected_tests.py" "${ABG_REPO_ROOT}/workflow-templates/.claude/hooks/prepush_affected_tests.py"; do
		[ -f "${module}" ] && [ ! -L "${module}" ] && break
		module=""
	done
	if [ -z "${module}" ] || [ ! -f "${wrapper}" ] || [ ! -f "${exec_helper}" ]; then
		_abg_eval "${pr}" "${head}" 5 false gate_missing
		return 0
	fi
	(cd "${ABG_HEAD_DIR}" && find tests -maxdepth 1 -type f -name 'test_*.py' 2>/dev/null | LC_ALL=C sort) > "${tests_file}"
	if [ ! -s "${tests_file}" ]; then
		_abg_eval "${pr}" "${head}" 5 false no_tests
		return 0
	fi
	local -a clean_env=(env -i "PATH=${PATH}" "HOME=${HOME:-/tmp}" "RUNNER_TEMP=${ABG_TMP}" PYTHONDONTWRITEBYTECODE=1)
	image="$("${clean_env[@]}" bash "${exec_helper}" image </dev/null 2>/dev/null | tail -n 1)" || image=""
	if ! [[ "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
		_abg_eval "${pr}" "${head}" 5 false image_unavailable
		return 0
	fi
	prep="$("${clean_env[@]}" bash "${exec_helper}" prepare --workdir "${ABG_HEAD_DIR}" --deps </dev/null 2>/dev/null | tail -n 1)" || prep=""
	[[ "${prep}" == /* ]] && [ -d "${prep}" ] || prep=""
	local -a gate_args=(--worktree "${ABG_HEAD_DIR}" --module "${module}" --image "${image}" --base "${base}"
		--out-dir "${gate_dir}" --timeout-minutes "${minutes}" --shards 4 --tests-only "${tests_file}")
	if [ -n "${prep}" ] && [ -d "${prep}/venv/bin" ]; then
		gate_args+=(--venv "${prep}/venv")
	fi
	if [ -n "${prep}" ] && [ -d "${prep}/work/node_modules" ] && [ ! -L "${prep}/work/node_modules" ]; then
		gate_args+=(--node-modules "${prep}/work/node_modules")
	fi
	rm -rf -- "${gate_dir}"
	"${clean_env[@]}" bash "${wrapper}" "${gate_args[@]}" </dev/null >/dev/null 2>&1 || true
	status="$(python3 -I -B -c '
import json, sys
try:
	data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
	print("invalid")
	sys.exit(0)
if not isinstance(data, dict):
	print("invalid")
elif data.get("status") != "pass":
	status = data.get("status")
	print(status if status in ("fail", "unverified", "skipped", "timeout") else "invalid")
elif data.get("failures") or data.get("inherited"):
	print("failures")
else:
	print("pass")
' "${gate_dir}/result.json" 2>/dev/null)" || status="invalid"
	if [ "${status}" = "pass" ]; then
		_abg_eval "${pr}" "${head}" 5 true tests_pass
		printf 'pass\n' > "${ABG_STATE_DIR}/test_status"
		_abg_output "tests_passed=true"
	else
		_abg_eval "${pr}" "${head}" 5 false "tests_${status}"
	fi
}

# --- post --------------------------------------------------------------------------------

abg_post()
{
	local pr="" head="" cap=0 passes=0 current="" statuses="" result="" verdict="" reason="" stamp="" record="${ABG_STATE_DIR}/pass.json"
	local put_rc=0 description="" msg_id=""
	pr="$(_abg_candidate pr)" || { _abg_log "skip reason=no_candidate"; return 0; }
	head="$(_abg_candidate head_sha)"
	if [ "$(cat "${ABG_STATE_DIR}/review_verdict" 2>/dev/null)" != "approve" ] || [ "$(cat "${ABG_STATE_DIR}/test_status" 2>/dev/null)" != "pass" ]; then
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=conditions_incomplete"
		return 0
	fi
	if ! _abg_enabled; then
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=disabled"
		return 0
	fi
	if [ -z "${GH_TOKEN:-}" ]; then
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=no_status_token"
		return 1
	fi
	# Re-check just before posting (TOCTOU): head unchanged, gate status
	# still missing or error, the cap still has room.
	current="$(_abg_gh_read api "repos/${ABG_SELF_REPO}/pulls/${pr}" --jq '[.state, .head.sha] | join(" ")' 2>/dev/null)" || current=""
	if [ "${current}" != "open ${head}" ]; then
		_abg_eval "${pr}" "${head}" 1 false head_moved
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=head_moved"
		return 0
	fi
	statuses="$(_abg_gh_read api "repos/${ABG_SELF_REPO}/commits/${head}/statuses?per_page=100" 2>/dev/null)" || statuses="null"
	# The 60-minute wait was proved by the scan; only the state matters here.
	result="$(_abg_c2 "${statuses}" "1970-01-01T00:00:00Z")"
	read -r verdict reason <<< "${result}"
	if [ "${verdict}" != "true" ]; then
		_abg_eval "${pr}" "${head}" 2 false "${reason}"
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=${reason}"
		[ "${verdict}" = "failure" ] && _abg_escalate_failure "${pr}" "${head}"
		return 0
	fi
	cap="$(_abg_cap)"
	if ! _abg_fetch_inbox; then
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=no_inbox"
		return 0
	fi
	passes="$(_abg_recent_passes)"
	if [ "${cap}" -eq 0 ] || [ "${passes}" -ge "${cap}" ]; then
		_abg_eval "${pr}" "${head}" 6 false cap_reached
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=recheck_failed reason=cap_reached"
		return 0
	fi
	_abg_eval "${pr}" "${head}" 6 true "passes_${passes}_of_${cap}"
	# The pass record first: a failed write posts nothing, and a failed
	# POST after it still counts against the cap.
	stamp="$(date -u +%Y%m%dT%H%M%SZ)"
	jq -n --argjson pr "${pr}" --arg head "${head}" --arg run_id "${ABG_RUN_ID}" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
		'{schema: "alert_break_glass_pass.v1", pr: $pr, head_sha: $head, run_id: $run_id, posted_at: $at}' > "${record}"
	bash "${ABG_SCRIPT_DIR}/alert_self_fix.sh" ledger-put "break_glass/pass-${stamp}-pr${pr}.json" "${record}" >/dev/null 2>&1 || put_rc=$?
	if [ "${put_rc}" -ne 0 ]; then
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=ledger_failed reason=ledger_exit_${put_rc}"
		return 1
	fi
	description="break-glass: alert self-fix run ${ABG_RUN_ID}"
	if ! GH_TOKEN="${GH_TOKEN}" gh api -X POST "repos/${ABG_SELF_REPO}/statuses/${head}" \
		-f state=success -f context="${ABG_CONTEXT}" -f description="${description:0:140}" -f target_url="${ABG_RUN_URL}" >/dev/null 2>&1; then
		_abg_log "pass pr=${pr} head=${head:0:12} outcome=post_failed reason=status_post"
		_abg_send "Alert break-glass for PR ${pr} (${head:0:12}) recorded a pass but could not post the status.
Run: ${ABG_RUN_URL}" ERROR >/dev/null || true
		return 1
	fi
	_abg_log "pass pr=${pr} head=${head:0:12} outcome=posted reason=all_conditions"
	msg_id="$(_abg_send "Alert break-glass: posted ${ABG_CONTEXT}=success on alert self-fix PR ${pr} (head ${head:0:12}); all six conditions held. Passes in 24h: $((passes + 1)) of ${cap}.
Run: ${ABG_RUN_URL}" CRITICAL)" || msg_id=""
	if [ -n "${msg_id}" ]; then
		_abg_log "notice pr=${pr} outcome=sent"
	else
		_abg_log "notice pr=${pr} outcome=failed"
	fi
}

case "${1:-}" in
	scan) abg_scan ;;
	review) abg_review ;;
	test) abg_test ;;
	post) abg_post ;;
	*)
		echo "usage: alert_self_fix_break_glass.sh scan|review|test|post" >&2
		exit 2
		;;
esac
