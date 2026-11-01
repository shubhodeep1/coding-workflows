#!/usr/bin/env bash
# workflow_failure_heal_verify.sh — hourly post-merge heal verification (P7 of
# docs/plans/heal-evidence-reproduction-and-trends-plan.md, Refs #8434).
#
# Nothing else checks that a merged workflow-failure heal fixed the failure: a
# recurrence was noticed only when the identical fingerprint repeated. This
# pass, run by the `heal-verify` job of
# .github/workflows/workflow-failure-heal-intake.yml on its hourly schedule,
# checks each recently closed heal issue once per merge:
#
#   1. One GraphQL query lists the token's login (`viewer`), the closed
#      ai:workflow-heal issues updated in the last 7 days (closing pull
#      requests and last 30 comments included) and the open heal issues.
#   2. Kept: issues by the pipeline login with fp/root markers, a
#      `source=<this repo>#run` marker (a workflow_run heal of this repository),
#      closed as completed in the last 7 days by a merged same-repo pull
#      request, whose failed workflow resolves to a release / CI /
#      scheduled-check workflow file of this checkout, and with no trusted
#      `<!-- workflow-failure-heal:verified:v1 ... -->` marker posted at or
#      after the merge (D1: an earlier `outcome=recurred` marker counts toward
#      escalation instead).
#   3. For each kept heal (at most WORKFLOW_HEAL_VERIFY_MAX_PER_TICK), the
#      workflow's runs on the target branch created since the merge are sorted
#      oldest first; the oldest decisive run (success / failure / timed_out) on
#      the merge commit or a descendant of it (compare `ahead` / `identical`)
#      decides:
#        - none completed yet: nothing this tick (outcome=pending);
#        - success: `outcome=held` marker comment;
#        - failure at a different step: `outcome=inconclusive` marker comment;
#        - failure at the same step with an open heal of the same `root=`
#          lineage filed after the merge: a comment linking it, ending in an
#          `outcome=recurred_lineage` marker; the heal stays closed;
#        - otherwise the heal is reopened with a `## Fix did not hold` comment
#          ending in `outcome=recurred`; with an earlier trusted recurred
#          marker it also gets `ai:workflow-heal-escalated` and a CRITICAL
#          alert.
#
# Environment (all defaulted):
#   GITHUB_REPOSITORY                  this repository (empty: nothing to verify)
#   GITHUB_EVENT_NAME                  must be `schedule` (else skip, reason=not_scheduled)
#   WORKFLOW_HEAL_ENABLED              "false" skips (default true)
#   WORKFLOW_HEAL_VERIFY_ENABLED       "true" (any letter case) runs; unset, empty or
#                                      "false" skips (default false); any other value
#                                      skips with a warning
#   WORKFLOW_HEAL_VERIFY_MAX_PER_TICK  heals examined per run (default 3, clamped 1-10)
#   WORKFLOW_HEAL_PY                   helper (default scripts/workflow_failure_heal.py)
#   RUNTIME_DIR                        scratch directory (default /tmp/workflow-heal-verify-<run>)
#
# GitHub API cost per run (CLAUDE.md §14): one GraphQL list query. Per
# examined heal at most 3 REST reads (one workflow-runs list, at most 2
# compares, one jobs read when the deciding run failed), and at most 3 writes
# (reopen, comment, label). The recovery scan's `/user` read and OPEN-only
# list cannot serve this pass, so the list query carries `viewer` and both
# issue states as aliases. Every API error is logged and fails open: nothing
# is written and the next hourly run retries. The script always exits 0.

set -uo pipefail

log()
{
	echo "WORKFLOW_HEAL_VERIFY $*"
}

source scripts/gh_helpers.sh 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
source scripts/tg_helpers.sh 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }

SELF_REPO="${GITHUB_REPOSITORY:-}"
HEAL_PY="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
RUNTIME_DIR="${RUNTIME_DIR:-/tmp/workflow-heal-verify-${GITHUB_RUN_ID:-local}}"
RUN_ID="${GITHUB_RUN_ID:-0}"
[[ "${RUN_ID}" =~ ^[0-9]{1,20}$ ]] || RUN_ID=0
export PYTHONDONTWRITEBYTECODE=1
# One attempt per call, so retries cannot push a tick past its call budget.
export GH_RETRY_MAX_ATTEMPTS="${GH_RETRY_MAX_ATTEMPTS:-1}"
VERIFY_WINDOW_DAYS=7
MAX_READS_PER_HEAL=3
MAX_COMPARES_PER_HEAL=2

_bounded_int()
{
	local value="${1:-}" fallback="$2" low="$3" high="$4"
	case "${value}" in
		''|*[!0-9]*) value="${fallback}" ;;
	esac
	value=$((10#${value}))
	[ "${value}" -ge "${low}" ] || value="${low}"
	[ "${value}" -le "${high}" ] || value="${high}"
	printf '%s' "${value}"
}

# --- Gates -------------------------------------------------------------------

case "${WORKFLOW_HEAL_VERIFY_ENABLED:-false}" in
	[Tt][Rr][Uu][Ee]) ;;
	''|[Ff][Aa][Ll][Ss][Ee])
		log "outcome=skip reason=disabled"
		exit 0
		;;
	*)
		# The value is never echoed.
		echo "::warning::WORKFLOW_HEAL_VERIFY outcome=skip reason=invalid_switch"
		exit 0
		;;
esac
if [ "${WORKFLOW_HEAL_ENABLED:-true}" = "false" ]; then
	log "outcome=skip reason=heal_disabled"
	exit 0
fi
if [ "${GITHUB_EVENT_NAME:-}" != "schedule" ]; then
	log "outcome=skip reason=not_scheduled"
	exit 0
fi
if ! [[ "${SELF_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	log "outcome=skip reason=repository_unavailable"
	exit 0
fi
MAX_PER_TICK="$(_bounded_int "${WORKFLOW_HEAL_VERIFY_MAX_PER_TICK:-}" 3 1 10)"
mkdir -p "${RUNTIME_DIR}"

# --- Helper (selection, step matching, comment rendering) ---------------------

# Subcommands:
#   select LIST_FILE OUT_FILE          candidates JSON; logs one skip line per dropped heal
#   match-step SELECTED_JOBS CANDIDATE prints "match|nomatch<TAB><sanitised first failing step>"
#   render KIND CANDIDATE RUN_ID CONCLUSION STEP   comment body on stdout
_verify_py()
{
	HEAL_VERIFY_SELF_REPO="${SELF_REPO}" HEAL_VERIFY_HEAL_PY="${HEAL_PY}" \
		HEAL_VERIFY_WINDOW_DAYS="${VERIFY_WINDOW_DAYS}" python3 - "$@" <<'PY'
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(os.environ["HEAL_VERIFY_HEAL_PY"])))
import workflow_failure_heal as heal  # noqa: E402

SELF_REPO = os.environ["HEAL_VERIFY_SELF_REPO"]
WINDOW = timedelta(days=int(os.environ["HEAL_VERIFY_WINDOW_DAYS"]))
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")
ISO_Z_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
MARKER_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
VERIFIED_RE = re.compile(
	r"^<!-- workflow-failure-heal:verified:v1 outcome=(held|recurred|recurred_lineage|inconclusive) run=[0-9]{1,20} -->$"
)
FAILED_RUN_RE = re.compile(r"^- \*\*Failed run:\*\* (?P<url>\S+) — workflow `(?P<workflow>[^`]*)`, step `(?P<step>[^`]*)`\s*$")
STEP_CONCLUSIONS = ("failure", "timed_out", "cancelled")


def log(text):
	print(f"WORKFLOW_HEAL_VERIFY {text}", flush=True)


def norm(value):
	return heal.neutralize_untrusted_routing(heal.single_line(value, 200))[0]


def code(value, limit=200):
	text = norm(heal.sanitize_text(value))[:limit].replace("`", "'")
	return text or "unknown"


def iso(value):
	if not isinstance(value, str) or not ISO_Z_RE.fullmatch(value):
		return None
	return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def login_of(node):
	author = node.get("author") if isinstance(node, dict) else None
	return (author or {}).get("login") if isinstance(author, dict) else None


def workflow_files():
	files = {}
	for name in tuple(heal.RELEASE_WORKFLOW_FILES) + tuple(heal.CI_WORKFLOW_FILES) + tuple(heal.SCHEDULED_CHECK_WORKFLOW_FILES):
		path = os.path.join(".github", "workflows", name)
		try:
			with open(path, encoding="utf-8") as handle:
				for line in handle:
					match = re.match(r"^name:\s*(.+?)\s*$", line)
					if match:
						files[name] = match.group(1).strip().strip("'\"")
						break
		except OSError:
			continue
	return files


def cmd_select(list_file, out_file):
	candidates = []
	try:
		with open(list_file, encoding="utf-8") as handle:
			data = json.load(handle)["data"]
		closed = data["repository"]["closed"]["nodes"]
		opened = data["repository"]["open"]["nodes"]
		if not isinstance(closed, list) or not isinstance(opened, list):
			raise ValueError("nodes")
	except Exception:  # noqa: BLE001 - malformed list fails open
		return 2
	viewer = ((data.get("viewer") or {}).get("login") or "") if isinstance(data.get("viewer"), dict) else ""
	if not isinstance(viewer, str) or not LOGIN_RE.fullmatch(viewer):
		log("outcome=skip reason=identity_unavailable")
		with open(out_file, "w", encoding="utf-8") as handle:
			json.dump([], handle)
		return 0
	if ((data["repository"]["closed"].get("pageInfo") or {}).get("hasNextPage")) is True:
		log("warn reason=list_truncated")
	viewer_key = viewer.casefold()
	now = datetime.now(timezone.utc)
	resolved = workflow_files()
	trusted_open = []
	for node in opened:
		if not isinstance(node, dict) or not isinstance(node.get("number"), int):
			continue
		if not isinstance(login_of(node), str) or login_of(node).casefold() != viewer_key:
			continue
		markers = heal.parse_heal_markers(node.get("body") or "")
		created = iso(node.get("createdAt"))
		if markers.get("root") and created:
			trusted_open.append((node["number"], markers["root"], created))
	for node in closed:
		if not isinstance(node, dict) or not isinstance(node.get("number"), int) or node["number"] < 1:
			continue
		number = node["number"]

		def skip(reason):
			log(f"outcome=skip issue={number} reason={reason}")

		author = login_of(node)
		if not isinstance(author, str) or author.casefold() != viewer_key:
			skip("untrusted_author")
			continue
		body = node.get("body") or ""
		if not isinstance(body, str):
			skip("no_markers")
			continue
		markers = heal.parse_heal_markers(body)
		fp, root = markers.get("fp", ""), markers.get("root", "")
		if not MARKER_VALUE_RE.fullmatch(fp) or not MARKER_VALUE_RE.fullmatch(root):
			skip("no_markers")
			continue
		if markers.get("source") != f"{SELF_REPO}#run":
			skip("unsupported_source")
			continue
		closed_at = iso(node.get("closedAt"))
		if closed_at is None or node.get("stateReason") != "COMPLETED":
			skip("not_completed")
			continue
		if now - closed_at > WINDOW:
			skip("outside_window")
			continue
		pr = None
		prs = ((node.get("closedByPullRequestsReferences") or {}).get("nodes") or [])
		for item in prs if isinstance(prs, list) else []:
			if not isinstance(item, dict) or item.get("merged") is not True:
				continue
			if ((item.get("repository") or {}).get("nameWithOwner")) != SELF_REPO:
				continue
			merged_at = iso(item.get("mergedAt"))
			sha = (item.get("mergeCommit") or {}).get("oid")
			if merged_at is None or merged_at > closed_at or not heal.is_valid_sha(sha) or not isinstance(item.get("number"), int):
				continue
			if pr is None or merged_at > pr[0]:
				pr = (merged_at, item)
		if pr is None:
			skip("no_merged_pr")
			continue
		merged_at, pr_node = pr
		branch_match = heal._HEAL_TARGET_BRANCH_RE.search(body)
		branch = ((branch_match.group(1) or branch_match.group(2)).strip() if branch_match else "") or (pr_node.get("baseRefName") or "")
		if not heal.is_valid_branch(branch):
			skip("branch_invalid")
			continue
		runs = []
		for line in body.splitlines():
			match = FAILED_RUN_RE.match(line)
			if not match:
				continue
			url = heal._RUN_URL_RE.fullmatch(match.group("url"))
			if url and url.group("repo") == SELF_REPO:
				runs.append((match.group("workflow"), match.group("step")))
		if not runs:
			skip("workflow_unresolved")
			continue
		workflow_name = runs[0][0]
		matches = [name for name, title in resolved.items() if norm(title) == workflow_name]
		if len(matches) != 1:
			skip("workflow_unresolved")
			continue
		steps = sorted({step for name, step in runs if name == workflow_name and step and step != "unknown step"})
		prior_recurred = 0
		already = False
		for comment in ((node.get("comments") or {}).get("nodes") or []):
			if not isinstance(comment, dict) or not isinstance(login_of(comment), str) or login_of(comment).casefold() != viewer_key:
				continue
			text = (comment.get("body") or "").rstrip()
			last = text.split("\n")[-1].strip() if text else ""
			marker = VERIFIED_RE.fullmatch(last)
			created = iso(comment.get("createdAt"))
			if not marker or created is None:
				continue
			if created >= merged_at:
				already = True
			elif marker.group(1) == "recurred":
				prior_recurred += 1
		if already:
			skip("already_verified")
			continue
		lineage = None
		for open_number, open_root, open_created in trusted_open:
			if open_root == root and open_number != number and open_created > merged_at:
				lineage = {"number": open_number, "url": f"https://github.com/{SELF_REPO}/issues/{open_number}"}
				break
		candidates.append({
			"number": number,
			"url": f"https://github.com/{SELF_REPO}/issues/{number}",
			"fp": fp,
			"root": root,
			"merge_sha": pr_node["mergeCommit"]["oid"],
			"merged_at": pr_node["mergedAt"],
			"pr_number": pr_node["number"],
			"branch": branch,
			"workflow_file": matches[0],
			"workflow_name": workflow_name,
			"steps": steps,
			"prior_recurred": prior_recurred,
			"lineage": lineage,
			"closed_at": node["closedAt"],
		})
	candidates.sort(key=lambda item: (item["closed_at"], item["number"]))
	with open(out_file, "w", encoding="utf-8") as handle:
		json.dump(candidates, handle)
	return 0


def cmd_match_step(selected_file, candidate_file):
	with open(selected_file, encoding="utf-8") as handle:
		selected = json.load(handle)
	with open(candidate_file, encoding="utf-8") as handle:
		candidate = json.load(handle)
	failing = []
	for job in selected if isinstance(selected, list) else []:
		steps = job.get("steps") if isinstance(job, dict) else None
		for step in steps if isinstance(steps, list) else []:
			if isinstance(step, dict) and step.get("conclusion") in STEP_CONCLUSIONS and step.get("name"):
				failing.append(str(step["name"]))
				break
	wanted = {norm(step).casefold() for step in candidate.get("steps") or []}
	matched = any(norm(step).casefold() in wanted for step in failing)
	print(("match" if matched else "nomatch") + "\t" + code(failing[0] if failing else ""))
	return 0


def cmd_render(kind, candidate_file, run_id, conclusion, step):
	with open(candidate_file, encoding="utf-8") as handle:
		candidate = json.load(handle)
	if not re.fullmatch(r"[0-9]{1,20}", run_id):
		return 2
	run_url = f"https://github.com/{SELF_REPO}/actions/runs/{run_id}"
	branch = code(candidate["branch"])
	sha12 = candidate["merge_sha"][:12]
	lines = []
	if kind == "held":
		lines.append(f"Post-merge verification: run [#{run_id}]({run_url}) on `{branch}` at or after `{sha12}` passed.")
	elif kind == "inconclusive":
		lines.append(f"Post-merge verification: run [#{run_id}]({run_url}) on `{branch}` failed at a different step (`{code(step)}`); not counted as a recurrence.")
	elif kind == "recurred_lineage":
		lineage = candidate.get("lineage") or {}
		lines.append(f"Post-merge verification: the failure recurred in run [#{run_id}]({run_url}) on `{branch}`; the intake already filed #{int(lineage.get('number') or 0)} in the same lineage (root `{code(candidate['root'])[:12]}`).")
	elif kind == "recurred":
		lines.extend([
			"## Fix did not hold",
			"",
			f"Post-merge verification: run [#{run_id}]({run_url}) on `{branch}` failed again after the fix merged.",
			"",
			f"- Signature: workflow `{code(candidate['workflow_name'])}`, step `{code(step)}`, conclusion `{code(conclusion, 20)}`, fp `{code(candidate['fp'])[:12]}`",
			f"- Merge commit: `{candidate['merge_sha']}` (pull request #{int(candidate['pr_number'])})",
		])
	else:
		return 2
	lines.extend(["", f"<!-- workflow-failure-heal:verified:v1 outcome={kind} run={run_id} -->"])
	sys.stdout.write("\n".join(lines) + "\n")
	return 0


command, rest = sys.argv[1], sys.argv[2:]
if command == "select":
	sys.exit(cmd_select(*rest))
if command == "match-step":
	sys.exit(cmd_match_step(*rest))
if command == "render":
	sys.exit(cmd_render(*rest))
sys.exit(2)
PY
}

# --- List (the tick's one GraphQL call) ----------------------------------------

LIST_FILE="${RUNTIME_DIR}/verify_list.json"
CANDIDATES_FILE="${RUNTIME_DIR}/verify_candidates.json"
SINCE="$(python3 -c 'import sys; from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) - timedelta(days=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%SZ"))' "${VERIFY_WINDOW_DAYS}")"
# shellcheck disable=SC2016  # GraphQL variables, not shell expansions.
LIST_QUERY='query($owner:String!,$name:String!,$since:DateTime!){viewer{login} repository(owner:$owner,name:$name){closed: issues(first:50,states:CLOSED,labels:["ai:workflow-heal"],filterBy:{since:$since},orderBy:{field:UPDATED_AT,direction:DESC}){pageInfo{hasNextPage} nodes{number url body createdAt closedAt stateReason author{login} closedByPullRequestsReferences(first:5,includeClosedPrs:true){nodes{number merged mergedAt baseRefName mergeCommit{oid} repository{nameWithOwner}}} comments(last:30){nodes{body createdAt author{login}}}}} open: issues(first:50,states:OPEN,labels:["ai:workflow-heal"],orderBy:{field:CREATED_AT,direction:DESC}){nodes{number url body createdAt author{login}}}}}'
if ! gh_retry gh api graphql -f query="${LIST_QUERY}" -f owner="${SELF_REPO%%/*}" -f name="${SELF_REPO#*/}" -f since="${SINCE}" > "${LIST_FILE}" 2>/dev/null \
	|| ! jq -e '.data.repository.closed.nodes | type == "array"' "${LIST_FILE}" >/dev/null 2>&1; then
	log "outcome=error reason=list_failed"
	exit 0
fi
if ! _verify_py select "${LIST_FILE}" "${CANDIDATES_FILE}" || ! jq -e 'type == "array"' "${CANDIDATES_FILE}" >/dev/null 2>&1; then
	log "outcome=error reason=list_failed"
	exit 0
fi

# --- Per-heal verification -------------------------------------------------------

TOTAL_READS=0
TOTAL_WRITES=0
EXAMINED=0
CANDIDATE_COUNT="$(jq 'length' "${CANDIDATES_FILE}")"

_post_comment()
{
	local number="$1" body_file="$2"
	TOTAL_WRITES=$((TOTAL_WRITES + 1))
	gh_retry gh api --method POST "repos/${SELF_REPO}/issues/${number}/comments" -F body=@"${body_file}" >/dev/null 2>&1
}

_verify_one()
{
	local cand_file="$1"
	local number merge_sha merged_at branch file lineage_number prior_recurred
	number="$(jq -r '.number' "${cand_file}")"
	merge_sha="$(jq -r '.merge_sha' "${cand_file}")"
	merged_at="$(jq -r '.merged_at' "${cand_file}")"
	branch="$(jq -r '.branch' "${cand_file}")"
	file="$(jq -r '.workflow_file' "${cand_file}")"
	lineage_number="$(jq -r '.lineage.number // ""' "${cand_file}")"
	prior_recurred="$(jq -r '.prior_recurred // 0' "${cand_file}")"
	if ! [[ "${number}" =~ ^[0-9]{1,20}$ && "${merge_sha}" =~ ^[0-9a-f]{40}$ && "${prior_recurred}" =~ ^[0-9]+$ ]] \
		|| ! [[ "${merged_at}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
		|| ! [[ "${file}" =~ ^[a-z0-9-]+\.yml$ ]]; then
		log "outcome=error issue=${number//[^0-9]/} reason=candidate_invalid"
		return 0
	fi

	local reads=0 compares=0
	local runs_file="${RUNTIME_DIR}/runs_${number}.json"
	local branch_enc created_enc
	branch_enc="$(jq -rn --arg value "${branch}" '$value | @uri')"
	created_enc="$(jq -rn --arg value ">=${merged_at}" '$value | @uri')"
	reads=$((reads + 1))
	TOTAL_READS=$((TOTAL_READS + 1))
	if ! gh_retry gh api --method GET "repos/${SELF_REPO}/actions/workflows/${file}/runs?branch=${branch_enc}&created=${created_enc}&per_page=100" > "${runs_file}" 2>/dev/null \
		|| ! jq -e '.workflow_runs | type == "array"' "${runs_file}" >/dev/null 2>&1; then
		log "outcome=error issue=${number} reason=runs_failed"
		return 0
	fi

	local picked_id="" picked_conclusion="" stopped=false
	local run_id head_sha conclusion need status
	while IFS=$'\t' read -r run_id head_sha conclusion; do
		[[ "${run_id}" =~ ^[0-9]{1,20}$ ]] || continue
		case "${conclusion}" in
			success|failure|timed_out) ;;
			*) continue ;;
		esac
		if [ "${head_sha}" != "${merge_sha}" ]; then
			need=1
			[ "${conclusion}" = "success" ] || need=2
			if [ "${compares}" -ge "${MAX_COMPARES_PER_HEAL}" ] || [ $((reads + need)) -gt "${MAX_READS_PER_HEAL}" ]; then
				log "outcome=pending issue=${number} reason=read_budget"
				stopped=true
				break
			fi
			reads=$((reads + 1))
			compares=$((compares + 1))
			TOTAL_READS=$((TOTAL_READS + 1))
			if ! status="$(gh_retry gh api --method GET "repos/${SELF_REPO}/compare/${merge_sha}...${head_sha}" --jq '.status // ""' 2>/dev/null)"; then
				log "outcome=error issue=${number} reason=compare_failed"
				stopped=true
				break
			fi
			case "${status}" in
				ahead|identical) ;;
				behind|diverged)
					log "skip_run issue=${number} run=${run_id} reason=unrelated_commit"
					continue
					;;
				*)
					log "outcome=error issue=${number} reason=compare_failed"
					stopped=true
					break
					;;
			esac
		fi
		picked_id="${run_id}"
		picked_conclusion="${conclusion}"
		break
	done < <(jq -r --arg branch "${branch}" --arg repo "${SELF_REPO}" --arg merged_at "${merged_at}" '[.workflow_runs[]?
		| select(type == "object" and .status == "completed"
			and (.id | type == "number") and .id > 0
			and .head_branch == $branch
			and ((.head_repository.full_name // "") == $repo)
			and (((.event // "") | IN("pull_request", "pull_request_target")) | not)
			and ((.head_sha // "") | type == "string" and test("^[0-9a-f]{40}$"))
			and ((.created_at // "") | type == "string" and . >= $merged_at))]
		| sort_by([.created_at, .id]) | .[]
		| [(.id | tostring), .head_sha, (.conclusion // "")] | @tsv' "${runs_file}" 2>/dev/null)

	[ "${stopped}" = "false" ] || return 0
	if [ -z "${picked_id}" ]; then
		log "outcome=pending issue=${number} reason=no_completed_run"
		return 0
	fi

	local comment_file="${RUNTIME_DIR}/comment_${number}.md"
	if [ "${picked_conclusion}" = "success" ]; then
		if ! _verify_py render held "${cand_file}" "${picked_id}" success "" > "${comment_file}"; then
			log "outcome=error issue=${number} reason=render_failed"
			return 0
		fi
		if _post_comment "${number}" "${comment_file}"; then
			log "outcome=held issue=${number} run=${picked_id}"
		else
			log "outcome=error issue=${number} reason=comment_failed"
		fi
		return 0
	fi

	local jobs_file="${RUNTIME_DIR}/jobs_${picked_id}.json" selected_file="${RUNTIME_DIR}/jobs_${picked_id}_selected.json"
	reads=$((reads + 1))
	TOTAL_READS=$((TOTAL_READS + 1))
	if ! gh_retry gh api --method GET "repos/${SELF_REPO}/actions/runs/${picked_id}/jobs?filter=latest&per_page=100" > "${jobs_file}" 2>/dev/null \
		|| ! jq -e '.jobs | type == "array"' "${jobs_file}" >/dev/null 2>&1 \
		|| ! python3 "${HEAL_PY}" select-evidence-jobs --jobs-json "${jobs_file}" --kind workflow_run --limit 3 > "${selected_file}" 2>/dev/null; then
		log "outcome=error issue=${number} reason=jobs_failed"
		return 0
	fi
	local match_line verdict step
	if ! match_line="$(_verify_py match-step "${selected_file}" "${cand_file}")"; then
		log "outcome=error issue=${number} reason=jobs_failed"
		return 0
	fi
	verdict="${match_line%%$'\t'*}"
	step="${match_line#*$'\t'}"

	if [ "${verdict}" != "match" ]; then
		if _verify_py render inconclusive "${cand_file}" "${picked_id}" "${picked_conclusion}" "${step}" > "${comment_file}" \
			&& _post_comment "${number}" "${comment_file}"; then
			log "outcome=inconclusive issue=${number} run=${picked_id} reason=different_step"
		else
			log "outcome=error issue=${number} reason=comment_failed"
		fi
		return 0
	fi

	if [ -n "${lineage_number}" ]; then
		if _verify_py render recurred_lineage "${cand_file}" "${picked_id}" "${picked_conclusion}" "${step}" > "${comment_file}" \
			&& _post_comment "${number}" "${comment_file}"; then
			log "outcome=recurred_lineage issue=${number} lineage=${lineage_number//[^0-9]/} run=${picked_id}"
		else
			log "outcome=error issue=${number} reason=comment_failed"
		fi
		return 0
	fi

	if ! _verify_py render recurred "${cand_file}" "${picked_id}" "${picked_conclusion}" "${step}" > "${comment_file}"; then
		log "outcome=error issue=${number} reason=render_failed"
		return 0
	fi
	TOTAL_WRITES=$((TOTAL_WRITES + 1))
	if ! gh_retry gh api --method PATCH "repos/${SELF_REPO}/issues/${number}" -f state=open >/dev/null 2>&1; then
		log "outcome=error issue=${number} reason=reopen_failed"
		return 0
	fi
	local run_url="https://github.com/${SELF_REPO}/actions/runs/${picked_id}"
	local heal_url="https://github.com/${SELF_REPO}/issues/${number}"
	if ! _post_comment "${number}" "${comment_file}"; then
		log "warn issue=${number} reason=comment_failed"
		tg_send_msg "Workflow heal #${number} was reopened by post-merge verification, but its comment could not be posted."$'\n'"Run: ${run_url}"$'\n'"Heal: ${heal_url}" "WARNING" >/dev/null 2>&1 || true
	fi
	local escalated=false
	if [ "${prior_recurred}" -ge 1 ]; then
		TOTAL_WRITES=$((TOTAL_WRITES + 1))
		if gh_retry gh api --method POST "repos/${SELF_REPO}/issues/${number}/labels" -f 'labels[]=ai:workflow-heal-escalated' >/dev/null 2>&1; then
			escalated=true
		else
			log "warn issue=${number} reason=label_failed"
		fi
		tg_send_msg "Workflow heal #${number}: the fix did not hold for the second time."$'\n'"Run: ${run_url}"$'\n'"Heal: ${heal_url}" "CRITICAL" >/dev/null 2>&1 || true
		log "alert=critical issue=${number} escalated=${escalated}"
	fi
	log "outcome=recurred issue=${number} run=${picked_id}"
	return 0
}

index=0
while [ "${index}" -lt "${CANDIDATE_COUNT}" ] && [ "${EXAMINED}" -lt "${MAX_PER_TICK}" ]; do
	CAND_FILE="${RUNTIME_DIR}/candidate_${index}.json"
	jq ".[${index}]" "${CANDIDATES_FILE}" > "${CAND_FILE}"
	index=$((index + 1))
	EXAMINED=$((EXAMINED + 1))
	_verify_one "${CAND_FILE}" || true
done
if [ "${CANDIDATE_COUNT}" -gt "${MAX_PER_TICK}" ]; then
	log "warn reason=tick_cap candidates=${CANDIDATE_COUNT} max_per_tick=${MAX_PER_TICK}"
fi
log "outcome=tick candidates=${CANDIDATE_COUNT} examined=${EXAMINED} reads=${TOTAL_READS} writes=${TOTAL_WRITES}"
exit 0
