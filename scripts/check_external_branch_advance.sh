#!/usr/bin/env bash
# check_external_branch_advance.sh — detect whether the remote tip of a
# branch has advanced past a pinned local SHA due to a non-autofix push.
#
# This runs twice in the review_autofix cycle:
#
#   1. Pre-editor gate — after reviewers finish, before the expensive
#      editor LLM call.  If the branch advanced with an external commit,
#      the editor would produce output against a stale base that either
#      conflicts at push time or silently clobbers the mid-run push.
#      Soft-exiting 0 here abandons the current iteration cheaply and
#      relies on the synchronize event from the external push to kick
#      off a fresh reviewer/editor pass.
#
#   2. Pre-push gate — right before "git push" at the end of the run.
#      Same detection; avoids the merge-retry hard-fail when a concurrent
#      external push touched overlapping hunks.
#
# Stdout protocol (sole machine-readable output — caller greps
# `^ADVANCE=...$` from captured stdout):
#
#   ADVANCE=none      — branch unchanged; caller continues normally.
#   ADVANCE=self_only — all advancing commits are [ai-autofix] /
#                       [ai-merge-resolve] attributed to AUTOFIX_BOT_LOGIN;
#                       caller continues (peer-autofix race is rare and
#                       the next cycle catches up).
#   ADVANCE=external  — at least one advancing commit is external
#                       (non-autofix subject, or self-like subject whose
#                       GitHub-attributed author/committer login does NOT
#                       match AUTOFIX_BOT_LOGIN — spoof defence); caller
#                       soft-exits 0.
#   ADVANCE=unknown   — detection inconclusive or inputs unavailable
#                       (git fetch failed, gh api errored,
#                       GITHUB_REPOSITORY unresolvable, GitHub-attributed
#                       identity empty, or TARGET_BRANCH/LOCAL_HEAD_SHA
#                       missing); caller fail-opens and continues.  This
#                       mirrors the fail-open convention of the gate-job
#                       self-trigger skip (probably_unnecessary_but_read_if_stuck.md §20.1) — GitHub API
#                       blips must never silently drop the review.
#
# Exit code is 0 for all documented production outcomes, including
# ADVANCE=unknown fail-open cases. Non-zero exits are reserved for
# internal/script errors outside this protocol and are not expected on
# any well-formed caller invocation.
#
# Required env:
#   TARGET_BRANCH   — branch name to check (typically the PR head branch)
#   LOCAL_HEAD_SHA  — SHA that reviewers/editor operated against (capture
#                     immediately after "Checkout PR head branch", NOT
#                     after the autofix commit is made)
#   GH_TOKEN        — for attribution lookup via `gh api`
#
# Optional env:
#   AUTOFIX_BOT_LOGIN  — login counted as "ours" (default: codex)
#   GITHUB_REPOSITORY  — owner/repo; auto-derived from `git remote` if
#                        unset
#
# Stderr is free-form human-readable diagnostic logging.

set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/gh_helpers.sh" ]; then
	# shellcheck source=/dev/null
	. "${SCRIPT_DIR}/gh_helpers.sh" || true
fi

log() { printf '%s\n' "$*" >&2; }

TARGET_BRANCH="${TARGET_BRANCH:-}"
LOCAL_HEAD_SHA="${LOCAL_HEAD_SHA:-}"
BOT_LOGIN="${AUTOFIX_BOT_LOGIN:-codex}"
REPOSITORY="${GITHUB_REPOSITORY:-}"

if [ -z "${TARGET_BRANCH}" ] || [ -z "${LOCAL_HEAD_SHA}" ]; then
	log "::warning::check_external_branch_advance: TARGET_BRANCH and LOCAL_HEAD_SHA must both be set; fail-open."
	printf 'ADVANCE=unknown\n'
	exit 0
fi

if ! git cat-file -e "${LOCAL_HEAD_SHA}^{commit}" 2>/dev/null; then
	log "::warning::check_external_branch_advance: LOCAL_HEAD_SHA=${LOCAL_HEAD_SHA} is not a local commit object; fail-open."
	printf 'ADVANCE=unknown\n'
	exit 0
fi

if [ -z "${REPOSITORY}" ]; then
	remote_url="$(git remote get-url origin 2>/dev/null || true)"
	case "${remote_url}" in
		*github.com[:/]*)
			REPOSITORY="${remote_url##*github.com[:/]}"
			REPOSITORY="${REPOSITORY%.git}"
			;;
	esac
fi

# Fetch the current remote tip.  A fetch failure is a detection failure,
# not a branch-advance signal — fail open.
if ! git fetch --quiet --no-tags --prune origin \
	"+refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}" 2>/dev/null; then
	log "::warning::check_external_branch_advance: git fetch origin +refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH} failed; fail-open."
	printf 'ADVANCE=unknown\n'
	exit 0
fi

REMOTE_TIP_SHA="$(git rev-parse --verify --quiet "refs/remotes/origin/${TARGET_BRANCH}" 2>/dev/null || true)"
if [ -z "${REMOTE_TIP_SHA}" ]; then
	log "::warning::check_external_branch_advance: refs/remotes/origin/${TARGET_BRANCH} unresolved after fetch; fail-open."
	printf 'ADVANCE=unknown\n'
	exit 0
fi

if [ "${REMOTE_TIP_SHA}" = "${LOCAL_HEAD_SHA}" ]; then
	log "check_external_branch_advance: branch unchanged (${LOCAL_HEAD_SHA})."
	printf 'ADVANCE=none\n'
	exit 0
fi

# If LOCAL_HEAD_SHA is not an ancestor of the remote tip, the branch was
# force-pushed / rewritten — treat as external advance so we soft-exit
# cleanly rather than attempting any merge downstream.
if ! git merge-base --is-ancestor "${LOCAL_HEAD_SHA}" "${REMOTE_TIP_SHA}" 2>/dev/null; then
	log "check_external_branch_advance: local ${LOCAL_HEAD_SHA} is not an ancestor of origin/${TARGET_BRANCH} tip ${REMOTE_TIP_SHA} — treating as external (force-push or history rewrite)."
	printf 'ADVANCE=external\n'
	exit 0
fi

# Subject-line check first (cheap, local-only): any commit whose subject
# does NOT start with the autofix prefixes is unambiguously external —
# no API call needed.  Collect the self-like subjects for identity
# verification in a second pass.
advance_shas="$(git rev-list "${LOCAL_HEAD_SHA}..${REMOTE_TIP_SHA}" 2>/dev/null || true)"
if [ -z "${advance_shas}" ]; then
	log "check_external_branch_advance: empty advance set despite non-equal tips (${LOCAL_HEAD_SHA} vs ${REMOTE_TIP_SHA}); fail-open."
	printf 'ADVANCE=unknown\n'
	exit 0
fi

advance_count=0
self_subject_shas=""
while IFS= read -r sha; do
	[ -n "${sha}" ] || continue
	advance_count=$((advance_count + 1))
	subject="$(git log -1 --pretty=%s "${sha}" 2>/dev/null || printf '')"
	if [ -z "${subject}" ]; then
		log "::warning::check_external_branch_advance: unable to read subject for ${sha}; fail-open."
		printf 'ADVANCE=unknown\n'
		exit 0
	fi
	case "${subject}" in
		"[ai-autofix]"*|"[ai-merge-resolve]"*)
			self_subject_shas+="${sha} "
			;;
		*)
			log "check_external_branch_advance: external commit ${sha} subject=${subject}"
			printf 'ADVANCE=external\n'
			exit 0
			;;
	esac
done <<< "${advance_shas}"

# All advancing commits have self-like subjects.  The subject is
# spoofable (probably_unnecessary_but_read_if_stuck.md §20.1 identity guard), so verify each commit's
# GitHub-attributed author/committer login against AUTOFIX_BOT_LOGIN
# before concluding self_only.
if [ -z "${REPOSITORY}" ]; then
	log "::warning::check_external_branch_advance: GITHUB_REPOSITORY unset and could not be derived; cannot verify identity; fail-open."
	printf 'ADVANCE=unknown\n'
	exit 0
fi

bot_login_lc="$(printf '%s' "${BOT_LOGIN}" | tr '[:upper:]' '[:lower:]')"

# The commit set is usually tiny (0-2 SHAs) and there is no existing
# batched helper for commit-attribution lookups, so per-commit calls are
# acceptable here. gh_retry keeps the rare API path rate-limit aware.
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

for sha in ${self_subject_shas}; do
	meta=""
	if ! meta="$(gh_retry gh api "repos/${REPOSITORY}/commits/${sha}" \
		--jq '[(.author.login // ""), (.committer.login // "")] | @tsv' \
		2>/dev/null)"; then
		log "::warning::check_external_branch_advance: gh api commits/${sha} failed; fail-open."
		printf 'ADVANCE=unknown\n'
		exit 0
	fi
	author_login_lc="$(printf '%s' "${meta}" | cut -f1 | tr '[:upper:]' '[:lower:]')"
	committer_login_lc="$(printf '%s' "${meta}" | cut -f2 | tr '[:upper:]' '[:lower:]')"
	if [ -z "${author_login_lc}" ] && [ -z "${committer_login_lc}" ]; then
		log "check_external_branch_advance: ${sha} has self-like subject but no GitHub-attributed identity (both logins empty); fail-open."
		printf 'ADVANCE=unknown\n'
		exit 0
	fi
	if [ "${author_login_lc}" != "${bot_login_lc}" ] \
		&& [ "${committer_login_lc}" != "${bot_login_lc}" ]; then
		log "check_external_branch_advance: ${sha} subject matches autofix prefix but identity author=${author_login_lc} committer=${committer_login_lc} != bot=${bot_login_lc}; treating as external (spoof defence)."
		printf 'ADVANCE=external\n'
		exit 0
	fi
done

log "check_external_branch_advance: ${advance_count} advancing commit(s) all attributed to ${BOT_LOGIN}; self_only."
printf 'ADVANCE=self_only\n'
exit 0
