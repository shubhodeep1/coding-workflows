#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Push all pending commits" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under the 430,000-byte contract guard and the 480,000-byte workflow size
# guard (GitHub never starts runs for a workflow file over 512,000 bytes).
# The step sources this file in its own shell, so the step's name:, if: and
# env: stay in the workflow; edit those there. The body has no workflow
# expressions: every value comes in through the step's env:.
set -euo pipefail
export LANG=C

# Consumer-updater PRs (#7004): only update_workflows.yml writes these
# branches and only its verify job may merge them. A review push
# (editor, [ai-merge-resolve]) would land an unverified head.
if [[ "${TARGET_BRANCH:-}" =~ ^auto/update-workflows-[0-9a-f]{12}(-r[2-5])?$ ]]; then
  echo "AUTOFIX_PUSH_SKIPPED pr=${PR_NUMBER:-unknown} reason=updater_pr_verify_job_owns_branch"
  echo "DID_COMMIT=false" >> "$GITHUB_ENV"
  echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
  exit 0
fi

# Pre-push stale-base gate.  Soft-exit 0 if the remote tip
# advanced past INITIAL_HEAD_SHA with a non-autofix commit
# during the editor run — the synchronize event from that
# external push has already queued (or will queue) a fresh
# review_autofix run against the new tip, so pushing our
# locally-committed autofix on top would either lose the
# external change via merge or hard-fail at the merge-retry
# step below.  See probably_unnecessary_but_read_if_stuck.md §20.2.
if [ -x "${SUPPORT_SCRIPTS_DIR}/check_external_branch_advance.sh" ]; then
  pre_push_out="$(bash "${SUPPORT_SCRIPTS_DIR}/check_external_branch_advance.sh" || printf 'ADVANCE=unknown\n')"
  pre_push_val="$(printf '%s\n' "${pre_push_out}" | grep -E '^ADVANCE=' | tail -n 1 | cut -d= -f2 || true)"
  pre_push_val="${pre_push_val:-unknown}"
  case "${pre_push_val}" in
    external)
      echo "AUTOFIX_PRE_PUSH_STALE_BASE pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH} action=soft_exit"
      echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
      exit 0
      ;;
    self_only)
      echo "AUTOFIX_PRE_PUSH_SELF_ADVANCE pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH} action=continue"
      ;;
    none)
      echo "AUTOFIX_PRE_PUSH_BASE_FRESH pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH}"
      ;;
    *)
      echo "AUTOFIX_PRE_PUSH_UNKNOWN pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH} action=fail_open"
      ;;
  esac
else
  echo "::warning::check_external_branch_advance.sh missing from support scripts; fail-open (skipping pre-push gate)."
fi

git remote set-url origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}"
echo "Pushing pending commits to ${TARGET_BRANCH}..."

# Retry non-fast-forward rejections with fetch+merge.  A concurrent
# push to ${TARGET_BRANCH} (another workflow run, a human push, a
# sibling autofix job) during this ~30-minute job would otherwise
# cause an unrecoverable `! [rejected] (fetch first)` failure and
# lose the deferred autofix / [ai-merge-resolve] commit.  We merge
# the advanced remote tip into our local HEAD and retry up to 3
# times with exponential backoff.
#
# Why `git merge` and NOT `git rebase` here: the conflict-resolver
# step above (see the "NOTE: git is intentionally left in merge
# state" block around the `git commit -m "[ai-merge-resolve]…"`
# call) deliberately creates the ai-merge-resolve commit as a
# real two-parent merge commit.  That second parent is load-
# bearing — without it, git's merge-base walker rewinds to the
# original merge base on every subsequent `git merge` attempt
# and re-raises the identical conflict (observed on PR #908,
# which left the PR in mergeable_state=dirty).  A plain
# `git rebase` would flatten that merge commit into a single-
# parent commit and resurrect the exact bug PR #908 was designed
# to fix.  `git rebase --rebase-merges` would rewrite the merge
# commit SHA too.  `git merge` preserves the existing commit
# exactly and simply adds another merge commit on top when
# origin has genuinely advanced — never rewriting history and
# never clobbering other pushers' commits (no --force / no
# --force-with-lease on this path).  If the merge itself would
# introduce new conflicts we abort the merge and fail loudly —
# we must never silently resurrect conflict markers that the
# resolver just cleared.
max_push_attempts=3
push_attempt=1
push_backoff=2
# Transient failures (GitHub 5xx, `remote rejected ... (failed)`,
# network drops) get their own bounded counter: at most 2 retries
# with 5s/10s backoff. Before each retry the remote tip is read with
# `git ls-remote`; when it already equals HEAD the push landed.
# Repository-rule / protected-branch rejections are never retried.
# This path also never uses --force / --force-with-lease and never
# rebases; a remote that advanced meanwhile is handled by the
# non-fast-forward merge path in the loop below. Kill switch:
# AUTOFIX_PUSH_TRANSIENT_RETRY_ENABLED=false (any case).
push_transient_enabled="${AUTOFIX_PUSH_TRANSIENT_RETRY_ENABLED:-true}"
push_transient_max=2
push_transient_retry=0
push_stderr_file="$(mktemp)"
trap 'rm -f "${push_stderr_file}"' EXIT
while :; do
  push_exit=0
  # Capture BOTH stdout and stderr: `git push` normally emits
  # rejection details on stderr, but remote helpers and some
  # git versions can surface `! [rejected]` / `non-fast-forward`
  # on stdout, which would cause stderr-only classification to
  # mis-route a retryable failure to the hard-fail branch.
  git push origin "HEAD:${TARGET_BRANCH}" >"${push_stderr_file}" 2>&1 || push_exit=$?
  # The origin URL carries the token; redact URL userinfo before echoing.
  sed -E 's#(https?://)[^/@[:space:]]+@#\1***@#g' "${push_stderr_file}" >&2
  if [ "${push_exit}" -eq 0 ]; then
    echo "Push complete."
    echo "AUTOFIX_EDITS_PUSHED=true" >> "$GITHUB_ENV"
    break
  fi
  if grep -qiE 'GH006|GH013|protected branch|push declined|repository rule' "${push_stderr_file}"; then
    echo "::error::git push failed (exit ${push_exit}) for a reason other than [rejected] (fetch first/non-fast-forward); not retrying."
    exit "${push_exit}"
  fi
  if ! grep -qE '\[rejected\].*\((fetch first|non-fast-forward)\)' "${push_stderr_file}"; then
    push_transient_class=""
    if [ "${push_transient_enabled,,}" != "false" ]; then
      if grep -qiE 'Internal Server Error|The requested URL returned error: 5[0-9][0-9]|HTTP 5[0-9][0-9]' "${push_stderr_file}"; then
        push_transient_class="server_error"
      elif grep -qiE 'remote rejected.*\(failed\)' "${push_stderr_file}"; then
        push_transient_class="remote_rejected_failed"
      elif grep -qiE 'RPC failed|unexpected disconnect|early EOF|Connection reset|Could not resolve host|Operation timed out' "${push_stderr_file}"; then
        push_transient_class="network"
      fi
    fi
    if [ -n "${push_transient_class}" ]; then
      if [ "${push_transient_retry}" -ge "${push_transient_max}" ]; then
        # The last retry's push may have landed despite the error.
        if final_ls_out="$(timeout 60 git ls-remote origin "refs/heads/${TARGET_BRANCH}" 2>/dev/null)" \
          && [ -n "${final_ls_out}" ] \
          && [ "${final_ls_out%%[[:space:]]*}" = "$(git rev-parse HEAD)" ]; then
          echo "AUTOFIX_PUSH_TRANSIENT_RETRY pr=${PR_NUMBER:-unknown} attempt=${push_transient_retry}/${push_transient_max} class=${push_transient_class} outcome=landed"
          echo "Push complete."
          echo "AUTOFIX_EDITS_PUSHED=true" >> "$GITHUB_ENV"
          break
        fi
        echo "AUTOFIX_PUSH_TRANSIENT_RETRY pr=${PR_NUMBER:-unknown} attempt=${push_transient_retry}/${push_transient_max} class=${push_transient_class} outcome=gave_up"
        echo "::error::git push failed (exit ${push_exit}) with a transient error after ${push_transient_max} retries; giving up."
        exit "${push_exit}"
      fi
      push_transient_retry=$((push_transient_retry + 1))
      sleep "$((5 * push_transient_retry))"
      local_head="$(git rev-parse HEAD)"
      if ls_out="$(timeout 60 git ls-remote origin "refs/heads/${TARGET_BRANCH}" 2>/dev/null)"; then
        remote_tip="${ls_out%%[[:space:]]*}"
      else
        remote_tip=""
      fi
      if [ -n "${remote_tip}" ] && [ "${remote_tip}" = "${local_head}" ]; then
        echo "AUTOFIX_PUSH_TRANSIENT_RETRY pr=${PR_NUMBER:-unknown} attempt=${push_transient_retry}/${push_transient_max} class=${push_transient_class} outcome=landed"
        echo "Push complete."
        echo "AUTOFIX_EDITS_PUSHED=true" >> "$GITHUB_ENV"
        break
      fi
      echo "AUTOFIX_PUSH_TRANSIENT_RETRY pr=${PR_NUMBER:-unknown} attempt=${push_transient_retry}/${push_transient_max} class=${push_transient_class} outcome=retry"
      : > "${push_stderr_file}"
      continue
    fi
    echo "::error::git push failed (exit ${push_exit}) for a reason other than [rejected] (fetch first/non-fast-forward); not retrying."
    exit "${push_exit}"
  fi
  if [ "${push_attempt}" -ge "${max_push_attempts}" ]; then
    echo "::error::git push still rejected as non-fast-forward after ${max_push_attempts} attempts; giving up."
    exit 1
  fi
  echo "::warning::git push rejected as non-fast-forward (attempt ${push_attempt}/${max_push_attempts}); fetching ${TARGET_BRANCH} and merging before retry in ${push_backoff}s."
  sleep "${push_backoff}"
  push_backoff=$((push_backoff * 2))
  push_attempt=$((push_attempt + 1))
  fetch_attempt=1
  while ! git fetch origin "${TARGET_BRANCH}"; do
    if [ "${fetch_attempt}" -ge 2 ]; then
      echo "::error::git fetch origin ${TARGET_BRANCH} failed twice during push-retry; giving up to avoid pushing a stale tree."
      exit 1
    fi
    echo "::warning::git fetch origin ${TARGET_BRANCH} failed during push-retry (attempt ${fetch_attempt}/2); retrying in 2s."
    fetch_attempt=$((fetch_attempt + 1))
    sleep 2
  done
  if ! git merge --no-edit --no-ff "origin/${TARGET_BRANCH}"; then
    echo "::error::git merge origin/${TARGET_BRANCH} failed — merge would introduce new conflicts. Aborting merge and failing the job; refusing to silently resurrect conflict markers."
    git merge --abort || true
    exit 1
  fi
  : > "${push_stderr_file}"
done
( source "${SUPPORT_SCRIPTS_DIR:-}/gh_helpers.sh" && source "${SUPPORT_SCRIPTS_DIR:-}/pr_checks_lib.sh" && _pr_merge_resolve_marker_post "${PR_NUMBER:-}" "${LOCAL_HEAD_SHA:-}" ) || true
( source "${SUPPORT_SCRIPTS_DIR:-}/gh_helpers.sh" && gh_pr_cache_evict "${GITHUB_REPOSITORY}" "${PR_NUMBER:-}" ) >/dev/null 2>&1 || true
