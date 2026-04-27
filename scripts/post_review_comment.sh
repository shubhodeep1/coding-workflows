#!/usr/bin/env bash
# post_review_comment.sh — Post a consolidated reviewer-consensus ledger to
# the appropriate GitHub surface for the claude-branch-review mode of
# review_autofix.yml.
#
# Routing (matches CLAUDE.md Q3 decision: PR-comment when an open PR exists,
# fall back to commit comment otherwise):
#   * If PR_NUMBER is set + numeric → POST to /repos/{repo}/issues/{pr}/comments
#   * Else (push event without an open PR for the head SHA) → POST to
#     /repos/{repo}/commits/{sha}/comments
#
# Chunking (matches Q4: chunk on finding boundaries; never mid-finding):
#   GitHub caps a single comment body at 65 536 chars. We aim for a soft
#   cap (COMMENT_BODY_SOFT_LIMIT, default 60 000) to leave headroom for the
#   per-chunk envelope. The ledger is split so that:
#     * The CONSENSUS FINDINGS block stays contiguous when it fits;
#     * If oversized, the CONSENSUS block is split between findings (the
#       boundary is the next "- " bullet at column 0); per-reviewer sections
#       are split between FINDINGS FROM <slug> blocks.
#   Each emitted comment body has a `(part k/N)` suffix in its header so
#   readers can reassemble the ledger, and a stable trailer marker so a
#   follow-up run can detect / replace prior posts (future: not implemented
#   in this version — every run posts new comments; suppression is handled
#   upstream by concurrency: cancel-in-progress).
#
# Env contract:
#   REVIEWER_CONSENSUS_FILE   path to the ledger written by
#                             summarize_reviewer_consensus.sh --prefix review
#   REPOSITORY                <owner>/<repo> (default github.repository)
#   PR_NUMBER                 numeric PR number, optional
#   HEAD_SHA                  commit SHA the review covers (used for commit
#                             comment fallback AND for the comment header)
#   HEAD_REF                  ref the push targeted (e.g. claude/foo) — used
#                             in header only
#   GITHUB_RUN_ID             actions run id, used to link back from header
#   GITHUB_SERVER_URL         actions server url, default https://github.com
#   GH_TOKEN                  must have repo scope to post comments
#   SUPPORT_SCRIPTS_DIR       path containing gh_helpers.sh
#   COMMENT_BODY_SOFT_LIMIT   per-comment body cap in chars (default 60000)
#   POST_REVIEW_DRY_RUN       when "true", print the chunked bodies to stdout
#                             instead of posting (used by tests / debugging)
#
# Exit codes:
#   0   posted (or dry-ran) successfully, OR ledger was empty/missing and
#       posting was skipped (warning emitted)
#   1   posting failed after retries
#   2   bad usage / missing required env

set -euo pipefail

# ── Required env ────────────────────────────────────────────────────────
: "${REVIEWER_CONSENSUS_FILE:?REVIEWER_CONSENSUS_FILE must be set}"
: "${REPOSITORY:?REPOSITORY must be set (e.g. owner/repo)}"
: "${HEAD_SHA:?HEAD_SHA must be set (the commit the review covers)}"
: "${SUPPORT_SCRIPTS_DIR:?SUPPORT_SCRIPTS_DIR must be set (gh_helpers.sh source)}"

PR_NUMBER="${PR_NUMBER:-}"
HEAD_REF="${HEAD_REF:-}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-}"
GITHUB_SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
COMMENT_BODY_SOFT_LIMIT="${COMMENT_BODY_SOFT_LIMIT:-60000}"
POST_REVIEW_DRY_RUN="${POST_REVIEW_DRY_RUN:-false}"

# ── Source gh_helpers (rate-limit-aware retry) ──────────────────────────
# shellcheck disable=SC1091
if [ -f "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" ]; then
	source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh"
fi
# Fallback shim when gh_helpers is unavailable — bare `gh` with no retry.
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

# ── Ledger sanity ───────────────────────────────────────────────────────
if [ ! -s "${REVIEWER_CONSENSUS_FILE}" ]; then
	echo "::warning::post_review_comment: REVIEWER_CONSENSUS_FILE is empty or missing (${REVIEWER_CONSENSUS_FILE}); nothing to post."
	exit 0
fi

# Treat the "empty ledger" sentinel emitted by summarize_reviewer_consensus.sh
# as a no-op so we don't post a "(No reviewer outputs available)" comment for
# every push that fails reviewer fan-out. The summariser already logs the
# failure to the workflow run.
if grep -Fq "(No reviewer outputs available for this pass.)" "${REVIEWER_CONSENSUS_FILE}"; then
	echo "::warning::post_review_comment: ledger contains the empty-pass sentinel; skipping post."
	exit 0
fi

LEDGER_BYTES="$(wc -c < "${REVIEWER_CONSENSUS_FILE}" | tr -d '[:space:]')"
echo "post_review_comment: ledger=${REVIEWER_CONSENSUS_FILE} bytes=${LEDGER_BYTES} pr=${PR_NUMBER:-<none>} sha=${HEAD_SHA}"

# ── Routing decision ────────────────────────────────────────────────────
ROUTE=""
if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
	ROUTE="pr"
else
	ROUTE="commit"
fi
echo "post_review_comment: route=${ROUTE}"

# ── Build header / trailer envelope ─────────────────────────────────────
RUN_URL=""
if [ -n "${GITHUB_RUN_ID}" ]; then
	RUN_URL="${GITHUB_SERVER_URL}/${REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
fi

# Stable identifier so future versions can detect/replace prior posts.
COMMENT_MARKER="<!-- claude-branch-review:v1 sha=${HEAD_SHA} -->"

_header_for_part()
{
	local part="$1"
	local total="$2"
	local part_suffix=""
	if [ "${total}" -gt 1 ]; then
		part_suffix=" (part ${part}/${total})"
	fi
	{
		printf '%s\n' "${COMMENT_MARKER}"
		printf '## Claude-branch review%s\n\n' "${part_suffix}"
		printf '* Commit: `%s`\n' "${HEAD_SHA}"
		if [ -n "${HEAD_REF}" ]; then
			printf '* Ref: `%s`\n' "${HEAD_REF}"
		fi
		if [ -n "${RUN_URL}" ]; then
			printf '* Run: %s\n' "${RUN_URL}"
		fi
		printf '\n'
	}
}

_trailer()
{
	printf '\n_Generated by claude-branch-review (mode of `review_autofix.yml`)._\n'
}

# Compute envelope overhead so the chunker knows how much body it can fit
# inside COMMENT_BODY_SOFT_LIMIT. We measure with a worst-case "(part 9/9)"
# suffix so we never under-estimate.
HEADER_TMP="$(mktemp)"
TRAILER_TMP="$(mktemp)"
_header_for_part 9 9 > "${HEADER_TMP}"
_trailer > "${TRAILER_TMP}"
HEADER_BYTES="$(wc -c < "${HEADER_TMP}" | tr -d '[:space:]')"
TRAILER_BYTES="$(wc -c < "${TRAILER_TMP}" | tr -d '[:space:]')"
ENVELOPE_BYTES=$(( HEADER_BYTES + TRAILER_BYTES + 64 ))  # 64 bytes safety margin
rm -f "${HEADER_TMP}" "${TRAILER_TMP}"

MAX_BODY=$(( COMMENT_BODY_SOFT_LIMIT - ENVELOPE_BYTES ))
if [ "${MAX_BODY}" -lt 1024 ]; then
	echo "::error::post_review_comment: COMMENT_BODY_SOFT_LIMIT=${COMMENT_BODY_SOFT_LIMIT} too small for envelope=${ENVELOPE_BYTES}; need >= $(( ENVELOPE_BYTES + 1024 ))." >&2
	exit 2
fi

# ── Chunk on finding boundaries ─────────────────────────────────────────
# Strategy:
#   Walk the ledger one line at a time, accumulating into a buffer. At every
#   "safe split point" (defined below), if adding the next finding would
#   overflow MAX_BODY, flush the current buffer as a chunk and start a new
#   one. Safe split points:
#     * Before any "- " bullet at column 0 inside the CONSENSUS block
#     * Before any "=== FINDINGS FROM " sentinel (per-reviewer section start)
#     * Before "=== END CONSENSUS FINDINGS ===" (so the closing sentinel can
#       move to the next chunk if the consensus block itself is huge)
#   If a single finding is itself larger than MAX_BODY (pathological), it
#   is emitted as its own oversized chunk and GitHub will reject it — we
#   surface a clear error rather than silently truncate.
#
# Implementation: read the ledger into a Python helper which is much easier
# to reason about than awk for this. We require python3 (already a workflow
# dependency).
CHUNK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/post_review_comment_chunks.XXXXXX")"
trap 'rm -rf "${CHUNK_DIR}"' EXIT

PYTHONDONTWRITEBYTECODE=1 python3 - "${REVIEWER_CONSENSUS_FILE}" "${CHUNK_DIR}" "${MAX_BODY}" <<'PYCHUNK'
import os
import sys

ledger_path = sys.argv[1]
out_dir = sys.argv[2]
max_body = int(sys.argv[3])

with open(ledger_path, "r", encoding="utf-8", errors="replace") as fh:
	lines = fh.readlines()

def is_safe_split(line: str, prev_line: str) -> bool:
	# Safe split points (start of the upcoming line creates a new chunk):
	#  1. New finding bullet inside the CONSENSUS block.
	#  2. New per-reviewer section start.
	#  3. The consensus block's closing sentinel, so the closer can move
	#     to the next chunk when the block straddles a boundary.
	if line.startswith("- "):
		return True
	if line.startswith("=== FINDINGS FROM "):
		return True
	if line.startswith("=== END CONSENSUS FINDINGS ==="):
		return True
	return False

chunks: list[list[str]] = [[]]
current_size = 0
prev_line = ""
for line in lines:
	line_size = len(line.encode("utf-8"))
	# If adding this line overflows AND we're at a safe split point AND the
	# current chunk is non-empty, start a new chunk.
	if (
		current_size + line_size > max_body
		and chunks[-1]
		and is_safe_split(line, prev_line)
	):
		chunks.append([])
		current_size = 0
	chunks[-1].append(line)
	current_size += line_size
	prev_line = line

# Pathological-finding check: any chunk whose total size still exceeds
# max_body indicates a single finding bigger than the per-comment cap.
oversize = []
for idx, chunk in enumerate(chunks):
	body = "".join(chunk)
	body_bytes = len(body.encode("utf-8"))
	if body_bytes > max_body:
		oversize.append((idx, body_bytes))

if oversize:
	# Emit a sentinel file the bash caller can detect.
	with open(os.path.join(out_dir, "OVERSIZE"), "w", encoding="utf-8") as fh:
		for idx, sz in oversize:
			fh.write(f"chunk={idx} bytes={sz} max={max_body}\n")

for idx, chunk in enumerate(chunks):
	with open(os.path.join(out_dir, f"chunk_{idx:03d}.txt"), "w", encoding="utf-8") as fh:
		fh.write("".join(chunk))

print(f"chunk_count={len(chunks)}")
PYCHUNK

if [ -f "${CHUNK_DIR}/OVERSIZE" ]; then
	echo "::error::post_review_comment: at least one finding exceeds COMMENT_BODY_SOFT_LIMIT=${COMMENT_BODY_SOFT_LIMIT}; GitHub will reject the post." >&2
	cat "${CHUNK_DIR}/OVERSIZE" >&2
	# Continue posting anyway — operator visibility into the oversized chunk
	# is more useful than silent dropping. The gh API call will surface the
	# 422 error per chunk.
fi

shopt -s nullglob
CHUNK_FILES=( "${CHUNK_DIR}"/chunk_*.txt )
shopt -u nullglob
TOTAL_CHUNKS="${#CHUNK_FILES[@]}"
if [ "${TOTAL_CHUNKS}" -eq 0 ]; then
	echo "::warning::post_review_comment: chunker produced zero chunks; nothing to post."
	exit 0
fi

echo "post_review_comment: chunks=${TOTAL_CHUNKS} max_body_bytes=${MAX_BODY}"

# ── Post each chunk ─────────────────────────────────────────────────────
post_failed=0
posted_count=0
part=0
for cf in "${CHUNK_FILES[@]}"; do
	part=$(( part + 1 ))
	body_tmp="$(mktemp)"
	{
		_header_for_part "${part}" "${TOTAL_CHUNKS}"
		cat "${cf}"
		_trailer
	} > "${body_tmp}"

	if [ "${POST_REVIEW_DRY_RUN}" = "true" ]; then
		echo "─── DRY RUN: chunk ${part}/${TOTAL_CHUNKS} (route=${ROUTE}) ───"
		cat "${body_tmp}"
		echo "─── END DRY RUN: chunk ${part}/${TOTAL_CHUNKS} ───"
		posted_count=$(( posted_count + 1 ))
		rm -f "${body_tmp}"
		continue
	fi

	# Endpoint per route. Both accept body= as a form field.
	if [ "${ROUTE}" = "pr" ]; then
		endpoint="repos/${REPOSITORY}/issues/${PR_NUMBER}/comments"
	else
		endpoint="repos/${REPOSITORY}/commits/${HEAD_SHA}/comments"
	fi

	if gh_retry gh api -X POST "${endpoint}" \
		-F "body=@${body_tmp}" >/dev/null; then
		posted_count=$(( posted_count + 1 ))
		echo "post_review_comment: posted chunk ${part}/${TOTAL_CHUNKS} → ${endpoint}"
	else
		post_failed=$(( post_failed + 1 ))
		echo "::error::post_review_comment: failed to post chunk ${part}/${TOTAL_CHUNKS} → ${endpoint}" >&2
	fi
	rm -f "${body_tmp}"
done

if [ "${post_failed}" -gt 0 ]; then
	echo "::error::post_review_comment: ${post_failed}/${TOTAL_CHUNKS} chunk(s) failed to post." >&2
	exit 1
fi

echo "post_review_comment: posted ${posted_count}/${TOTAL_CHUNKS} chunk(s) successfully."
