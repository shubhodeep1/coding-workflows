#!/usr/bin/env bash
# validate_scheduled_sink.sh — report the result of a scheduled validation run
# (issue #7553).
#
# Runs from the "Report scheduled validation result" step of
# .github/workflows/validate.yml, only when VALIDATE_SCHEDULED_MODE is true (the
# opt-in weekly schedule of workflow-templates/ai-validate.yml validating the
# default branch with tracking_issue=0 and no target_ref). The step runs this
# file from the trusted support checkout (VALIDATE_HELPER_STAGE_DIR), never from
# the checkout being validated.
#
# One issue per repository carries the failure: its body starts with the line
# <!-- ai:scheduled-validation:v1 --> and it has the label
# ai:scheduled-validation-failed, which the clarify predicate excludes, so the
# issue never enters clarify -> plan -> implement. Only an issue the GH_PAT
# login wrote with that exact first line counts; others are ignored.
#   - red run, no open issue:   open one and send one Telegram WARNING;
#   - red run, open issue:      edit its body in place (no comment, no alert);
#   - green run, open issue:    close it first, then post one comment (a retry
#                               after a failed comment never duplicates it);
#   - green run, no open issue: nothing.
# A failed identity or issue-list read writes nothing, so a read failure can
# never open a duplicate issue.
#
# API calls (unattended §14): 1 /user read, 1 paginated read of the open
# ai:scheduled-validation-failed issues (one page below 100), at most 1 label
# create, and at most one of: issue create, body PATCH, close PATCH + comment.
# No earlier call in the validate job reads these issues.
#
# Environment:
#   GH_TOKEN, GITHUB_REPOSITORY   required (SINK_REPOSITORY overrides the repo)
#   SINK_STATUS                   validation status (pass|fail|error); empty is red
#   SINK_RAW_STATUS               raw status (harness_missing, tests_failed, ...)
#   SINK_SUMMARY, SINK_FAILURE_SUMMARY
#   SINK_RESULT_FILE              validation_result.json (optional; failing tests)
#   SINK_RUN_URL                  link to this run
#   SINK_DEFAULT_BRANCH           default branch name
#   SINK_HEAD_SHA                 validated commit (optional)
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID, ALERT_MSG_LEVEL  Telegram (optional)
#
# Logs (stable prefix VALIDATE_SCHEDULED_SINK):
#   VALIDATE_SCHEDULED_SINK outcome=opened|updated|closed|noop issue=<n>
#   VALIDATE_SCHEDULED_SINK outcome=closed issue=<n> comment=failed
#   VALIDATE_SCHEDULED_SINK outcome=skip reason=identity_unavailable|list_failed|repository_invalid
#   VALIDATE_SCHEDULED_SINK outcome=failed reason=create_failed|update_failed|close_failed|body_failed
# Every path exits 0.

set -uo pipefail

SINK_MARKER='<!-- ai:scheduled-validation:v1 -->'
SINK_LABEL='ai:scheduled-validation-failed'
SINK_BODY_MAX_BYTES=60000

sink_log()
{
	echo "VALIDATE_SCHEDULED_SINK $*"
}

sink_warn()
{
	echo "::warning::VALIDATE_SCHEDULED_SINK $*"
}

SINK_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
# shellcheck source=/dev/null
source "${SINK_SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }
# shellcheck source=/dev/null
source "${SINK_SCRIPT_DIR}/tg_helpers.sh" 2>/dev/null || true
type tg_send_msg >/dev/null 2>&1 || tg_send_msg() { :; }
# shellcheck source=/dev/null
source "${SINK_SCRIPT_DIR}/label_helpers.sh" 2>/dev/null || true
type ensure_label_exists >/dev/null 2>&1 || ensure_label_exists() { :; }

sink_repo="${SINK_REPOSITORY:-${GITHUB_REPOSITORY:-}}"
if ! [[ "${sink_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	sink_log "outcome=skip reason=repository_invalid"
	sink_warn "outcome=skip reason=repository_invalid"
	exit 0
fi

sink_status="${SINK_STATUS:-}"
sink_red=true
if [ "${sink_status}" = "pass" ]; then
	sink_red=false
fi
if [ -z "${sink_status}" ]; then
	sink_status="unknown"
fi

sink_work="$(mktemp -d 2>/dev/null || true)"
if [ -z "${sink_work}" ] || [ ! -d "${sink_work}" ]; then
	sink_log "outcome=failed reason=tmpdir_unavailable"
	sink_warn "outcome=failed reason=tmpdir_unavailable"
	exit 0
fi
trap 'rm -rf "${sink_work}"' EXIT

sink_login="$(gh_retry gh api user --jq .login 2>/dev/null || true)"
sink_login="$(printf '%s' "${sink_login}" | tr -d '\r\n')"
if ! [[ "${sink_login}" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ]]; then
	sink_log "outcome=skip reason=identity_unavailable"
	sink_warn "outcome=skip reason=identity_unavailable; the run conclusion still shows the result"
	exit 0
fi

sink_list_url="repos/${sink_repo}/issues?state=open&labels=ai%3Ascheduled-validation-failed&per_page=100"
if [[ "${sink_login}" =~ ^[A-Za-z0-9-]+$ ]]; then
	sink_list_url="${sink_list_url}&creator=${sink_login}"
fi
# gh api --paginate prints one JSON array per page. The issue is selected in
# Python (already a dependency of this script), so jq is not needed.
sink_issue=""
if ! gh_retry gh api --paginate "${sink_list_url}" > "${sink_work}/pages.json" 2>/dev/null \
	|| ! sink_issue="$(SINK_LOGIN="${sink_login}" SINK_MARKER_LINE="${SINK_MARKER}" \
		python3 -I - "${sink_work}/pages.json" 2>/dev/null <<'PY'
import json
import os
import sys

text = open(sys.argv[1], encoding="utf-8").read()
decoder = json.JSONDecoder()
position = 0
issues = []
while True:
	while position < len(text) and text[position].isspace():
		position += 1
	if position >= len(text):
		break
	page, position = decoder.raw_decode(text, position)
	if not isinstance(page, list):
		sys.exit(1)
	issues.extend(page)
numbers = []
for issue in issues:
	if not isinstance(issue, dict) or issue.get("pull_request") is not None:
		continue
	if (issue.get("user") or {}).get("login") != os.environ["SINK_LOGIN"]:
		continue
	body = issue.get("body") or ""
	if body.split("\n", 1)[0].rstrip("\r") != os.environ["SINK_MARKER_LINE"]:
		continue
	number = issue.get("number")
	if isinstance(number, int) and not isinstance(number, bool):
		numbers.append(number)
print(min(numbers) if numbers else "")
PY
	)"; then
	sink_log "outcome=skip reason=list_failed"
	sink_warn "outcome=skip reason=list_failed; no issue is opened so a read failure cannot duplicate it"
	exit 0
fi

if [ "${sink_red}" = false ]; then
	if [ -z "${sink_issue}" ]; then
		sink_log "outcome=noop status=${sink_status}"
		exit 0
	fi
	if ! gh_retry gh api -X PATCH "repos/${sink_repo}/issues/${sink_issue}" \
		-f state=closed -f state_reason=completed >/dev/null 2>&1; then
		sink_log "outcome=failed reason=close_failed issue=${sink_issue}"
		sink_warn "outcome=failed reason=close_failed issue=${sink_issue}"
		exit 0
	fi
	sink_comment="Scheduled validation of the default branch passed again: ${SINK_RUN_URL:-this run}. Closing."
	if gh_retry gh api -X POST "repos/${sink_repo}/issues/${sink_issue}/comments" \
		-f body="${sink_comment}" >/dev/null 2>&1; then
		sink_log "outcome=closed issue=${sink_issue}"
	else
		sink_log "outcome=closed issue=${sink_issue} comment=failed"
		sink_warn "outcome=closed issue=${sink_issue} comment=failed"
	fi
	exit 0
fi

# Red run: compose the body. Test names, summaries and failure text come from
# the consumer's checkout, so they are neutralised: no HTML comment opener (no
# forged marker), no backticks, no table-breaking pipes or newlines in cells.
if ! SINK_MARKER="${SINK_MARKER}" SINK_STATUS_TEXT="${sink_status}" SINK_BODY_MAX_BYTES="${SINK_BODY_MAX_BYTES}" \
	python3 -I - > "${sink_work}/body.md" 2>/dev/null <<'PY'
import json
import os
import stat


def clean(text, limit):
	text = str(text or "")
	text = text.replace("<!--", "&lt;!--").replace("`", "'")
	return text[:limit]


def cell(text, limit=200):
	text = clean(text, limit)
	text = " ".join(text.split())
	return text.replace("|", "\\|") or "-"


env = os.environ
failing = []
path = env.get("SINK_RESULT_FILE", "")
try:
	if path:
		info = os.lstat(path)
		if stat.S_ISREG(info.st_mode) and info.st_size <= 5 * 1024 * 1024:
			with open(path, encoding="utf-8", errors="replace") as handle:
				data = json.load(handle)
			for failure in (data.get("failures") or [])[:20]:
				if isinstance(failure, dict):
					failing.append(cell(failure.get("test"), 200))
except (OSError, ValueError, AttributeError, TypeError):
	failing = []

run_url = env.get("SINK_RUN_URL", "")
lines = [
	env["SINK_MARKER"],
	"## Scheduled validation is failing",
	"",
	"The weekly scheduled validation of the default branch did not pass. "
	"This issue is updated in place on every failing scheduled run and closes "
	"when a later scheduled run passes.",
	"",
	"| Key | Value |",
	"|-----|-------|",
	"| Run | %s |" % cell(run_url, 300),
	"| Status | %s |" % cell(env.get("SINK_STATUS_TEXT")),
	"| Raw status | %s |" % cell(env.get("SINK_RAW_STATUS")),
	"| Default branch | %s |" % cell(env.get("SINK_DEFAULT_BRANCH")),
	"| Commit | %s |" % cell(env.get("SINK_HEAD_SHA")),
	"| Summary | %s |" % cell(env.get("SINK_SUMMARY"), 500),
	"",
	"### Failing tests",
	"",
]
if failing:
	lines.extend("- %s" % name for name in failing)
else:
	lines.append("- (none recorded)")
lines.extend([
	"",
	"### Failure summary",
	"",
	clean(env.get("SINK_FAILURE_SUMMARY"), 2000) or "(none)",
	"",
	"### What to do",
	"",
	"Fix the failure on the default branch, or commit or fix `.ai/validate.yml` "
	"(a scheduled run never calls a model to generate or repair it). "
	"Post `/revalidate` on a tracking issue or dispatch AI Validate by hand for a "
	"run with diagnosis and repair.",
	"",
])
body = "\n".join(lines)
limit = int(env.get("SINK_BODY_MAX_BYTES", "60000"))
encoded = body.encode("utf-8")
if len(encoded) > limit:
	body = encoded[:limit].decode("utf-8", errors="ignore") + "\n\n(truncated)\n"
print(body)
PY
then
	sink_log "outcome=failed reason=body_failed"
	sink_warn "outcome=failed reason=body_failed"
	exit 0
fi
sink_body="$(cat "${sink_work}/body.md")"

if [ -n "${sink_issue}" ]; then
	if gh_retry gh api -X PATCH "repos/${sink_repo}/issues/${sink_issue}" \
		-f body="${sink_body}" >/dev/null 2>&1; then
		sink_log "outcome=updated issue=${sink_issue} status=${sink_status}"
	else
		sink_log "outcome=failed reason=update_failed issue=${sink_issue}"
		sink_warn "outcome=failed reason=update_failed issue=${sink_issue}"
	fi
	exit 0
fi

sink_branch="${SINK_DEFAULT_BRANCH:-}"
if ! [[ "${sink_branch}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
	sink_branch="the default branch"
fi
sink_title="Scheduled validation failing on ${sink_branch}"
ensure_label_exists "${SINK_LABEL}" "${sink_repo}" >/dev/null 2>&1 || true
if sink_number="$(gh_retry gh api -X POST "repos/${sink_repo}/issues" \
	-f title="${sink_title}" -f body="${sink_body}" -f "labels[]=${SINK_LABEL}" \
	--jq .number 2>/dev/null)" && [[ "${sink_number}" =~ ^[0-9]+$ ]]; then
	sink_log "outcome=opened issue=${sink_number} status=${sink_status}"
	tg_send_msg "Scheduled validation of ${sink_repo} (${sink_branch}) is failing (status ${sink_status}). Issue #${sink_number}: ${SINK_RUN_URL:-}" "WARNING" >/dev/null 2>&1 || true
else
	sink_log "outcome=failed reason=create_failed"
	sink_warn "outcome=failed reason=create_failed"
fi
exit 0
