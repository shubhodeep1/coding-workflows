#!/usr/bin/env bash
# plan_scope_block_route.sh -- route a planning block caused only by missing
# scope into the unblock judge's override_guard flow (issue #9534).
#
# Run by the "Route planning scope block" step of .github/workflows/plan.yml,
# never by hand. When the planner answered `BLOCKED:` followed by one
# `SCOPE_MISSING: <repo-relative path>` line per file outside the issue's
# files_touched list, this script validates those paths and prints the trusted
# guard-rejection marker the blocked comment ends with:
#
#   <!-- ai:guard-rejection:v1 item=<n> guard=scope reason=out-of-scope run=<run> count=<n> truncated=false paths=<b64> -->
#
# built like guard_rejection_marker() in scripts/implement_handle_guard_block.sh
# (base64 of a JSON array). scripts/unblock_ledger.py (REJECTION_RE) reads it;
# the paths are only a proposal, override_guard validation decides the grant.
#
# Inputs (env, each with a default):
#   CODEX_OUTPUT_PARSE_FILE   fence-stripped planner output (parse_plan step)
#   ISSUE_NUMBER              the issue being planned
#   ISSUE_META_FILE           issue JSON this run already fetched (no API call)
#   GITHUB_RUN_ID             the plan run id (marker `run=`)
#   GITHUB_OUTPUT             step output file (default /dev/null)
#   WAITING_PARSED            parse_plan's `waiting` output (default false)
#   PLAN_SCOPE_BLOCK_ROUTING_ENABLED  `false` disables routing (default true)
#   SUPPORT_SCRIPTS_DIR       where files_touched_scope_guard.py lives
#                             (default: this script's directory)
#
# Outputs ($GITHUB_OUTPUT): scope_routed=true|false, scope_marker=<line or
# empty>, scope_paths_count=<n>.
# Log: PLAN_SCOPE_BLOCK outcome=routed|not_routed issue=<n> paths=<n> [reason=...]
# Makes no GitHub API calls and always exits 0 (fail open: not routed).
set -euo pipefail

PSBR_MAX_PATHS=20
psbr_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || psbr_self_dir="."
psbr_output="${GITHUB_OUTPUT:-/dev/null}"
psbr_issue="${ISSUE_NUMBER:-}"
psbr_run="${GITHUB_RUN_ID:-}"
psbr_parse_file="${CODEX_OUTPUT_PARSE_FILE:-}"
psbr_meta_file="${ISSUE_META_FILE:-}"
psbr_support_dir="${SUPPORT_SCRIPTS_DIR:-${psbr_self_dir}}"

psbr_log_issue()
{
	if [[ "${psbr_issue}" =~ ^[1-9][0-9]*$ ]]; then
		printf '%s' "${psbr_issue}"
	else
		printf '%s' "-"
	fi
}

# psbr_emit <routed> <count> <marker> [reason]
psbr_emit()
{
	local routed="$1" count="$2" marker="$3" reason="${4:-}"
	{
		printf 'scope_routed=%s\n' "${routed}"
		printf 'scope_marker=%s\n' "${marker}"
		printf 'scope_paths_count=%s\n' "${count}"
	} >> "${psbr_output}" 2>/dev/null || true
	if [ "${routed}" = "true" ]; then
		echo "PLAN_SCOPE_BLOCK outcome=routed issue=$(psbr_log_issue) paths=${count}"
	else
		echo "PLAN_SCOPE_BLOCK outcome=not_routed issue=$(psbr_log_issue) paths=${count} reason=${reason}"
	fi
}

psbr_guard_dir()
{
	if [ -f "${psbr_support_dir}/files_touched_scope_guard.py" ] && [ ! -L "${psbr_support_dir}/files_touched_scope_guard.py" ]; then
		printf '%s' "${psbr_support_dir}"
		return 0
	fi
	if [ -f "${psbr_self_dir}/files_touched_scope_guard.py" ] && [ ! -L "${psbr_self_dir}/files_touched_scope_guard.py" ]; then
		printf '%s' "${psbr_self_dir}"
		return 0
	fi
	return 1
}

# Prints `ok <path>...` (one path per line after the first) or `reason <name>`.
psbr_select_paths()
{
	local guard_dir="$1"
	python3 -I -B - "${guard_dir}" "${psbr_parse_file}" "${psbr_meta_file}" "${PSBR_MAX_PATHS}" <<'PY'
import json
import re
import sys

guard_dir, parse_file, meta_file, max_paths = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, guard_dir)
from files_touched_scope_guard import entry_matches, extract_files_touched, normalize_allowlist

SCOPE_LINE_RE = re.compile(r"^\s*SCOPE_MISSING:")
SCOPE_PATH_RE = re.compile(r"^\s*SCOPE_MISSING:\s*(\S+)\s*$")
# Same closed-fence strip as the parse_plan step, so a line inside a fenced
# block never counts even when the raw output is passed.
FENCE_RE = re.compile(r"(^|\n)([ \t]*((?:```|~~~))[^\n]*\n.*?\n[ \t]*\3[ \t]*(?=\n|$))", re.S | re.M)


def reason(name):
	print(f"reason {name}")
	raise SystemExit(0)


def clean(raw):
	# The rules of scripts/unblock_ledger.py _clean_path, plus no trailing `/`:
	# an exact repository-relative file path, never a directory or a glob.
	path = raw.strip()
	while path.startswith("./"):
		path = path[2:]
	if (
		not path
		or len(path) > 300
		or path.startswith("/")
		or path.endswith("/")
		or "\\" in path
		or any(part in ("", ".", "..") for part in path.split("/"))
		or any(ch in path for ch in "*?[]{}")
		or any(ord(ch) <= 0x20 or ord(ch) > 0x7E for ch in path)
	):
		return None
	return path


with open(parse_file, encoding="utf-8", errors="replace") as handle:
	text = FENCE_RE.sub(r"\1", handle.read())
raw_paths = []
malformed = False
for line in text.splitlines():
	if not SCOPE_LINE_RE.match(line):
		continue
	match = SCOPE_PATH_RE.match(line)
	if not match:
		malformed = True
		continue
	raw_paths.append(match.group(1))
if not raw_paths and not malformed:
	reason("no_scope_lines")
paths = []
for raw in raw_paths:
	if raw not in paths:
		paths.append(raw)
if len(paths) > max_paths:
	reason("too_many_paths")
cleaned = []
for raw in paths:
	path = clean(raw)
	if path is None:
		reason("invalid_path")
	if path not in cleaned:
		cleaned.append(path)
if malformed or not cleaned:
	reason("invalid_path")
with open(meta_file, encoding="utf-8") as handle:
	meta = json.load(handle)
body = meta.get("body") if isinstance(meta, dict) else None
entries = extract_files_touched(body if isinstance(body, str) else "")
if entries is None:
	reason("no_scope_block")
allowlist = normalize_allowlist(entries)
if any(entry_matches(entry, path) for path in cleaned for entry in allowlist):
	reason("already_in_scope")
print("ok")
for path in cleaned:
	print(path)
PY
}

psbr_main()
{
	local flag="" guard_dir="" selection="" first="" paths_lines="" count=0 paths_json="" paths_b64="" marker=""
	local marker_re='^<!-- ai:guard-rejection:v1 item=[1-9][0-9]* guard=scope reason=out-of-scope run=[0-9]+ count=[1-9][0-9]* truncated=false paths=[A-Za-z0-9+/=]+ -->$'

	flag="$(printf '%s' "${PLAN_SCOPE_BLOCK_ROUTING_ENABLED:-true}" | tr '[:upper:]' '[:lower:]' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')" || flag="true"
	if [ "${flag}" = "false" ]; then
		psbr_emit false 0 "" disabled
		return 0
	fi
	if [ "${WAITING_PARSED:-false}" = "true" ]; then
		psbr_emit false 0 "" waiting
		return 0
	fi
	if ! [[ "${psbr_issue}" =~ ^[1-9][0-9]*$ && "${psbr_run}" =~ ^[0-9]+$ ]] \
		|| [ -z "${psbr_meta_file}" ] || [ ! -s "${psbr_meta_file}" ] \
		|| ! jq -e --arg n "${psbr_issue}" '(.number | tostring) == $n and (has("pull_request") | not)' "${psbr_meta_file}" >/dev/null 2>&1; then
		psbr_emit false 0 "" not_an_issue
		return 0
	fi
	if [ -z "${psbr_parse_file}" ] || [ ! -f "${psbr_parse_file}" ]; then
		psbr_emit false 0 "" no_scope_lines
		return 0
	fi
	guard_dir="$(psbr_guard_dir)" || return 1
	selection="$(psbr_select_paths "${guard_dir}")" || return 1
	first="$(printf '%s\n' "${selection}" | head -n 1)"
	case "${first}" in
		"reason "*)
			psbr_emit false 0 "" "$(printf '%s' "${first#reason }" | tr -cd 'a-z_')"
			return 0
			;;
		ok) ;;
		*) return 1 ;;
	esac
	paths_lines="$(printf '%s\n' "${selection}" | tail -n +2)"
	paths_json="$(printf '%s' "${paths_lines}" | jq -R -s -c 'split("\n") | map(select(length > 0))')" || return 1
	count="$(jq 'length' <<< "${paths_json}")" || return 1
	if ! [[ "${count}" =~ ^[1-9][0-9]*$ ]] || [ "${count}" -gt "${PSBR_MAX_PATHS}" ]; then
		return 1
	fi
	paths_b64="$(jq -c '.[:100]' <<< "${paths_json}" | base64 | tr -d '\n')" || return 1
	[ -n "${paths_b64}" ] || return 1
	marker="$(printf '<!-- ai:guard-rejection:v1 item=%s guard=scope reason=out-of-scope run=%s count=%s truncated=false paths=%s -->' \
		"${psbr_issue}" "${psbr_run}" "${count}" "${paths_b64}")"
	[[ "${marker}" =~ ${marker_re} ]] || return 1
	psbr_emit true "${count}" "${marker}"
	return 0
}

if ! psbr_main; then
	psbr_emit false 0 "" helper_failed
fi
exit 0
