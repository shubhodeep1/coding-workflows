#!/usr/bin/env bash
# post_review_comment.sh — Post a consolidated reviewer-consensus ledger to
# the appropriate GitHub surface for the claude-branch-review mode of
# review_autofix.yml.
#
# Routing (matches CLAUDE.md Q3 decision: PR-comment when an open PR exists,
# fall back to commit comment otherwise):
#   * If PR_NUMBER is set + numeric → POST to /repos/{repo}/issues/{pr}/comments
#   * Else, re-check for an open PR on HEAD_REF whose head.sha == HEAD_SHA
#     (open-then-push race recovery: review_autofix's caller checks for a
#     PR once at push time, but the PR is often opened seconds later while
#     the ~hour-long review proceeds). On match → POST to that PR.
#   * Else (no matching open PR) → POST to /repos/{repo}/commits/{sha}/comments
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

# ── Env defaults + required-input validation ────────────────────────────
# Required vars use explicit `if [ -z ]; then exit 2` (instead of bash's
# `: "${VAR:?...}"`) so missing input matches the documented exit code 2.
REPOSITORY="${REPOSITORY:-${GITHUB_REPOSITORY:-}}"
SUPPORT_SCRIPTS_DIR="${SUPPORT_SCRIPTS_DIR:-scripts}"
PR_NUMBER="${PR_NUMBER:-}"
HEAD_REF="${HEAD_REF:-}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-}"
GITHUB_SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
COMMENT_BODY_SOFT_LIMIT="${COMMENT_BODY_SOFT_LIMIT:-60000}"
POST_REVIEW_DRY_RUN="${POST_REVIEW_DRY_RUN:-false}"

if [ -z "${REVIEWER_CONSENSUS_FILE:-}" ]; then
	echo "::error::post_review_comment: REVIEWER_CONSENSUS_FILE must be set." >&2
	exit 2
fi
if [ -z "${REPOSITORY}" ]; then
	echo "::error::post_review_comment: REPOSITORY must be set (e.g. owner/repo); GITHUB_REPOSITORY also empty." >&2
	exit 2
fi
if ! [[ "${REPOSITORY}" =~ ^[^/]+/[^/]+$ ]]; then
	echo "::error::post_review_comment: REPOSITORY must be in 'owner/repo' format; got '${REPOSITORY}'." >&2
	exit 2
fi
if [ -z "${HEAD_SHA:-}" ]; then
	echo "::error::post_review_comment: HEAD_SHA must be set (the commit the review covers)." >&2
	exit 2
fi
if ! [[ "${COMMENT_BODY_SOFT_LIMIT}" =~ ^[0-9]+$ ]]; then
	echo "::error::post_review_comment: COMMENT_BODY_SOFT_LIMIT must be a non-negative integer; got '${COMMENT_BODY_SOFT_LIMIT}'." >&2
	exit 2
fi

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

# Treat the legacy empty-pass sentinel and the new structured empty ledger
# emitted by summarize_reviewer_consensus.sh as a no-op so we don't post a
# review comment for every push that fails reviewer fan-out. The summariser
# already logs the failure to the workflow run.
if grep -Fq "(No reviewer outputs available for this pass.)" "${REVIEWER_CONSENSUS_FILE}" || \
	{ grep -Fqx "(No findings reported.)" "${REVIEWER_CONSENSUS_FILE}" && \
	  grep -Fqx "(No task gaps reported.)" "${REVIEWER_CONSENSUS_FILE}" && \
	  ! grep -Fq "=== FINDINGS FROM " "${REVIEWER_CONSENSUS_FILE}"; }; then
	echo "::warning::post_review_comment: ledger contains the empty-pass sentinel; skipping post."
	exit 0
fi

LEDGER_BYTES="$(wc -c < "${REVIEWER_CONSENSUS_FILE}" | tr -d '[:space:]')"
echo "post_review_comment: ledger=${REVIEWER_CONSENSUS_FILE} bytes=${LEDGER_BYTES} sha=${HEAD_SHA} pr_in=${PR_NUMBER:-<none>}"

# ── Open-then-push race recovery ────────────────────────────────────────
# review_autofix.yml's caller (internal-review.yml resolve-claude-branch-pr)
# checks for an open PR exactly once, ~milliseconds after the push event
# fires. When the user pushes the branch and opens the PR a few seconds
# later (the open-then-push race documented in PR #1729 for concurrency),
# the resolve job sees no PR, the codex-agent runs in claude-branch-review
# mode with PR_NUMBER="", and the consensus ledger lands on the commit
# comment thread instead of the PR. The review takes ~30–60 minutes, so
# by the time we post the PR almost always exists.
#
# Re-check here, at the actual routing decision point, so we capture any
# PR opened during the entire review window. Reuse the same head-ref
# query pattern as resolve-claude-branch-pr (internal-review.yml line 99)
# and add a head.sha guard so a force-push during the review (PR head
# advanced past what we reviewed) still falls through to the commit
# comment route — posting a stale-SHA review on a force-pushed PR would
# mislead readers.
#
# Fail-open: any API error / empty result falls through to the existing
# commit-comment route, which is the current behaviour.
if ! [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] && [ -n "${HEAD_REF}" ]; then
	resolved_pr=""
	owner="${REPOSITORY%/*}"
	# URL-percent-encode HEAD_REF for the query parameter. Git allows
	# branch names containing query-reserved characters (& # + % ? space
	# etc.), and a `feat&test` ref would otherwise turn the query into
	# ?state=open&head=owner:feat&test=  — collapsing to head=owner:feat
	# server-side, the lookup misses the PR, and we'd fall through to
	# commit-comment routing (the original bug this script is fixing).
	# jq is already a workflow dependency; @uri matches RFC 3986
	# percent-encoding and / → %2F decodes back server-side.
	encoded_head_ref="$(jq -nr --arg ref "${HEAD_REF}" '$ref | @uri')"
	# Let gh_retry's stderr (auth errors, rate-limit warnings, permanent
	# failures after retry exhaustion) flow through to the workflow log
	# for observability. The numeric regex on resolved_pr below still
	# fails open to commit-comment route on any empty / non-numeric
	# output, so no observability gain costs us behavioural safety.
	if resolved_pr="$(HEAD_SHA="${HEAD_SHA}" gh_retry gh api \
		"repos/${REPOSITORY}/pulls?state=open&head=${owner}:${encoded_head_ref}" \
		--jq '[.[] | select(.head.sha == env.HEAD_SHA) | .number] | first // empty')" \
		&& [[ "${resolved_pr}" =~ ^[0-9]+$ ]]; then
		echo "post_review_comment: resolved PR_NUMBER=${resolved_pr} from HEAD_REF=${HEAD_REF} HEAD_SHA=${HEAD_SHA} (open-then-push race recovery)"
		PR_NUMBER="${resolved_pr}"
	else
		echo "post_review_comment: no matching open PR for HEAD_REF=${HEAD_REF} HEAD_SHA=${HEAD_SHA}; routing to commit comment"
	fi
fi

# ── Routing decision ────────────────────────────────────────────────────
ROUTE=""
if [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
	ROUTE="pr"
else
	ROUTE="commit"
fi
echo "post_review_comment: route=${ROUTE} pr=${PR_NUMBER:-<none>}"

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
	#  1. New finding bullet inside the CONSENSUS or CONSENSUS TASK GAPS block.
	#     Each finding bullet has the canonical form
	#       "- {file}:{line_range} | severity=... | confidence=..."
	#     and each task-gap bullet starts "- requirement:", so we additionally
	#     require ':' on the line — this rejects prose sub-lists like
	#     "- this is a nested bullet" that a model might emit inside PROBLEM:,
	#     WHY:, or EVIDENCE: text and which would otherwise cause a false split
	#     mid-finding.
	#  2. New per-reviewer section start.
	#  3. Per-reviewer section closing sentinel, so the closer can move
	#     to the next chunk when a reviewer block straddles a boundary
	#     (otherwise a chunk near the cap could overflow on the bare
	#     "=== END FINDINGS FROM <slug> ===" line and force a false
	#     oversize error).
	#  4. The consensus block's closing sentinel, same reasoning.
	#  5. The task-gaps consensus block's opening and closing sentinels,
	#     same reasoning as #4 — a very large TASK GAPS block must not
	#     overflow a chunk on its bare sentinel lines.
	if line.startswith("- ") and ":" in line:
		return True
	if line.startswith("=== FINDINGS FROM "):
		return True
	if line.startswith("=== END FINDINGS FROM "):
		return True
	if line.startswith("=== END CONSENSUS FINDINGS ==="):
		return True
	if line.startswith("=== CONSENSUS TASK GAPS ==="):
		return True
	if line.startswith("=== END CONSENSUS TASK GAPS ==="):
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
	# At least one chunk's body still exceeds MAX_BODY after splitting at
	# every available safe boundary. The most common cause is a single
	# finding (CONSENSUS bullet or per-reviewer entry) larger than the
	# per-comment cap, but it can also fire when a non-splittable run of
	# lines (e.g. a long unbroken section between sentinels) exceeds the
	# cap. Either way the GitHub /comments POST will return 422; we keep
	# trying so the operator sees the failure rather than a silent drop.
	echo "::error::post_review_comment: at least one chunk exceeds COMMENT_BODY_SOFT_LIMIT=${COMMENT_BODY_SOFT_LIMIT} after exhausting safe split points; GitHub will reject the post." >&2
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
