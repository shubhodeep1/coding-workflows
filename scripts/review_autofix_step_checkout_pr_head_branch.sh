#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Checkout PR head branch" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under the 480,000-byte workflow size guard (GitHub never starts runs for a
# workflow file over 512,000 bytes). The step sources this file in its own
# shell, so the step's name: and env: stay in the workflow; edit those there.
# GitHub does not substitute workflow expressions in a sourced script, so the
# step passes github.repository in as REVIEW_HEAD_CHECKOUT_REPOSITORY. An unset
# or empty value falls back to the runner's GITHUB_REPOSITORY; only when both
# are empty does the guard fail closed (head repo treated as a fork,
# CAN_PUSH=false).
set -euo pipefail

# REVIEW_HEAD_CHECKOUT diagnostics (analysis/workflow-optimization-2026-10-08.md
# Speed #1, Reliability #2): the EXIT trap below prints exactly one
#   REVIEW_HEAD_CHECKOUT stage=<code> outcome=<ok|skipped_stale|skipped_no_push|failed>
#   duration_ms=<n> fetch_ms=<n|na> switch_ms=<n|na> fetch_attempts=<n>
#   git_exit_class=<none|transport_5xx|network|auth|not_found|other>
#   head_match=<true|false|na> exit_code=<n>
# line per step run, so a slow or failed run shows whether the guards, the
# ref-health repair, the fetch or the branch switch ended it. Purely additive:
# the retry count, backoff, guards, exit codes and $GITHUB_ENV writes are
# unchanged. The trap never calls exit (bash keeps the original status) and
# every statement in it runs in an `|| true` context. Values are integers or
# enum tokens only; git stderr text never reaches the summary line. The step
# sources this file, so the trap fires once when the step shell exits; that
# covers explicit exits, `set -e` aborts and normal completion.
REVIEW_HEAD_CHECKOUT_STAGE="pr_metadata"
REVIEW_HEAD_CHECKOUT_FETCH_ATTEMPTS=0
REVIEW_HEAD_CHECKOUT_GIT_EXIT_CLASS="none"
REVIEW_HEAD_CHECKOUT_HEAD_MATCH="na"
REVIEW_HEAD_CHECKOUT_FETCH_START_MS=""
REVIEW_HEAD_CHECKOUT_FETCH_END_MS=""
REVIEW_HEAD_CHECKOUT_SWITCH_START_MS=""
REVIEW_HEAD_CHECKOUT_SWITCH_END_MS=""
REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE=""
REVIEW_HEAD_CHECKOUT_EMITTED=false

review_head_checkout_now_ms()
{
  local review_head_checkout_now_value="" review_head_checkout_now_realtime="${EPOCHREALTIME:-}"
  # Bash's own clock first: on ubuntu-26.04 runners `date +%s%3N` (Rust
  # coreutils) gave readings that went backwards within a run, so fetch_ms /
  # switch_ms came out negative and dropped to `na`. Same clock order as
  # _orch_poll_cycle_now_ms in scripts/orchestrate_poll_process.sh; `date`
  # stays as the fallback for bash < 5.
  if [[ "${review_head_checkout_now_realtime}" =~ ^([0-9]+)[.,]([0-9]{6})$ ]]; then
    printf '%s' "$(( 10#${BASH_REMATCH[1]} * 1000 + 10#${BASH_REMATCH[2]} / 1000 ))"
    return 0
  fi
  review_head_checkout_now_value="$(date +%s%3N 2>/dev/null || true)"
  if [[ "${review_head_checkout_now_value}" =~ ^[0-9]{13,}$ ]]; then
    printf '%s' "${review_head_checkout_now_value}"
    return 0
  fi
  review_head_checkout_now_value="$(date +%s 2>/dev/null || true)"
  if [[ "${review_head_checkout_now_value}" =~ ^[0-9]+$ ]]; then
    printf '%s' "$(( review_head_checkout_now_value * 1000 ))"
  fi
  return 0
}

# Prints one class token for the captured stderr of the last failed fetch.
# Fixed patterns only; the stderr text itself is never printed.
review_head_checkout_classify_fetch_stderr()
{
  local review_head_checkout_classify_file="${1:-}"
  if [ -z "${review_head_checkout_classify_file}" ] || [ ! -r "${review_head_checkout_classify_file}" ]; then
    printf 'other'
    return 0
  fi
  if grep -Eiq '(HTTP|error:|status)[ :]*5[0-9]{2}|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out' "${review_head_checkout_classify_file}" 2>/dev/null; then
    printf 'transport_5xx'
  elif grep -Eiq 'Authentication failed|could not read (Username|Password)|terminal prompts disabled|Permission denied|(HTTP|error:)[ :]*40[13]' "${review_head_checkout_classify_file}" 2>/dev/null; then
    printf 'auth'
  elif grep -Eiq "couldn't find remote ref|Repository not found|does not appear to be a git repository|(HTTP|error:)[ :]*404" "${review_head_checkout_classify_file}" 2>/dev/null; then
    printf 'not_found'
  elif grep -Eiq 'Could not resolve host|Connection (reset|refused|timed out)|Operation timed out|Failed to connect|early EOF|unexpected disconnect|RPC failed|Recv failure|Empty reply|(^|[^A-Za-z])(SSL|TLS)([^A-Za-z]|$)' "${review_head_checkout_classify_file}" 2>/dev/null; then
    printf 'network'
  else
    printf 'other'
  fi
  return 0
}

review_head_checkout_elapsed_ms()
{
  # Args: start_ms end_ms now_ms. Prints END-START, NOW-START when the
  # operation was interrupted, or na when it never started.
  local review_head_checkout_elapsed_start="${1:-}" review_head_checkout_elapsed_end="${2:-}" review_head_checkout_elapsed_now="${3:-}"
  if ! [[ "${review_head_checkout_elapsed_start}" =~ ^[0-9]+$ ]]; then
    printf 'na'
  elif [[ "${review_head_checkout_elapsed_end}" =~ ^[0-9]+$ ]]; then
    printf '%s' "$(( review_head_checkout_elapsed_end - review_head_checkout_elapsed_start ))"
  elif [[ "${review_head_checkout_elapsed_now}" =~ ^[0-9]+$ ]]; then
    printf '%s' "$(( review_head_checkout_elapsed_now - review_head_checkout_elapsed_start ))"
  else
    printf 'na'
  fi
  return 0
}

review_head_checkout_on_exit()
{
  local review_head_checkout_exit_rc="${1:-}" review_head_checkout_exit_now="" review_head_checkout_exit_duration=0
  local review_head_checkout_exit_fetch_ms="na" review_head_checkout_exit_switch_ms="na" review_head_checkout_exit_outcome="ok"
  local review_head_checkout_exit_stage="${REVIEW_HEAD_CHECKOUT_STAGE:-other}" review_head_checkout_exit_attempts="${REVIEW_HEAD_CHECKOUT_FETCH_ATTEMPTS:-0}"
  local review_head_checkout_exit_class="${REVIEW_HEAD_CHECKOUT_GIT_EXIT_CLASS:-other}" review_head_checkout_exit_match="${REVIEW_HEAD_CHECKOUT_HEAD_MATCH:-na}"
  # Subshells never emit; the step shell emits exactly once.
  if [ "${REVIEW_HEAD_CHECKOUT_EMITTED:-false}" = "true" ] || [ "${BASHPID:-$$}" != "$$" ]; then
    return 0
  fi
  REVIEW_HEAD_CHECKOUT_EMITTED=true
  review_head_checkout_exit_now="$(review_head_checkout_now_ms 2>/dev/null || true)"
  if [[ "${review_head_checkout_exit_now}" =~ ^[0-9]+$ ]] && [[ "${REVIEW_HEAD_CHECKOUT_START_MS:-}" =~ ^[0-9]+$ ]]; then
    review_head_checkout_exit_duration="$(( review_head_checkout_exit_now - REVIEW_HEAD_CHECKOUT_START_MS ))"
  fi
  review_head_checkout_exit_fetch_ms="$(review_head_checkout_elapsed_ms "${REVIEW_HEAD_CHECKOUT_FETCH_START_MS:-}" "${REVIEW_HEAD_CHECKOUT_FETCH_END_MS:-}" "${review_head_checkout_exit_now}" 2>/dev/null || true)"
  review_head_checkout_exit_switch_ms="$(review_head_checkout_elapsed_ms "${REVIEW_HEAD_CHECKOUT_SWITCH_START_MS:-}" "${REVIEW_HEAD_CHECKOUT_SWITCH_END_MS:-}" "${review_head_checkout_exit_now}" 2>/dev/null || true)"
  case "${review_head_checkout_exit_stage}" in
    pr_metadata|head_match_guard|workspace_guard|opencode_guard|head_ref_invalid|head_repo_foreign|ref_health_repair|fetch|branch_switch|head_moved_guard|publish_env|done) ;;
    *) review_head_checkout_exit_stage="other" ;;
  esac
  if ! [[ "${review_head_checkout_exit_rc}" =~ ^[0-9]+$ ]]; then
    review_head_checkout_exit_rc="na"
    review_head_checkout_exit_outcome="failed"
  elif [ "${review_head_checkout_exit_rc}" != "0" ]; then
    review_head_checkout_exit_outcome="failed"
  else
    case "${review_head_checkout_exit_stage}" in
      head_match_guard|head_moved_guard) review_head_checkout_exit_outcome="skipped_stale" ;;
      head_ref_invalid|head_repo_foreign) review_head_checkout_exit_outcome="skipped_no_push" ;;
      *) review_head_checkout_exit_outcome="ok" ;;
    esac
  fi
  [[ "${review_head_checkout_exit_duration}" =~ ^[0-9]+$ ]] || review_head_checkout_exit_duration=0
  [[ "${review_head_checkout_exit_fetch_ms}" =~ ^[0-9]+$ ]] || review_head_checkout_exit_fetch_ms="na"
  [[ "${review_head_checkout_exit_switch_ms}" =~ ^[0-9]+$ ]] || review_head_checkout_exit_switch_ms="na"
  [[ "${review_head_checkout_exit_attempts}" =~ ^[0-9]+$ ]] || review_head_checkout_exit_attempts=0
  case "${review_head_checkout_exit_class}" in
    none|transport_5xx|network|auth|not_found|other) ;;
    *) review_head_checkout_exit_class="other" ;;
  esac
  case "${review_head_checkout_exit_match}" in
    true|false|na) ;;
    *) review_head_checkout_exit_match="na" ;;
  esac
  if [ -n "${REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE:-}" ]; then
    rm -f "${REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE}" 2>/dev/null || true
  fi
  echo "REVIEW_HEAD_CHECKOUT stage=${review_head_checkout_exit_stage} outcome=${review_head_checkout_exit_outcome} duration_ms=${review_head_checkout_exit_duration} fetch_ms=${review_head_checkout_exit_fetch_ms} switch_ms=${review_head_checkout_exit_switch_ms} fetch_attempts=${review_head_checkout_exit_attempts} git_exit_class=${review_head_checkout_exit_class} head_match=${review_head_checkout_exit_match} exit_code=${review_head_checkout_exit_rc}" 2>/dev/null || true
  return 0
}

REVIEW_HEAD_CHECKOUT_START_MS="$(review_head_checkout_now_ms 2>/dev/null || true)"
trap 'review_head_checkout_on_exit "$?" || true' EXIT

HEAD_REF="$(jq -r '.headRefName' "${PR_META_FILE}")"
HEAD_REPO="$(jq -r '.headRepoFullName' "${PR_META_FILE}")"

# Capture the commit actually checked out before any non-push exit.
# Live PR metadata may already point at a newer, unreviewed head.
INITIAL_HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"
echo "INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA}" >> "$GITHUB_ENV"

REVIEW_HEAD_CHECKOUT_STAGE="head_match_guard"
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
    REVIEW_HEAD_CHECKOUT_HEAD_MATCH=false
    echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
    exit 0
  fi
  REVIEW_HEAD_CHECKOUT_HEAD_MATCH=true
fi

REVIEW_HEAD_CHECKOUT_STAGE="workspace_guard"
if [ -z "${WORKSPACE_PATH:-}" ] || [ ! -d "${WORKSPACE_PATH}" ]; then
  echo "::error::Review workspace is unavailable; refusing to start agents."
  exit 1
fi
REVIEW_HEAD_CHECKOUT_STAGE="opencode_guard"
for path in .opencode opencode.json opencode.jsonc .opencode.json .opencode.jsonc; do
  if [ -e "${WORKSPACE_PATH}/${path}" ] || [ -L "${WORKSPACE_PATH}/${path}" ]; then
    echo "::error::Review workspace contains project OpenCode configuration; refusing to start agents."
    exit 1
  fi
done

REVIEW_HEAD_CHECKOUT_STAGE="head_ref_invalid"
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

REVIEW_HEAD_CHECKOUT_STAGE="head_repo_foreign"
if [ "${HEAD_REPO}" != "${REVIEW_HEAD_CHECKOUT_REPOSITORY:-${GITHUB_REPOSITORY:-}}" ]; then
  echo "PR head repo (${HEAD_REPO}) is not writable from this workflow."
  echo "CAN_PUSH=false" >> "$GITHUB_ENV"
  exit 0
fi

REVIEW_HEAD_CHECKOUT_STAGE="ref_health_repair"
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
REVIEW_HEAD_CHECKOUT_STAGE="fetch"
REVIEW_HEAD_CHECKOUT_FETCH_START_MS="$(review_head_checkout_now_ms 2>/dev/null || true)"
# Each attempt's git stderr is still streamed to the step log (via tee) and
# also kept in a run-local 0600 temp file, read only by the fixed-pattern
# classifier above and removed by the EXIT trap. When mktemp fails the
# original fetch command runs unchanged.
REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/review-head-checkout-fetch.XXXXXX" 2>/dev/null || true)"
fetch_attempt=1
fetch_max_attempts=4
fetch_backoff=2
while true; do
  REVIEW_HEAD_CHECKOUT_FETCH_ATTEMPTS="${fetch_attempt}"
  if [ -n "${REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE}" ] && [ -f "${REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE}" ]; then
    if { git fetch --no-tags --prune origin "+refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}" 2>&1 1>&3 3>&- | { tee "${REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE}" >&2 || true; } 3>&-; } 3>&1; then
      REVIEW_HEAD_CHECKOUT_FETCH_END_MS="$(review_head_checkout_now_ms 2>/dev/null || true)"
      break
    fi
    REVIEW_HEAD_CHECKOUT_GIT_EXIT_CLASS="$(review_head_checkout_classify_fetch_stderr "${REVIEW_HEAD_CHECKOUT_FETCH_STDERR_FILE}" 2>/dev/null || true)"
  else
    if git fetch --no-tags --prune origin "+refs/heads/${HEAD_REF}:refs/remotes/origin/${HEAD_REF}"; then
      REVIEW_HEAD_CHECKOUT_FETCH_END_MS="$(review_head_checkout_now_ms 2>/dev/null || true)"
      break
    fi
    REVIEW_HEAD_CHECKOUT_GIT_EXIT_CLASS="other"
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

REVIEW_HEAD_CHECKOUT_STAGE="branch_switch"
REVIEW_HEAD_CHECKOUT_SWITCH_START_MS="$(review_head_checkout_now_ms 2>/dev/null || true)"
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
REVIEW_HEAD_CHECKOUT_SWITCH_END_MS="$(review_head_checkout_now_ms 2>/dev/null || true)"

REVIEW_HEAD_CHECKOUT_STAGE="head_moved_guard"

if [[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]]; then
  review_branch_sha="$(git rev-parse HEAD 2>/dev/null)" || { echo "::error::Unable to read the fetched PR head."; exit 1; }
  if [ "${review_branch_sha}" != "${review_source_sha}" ]; then
    echo "::warning::PR head moved after source checkout; skipping review of the older file tree."
    REVIEW_HEAD_CHECKOUT_HEAD_MATCH=false
    echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
    exit 0
  fi
  REVIEW_HEAD_CHECKOUT_HEAD_MATCH=true
fi

REVIEW_HEAD_CHECKOUT_STAGE="publish_env"
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
REVIEW_HEAD_CHECKOUT_STAGE="done"
