#!/usr/bin/env bash
# Body of the "Detect merge conflicts" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

echo "Checking for merge conflicts"
echo "MERGE_CONFLICT=false" >> "$GITHUB_ENV"
echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"

# git merge requires a clean working tree.  Save untracked dirs
# (like scripts/, prompts/) so they survive into the
# conflict-resolver step instead of being destroyed by git clean.
UNTRACKED_BACKUP="$(mktemp -d)"
for d in scripts prompts; do
  if [ -d "${d}" ]; then
    cp -a "${d}" "${UNTRACKED_BACKUP}/${d}"
  fi
done
# Explicitly remove known CI-generated files before reset/clean.
# In consumer repos these files are fetched at runtime (untracked);
# git reset --hard does not remove untracked files, so they linger
# and cause "Untracked working tree file would be overwritten" when
# git merge later tries to write the same path from origin/BASE_BRANCH.
# In the source repo these files are tracked, so git reset --hard HEAD
# will restore them after this removal — no data loss occurs.
rm -f scripts/ai_memory.py scripts/ai_memory_lib.py scripts/memory_helpers.sh scripts/openrouter_prompt_cache.py scripts/cost_audit.py scripts/codex_heartbeat.sh scripts/review_run_reviewers.sh scripts/review_apply_fixes.sh scripts/review_rb_judge.sh scripts/pr_checks_lib.sh scripts/summarize_reviewer_consensus.sh scripts/check_external_branch_advance.sh 2>/dev/null || true
git reset --hard HEAD
# Use -ffdx: double-force clears nested git repos (-ff), and -x also
# removes files matching .gitignore so nothing
# ignored can survive to block the upcoming merge simulation.
git clean -ffdx -e .codex-workflow-src -e .codex-workflow-src-main

# The scripts may have been removed during artifact cleanup in the
# commit step (caller repos delete fetched scripts before committing)
# or wiped by git clean -ffdx above.
# Re-stage any missing scripts from checked-out support source.
for f in gh_helpers.sh pr_checks_lib.sh git_ref_health_check.sh tg_helpers.sh label_helpers.sh cost_audit.py codex_helpers.sh codex_heartbeat.sh review_run_reviewers.sh review_apply_fixes.sh review_rb_judge.sh check_workflow_script_refs.py check_resolver_diff.sh post_agent_workspace_guard.py summarize_reviewer_consensus.sh watchdog_helpers.sh check_external_branch_advance.sh workspace_safety_check.sh; do
  if [ ! -f "${SUPPORT_SCRIPTS_DIR}/${f}" ]; then
    mkdir -p "${SUPPORT_SCRIPTS_DIR}"
    src="${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/${f}"
    if [ ! -f "${src}" ] && [ -f "${GITHUB_WORKSPACE}/.codex-workflow-src-main/scripts/${f}" ]; then
      src="${GITHUB_WORKSPACE}/.codex-workflow-src-main/scripts/${f}"
    fi
    if [ ! -f "${src}" ]; then
      echo "::warning::${f} not found in checked-out support source; downstream steps that need it will fail with a specific error."
      rm -f "${SUPPORT_SCRIPTS_DIR}/${f}"
      continue
    fi
    install -m 0755 "${src}" "${SUPPORT_SCRIPTS_DIR}/${f}"
  fi
done
bash "${SUPPORT_SCRIPTS_DIR}/git_ref_health_check.sh" repair
git fetch --no-tags --prune origin "+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}"

# Ensure the merge-base between HEAD and origin/<base> is reachable
# in the local clone before invoking `git merge --no-commit` below.
# The codex-agent's "Checkout repo" step uses fetch-depth: 100 to
# bound clone size on history-heavy consumer repos.  On long-running
# PRs (e.g. orchestrator integration branches that have lived through
# many base-branch advances) the merge-base may sit deeper than the
# shallow boundary.  Without this guard, `git merge origin/<base>`
# would fail with "refusing to merge unrelated histories" — which
# the existing handler below would surface as a (misleading)
# force-push / orphan-root error requiring manual repair.  Deepen
# iteratively, falling back to --unshallow as a last resort.
# Fail-open: if the deepen path fails, the merge precheck below
# will still run and its existing error handling will catch it.
#
# Refspec discipline: every `git fetch` here passes explicit
# refspecs limited to BASE_BRANCH + (when known) TARGET_BRANCH.
# Without them, `git fetch ... origin` defers to the default
# `remote.origin.fetch = +refs/heads/*:refs/remotes/origin/*`
# and would pull every remote branch on each deepen call —
# undoing the fetch-depth: 100 budget this PR is trying to
# enforce.  Either side of the merge-base reaching its full
# ancestor is sufficient for git merge-base to resolve, so
# deepening just these two refs is enough.
if [ -f "$(git rev-parse --git-dir)/shallow" ]; then
  _merge_base_refspecs=("+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}")
  if [ -n "${TARGET_BRANCH:-}" ]; then
    _merge_base_refspecs+=("+refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}")
  fi
  _merge_base_deepen_iter=0
  while [ "${_merge_base_deepen_iter}" -lt 4 ]; do
    if git merge-base "origin/${BASE_BRANCH}" HEAD >/dev/null 2>&1; then
      break
    fi
    _merge_base_deepen_iter=$((_merge_base_deepen_iter + 1))
    echo "merge-base origin/${BASE_BRANCH}…HEAD unreachable in shallow clone — deepening (attempt ${_merge_base_deepen_iter}/4) on refs: ${_merge_base_refspecs[*]}."
    if ! git fetch --no-tags --prune --deepen=200 origin "${_merge_base_refspecs[@]}" 2>/dev/null; then
      echo "::warning::git fetch --deepen=200 origin ${_merge_base_refspecs[*]} failed during merge-base deepen-on-miss; falling back to --unshallow on the same refs."
      git fetch --no-tags --prune --unshallow origin "${_merge_base_refspecs[@]}" 2>/dev/null || true
      break
    fi
  done
  if ! git merge-base "origin/${BASE_BRANCH}" HEAD >/dev/null 2>&1; then
    echo "::warning::merge-base origin/${BASE_BRANCH}…HEAD still unreachable after deepen attempts; trying final --unshallow on refs: ${_merge_base_refspecs[*]}."
    git fetch --no-tags --prune --unshallow origin "${_merge_base_refspecs[@]}" 2>/dev/null || true
  fi
fi

# Ensure committer identity is available — git merge (even with
# --no-commit) requires it when recording a merge result.  Earlier
# steps may set this, but after git clean / reset it can be lost if
# the runner has no global git config.
git config user.name  "codex-bot"        2>/dev/null || true
git config user.email "codex@users.noreply.github.com" 2>/dev/null || true

# Move untracked content out of the working tree so it doesn't
# collide with files coming from origin/BASE_BRANCH during the
# merge simulation.  "git merge" refuses to proceed when untracked
# files would be overwritten by the merge result.
#
# IMPORTANT: tracked files MUST remain in the working tree so that
# the index and working tree agree when git merge is invoked.
# Moving tracked files out of the tree causes
# "Entry … not uptodate. Cannot merge." errors and prevents
# git merge --abort from resetting cleanly afterward.
MERGE_STASH="$(mktemp -d)"
for d in scripts prompts ai-memory .codex-workflow-src .codex-workflow-src-main; do
  if [ -d "${d}" ]; then
    if git ls-files -- "${d}/" 2>/dev/null | grep -q .; then
      # Directory contains tracked files — move only the untracked
      # files individually so tracked files stay in place and the
      # index remains consistent with the working tree.
      # Preserve full relative-path structure to avoid collisions
      # between files with identical names in different subdirs.
      while IFS= read -r f; do
        [ -n "${f}" ] && [ -e "${f}" ] && {
          mkdir -p "${MERGE_STASH}/$(dirname "${f}")"
          mv "${f}" "${MERGE_STASH}/${f}"
        }
      done < <(git ls-files --others -- "${d}/")
    else
      # Directory is entirely untracked — safe to move as a whole.
      mv "${d}" "${MERGE_STASH}/${d}"
    fi
  fi
done

# Safety net: clean any remaining untracked files (including
# .gitignore'd ones) that the MERGE_STASH loop may have missed.
# git merge refuses to proceed when an untracked working-tree file
# would be overwritten; this ensures the tree is fully clean before
# the merge simulation.  Tracked files are never removed by git clean.
git clean -ffdx -e .codex-workflow-src -e .codex-workflow-src-main 2>/dev/null || true

# Guardrail diagnostics: emit working tree state before merge so any
# future untracked-file collision is immediately obvious in CI logs.
echo "=== pre-merge working tree state ==="
git status --porcelain 2>/dev/null || true
_pre_merge_untracked="$(git ls-files --others --exclude-standard 2>/dev/null || true)"
if [ -n "${_pre_merge_untracked}" ]; then
  echo "untracked files:"
  printf '%s\n' "${_pre_merge_untracked}" | head -20
fi
echo "====================================="

merge_exit=0
merge_promisor_fetch_failure_seen=false
merge_promisor_initial_stderr=""
merge_promisor_retry_stderr_matches=false
# Capture git merge stderr so the failure annotation can surface
# the real cause (e.g. "refusing to merge unrelated histories",
# "Untracked working tree file … would be overwritten by merge").
# The previous implementation hard-coded "untracked files or a
# corrupted working tree" in the ::error:: annotation, which was
# misleading whenever git's actual complaint was something else.
MERGE_STDERR_FILE="$(mktemp)"
trap 'rm -f "${MERGE_STDERR_FILE}"' EXIT
git merge --no-commit --no-ff "origin/${BASE_BRANCH}" 2> "${MERGE_STDERR_FILE}" || merge_exit=$?
# Echo captured stderr so it remains visible in the raw step log
# even when we wrap it in an annotation below.
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
# Without this the step misreads an infrastructure fetch failure as
# a merge outcome: it emits a hard ::error:: on an otherwise green
# run and sets MERGE_CONFLICT=true, firing the Codex resolver on a
# PR that may have no conflict at all — exactly the cascade this
# step's header comment warns about.
#
# Mirrors the pre-review merge-topology gate above and the merge
# replay in scripts/review_conflict_prepare.sh; the refspec list is
# rebuilt here rather than reusing _merge_base_refspecs, which is
# only defined inside the shallow-clone branch above and would be
# unset under `set -u` on a full clone.
if [ "${merge_exit}" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ] \
   && [ "${merge_promisor_fetch_failure_seen}" = "true" ]; then
  echo "::warning::Merge precheck hit a partial-clone promisor fetch failure; backfilling blobs and retrying the merge once."
  git reset --hard HEAD >/dev/null 2>&1 || true
  git config --unset-all remote.origin.partialclonefilter 2>/dev/null || true
  _merge_backfill_refspecs=("+refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH}")
  if [ -n "${TARGET_BRANCH:-}" ]; then
    _merge_backfill_refspecs+=("+refs/heads/${TARGET_BRANCH}:refs/remotes/origin/${TARGET_BRANCH}")
  fi
  _merge_refetch_rc=0
  _merge_refetch_stderr="$(git fetch --no-tags --prune --refetch origin "${_merge_backfill_refspecs[@]}" 2>&1 >/dev/null)" || _merge_refetch_rc=$?
  if [ "${_merge_refetch_rc}" -ne 0 ]; then
    _merge_refetch_stderr="$(printf '%s' "${_merge_refetch_stderr}" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    _merge_refetch_stderr="${_merge_refetch_stderr:-<git fetch produced no stderr>}"
    echo "::warning::blob-backfill refetch failed during merge precheck; retrying the merge anyway. git fetch stderr: ${_merge_refetch_stderr}"
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

# Restore the stashed dirs so later steps have them available.
for d in scripts prompts ai-memory .codex-workflow-src .codex-workflow-src-main; do
  if [ -d "${MERGE_STASH}/${d}" ]; then
    # Use cp + rm instead of mv to handle the case where the merge
    # already created the directory (e.g. scripts/ from main).
    cp -a "${MERGE_STASH}/${d}/." "${d}/" 2>/dev/null || cp -a "${MERGE_STASH}/${d}" "${d}"
  fi
done
rm -rf "${MERGE_STASH}"

# If the merge command itself failed (e.g. missing identity, corrupt
# refs, or untracked file collision) WITHOUT producing unmerged index
# entries, we must not silently report "no conflicts".  Treat an
# unexpected merge failure as a conflict so the resolver step can
# attempt a proper merge.
if [ -z "$(git ls-files --unmerged)" ]; then
  if [ "${merge_exit}" -ne 0 ] && [ ! -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
    # Flatten the captured stderr onto a single line for the
    # annotation (::error:: notations cannot span newlines).
    _merge_stderr_oneline="$(tr '\n' ' ' < "${MERGE_STDERR_FILE}" 2>/dev/null | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    _merge_stderr_oneline="${_merge_stderr_oneline:-<git merge produced no stderr>}"
    _merge_classifier_stderr_oneline="${_merge_stderr_oneline}"
    if [ "${merge_promisor_fetch_failure_seen}" = "true" ] && [ -n "${merge_promisor_initial_stderr}" ] \
       && [ "${merge_promisor_initial_stderr}" != "${_merge_stderr_oneline}" ]; then
      _merge_stderr_oneline="${_merge_stderr_oneline} | initial promisor stderr: ${merge_promisor_initial_stderr}"
    fi
    _merge_stderr_oneline="$(printf '%s' "${_merge_stderr_oneline}" | sed 's/%/%25/g; s/\r/%0D/g')"
    if grep -qi 'refusing to merge unrelated histories' "${MERGE_STDERR_FILE}" 2>/dev/null; then
      _head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
      _base_sha="$(git rev-parse --short "origin/${BASE_BRANCH}" 2>/dev/null || echo unknown)"
      echo "::error::Merge precheck failed (exit ${merge_exit}): HEAD (${_head_sha}) and origin/${BASE_BRANCH} (${_base_sha}) have no common ancestor. This PR's branch was likely force-pushed to an orphan root, or ${BASE_BRANCH} was force-pushed since this branch diverged. Manual repair required: rebase the PR branch onto a current ${BASE_BRANCH} ancestor, or recreate the branch from ${BASE_BRANCH} and re-apply the changes. git stderr: ${_merge_stderr_oneline}"
    elif [ "${merge_exit}" -eq 128 ]; then
      if [ "${merge_promisor_retry_stderr_matches}" = "true" ] \
         || { [ "${merge_promisor_fetch_failure_seen}" = "true" ] \
              && [ "${_merge_classifier_stderr_oneline}" = "<git merge produced no stderr>" ]; }; then
        # A partial-clone promisor fetch failure that survived the
        # blob-backfill retry above is an infrastructure error, not
        # a merge outcome. Use the retry probe's own promisor
        # signature when it is still present; fall back to the
        # initial probe only when the retry produced no stderr at
        # all. Any other retry-side exit-128 cause keeps the hard
        # error path below. Downgrade to a ::warning:: so the run's
        # annotations stay honest, and fail OPEN into the resolver
        # path with MERGE_CONFLICT=true:
        # scripts/review_conflict_prepare.sh runs its own merge
        # replay with its own blob backfill and clears
        # MERGE_CONFLICT when that replay finds nothing to resolve.
        echo "::warning::Merge precheck: exit 128 from a partial-clone promisor fetch failure that survived the blob-backfill retry. Failing open to the conflict-resolver path, which re-runs the merge with its own backfill. git stderr: ${_merge_stderr_oneline}"
      else
        echo "::error::Merge precheck failed (exit 128): git merge aborted before entering merge state. See pre-merge diagnostics above. git stderr: ${_merge_stderr_oneline}"
      fi
    else
      echo "::warning::git merge exited ${merge_exit} without entering merge state — treating as conflict. git stderr: ${_merge_stderr_oneline}"
    fi
    rm -f "${MERGE_STDERR_FILE}"
    echo "Merge conflict detected (merge command failed)"
    echo "MERGE_CONFLICT=true" >> "$GITHUB_ENV"
    for d in scripts prompts; do
      if [ -d "${UNTRACKED_BACKUP}/${d}" ]; then
        cp -a "${UNTRACKED_BACKUP}/${d}" .
      fi
    done
    rm -rf "${UNTRACKED_BACKUP}"
    exit 0
  fi
  echo "No merge conflicts detected"
  # Only abort if a merge is actually in progress (MERGE_HEAD exists).
  # When the branch is already up-to-date, git merge succeeds immediately
  # without entering merge state, so there is nothing to abort.
  # Cleanup is best-effort: merge --abort and reset can fail when
  # the index is in an inconsistent state.  Use || true so cleanup
  # never hard-fails the job.
  if [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
    git merge --abort || true
  fi
  git reset --hard HEAD || true
  git clean -fd || true
  rm -rf "${UNTRACKED_BACKUP}"
  rm -f "${MERGE_STDERR_FILE}"
  exit 0
else
  echo "Merge conflict detected"
  echo "MERGE_CONFLICT=true" >> "$GITHUB_ENV"
  # Conflict path is non-fatal: abort the simulated merge so the
  # working tree is clean for the conflict-resolver step, then
  # restore env dirs and exit 0.  The job must not fail here —
  # human review or the resolver step handles the actual conflict.
  # Cleanup is best-effort; || true prevents exit 128 from a
  # failed abort/reset from surfacing as a job failure.
  if [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
    git merge --abort || true
  fi
  # Restore untracked dirs so the conflict resolver's codex exec
  # has its full environment (scripts/, prompts/).
  for d in scripts prompts; do
    if [ -d "${UNTRACKED_BACKUP}/${d}" ]; then
      cp -a "${UNTRACKED_BACKUP}/${d}" .
    fi
  done
fi
rm -rf "${UNTRACKED_BACKUP}"
rm -f "${MERGE_STDERR_FILE}"
