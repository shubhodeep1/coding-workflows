#!/usr/bin/env bash
# review_resolve_review_threads.sh — resolve PR review threads that the
# editor stage already audited, so a fixed comment stops looking ignored.
#
# Problem this solves
# -------------------
# The autofix pipeline has always *read* PR review comments: they are
# fetched by scripts/review_collect_pr_metadata.sh into
# PR_ALL_COMMENTS_CONTEXT_FILE and inlined into both the reviewer prompts
# (scripts/review_run_reviewers.sh) and the editor prompt
# (scripts/review_apply_fixes.sh), which is required to emit a
# "PR comment audit:" section giving each bot comment a disposition.
# Nothing, however, ever marked the *thread* resolved. A comment the
# editor fixed on iteration 2 stayed visually identical to one it never
# looked at, so a PR with a dozen addressed Copilot findings still reads
# as a PR whose review feedback was ignored.
#
# What this does
# --------------
# Reads the "PR comment audit:" section of EDITOR_SUMMARY_FILE, maps each
# audited "entry[N]" back to a concrete review-comment id via
# PR_ALL_COMMENTS_CONTEXT_FILE, looks up the GraphQL thread node that
# comment belongs to, and resolves it.
#
# Safety model (why this cannot silently bury real feedback)
# ----------------------------------------------------------
# Resolution is keyed on the comment *id*, never on a path/line pair, and
# only entries the editor explicitly listed are eligible:
#
#   * An audit entry whose index has no matching context entry is skipped.
#   * An entry whose context kind is not review_comment is skipped
#     (issue comments and top-level review bodies have no thread).
#   * An entry whose audit line names a path that disagrees with the
#     context entry's path is skipped — that is the mis-key signature.
#   * A comment id with no matching thread, or an already-resolved
#     thread, is skipped.
#
# The mis-key guard is not theoretical. On shubhodeep1/fun-token-multi-chain
# PR #404 a reviewer bot posted two *contradictory* findings at the same
# services/session-server/src/repository.ts:1383 location an hour apart;
# the editor audited the older one and wrote "already satisfied". Because
# only explicitly-listed entry indices are eligible here, the newer,
# genuinely-open comment is left alone instead of being resolved by a
# path+line collision.
#
# Dispositions
# ------------
# Every audited disposition — applied, already satisfied, and ignored —
# resolves its thread. For "ignored" the editor's stated reason is first
# posted as a reply into the thread, so the reviewer can see why the
# suggestion was rejected and reopen the thread if they disagree.
#
# Inputs (environment):
#   GITHUB_REPOSITORY            owner/repo
#   PR_NUMBER                    pull request number
#   GH_TOKEN                     token with pull-request write scope
#   EDITOR_SUMMARY_FILE          editor summary containing "PR comment audit:"
#   PR_ALL_COMMENTS_CONTEXT_FILE entry[N].{kind,id,path,author} context dump
#   REVIEW_RESOLVE_THREADS_ENABLED  "false" disables the whole step
#   REVIEW_RESOLVE_THREADS_MAX      cap on threads touched per run (default 50)
#   CLAUDE_BRANCH_REVIEW_MODE       "true" (no PR) short-circuits
#
# Outputs:
#   stdout  human-readable per-thread decisions plus a final count line
#           "review_resolve_review_threads: resolved=N replied=N skipped=N"
#   Appends REVIEW_THREADS_RESOLVED_COUNT to GITHUB_ENV when that file is set.
#
# API budget (§15): one paginated GraphQL query for all threads on the PR
# (not one REST call per comment), then one mutation per thread actually
# resolved, plus one REST reply per ignored thread. REST has no
# resolve-review-thread endpoint at all, so the GraphQL mutation is the
# only available transport; the §21.D/§23.D preference for REST addresses
# the Claude Code Web agent proxy in interactive sessions and does not
# apply to this Actions-side caller, which talks to api.github.com
# directly with GH_PAT.
#
# Fail-open: every failure path warns and exits 0. A review thread that
# stays open costs a reader one glance; a failed autofix run costs a
# retry cycle.
set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/gh_helpers.sh" 2>/dev/null || true
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

RESOLVE_TMP_DIR=""
cleanup_review_resolve_review_threads_tmp()
{
	[ -n "${RESOLVE_TMP_DIR}" ] && rm -rf "${RESOLVE_TMP_DIR}"
	return 0
}
trap cleanup_review_resolve_review_threads_tmp EXIT

emit_resolve_counts()
{
	local resolved="$1"
	local replied="$2"
	local skipped="$3"
	echo "review_resolve_review_threads: resolved=${resolved} replied=${replied} skipped=${skipped}"
	if [ -n "${GITHUB_ENV:-}" ] && [ -w "${GITHUB_ENV}" ]; then
		echo "REVIEW_THREADS_RESOLVED_COUNT=${resolved}" >> "${GITHUB_ENV}"
	fi
	return 0
}

case "$(printf '%s' "${REVIEW_RESOLVE_THREADS_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
	0|false|no|off)
		echo "::notice::review_resolve_review_threads: disabled via REVIEW_RESOLVE_THREADS_ENABLED; skipping."
		emit_resolve_counts 0 0 0
		exit 0
		;;
esac

case "$(printf '%s' "${CLAUDE_BRANCH_REVIEW_MODE:-false}" | tr '[:upper:]' '[:lower:]')" in
	1|true|yes|on)
		echo "::notice::review_resolve_review_threads: claude-branch-review mode has no PR threads; skipping."
		emit_resolve_counts 0 0 0
		exit 0
		;;
esac

REPOSITORY="${GITHUB_REPOSITORY:-}"
if ! [[ "${REPOSITORY}" =~ ^[^/]+/[^/]+$ ]]; then
	echo "::warning::review_resolve_review_threads: GITHUB_REPOSITORY is not owner/repo; skipping."
	emit_resolve_counts 0 0 0
	exit 0
fi
REPOSITORY_OWNER="${REPOSITORY%%/*}"
REPOSITORY_NAME="${REPOSITORY#*/}"

if ! [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
	echo "::warning::review_resolve_review_threads: PR_NUMBER '${PR_NUMBER:-}' is not numeric; skipping."
	emit_resolve_counts 0 0 0
	exit 0
fi

for required_file in EDITOR_SUMMARY_FILE PR_ALL_COMMENTS_CONTEXT_FILE
do
	if [ -z "${!required_file:-}" ] || [ ! -s "${!required_file}" ]; then
		echo "::warning::review_resolve_review_threads: ${required_file} is unset or empty; skipping."
		emit_resolve_counts 0 0 0
		exit 0
	fi
done

MAX_THREADS="${REVIEW_RESOLVE_THREADS_MAX:-50}"
if ! [[ "${MAX_THREADS}" =~ ^[0-9]+$ ]] || [ "${MAX_THREADS}" -eq 0 ]; then
	MAX_THREADS=50
fi

if ! RESOLVE_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/review_resolve_threads.XXXXXX" 2>/dev/null)"; then
	echo "::warning::review_resolve_review_threads: mktemp failed; skipping."
	emit_resolve_counts 0 0 0
	exit 0
fi

THREADS_RAW="${RESOLVE_TMP_DIR}/threads_raw.json"
THREADS_JSON="${RESOLVE_TMP_DIR}/threads.json"
PLAN_FILE="${RESOLVE_TMP_DIR}/plan.jsonl"

# Single paginated GraphQL query for every review thread on the PR. One
# call per 100 threads rather than one REST lookup per audited comment
# (§15). Only the first comment of each thread is requested: that is the
# comment the audit entry's id refers to, and it is what anchors the
# thread.
read -r -d '' THREADS_QUERY <<'GRAPHQL'
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes { databaseId path author { login } }
          }
        }
      }
    }
  }
}
GRAPHQL

if ! gh_retry gh api graphql --paginate \
	-f owner="${REPOSITORY_OWNER}" \
	-f name="${REPOSITORY_NAME}" \
	-F number="${PR_NUMBER}" \
	-f query="${THREADS_QUERY}" > "${THREADS_RAW}" 2>/dev/null
then
	echo "::warning::review_resolve_review_threads: review-thread query failed; leaving all threads untouched."
	emit_resolve_counts 0 0 0
	exit 0
fi

if ! jq -s '[.[].data.repository.pullRequest.reviewThreads.nodes[]?] | map({
		thread_id: .id,
		is_resolved: .isResolved,
		comment_id: (.comments.nodes[0].databaseId // null),
		path: (.comments.nodes[0].path // ""),
		author: (.comments.nodes[0].author.login // "")
	})' "${THREADS_RAW}" > "${THREADS_JSON}" 2>/dev/null
then
	echo "::warning::review_resolve_review_threads: could not parse review-thread payload; leaving all threads untouched."
	emit_resolve_counts 0 0 0
	exit 0
fi

thread_total="$(jq 'length' "${THREADS_JSON}" 2>/dev/null || echo 0)"
echo "review_resolve_review_threads: ${thread_total} review thread(s) on PR #${PR_NUMBER}."

# Build the resolve plan. Emitted as JSONL so the shell loop below never
# has to re-parse free-form editor prose.
if ! PYTHONDONTWRITEBYTECODE=1 MAX_THREADS="${MAX_THREADS}" THREADS_JSON="${THREADS_JSON}" \
	PLAN_FILE="${PLAN_FILE}" python3 "${SCRIPT_DIR}/review_resolve_review_threads_plan.py"
then
	echo "::warning::review_resolve_review_threads: plan builder failed; leaving all threads untouched."
	emit_resolve_counts 0 0 0
	exit 0
fi

resolved_count=0
replied_count=0
skipped_count=0

if [ ! -s "${PLAN_FILE}" ]; then
	echo "review_resolve_review_threads: no audited entry mapped to an open thread."
	emit_resolve_counts 0 0 0
	exit 0
fi

read -r -d '' RESOLVE_MUTATION <<'GRAPHQL'
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { id isResolved }
  }
}
GRAPHQL

while IFS= read -r plan_line
do
	[ -z "${plan_line}" ] && continue

	thread_id="$(printf '%s' "${plan_line}" | jq -r '.thread_id // ""' 2>/dev/null || echo "")"
	comment_id="$(printf '%s' "${plan_line}" | jq -r '.comment_id // ""' 2>/dev/null || echo "")"
	disposition="$(printf '%s' "${plan_line}" | jq -r '.disposition // ""' 2>/dev/null || echo "")"
	reason="$(printf '%s' "${plan_line}" | jq -r '.reason // ""' 2>/dev/null || echo "")"
	comment_path="$(printf '%s' "${plan_line}" | jq -r '.path // ""' 2>/dev/null || echo "")"

	if [ -z "${thread_id}" ]; then
		skipped_count=$(( skipped_count + 1 ))
		continue
	fi

	# An "ignored" disposition means the editor deliberately rejected the
	# suggestion. Resolving that silently would hide a disagreement, so
	# the reason goes into the thread first; the reviewer can reopen.
	if [ "${disposition}" = "ignored" ]; then
		reply_body="$(printf '%s' "AI autofix did not apply this suggestion: ${reason:-no reason recorded}

Resolving the thread to record that it was reviewed rather than missed. Reopen it if you disagree — the AI autofix pipeline will pick it up again on the next iteration.")"
		if gh_retry gh api \
			"repos/${REPOSITORY}/pulls/${PR_NUMBER}/comments/${comment_id}/replies" \
			-f body="${reply_body}" >/dev/null 2>&1
		then
			replied_count=$(( replied_count + 1 ))
		else
			echo "::warning::review_resolve_review_threads: reply to comment ${comment_id} failed; resolving without it."
		fi
	fi

	if gh_retry gh api graphql \
		-F threadId="${thread_id}" \
		-f query="${RESOLVE_MUTATION}" >/dev/null 2>&1
	then
		resolved_count=$(( resolved_count + 1 ))
		echo "  resolved thread ${thread_id} (comment ${comment_id}, ${comment_path:-unknown path}, ${disposition})"
	else
		skipped_count=$(( skipped_count + 1 ))
		echo "::warning::review_resolve_review_threads: resolve mutation failed for thread ${thread_id} (comment ${comment_id}); leaving it open."
	fi
done < "${PLAN_FILE}"

emit_resolve_counts "${resolved_count}" "${replied_count}" "${skipped_count}"
exit 0
