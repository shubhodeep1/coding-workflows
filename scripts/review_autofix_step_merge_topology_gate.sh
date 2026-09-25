#!/usr/bin/env bash
# Body of the "Pre-review deterministic merge-topology gate" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

git_auth_header="$(printf 'x-access-token:%s' "${GH_TOKEN}" | base64 | tr -d '\n')"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Basic ${git_auth_header}"

if [ ! -x "${SUPPORT_SCRIPTS_DIR}/check_external_branch_advance.sh" ]; then
  echo "::warning::check_external_branch_advance.sh missing from support scripts; fail-open (skipping pre-review stale-base gate)."
else
  advance_out="$(bash "${SUPPORT_SCRIPTS_DIR}/check_external_branch_advance.sh" || printf 'ADVANCE=unknown\n')"
  advance_val="$(printf '%s\n' "${advance_out}" | grep -E '^ADVANCE=' | tail -n 1 | cut -d= -f2 || true)"
  advance_val="${advance_val:-unknown}"
  case "${advance_val}" in
    external)
      echo "AUTOFIX_PRE_REVIEW_STALE_BASE pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH} action=soft_exit"
      echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
      exit 0
      ;;
    self_only)
      echo "AUTOFIX_PRE_REVIEW_SELF_ADVANCE pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH} action=continue"
      ;;
    none)
      echo "AUTOFIX_PRE_REVIEW_BASE_FRESH pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH}"
      ;;
    *)
      echo "AUTOFIX_PRE_REVIEW_UNKNOWN pr=${PR_NUMBER} local_sha=${LOCAL_HEAD_SHA} target_branch=${TARGET_BRANCH} action=fail_open"
      ;;
  esac
fi

if [ ! -x "${SUPPORT_SCRIPTS_DIR}/git_ref_health_check.sh" ]; then
  echo "::warning::git_ref_health_check.sh missing from support scripts; fail-open (skipping pre-review merge-topology probe)."
  exit 0
fi

# Mirror the late merge-precheck cleanup so runtime-fetched helper
# files cannot trip an exit-128 pre-merge abort before reviewers run.
rm -f scripts/ai_memory.py scripts/ai_memory_lib.py scripts/memory_helpers.sh scripts/openrouter_prompt_cache.py scripts/cost_audit.py scripts/codex_heartbeat.sh scripts/review_run_reviewers.sh scripts/review_apply_fixes.sh scripts/review_rb_judge.sh scripts/pr_checks_lib.sh scripts/summarize_reviewer_consensus.sh scripts/check_external_branch_advance.sh 2>/dev/null || true
if ! git reset --hard HEAD; then
  echo "::warning::git reset --hard HEAD failed during pre-review merge-topology gate; fail-open to reviewer path."
  exit 0
fi
if ! git clean -ffdx -e .codex-workflow-src -e pre_assembled_static.txt; then
  echo "::warning::git clean -ffdx failed during pre-review merge-topology gate; fail-open to reviewer path."
  exit 0
fi

if ! bash "${SUPPORT_SCRIPTS_DIR}/git_ref_health_check.sh" repair; then
  echo "::warning::git_ref_health_check.sh repair failed during pre-review merge-topology gate; fail-open to reviewer path."
  exit 0
fi

if ! git fetch --no-tags --prune origin "+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}" 2>/dev/null; then
  echo "::warning::git fetch for origin/${BASE_BRANCH} failed during pre-review merge-topology gate; fail-open to reviewer path."
  exit 0
fi

merge_probe_ambiguous=false
_merge_refspecs=("+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}")
if [ -n "${TARGET_BRANCH:-}" ]; then
  _merge_refspecs+=("+refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}")
fi
if [ -f "$(git rev-parse --git-dir)/shallow" ]; then
  _merge_base_deepen_iter=0
  while [ "${_merge_base_deepen_iter}" -lt 4 ]; do
    if git merge-base "origin/${BASE_BRANCH}" HEAD >/dev/null 2>&1; then
      break
    fi
    _merge_base_deepen_iter=$((_merge_base_deepen_iter + 1))
    echo "pre-review merge-base origin/${BASE_BRANCH}…HEAD unreachable in shallow clone — deepening (attempt ${_merge_base_deepen_iter}/4) on refs: ${_merge_refspecs[*]}."
    if ! git fetch --no-tags --prune --deepen=200 origin "${_merge_refspecs[@]}" 2>/dev/null; then
      echo "::warning::git fetch --deepen=200 origin ${_merge_refspecs[*]} failed during pre-review gate; trying --unshallow and treating any structural merge failure as ambiguous if history stays incomplete."
      git fetch --no-tags --prune --unshallow origin "${_merge_refspecs[@]}" 2>/dev/null || true
      break
    fi
  done
  if ! git merge-base "origin/${BASE_BRANCH}" HEAD >/dev/null 2>&1; then
    echo "::warning::merge-base origin/${BASE_BRANCH}…HEAD still unreachable after pre-review deepen attempts; continuing to the merge probe, but any unrelated-histories result will fail-open as ambiguous."
    git fetch --no-tags --prune --unshallow origin "${_merge_refspecs[@]}" 2>/dev/null || true
    if ! git merge-base "origin/${BASE_BRANCH}" HEAD >/dev/null 2>&1; then
      if [ -f "$(git rev-parse --git-dir)/shallow" ]; then
        merge_probe_ambiguous=true
      fi
    fi
  fi
fi

git config user.name "codex-bot" 2>/dev/null || true
git config user.email "codex@users.noreply.github.com" 2>/dev/null || true

MERGE_STASH="$(mktemp -d 2>/dev/null || printf '')"
if [ -z "${MERGE_STASH}" ] || [ ! -d "${MERGE_STASH}" ]; then
  echo "::warning::mktemp -d failed during pre-review merge-topology gate; fail-open to reviewer path."
  exit 0
fi
MERGE_STDERR_FILE="$(mktemp 2>/dev/null || printf '')"
if [ -z "${MERGE_STDERR_FILE}" ] || [ ! -f "${MERGE_STDERR_FILE}" ]; then
  echo "::warning::mktemp failed for merge stderr capture during pre-review merge-topology gate; fail-open to reviewer path."
  rm -rf "${MERGE_STASH}" >/dev/null 2>&1 || true
  exit 0
fi
cleanup_pre_review_merge_probe() {
  local status="$1"
  trap - EXIT
  set +e
  if [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
    git merge --abort >/dev/null 2>&1 || true
  fi
  git reset --hard HEAD >/dev/null 2>&1 || true
  git clean -ffdx -e .codex-workflow-src -e pre_assembled_static.txt >/dev/null 2>&1 || true
  for d in scripts prompts ai-memory .codex-workflow-src; do
    if [ -d "${MERGE_STASH}/${d}" ]; then
      cp -a "${MERGE_STASH}/${d}/." "${d}/" 2>/dev/null || cp -a "${MERGE_STASH}/${d}" "${d}" 2>/dev/null || true
    fi
  done
  rm -rf "${MERGE_STASH}" >/dev/null 2>&1 || true
  rm -f "${MERGE_STDERR_FILE}" >/dev/null 2>&1 || true
  exit "${status}"
}
trap 'cleanup_pre_review_merge_probe $?' EXIT

for d in scripts prompts ai-memory .codex-workflow-src; do
  if [ -d "${d}" ]; then
    if git ls-files -- "${d}/" 2>/dev/null | grep -q .; then
      while IFS= read -r f; do
        [ -n "${f}" ] && [ -e "${f}" ] && {
          mkdir -p "${MERGE_STASH}/$(dirname "${f}")"
          mv "${f}" "${MERGE_STASH}/${f}"
        }
      done < <(git ls-files --others -- "${d}/")
    else
      mv "${d}" "${MERGE_STASH}/${d}"
    fi
  fi
done
git clean -ffdx -e .codex-workflow-src -e pre_assembled_static.txt 2>/dev/null || true

echo "=== pre-review merge working tree state ==="
git status --porcelain 2>/dev/null || true
_pre_review_untracked="$(git ls-files --others --exclude-standard 2>/dev/null || true)"
if [ -n "${_pre_review_untracked}" ]; then
  echo "untracked files:"
  printf '%s\n' "${_pre_review_untracked}" | head -20
fi
echo "========================================"

merge_exit=0
merge_promisor_fetch_failure_seen=false
merge_promisor_initial_stderr=""
merge_promisor_retry_stderr_matches=false
git merge --no-commit --no-ff "origin/${BASE_BRANCH}" 2> "${MERGE_STDERR_FILE}" || merge_exit=$?
if [ -s "${MERGE_STDERR_FILE}" ]; then
  sed 's/^/git merge stderr: /' "${MERGE_STDERR_FILE}"
fi
if grep -qiE 'promisor remote|not our ref|could not fetch|fetch-pack|remote error: upload-pack' "${MERGE_STDERR_FILE}" 2>/dev/null; then
  merge_promisor_fetch_failure_seen=true
  merge_promisor_initial_stderr="$(tr '\n' ' ' < "${MERGE_STDERR_FILE}" 2>/dev/null | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
  merge_promisor_initial_stderr="${merge_promisor_initial_stderr:-<git merge produced no stderr>}"
fi

# Blobless partial clone recovery (the codex-agent checkout uses
# `filter: blob:none`): the 3-way merge lazy-fetches blob content
# from the promisor remote, and that batched on-demand fetch fails
# hard (exit 128) when any single OID in the batch is unreachable
# server-side ("not our ref" / "could not fetch ... from promisor
# remote") — even though the merge itself is well-formed. On that
# signature, drop the partial filter, refetch the merge inputs so
# every blob is materialised locally, and retry the merge once.
# Without this the gate misreads a transient/infra fetch failure as
# a deterministic stale base and skips the conflict resolver.
if [ "${merge_exit}" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ] \
   && [ "${merge_promisor_fetch_failure_seen}" = "true" ]; then
  echo "::warning::Pre-review merge probe hit a partial-clone promisor fetch failure; backfilling blobs and retrying the merge once."
  git reset --hard HEAD >/dev/null 2>&1 || true
  git config --unset-all remote.origin.partialclonefilter 2>/dev/null || true
  _refetch_rc=0
  _refetch_stderr="$(git fetch --no-tags --prune --refetch origin "${_merge_refspecs[@]}" 2>&1 >/dev/null)" || _refetch_rc=$?
  if [ "${_refetch_rc}" -ne 0 ]; then
    _refetch_stderr="$(printf '%s' "${_refetch_stderr}" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    _refetch_stderr="${_refetch_stderr:-<git fetch produced no stderr>}"
    echo "::warning::blob-backfill refetch failed during pre-review merge gate; retrying the merge anyway. git fetch stderr: ${_refetch_stderr}"
  fi
  merge_exit=0
  merge_promisor_retry_stderr_matches=false
  : > "${MERGE_STDERR_FILE}"
  git merge --no-commit --no-ff "origin/${BASE_BRANCH}" 2> "${MERGE_STDERR_FILE}" || merge_exit=$?
  if [ -s "${MERGE_STDERR_FILE}" ]; then
    sed 's/^/git merge stderr (after blob backfill): /' "${MERGE_STDERR_FILE}"
  fi
  if grep -qiE 'promisor remote|not our ref|could not fetch|fetch-pack|remote error: upload-pack' "${MERGE_STDERR_FILE}" 2>/dev/null; then
    merge_promisor_retry_stderr_matches=true
  fi
fi

if [ -z "$(git ls-files --unmerged)" ]; then
  if [ "${merge_exit}" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
    _merge_stderr_oneline="$(tr '\n' ' ' < "${MERGE_STDERR_FILE}" 2>/dev/null | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    _merge_stderr_oneline="${_merge_stderr_oneline:-<git merge produced no stderr>}"
    _merge_classifier_stderr_oneline="${_merge_stderr_oneline}"
    if [ "${merge_promisor_fetch_failure_seen}" = "true" ] && [ -n "${merge_promisor_initial_stderr}" ] \
       && [ "${merge_promisor_initial_stderr}" != "${_merge_stderr_oneline}" ]; then
      _merge_stderr_oneline="${_merge_stderr_oneline} | initial promisor stderr: ${merge_promisor_initial_stderr}"
    fi
    _merge_stderr_oneline="$(printf '%s' "${_merge_stderr_oneline}" | sed 's/%/%25/g; s/\r/%0D/g')"
    if grep -qi 'refusing to merge unrelated histories' "${MERGE_STDERR_FILE}" 2>/dev/null; then
      if [ "${merge_probe_ambiguous}" = "true" ]; then
        echo "::warning::Pre-review merge topology gate saw 'refusing to merge unrelated histories' after ambiguous history preparation; continuing to reviewers. git stderr: ${_merge_stderr_oneline}"
        echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=unrelated_histories_ambiguous action=fail_open"
      else
        _head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        _base_sha="$(git rev-parse --short "origin/${BASE_BRANCH}" 2>/dev/null || echo unknown)"
        echo "::error::Pre-review merge topology gate failed: HEAD (${_head_sha}) and origin/${BASE_BRANCH} (${_base_sha}) have no common ancestor. Skipping reviewer/editor spend. git stderr: ${_merge_stderr_oneline}"
        echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=no_common_ancestor action=soft_exit"
        echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
        exit 0
      fi
    elif [ "${merge_exit}" -eq 128 ]; then
      if [ "${merge_promisor_retry_stderr_matches}" = "true" ] \
         || { [ "${merge_promisor_fetch_failure_seen}" = "true" ] \
              && [ "${_merge_classifier_stderr_oneline}" = "<git merge produced no stderr>" ]; }; then
        # A partial-clone promisor fetch failure that survived the
        # blob-backfill retry above is an infrastructure error, not
        # a deterministic stale base. Use the retry probe's own
        # promisor signature when it is still present; fall back to
        # the initial probe only when the retry produced no stderr
        # at all. Any other retry-side exit-128 cause keeps the hard
        # error path below. Fail OPEN so the reviewer +
        # conflict-resolver path still runs (the resolver runs its
        # own merge with its own backfill) rather than skipping it.
        echo "::warning::Pre-review merge topology gate: exit 128 from a partial-clone promisor fetch failure that survived the blob-backfill retry. Failing open to the reviewer/resolver path. git stderr: ${_merge_stderr_oneline}"
        echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=merge_precheck_promisor_fetch_failed action=fail_open"
      else
        echo "::error::Pre-review merge topology gate failed (exit 128): git merge aborted before entering merge state. Skipping reviewer/editor spend. See pre-review diagnostics above. git stderr: ${_merge_stderr_oneline}"
        echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=merge_precheck_failed_exit_128 action=soft_exit"
        echo "AUTOFIX_STALE_BASE_SKIP=true" >> "$GITHUB_ENV"
        exit 0
      fi
    else
      echo "::warning::Pre-review merge topology gate hit an inconclusive merge failure (exit ${merge_exit}); continuing to reviewers. git stderr: ${_merge_stderr_oneline}"
      echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=merge_failure_inconclusive_exit_${merge_exit} action=fail_open"
    fi
  else
    echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=clean_or_up_to_date action=continue"
  fi
else
  # Pre-review conflict resolution (default on): the base already
  # conflicts with this head, so a reviewer pass (35-96 min on the
  # 2026-09-07 runs) and an editor commit would only be redone
  # after the resolver merges the base in. Hand off to the
  # existing "Detect merge conflicts" -> resolver -> push ->
  # re-trigger tail now and skip the reviewer/editor phase; the
  # re-dispatched run reviews the merged head instead.
  _pre_review_unmerged="$(git ls-files --unmerged 2>/dev/null | awk '{print $4}' | sort -u | paste -sd, - || true)"
  case "$(printf '%s' "${PRE_REVIEW_CONFLICT_RESOLVE_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
      if [ "${CAN_PUSH:-false}" = "true" ]; then
        echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=content_conflict action=pre_review_resolve unmerged=${_pre_review_unmerged:-unknown}"
        echo "MERGE_CONFLICT=true" >> "$GITHUB_ENV"
        echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
        echo "AUTOFIX_PRE_REVIEW_RESOLVE=true" >> "$GITHUB_ENV"
        echo "AUTOFIX_PRE_REVIEW_RESOLVE_UNMERGED=${_pre_review_unmerged}" >> "$GITHUB_ENV"
      else
        echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=content_conflict action=continue reason=cannot_push unmerged=${_pre_review_unmerged:-unknown}"
      fi
      ;;
    *)
      echo "AUTOFIX_PRE_REVIEW_MERGE_TOPOLOGY pr=${PR_NUMBER} base_branch=${BASE_BRANCH} local_sha=${LOCAL_HEAD_SHA} result=content_conflict action=continue reason=pre_review_resolve_disabled unmerged=${_pre_review_unmerged:-unknown}"
      ;;
  esac
  unset _pre_review_unmerged
fi
