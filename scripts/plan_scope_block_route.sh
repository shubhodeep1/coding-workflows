#!/usr/bin/env bash
# Route a scope-only planning block to the unblock judge (issue #9534).
#
# The planner ends a block caused only by missing scope with its usual
# `BLOCKED: <reason>` line plus one `SCOPE_MISSING: <repo-relative path>` line
# per file it needs outside the issue's `files_touched:` list. This helper turns
# those lines into the trusted guard-rejection marker the unblock judge already
# reads (scripts/unblock_ledger.py REJECTION_RE, guard=scope reason=out-of-scope),
# so its existing `override_guard` verdict can widen the scope. The paths are
# only proposals: the judge's `validate` still refuses .github/, .claude/,
# workflow-templates/ and (in shubhodeep1/coding-workflows) scripts/, and caps
# a grant at MAX_OVERRIDE_PATHS paths.
#
# Modes (run only as steps of .github/workflows/plan.yml; never by hand):
#
#   route (default)
#     Env: CODEX_OUTPUT_PARSE_FILE (the fence-stripped planner output),
#     ISSUE_NUMBER, ISSUE_META_FILE (issue JSON already fetched), GITHUB_RUN_ID,
#     GITHUB_OUTPUT, PLAN_SCOPE_BLOCK_ROUTING_ENABLED (default true).
#     Writes `scope_routed=true|false` and `scope_marker=<marker or empty>` to
#     $GITHUB_OUTPUT and logs one line:
#       PLAN_SCOPE_BLOCK outcome=routed|not_routed issue=<n> paths=<n> [reason=...]
#     Reasons: disabled, invalid_input, not_an_issue, no_scope_lines,
#     invalid_path, too_many_paths, no_files_touched, already_in_scope,
#     helper_failed.
#
#   answer-release
#     Env: ISSUE_NUMBER, ISSUE_META_FILE, ISSUE_COMMENTS_FILE (both already
#     fetched). Prints `release=true|false` on stdout and logs
#       PLAN_SCOPE_BLOCK outcome=released|kept issue=<n> reason=<...>
#     on stderr. `true` only when the issue carries ai:blocked and
#     ai:scope-blocked and the newest trusted guard rejection on it is a
#     planning one, so a human /answer never clears an implement-time latch.
#     Not gated by PLAN_SCOPE_BLOCK_ROUTING_ENABLED, so markers posted before
#     a kill-switch flip can still be released. Fails closed (`false`).
#
# Makes no GitHub API calls. Always exits 0.

set -uo pipefail

PLAN_SCOPE_BLOCK_MODE="${1:-route}"
PLAN_SCOPE_BLOCK_ROUTING_ENABLED="${PLAN_SCOPE_BLOCK_ROUTING_ENABLED:-true}"
PSBR_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
export PSBR_SCRIPT_DIR

psbr_issue_label() {
	local value="${ISSUE_NUMBER:-}"
	if [[ "${value}" =~ ^[1-9][0-9]*$ ]]; then
		printf '%s' "${value}"
	else
		printf 'invalid'
	fi
}

psbr_write_route_output() {
	local routed="$1" marker="$2"
	if [ -n "${GITHUB_OUTPUT:-}" ]; then
		{
			printf 'scope_routed=%s\n' "${routed}"
			printf 'scope_marker=%s\n' "${marker}"
		} >> "${GITHUB_OUTPUT}" 2>/dev/null || true
	fi
}

psbr_route() {
	local enabled result routed reason count marker
	enabled="$(printf '%s' "${PLAN_SCOPE_BLOCK_ROUTING_ENABLED}" | tr '[:upper:]' '[:lower:]')"
	if [ "${enabled}" = "false" ]; then
		psbr_write_route_output false ""
		echo "PLAN_SCOPE_BLOCK outcome=not_routed issue=$(psbr_issue_label) paths=0 reason=disabled"
		return 0
	fi
	result="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B - <<'PY' 2>/dev/null
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("PSBR_SCRIPT_DIR", ""))
from unblock_ledger import MAX_OVERRIDE_PATHS, REJECTION_RE, UsageError, _clean_path
from files_touched_scope_guard import entry_matches, extract_files_touched, normalize_allowlist

SCOPE_LINE_RE = re.compile(r"^\s*SCOPE_MISSING:\s*(.+?)\s*$")
GLOB_CHARS = set("*?[]{}")


def emit(routed, reason, count, marker=""):
	print(f"routed={'true' if routed else 'false'}")
	print(f"reason={reason}")
	print(f"count={count}")
	print(f"marker={marker}")
	sys.exit(0)


issue = os.environ.get("ISSUE_NUMBER", "")
run_id = os.environ.get("GITHUB_RUN_ID", "")
if not re.fullmatch(r"[1-9][0-9]*", issue) or not re.fullmatch(r"[0-9]+", run_id):
	emit(False, "invalid_input", 0)

try:
	with open(os.environ.get("ISSUE_META_FILE", ""), encoding="utf-8") as handle:
		meta = json.load(handle)
except (OSError, ValueError):
	emit(False, "not_an_issue", 0)
if not isinstance(meta, dict) or "pull_request" in meta:
	emit(False, "not_an_issue", 0)

try:
	with open(os.environ.get("CODEX_OUTPUT_PARSE_FILE", ""), encoding="utf-8") as handle:
		parse_text = handle.read()
except (OSError, UnicodeDecodeError):
	emit(False, "invalid_input", 0)

raw_paths = []
for line in parse_text.splitlines():
	match = SCOPE_LINE_RE.match(line)
	if not match:
		continue
	value = match.group(1)
	if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
		value = value[1:-1]
	raw_paths.append(value)
if not raw_paths:
	emit(False, "no_scope_lines", 0)

paths = []
for value in raw_paths:
	if not value or any(ch.isspace() for ch in value):
		emit(False, "invalid_path", len(raw_paths))
	try:
		cleaned = _clean_path(value)
	except UsageError:
		emit(False, "invalid_path", len(raw_paths))
	if any(ch in GLOB_CHARS for ch in cleaned) or cleaned.endswith("/"):
		emit(False, "invalid_path", len(raw_paths))
	if cleaned not in paths:
		paths.append(cleaned)
if len(paths) > MAX_OVERRIDE_PATHS:
	emit(False, "too_many_paths", len(paths))

entries = extract_files_touched(str(meta.get("body") or ""))
if entries is None:
	emit(False, "no_files_touched", len(paths))
allowlist = normalize_allowlist(entries)
for path in paths:
	if any(entry_matches(entry, path) for entry in allowlist):
		emit(False, "already_in_scope", len(paths))

encoded = base64.b64encode(json.dumps(paths, separators=(",", ":")).encode("utf-8")).decode("ascii")
marker = (
	f"<!-- ai:guard-rejection:v1 item={issue} guard=scope reason=out-of-scope run={run_id} "
	f"count={len(paths)} truncated=false paths={encoded} -->"
)
match = REJECTION_RE.fullmatch(marker)
if not match or json.loads(base64.b64decode(match.group("paths"))) != paths:
	emit(False, "helper_failed", len(paths))
emit(True, "", len(paths), marker)
PY
)" || result=""
	routed="$(printf '%s\n' "${result}" | sed -n 's/^routed=//p' | head -n1)"
	reason="$(printf '%s\n' "${result}" | sed -n 's/^reason=//p' | head -n1 | tr -cd 'a-z_')"
	count="$(printf '%s\n' "${result}" | sed -n 's/^count=//p' | head -n1 | tr -cd '0-9')"
	marker="$(printf '%s\n' "${result}" | sed -n 's/^marker=//p' | head -n1)"
	if [ "${routed}" = "true" ] && [ -n "${marker}" ]; then
		psbr_write_route_output true "${marker}"
		echo "PLAN_SCOPE_BLOCK outcome=routed issue=$(psbr_issue_label) paths=${count:-0}"
		return 0
	fi
	if [ "${routed}" != "false" ] || [ -z "${reason}" ]; then
		reason="helper_failed"
	fi
	psbr_write_route_output false ""
	echo "PLAN_SCOPE_BLOCK outcome=not_routed issue=$(psbr_issue_label) paths=${count:-0} reason=${reason}"
	return 0
}

psbr_answer_release() {
	local result release reason
	result="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B - <<'PY' 2>/dev/null
import json
import os
import re
import sys

sys.path.insert(0, os.environ.get("PSBR_SCRIPT_DIR", ""))
from unblock_ledger import GUARD_REJECTION_HEADERS, REJECTION_RE

PLANNING_HEADER = "🚨 **files_touched scope guard rejected this planning run.**"
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def emit(release, reason):
	print(f"release={'true' if release else 'false'}")
	print(f"reason={reason}")
	sys.exit(0)


def load_comments(path):
	"""A JSON array, or the concatenated page arrays `gh api --paginate` writes."""
	with open(path, encoding="utf-8") as handle:
		text = handle.read()
	decoder = json.JSONDecoder()
	comments = []
	index = 0
	while True:
		while index < len(text) and text[index].isspace():
			index += 1
		if index >= len(text):
			break
		page, index = decoder.raw_decode(text, index)
		if not isinstance(page, list):
			raise ValueError("comments page is not an array")
		comments.extend(page)
	return comments


issue = os.environ.get("ISSUE_NUMBER", "")
if not re.fullmatch(r"[1-9][0-9]*", issue):
	emit(False, "invalid_input")
try:
	with open(os.environ.get("ISSUE_META_FILE", ""), encoding="utf-8") as handle:
		meta = json.load(handle)
	comments = load_comments(os.environ.get("ISSUE_COMMENTS_FILE", ""))
except (OSError, ValueError, UnicodeDecodeError):
	emit(False, "unreadable")
if not isinstance(meta, dict):
	emit(False, "unreadable")
labels = {label.get("name") for label in meta.get("labels") or [] if isinstance(label, dict)}
if "ai:blocked" not in labels or "ai:scope-blocked" not in labels:
	emit(False, "labels_absent")

item_prefix = f"<!-- ai:guard-rejection:v1 item={issue} "
newest = None
for comment in comments:
	if not isinstance(comment, dict):
		continue
	user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
	if comment.get("author_association") not in TRUSTED_ASSOCIATIONS and user.get("type") != "Bot":
		continue
	lines = [line.strip() for line in str(comment.get("body") or "").splitlines() if line.strip()]
	if not lines:
		continue
	if lines[-1].startswith(item_prefix) or lines[0] in GUARD_REJECTION_HEADERS:
		newest = lines
if newest is None:
	emit(False, "no_rejection")
match = REJECTION_RE.fullmatch(newest[-1])
if (
	not match
	or int(match.group("item")) != int(issue)
	or match.group("guard") != "scope"
	or len(newest) < 2
	or newest[-2] != PLANNING_HEADER
):
	emit(False, "newest_not_planning")
emit(True, "planning_rejection")
PY
)" || result=""
	release="$(printf '%s\n' "${result}" | sed -n 's/^release=//p' | head -n1)"
	reason="$(printf '%s\n' "${result}" | sed -n 's/^reason=//p' | head -n1 | tr -cd 'a-z_')"
	if [ "${release}" = "true" ]; then
		echo "PLAN_SCOPE_BLOCK outcome=released issue=$(psbr_issue_label) reason=${reason:-planning_rejection}" >&2
		echo "release=true"
		return 0
	fi
	echo "PLAN_SCOPE_BLOCK outcome=kept issue=$(psbr_issue_label) reason=${reason:-helper_failed}" >&2
	echo "release=false"
	return 0
}

case "${PLAN_SCOPE_BLOCK_MODE}" in
	route) psbr_route ;;
	answer-release) psbr_answer_release ;;
	*)
		echo "PLAN_SCOPE_BLOCK outcome=not_routed issue=$(psbr_issue_label) paths=0 reason=invalid_input" >&2
		;;
esac
exit 0
