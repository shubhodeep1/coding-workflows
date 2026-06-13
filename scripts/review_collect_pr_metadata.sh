#!/usr/bin/env bash
# review_collect_pr_metadata.sh — collect PR metadata + prompt-context
# artifacts for review_autofix.yml.
#
# Extracted from the workflow's "Collect PR metadata" step so the
# behaviour stays local to one helper while preserving the existing
# artifact/env contract.
#
# Inputs (environment):
#   GITHUB_REPOSITORY / GITHUB_REPOSITORY_OWNER
#   GH_TOKEN
#   PR_NUMBER
#   CLAUDE_BRANCH_REVIEW_MODE
#   REVIEW_BREAK_GLASS_ENABLED
#   HEAD_REF_OVERRIDE_INPUT / HEAD_SHA_OVERRIDE_INPUT / BASE_REF_OVERRIDE_INPUT
#   PR_PAYLOAD_FILE / PR_META_FILE / PR_ISSUE_COMMENTS_FILE
#   PR_REVIEWS_FILE / PR_REVIEW_COMMENTS_FILE
#   LINKED_ISSUE_CONTEXT_FILE / PR_ALL_COMMENTS_CONTEXT_FILE / PR_DIFF_FILE
#   GITHUB_ENV
#
# Outputs:
#   Writes the files above and appends LINKED_ISSUES_JSON, HAS_PR_DIFF,
#   PR_DIFF_SOURCE, PR_DIFF_ATTEMPTED_PATHS, and BASE_BRANCH to GITHUB_ENV.

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/gh_helpers.sh"

TMP_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/review_collect_pr_metadata.XXXXXX")"
cleanup_review_collect_pr_metadata_tmp()
{
	rm -rf "${TMP_RUNTIME_DIR}"
}
trap cleanup_review_collect_pr_metadata_tmp EXIT

REPOSITORY="${GITHUB_REPOSITORY:-}"
if [ -z "${REPOSITORY}" ] || ! [[ "${REPOSITORY}" =~ ^[^/]+/[^/]+$ ]]; then
	echo "::error::review_collect_pr_metadata: GITHUB_REPOSITORY must be set to owner/repo." >&2
	exit 1
fi
REPOSITORY_OWNER="${GITHUB_REPOSITORY_OWNER:-${REPOSITORY%%/*}}"
REPOSITORY_NAME="${REPOSITORY#*/}"

for required_var in \
	PR_PAYLOAD_FILE \
	PR_META_FILE \
	PR_ISSUE_COMMENTS_FILE \
	PR_REVIEWS_FILE \
	PR_REVIEW_COMMENTS_FILE \
	LINKED_ISSUE_CONTEXT_FILE \
	PR_ALL_COMMENTS_CONTEXT_FILE \
	PR_DIFF_FILE \
	GITHUB_ENV
do
	if [ -z "${!required_var:-}" ]; then
		echo "::error::review_collect_pr_metadata: required env ${required_var} is unset." >&2
		exit 1
	fi
done

gh_retry()
{
	local outfile="$1"
	shift
	gh_retry_to_file "${outfile}" gh "$@"
}

# _fetch_linked_issue_bodies_graphql — Batch-fetch linked issue or PR
# title/body fallback context via one GraphQL call.
#
# Input: JSON array of issue numbers, e.g. "[7,12]".
# Output: JSON array of objects shaped like
#   [{"number":7,"title":"...","body":"..."}, ...]
# API calls: exactly one `gh api graphql` call for the provided input
# (the caller preserves the existing 20-issue cap before invoking it).
# Fail-open: echoes [] and returns non-zero when the fetch or JSON
# transform fails so callers can warn and continue without fallback
# linked-issue context.
_fetch_linked_issue_bodies_graphql()
{
	local numbers_json="$1"
	local count
	local alias_count=0
	count="$(printf '%s' "${numbers_json}" | jq 'length' 2>/dev/null || echo 0)"
	if ! [[ "${count}" =~ ^[0-9]+$ ]] || [ "${count}" -eq 0 ]; then
		echo '[]'
		return 0
	fi

	local fragment=""
	local i
	local n
	for ((i=0; i<count; i++)); do
		n="$(printf '%s' "${numbers_json}" | jq -r ".[$i] // empty" 2>/dev/null || true)"
		[[ "${n}" =~ ^[0-9]+$ ]] || continue
		alias_count=$((alias_count + 1))
		fragment+=$'\n'"        i${i}: issueOrPullRequest(number: ${n}) {
          __typename
          ... on Issue {
            number
            title
            body
          }
          ... on PullRequest {
            number
            title
            body
          }
        }"
	done

	if [ -z "${fragment}" ]; then
		echo '[]'
		return 0
	fi

	local query
	query="query {
  repository(owner: \"${REPOSITORY_OWNER}\", name: \"${REPOSITORY_NAME}\") {${fragment}
  }
}"

	local response_file
	if ! response_file="$(mktemp "${TMP_RUNTIME_DIR}/linked_issue_fallback_graphql.XXXXXX" 2>/dev/null)"; then
		echo '[]'
		return 1
	fi
	if ! gh_retry "${response_file}" api graphql -f query="${query}"; then
		rm -f "${response_file}"
		echo '[]'
		return 1
	fi
	local had_graphql_errors=0
	if jq -e '(((.errors? // []) | length) > 0) and ((.data.repository? // null) == null)' "${response_file}" >/dev/null 2>&1; then
		rm -f "${response_file}"
		echo '[]'
		return 1
	elif jq -e '((.errors? // []) | length) > 0' "${response_file}" >/dev/null 2>&1; then
		had_graphql_errors=1
	fi

	local transformed
	transformed="$(jq -c '
		(.data.repository // {})
		| to_entries
		| map(select(.key | test("^i[0-9]+$")))
		| sort_by(.key | ltrimstr("i") | tonumber)
		| map(
			.value
			| select(type == "object" and (.number? != null))
			| {
				number: (.number // 0),
				title: (.title // ""),
				body: (.body // "")
			}
		)
	' "${response_file}" 2>/dev/null)" || {
		rm -f "${response_file}"
		echo '[]'
		return 1
	}
	local hydrated_count
	hydrated_count="$(printf '%s' "${transformed}" | jq 'length' 2>/dev/null || echo 0)"
	rm -f "${response_file}"
	if [ "${had_graphql_errors}" -eq 1 ] || { [[ "${hydrated_count}" =~ ^[0-9]+$ ]] && [ "${hydrated_count}" -lt "${alias_count}" ]; }; then
		echo "::warning::Linked-issue body-text fallback: batched GraphQL issue hydration returned partial data (hydrated ${hydrated_count} of ${alias_count} references); continuing with available context." >&2
	fi

	echo "${transformed}"
}

# No-PR claude-branch-review path: synthesize PR_PAYLOAD_FILE +
# PR_META_FILE from the caller-supplied head/base overrides.
if [ "${CLAUDE_BRANCH_REVIEW_MODE:-}" = "true" ] && [ -z "${PR_NUMBER:-}" ]; then
	HEAD_REF_OVERRIDE="${HEAD_REF_OVERRIDE_INPUT:-}"
	HEAD_SHA_OVERRIDE="${HEAD_SHA_OVERRIDE_INPUT:-}"
	BASE_REF_OVERRIDE="${BASE_REF_OVERRIDE_INPUT:-}"
	if [ -z "${BASE_REF_OVERRIDE}" ]; then
		BASE_REF_OVERRIDE="$(gh api "repos/${REPOSITORY}" --jq '.default_branch' 2>/dev/null || echo 'main')"
	fi
	if [ -z "${HEAD_REF_OVERRIDE}" ] || [ -z "${HEAD_SHA_OVERRIDE}" ]; then
		echo "::error::No-PR claude-branch-review path: head_ref_override and head_sha_override must be supplied by the caller."
		exit 1
	fi
	jq -n \
		--arg head_ref "${HEAD_REF_OVERRIDE}" \
		--arg head_sha "${HEAD_SHA_OVERRIDE}" \
		--arg base_ref "${BASE_REF_OVERRIDE}" \
		--arg head_repo "${REPOSITORY}" \
		'{
			title: "",
			body: "",
			head: {ref: $head_ref, sha: $head_sha, repo: {full_name: $head_repo}},
			base: {ref: $base_ref}
		}' > "${PR_PAYLOAD_FILE}"
	printf '[]\n' > "${PR_ISSUE_COMMENTS_FILE}"
	printf '[]\n' > "${PR_REVIEWS_FILE}"
	printf '[]\n' > "${PR_REVIEW_COMMENTS_FILE}"
	jq -n \
		--arg head_ref "${HEAD_REF_OVERRIDE}" \
		--arg base_ref "${BASE_REF_OVERRIDE}" \
		--arg head_repo "${REPOSITORY}" \
		'{title: "", body: "", baseRefName: $base_ref, headRefName: $head_ref, headRepoFullName: $head_repo}' \
		> "${PR_META_FILE}"
	echo "AUTOFIX_NO_PR_METADATA_SYNTHESIZED head_ref=${HEAD_REF_OVERRIDE} head_sha=${HEAD_SHA_OVERRIDE} base_ref=${BASE_REF_OVERRIDE}"
else
	gh_retry "${PR_PAYLOAD_FILE}" api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"
	issue_comments_raw="${TMP_RUNTIME_DIR}/gh_issue_comments_raw.json"
	reviews_raw="${TMP_RUNTIME_DIR}/gh_reviews_raw.json"
	review_comments_raw="${TMP_RUNTIME_DIR}/gh_review_comments_raw.json"
	gh_retry "${issue_comments_raw}" api --paginate "repos/${REPOSITORY}/issues/${PR_NUMBER}/comments"
	jq -s 'add // []' "${issue_comments_raw}" > "${PR_ISSUE_COMMENTS_FILE}"
	printf '[]\n' > "${PR_REVIEWS_FILE}"
	case "$(printf '%s' "${REVIEW_BREAK_GLASS_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			if gh_retry "${reviews_raw}" api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/reviews"; then
				jq -s 'add // []' "${reviews_raw}" > "${PR_REVIEWS_FILE}"
			else
				echo "::warning::Optional top-level PR reviews fetch failed; continuing with PR_REVIEWS_FILE=[] for break-glass/advisory consumers."
			fi
			;;
	esac
	gh_retry "${review_comments_raw}" api --paginate "repos/${REPOSITORY}/pulls/${PR_NUMBER}/comments"
	jq -s 'add // []' "${review_comments_raw}" > "${PR_REVIEW_COMMENTS_FILE}"

	jq '{
		title: (.title // ""),
		body: (.body // ""),
		baseRefName: (.base.ref // ""),
		headRefName: (.head.ref // ""),
		headRepoFullName: (.head.repo.full_name // "")
	}' "${PR_PAYLOAD_FILE}" > "${PR_META_FILE}"
fi

# Pre-compute the strict PR title/body fallback issue list once so later
# review-path steps can reuse it without carrying their own regex copies.
LINKED_ISSUE_FALLBACK_NUMBERS_JSON='[]'
if [ -s "${PR_META_FILE}" ] && type extract_repo_scoped_issue_refs_from_text >/dev/null 2>&1; then
	_pr_text_for_linked_issue_fallback="$(jq -r '[.title // "", .body // ""] | join(" ")' "${PR_META_FILE}" 2>/dev/null || echo "")"
	if [ -n "${_pr_text_for_linked_issue_fallback//[[:space:]]/}" ]; then
		_fallback_numbers="$(extract_repo_scoped_issue_refs_from_text "${REPOSITORY}" "${_pr_text_for_linked_issue_fallback}" || true)"
		if [ -n "${_fallback_numbers}" ]; then
			LINKED_ISSUE_FALLBACK_NUMBERS_JSON="$(printf '%s\n' "${_fallback_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0) | tonumber)' 2>/dev/null || echo '[]')"
		fi
	fi
fi
printf 'LINKED_ISSUE_FALLBACK_NUMBERS_JSON=%s\n' "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON}" >> "${GITHUB_ENV}"

# Fetch linked issue title+body via GraphQL — single call that also
# populates LINKED_ISSUES_JSON early so the late-stage cache step can
# skip its own fetch.
_linked_fetch_ok="false"
_linked_raw='[]'
if [ -n "${PR_NUMBER:-}" ]; then
	_linked_tmp="$(mktemp)"
	if gh_retry "${_linked_tmp}" api graphql \
		-f owner="${REPOSITORY_OWNER}" \
		-f name="${REPOSITORY_NAME}" \
		-F number="${PR_NUMBER}" \
		-f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){closingIssuesReferences(first:50){nodes{number title body}}}}}' \
		--jq '.data.repository.pullRequest.closingIssuesReferences.nodes // []'; then
		_linked_fetch_ok="true"
		_linked_raw="$(cat "${_linked_tmp}" 2>/dev/null || echo '[]')"
	else
		echo "::warning::Failed to fetch linked issues via GraphQL; proceeding without linked-issue context."
	fi
	rm -f "${_linked_tmp}"
fi
_linked_raw="$(printf '%s' "${_linked_raw}" | jq -c '.' 2>/dev/null || echo '[]')"

# Store a lightweight numbers-only array in the env var to avoid
# size/sensitivity issues. Only export cache state when fetch succeeded.
if [ "${_linked_fetch_ok}" = "true" ]; then
	if [ -n "${_linked_raw}" ] && [ "${_linked_raw}" != "[]" ]; then
		_linked_numbers="$(printf '%s' "${_linked_raw}" | jq -c '[.[] | {number}]' 2>/dev/null || echo '[]')"
		printf 'LINKED_ISSUES_JSON=%s\n' "${_linked_numbers}" >> "${GITHUB_ENV}"
	else
		printf 'LINKED_ISSUES_JSON=[]\n' >> "${GITHUB_ENV}"
	fi
elif [ -z "${PR_NUMBER:-}" ]; then
	printf 'LINKED_ISSUES_JSON=[]\n' >> "${GITHUB_ENV}"
fi

# Body-text fallback for linked-issue prompt context only.
_linked_context_raw="${_linked_raw}"
if [ "${_linked_context_raw}" = "[]" ] && [ "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON}" != "[]" ]; then
	_fallback_numbers="$(printf '%s' "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON}" | jq -r '.[]' 2>/dev/null || true)"
	if [ -n "${_fallback_numbers}" ]; then
		_FALLBACK_MAX_ISSUES=20
		_fallback_total="$(printf '%s' "${LINKED_ISSUE_FALLBACK_NUMBERS_JSON}" | jq -r 'length' 2>/dev/null || echo '0')"
		if [[ "${_fallback_total:-0}" =~ ^[0-9]+$ ]] && [ "${_fallback_total:-0}" -gt "${_FALLBACK_MAX_ISSUES}" ]; then
			echo "::warning::Linked-issue body-text fallback: PR title/body referenced ${_fallback_total} distinct in-repo issues; capping fetches at ${_FALLBACK_MAX_ISSUES}."
			_fallback_numbers="$(printf '%s\n' "${_fallback_numbers}" | head -n "${_FALLBACK_MAX_ISSUES}")"
		fi
		_fallback_numbers_json="$(printf '%s\n' "${_fallback_numbers}" | jq -Rsc 'split("\n") | map(select(length > 0) | tonumber)' 2>/dev/null || echo '[]')"
		_fallback_fetch_status=0
		_fallback_json="$(_fetch_linked_issue_bodies_graphql "${_fallback_numbers_json}")" || _fallback_fetch_status=$?
		if [ "${_fallback_fetch_status}" -ne 0 ]; then
			echo "::warning::Linked-issue body-text fallback: batched GraphQL issue hydration failed; skipping"
		elif [ "${_fallback_json}" != "[]" ]; then
			_linked_context_raw="${_fallback_json}"
			echo "Linked-issue body-text fallback resolved $(printf '%s' "${_fallback_json}" | jq 'length') issue(s) for context (GraphQL closingIssuesReferences returned empty — likely non-default base branch)."
		fi
	fi
fi

# Build linked issue context file for reviewer/editor prompts.
_linked_json_file="$(mktemp)"
printf '%s' "${_linked_context_raw}" > "${_linked_json_file}"
PYTHONDONTWRITEBYTECODE=1 python3 - "${_linked_json_file}" "${LINKED_ISSUE_CONTEXT_FILE}" <<'PYLINKED'
import json
import sys

json_path = sys.argv[1]
out_path = sys.argv[2]
try:
	with open(json_path, "r", encoding="utf-8") as fh:
		issues = json.load(fh)
except (json.JSONDecodeError, TypeError, OSError, UnicodeDecodeError):
	issues = []
if not isinstance(issues, list):
	issues = []
lines = []
for iss in issues:
	if not isinstance(iss, dict):
		continue
	num = iss.get("number", "?")
	title = iss.get("title", "")
	body = iss.get("body", "")
	lines.append(f"Issue #{num}: {title}")
	if body:
		lines.append(body)
	lines.append("")
if not lines:
	lines.append("No linked issues found.")
with open(out_path, "w", encoding="utf-8") as fh:
	fh.write("\n".join(lines))
PYLINKED
rm -f "${_linked_json_file}"
echo "Linked issue context bytes: $(wc -c < "${LINKED_ISSUE_CONTEXT_FILE}" | tr -d '[:space:]')"

PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

issue_comments_file = os.environ["PR_ISSUE_COMMENTS_FILE"]
reviews_file = os.environ["PR_REVIEWS_FILE"]
review_comments_file = os.environ["PR_REVIEW_COMMENTS_FILE"]
output_file = os.environ["PR_ALL_COMMENTS_CONTEXT_FILE"]


def load_json(path):
	try:
		with open(path, "r", encoding="utf-8") as handle:
			content = handle.read().strip()
			if not content:
				return []
			data = json.loads(content)
	except (json.JSONDecodeError, TypeError, OSError, UnicodeDecodeError):
		return []
	if not isinstance(data, list):
		return []
	return [item for item in data if isinstance(item, dict)]


def norm(value):
	if value is None:
		return ""
	return str(value)


def to_iso_sort_key(value):
	text = norm(value)
	if text == "":
		return "0000-00-00T00:00:00Z"
	return text


issue_comments = load_json(issue_comments_file)
reviews = load_json(reviews_file)
review_comments = load_json(review_comments_file)

entries = []

for comment in issue_comments:
	entries.append(
		{
			"kind": "issue_comment",
			"id": norm(comment.get("id")),
			"author": norm((comment.get("user") or {}).get("login")),
			"created_at": norm(comment.get("created_at")),
			"updated_at": norm(comment.get("updated_at")),
			"state": "",
			"path": "",
			"line": "",
			"body": norm(comment.get("body")),
		}
	)

for review in reviews:
	entries.append(
		{
			"kind": "review",
			"id": norm(review.get("id")),
			"author": norm((review.get("user") or {}).get("login")),
			"created_at": norm(review.get("submitted_at") or review.get("created_at")),
			"updated_at": norm(review.get("updated_at") or review.get("submitted_at") or review.get("created_at")),
			"state": norm(review.get("state")),
			"path": "",
			"line": "",
			"body": norm(review.get("body")),
		}
	)

for comment in review_comments:
	entries.append(
		{
			"kind": "review_comment",
			"id": norm(comment.get("id")),
			"author": norm((comment.get("user") or {}).get("login")),
			"created_at": norm(comment.get("created_at")),
			"updated_at": norm(comment.get("updated_at")),
			"state": "",
			"path": norm(comment.get("path")),
			"line": norm(comment.get("line") if comment.get("line") is not None else comment.get("original_line")),
			"body": norm(comment.get("body")),
		}
	)

entries.sort(key=lambda item: (to_iso_sort_key(item.get("created_at")), norm(item.get("kind")), norm(item.get("id"))))

with open(output_file, "w", encoding="utf-8") as handle:
	handle.write("PR_ALL_COMMENTS_CONTEXT\n")
	handle.write(f"generated_at_utc: {datetime.now(timezone.utc).isoformat()}\n")
	handle.write(f"issue_comments_count: {len(issue_comments)}\n")
	handle.write(f"reviews_count: {len(reviews)}\n")
	handle.write(f"review_comments_count: {len(review_comments)}\n")
	handle.write(f"total_entries: {len(entries)}\n")
	handle.write("\n")
	for index, entry in enumerate(entries):
		handle.write(f"entry[{index}].kind: {entry['kind']}\n")
		handle.write(f"entry[{index}].id: {entry['id']}\n")
		handle.write(f"entry[{index}].author: {entry['author']}\n")
		handle.write(f"entry[{index}].created_at: {entry['created_at']}\n")
		handle.write(f"entry[{index}].updated_at: {entry['updated_at']}\n")
		handle.write(f"entry[{index}].state: {entry['state']}\n")
		handle.write(f"entry[{index}].path: {entry['path']}\n")
		handle.write(f"entry[{index}].line: {entry['line']}\n")
		_body = str(entry["body"])
		_body_escaped = _body.replace("\\", "\\\\").replace(chr(10), "\\n").replace(chr(13), "\\r")
		handle.write(f"entry[{index}].body: {_body_escaped}\n")
		handle.write("\n")
PY

comments_context_bytes="$(wc -c < "${PR_ALL_COMMENTS_CONTEXT_FILE}" | tr -d '[:space:]')"
echo "PR comments context bytes: ${comments_context_bytes}"
if [ -s "${PR_ALL_COMMENTS_CONTEXT_FILE}" ]; then
	echo "PR comments context sha256: $(sha256sum "${PR_ALL_COMMENTS_CONTEXT_FILE}" | awk '{print $1}')"
fi

: > "${PR_DIFF_FILE}"
if [ -n "${PR_NUMBER:-}" ] && ! gh pr diff "${PR_NUMBER}" > "${PR_DIFF_FILE}"; then
	echo "Warning: gh pr diff failed for PR ${PR_NUMBER}; continuing with fallback diff generation."
fi

PR_DIFF_ATTEMPTED_PATHS="gh_pr_diff:${PR_DIFF_FILE}"
if [ -s "${PR_DIFF_FILE}" ]; then
	echo "HAS_PR_DIFF=true" >> "${GITHUB_ENV}"
	echo "PR_DIFF_SOURCE=gh_pr_diff" >> "${GITHUB_ENV}"
else
	echo "HAS_PR_DIFF=false" >> "${GITHUB_ENV}"
	echo "PR_DIFF_SOURCE=gh_pr_diff_empty" >> "${GITHUB_ENV}"
fi
echo "PR_DIFF_ATTEMPTED_PATHS=${PR_DIFF_ATTEMPTED_PATHS}" >> "${GITHUB_ENV}"

pr_diff_bytes="$(wc -c < "${PR_DIFF_FILE}" | tr -d '[:space:]')"
echo "PR diff snapshot (post gh pr diff) bytes: ${pr_diff_bytes}"
if [ -s "${PR_DIFF_FILE}" ]; then
	echo "PR diff snapshot (post gh pr diff) sha256: $(sha256sum "${PR_DIFF_FILE}" | awk '{print $1}')"
else
	echo "PR diff snapshot (post gh pr diff) sha256: unavailable (empty file)"
fi
echo "PR diff snapshot (post gh pr diff) preview suppressed in logs for security."

BASE_REF="$(jq -r '.baseRefName' "${PR_META_FILE}")"
if [ -z "${BASE_REF}" ] || [ "${BASE_REF}" = "null" ]; then
	echo "::error::Unable to determine PR base branch"
	exit 1
fi
echo "BASE_BRANCH=${BASE_REF}" >> "${GITHUB_ENV}"
