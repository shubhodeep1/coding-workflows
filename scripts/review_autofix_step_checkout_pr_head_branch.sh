#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Checkout PR head branch" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under the 480,000-byte workflow size guard (GitHub never starts runs for a
# workflow file over 512,000 bytes). The step sources this file in its own
# shell, so the step's name: and env: stay in the workflow; edit those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.repository in as REVIEW_HEAD_CHECKOUT_REPOSITORY; it
# defaults to the runner's GITHUB_REPOSITORY when unset (an empty value fails
# closed: the head repo is treated as a fork and CAN_PUSH=false).
set -euo pipefail

HEAD_REF="$(jq -r '.headRefName' "${PR_META_FILE}")"
HEAD_REPO="$(jq -r '.headRepoFullName' "${PR_META_FILE}")"

# Capture the commit actually checked out before any non-push exit.
# Live PR metadata may already point at a newer, unreviewed head.
INITIAL_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
echo "INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA}" >> "$GITHUB_ENV"

# GIT_DIR points at the source checkout, but GIT_WORK_TREE points at
# the split workspace. Check both the actual source tree commit and
# the metadata used for the reviewer diff before any early exit.
if [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
  review_source_sha="$(env -u GIT_DIR -u GIT_WORK_TREE git -C "${GITHUB_WORKSPACE}" rev-parse HEAD 2>/dev/null || true)"
  review_workspace_sha="$(git rev-parse HEAD 2>/dev/null || true)"
  review_metadata_sha="$(jq -r '.head.sha // ""' "${PR_PAYLOAD_FILE}" 2>/dev/null || true)"
  if ! [[ "${review_source_sha}" =~ ^[0-9a-f]{40}$ ]] \
    || [ "${review_source_sha}" != "${review_workspace_sha}" ] \
    || [ "${review_source_sha}" != "${review_metadata_sha}" ] \
    || [ "${review_source_sha}" != "${REVIEW_CHECKOUT_SHA}" ]; then
    echo "::warning::Review source, workspace, and PR metadata heads differ; skipping this review."
    echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
    exit 0
  fi
fi

if [ -z "${WORKSPACE_PATH:-}" ] || [ ! -d "${WORKSPACE_PATH}" ]; then
  echo "::error::Review workspace is unavailable; refusing to start agents."
  exit 1
fi
for path in .opencode opencode.json opencode.jsonc .opencode.json .opencode.jsonc; do
  if [ -e "${WORKSPACE_PATH}/${path}" ] || [ -L "${WORKSPACE_PATH}/${path}" ]; then
    echo "::error::Review workspace contains project OpenCode configuration; refusing to start agents."
    exit 1
  fi
done

if [ -z "${HEAD_REF}" ] || [ "${HEAD_REF}" = "null" ]; then
  echo "Unable to determine PR head branch"
  exit 1
fi

if ! [[ "${HEAD_REF}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "Rejecting PR head branch with unsupported characters: ${HEAD_REF}"
  echo "CAN_PUSH=false" >> "$GITHUB_ENV"
  exit 0
fi
if [[ "${HEAD_REF}" =~ (^|/)__pycache__(/|$) ]] || [[ "${HEAD_REF}" =~ \.pyc$ ]] || [[ "${HEAD_REF}" == */.* ]] || [[ "${HEAD_REF}" == .* ]] || [[ "${HEAD_REF}" == */ ]] || [[ "${HEAD_REF}" == /* ]] || [[ "${HEAD_REF}" == *..* ]] || [[ "${HEAD_REF}" == *//* ]] || [[ "${HEAD_REF}" == *@{* ]]; then
  echo "Rejecting PR head branch due to disallowed branch pattern: ${HEAD_REF}"
  echo "CAN_PUSH=false" >> "$GITHUB_ENV"
  exit 0
fi

if [ "${HEAD_REPO}" != "${REVIEW_HEAD_CHECKOUT_REPOSITORY:-${GITHUB_REPOSITORY:-}}" ]; then
  echo "PR head repo (${HEAD_REPO}) is not writable from this workflow."
  echo "CAN_PUSH=false" >> "$GITHUB_ENV"
  exit 0
fi

bash "${SUPPORT_SCRIPTS_DIR}/git_ref_health_check.sh" repair

CURRENT_BRANCH="$(git branch --show-current || true)"

# Retry git fetch against transient github.com errors (5xx,
# connection resets, DNS hiccups). A single ~minute-long
# GitHub-side blip otherwise kills a 15-minute autofix run
# with exit 128 (observed in run 25674867638, where two
# consecutive HTTPS calls returned "remote: Internal Server
# Error" / HTTP 500 within ~105s and the unguarded fetch
# failed the whole job). Backoff follows the project standard
# for git fetch/pull retries: 2s, 4s, 8s, 16s across 4
# attempts. Same shape as the existing push-retry fetch loop
# later in this file (search "fetch_attempt").
fetch_attempt=1
fetch_max_attempts=4
fetch_backoff=2
while true; do
  if git fetch --no-tags --prune origin "+refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}"; then
    break
  fi
  if [ "${fetch_attempt}" -ge "${fetch_max_attempts}" ]; then
    echo "::error::git fetch origin +refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF} failed after ${fetch_max_attempts} attempts; giving up."
    exit 1
  fi
  echo "::warning::git fetch origin +refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF} failed (attempt ${fetch_attempt}/${fetch_max_attempts}); retrying in ${fetch_backoff}s."
  sleep "${fetch_backoff}"
  fetch_attempt=$((fetch_attempt + 1))
  fetch_backoff=$((fetch_backoff * 2))
done

if [ "${CURRENT_BRANCH}" = "${HEAD_REF}" ]; then
  git reset --hard "refs/remotes/origin/${HEAD_REF}"
else
  # Use -f so the branch switch discards any runtime-local
  # modifications in the worktree, mirroring the `reset --hard`
  # in the sibling branch above. The "Initialize/Finalize
  # workspace contents" steps materialize the source tree into
  # this worktree and regenerate the gitignored runtime artifact
  # .ai/.workspace_source_manifest.txt (still tracked on older
  # branches). Without -f, a plain `git checkout -B` can abort on
  # older branches with "Your local changes
  # to the following files would be overwritten by checkout"
  # whenever the target branch's committed copy of that manifest
  # differs from the index's (e.g. a cross-base PR whose base is
  # `stable` while the worktree index is `main`). This step runs
  # before the editor touches the worktree, so the only local
  # changes here are regenerated runtime artifacts that are safe
  # to discard — landing cleanly on origin/${HEAD_REF} is exactly
  # the intent.
  git checkout -f -B "${HEAD_REF}" "refs/remotes/origin/${HEAD_REF}"
fi

if [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
  review_branch_sha="$(git rev-parse HEAD 2>/dev/null)" || { echo "::error::Unable to read the fetched PR head."; exit 1; }
  if [ "${review_branch_sha}" != "${review_source_sha}" ]; then
    echo "::warning::PR head moved after source checkout; skipping review of the older file tree."
    echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
    exit 0
  fi
fi

echo "CAN_PUSH=true" >> "$GITHUB_ENV"
echo "TARGET_BRANCH=${HEAD_REF}" >> "$GITHUB_ENV"
# Capture the SHA reviewers/editor will operate against so the
# mid-run stale-base gates (see probably_unnecessary_but_read_if_stuck.md §20.2) can detect an
# external push that happens between here and "Push all pending
# commits" ~30 minutes later.  Use the SHA AFTER the hard-reset
# above (not github.event.pull_request.head.sha, which may be
# stale if another run advanced the branch before this step ran).
INITIAL_HEAD_SHA="$(git rev-parse HEAD)"
echo "INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA}" >> "$GITHUB_ENV"
echo "Captured INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA} for stale-base detection."
