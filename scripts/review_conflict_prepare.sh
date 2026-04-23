#!/usr/bin/env bash
# review_conflict_prepare.sh — prepare merge-conflict resolver prompt and
# pre-snapshot for review_autofix.yml.
#
# Extracted from the "Prepare merge-conflict resolver prompt and pre-snapshot"
# step of review_autofix.yml to keep the `run:` block under GitHub Actions'
# 21,000-char template-expression limit. Shares IS_WORKFLOW_SOURCE_REPO /
# MERGE_CONFLICT short-circuit with review_conflict_resolve.sh — any change
# to one must be mirrored in the other.
#
# Inputs (environment):
#   SUPPORT_SCRIPTS_DIR           Directory holding gh_helpers.sh.
#   SUPPORT_PROMPTS_DIR           Directory holding conflict-resolver.txt templates.
#   IS_WORKFLOW_SOURCE_REPO       "true" on the coding-workflows repo itself.
#   BASE_BRANCH                   Base branch for the merge replay.
#   TARGET_BRANCH / HEAD_REF      Used to detect orchestrator/project-* integration sync.
#   RUNTIME_DIR                   Ephemeral per-run directory.
#   GITHUB_REPOSITORY             owner/repo slug (auto-set).
#   CONFLICT_RESOLVER_PROMPT_FILE Path the rendered prompt is written to.
#
# Outputs:
#   $GITHUB_ENV: MERGE_CONFLICT (cleared on clean replay),
#                INTEGRATION_FINGERPRINTS_FILE, INTEGRATION_BRANCH_NAME,
#                INTEGRATION_TRACKING_NUM, IS_INTEGRATION_SYNC.
#   ${RUNTIME_DIR}/pre_resolver_state.tsv, conflicted_paths.txt,
#                  resolver_unmerged_allowlist.txt, integration_fingerprints.json.
#   ${CONFLICT_RESOLVER_PROMPT_FILE} rendered prompt text.
#
# Failure modes:
#   - Exits 1 if merge replay fails for non-conflict reasons, or template missing.
#   - Exits 0 + clears MERGE_CONFLICT when merge replay produces no unmerged paths.

set -euo pipefail
source "${SUPPORT_SCRIPTS_DIR}/gh_helpers.sh" 2>/dev/null || true
if ! command -v gh_retry >/dev/null 2>&1; then
  echo "::warning::gh_helpers.sh unavailable or incomplete; falling back to direct gh calls without retry helper."
  gh_retry() { "$@"; }
fi

echo "Running Codex resolver"

# Ensure committer identity is set (see "Detect merge conflicts" step).
git config user.name  "codex-bot"        2>/dev/null || true
git config user.email "codex@users.noreply.github.com" 2>/dev/null || true

# Start this step from a clean tracked-file state.  The "Detect
# merge conflicts" step's conflict branch only runs `git merge
# --abort`; it does NOT `git reset --hard HEAD`, so stale
# working-tree modifications from an earlier autofix iteration
# (e.g. to scripts/label_helpers.sh) can linger into this step.
# When they do, the `git merge --no-commit --no-ff` replay below
# fails before entering merge state with "Your local changes to
# the following files would be overwritten by merge", the `|| true`
# swallows the failure, and `git diff --name-only --diff-filter=U`
# returns zero paths — fooling the downstream resolver into
# running Codex against an empty conflict set and producing
# hallucinated edits.  Reset symmetrically with the no-conflict
# cleanup in the detect step.  Only tracked files are affected;
# the untracked workflow-fetched dirs handled by RESOLVE_STASH
# below are untouched by reset.
if [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
  git merge --abort 2>/dev/null || true
fi
git reset --hard HEAD 2>/dev/null || true

# Move workflow-fetched untracked dirs out of the way so they don't
# collide with files from origin/BASE_BRANCH during the merge.
# First, explicitly remove known CI-generated files that may be
# untracked in consumer repos (see "Detect merge conflicts" step).
# Skipped on the workflow source repo, where these paths are
# tracked source files — removing them here would leave them
# missing for the rest of this step because the consumer-repo
# restore block below (git reset/checkout on protected paths) is
# intentionally skipped when IS_WORKFLOW_SOURCE_REPO=true, and
# `git merge --no-commit` does not rewrite paths whose content
# is identical between HEAD and BASE.  Downstream
# check_workflow_script_refs.py then fails the [ai-merge-resolve]
# commit because the referenced scripts are missing from disk.
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ]; then
  rm -f scripts/ai_memory.py scripts/ai_memory_lib.py scripts/memory_helpers.sh scripts/openrouter_prompt_cache.py scripts/review_run_reviewers.sh scripts/review_apply_fixes.sh scripts/review_rb_judge.sh scripts/summarize_reviewer_consensus.sh 2>/dev/null || true
fi
RESOLVE_STASH="$(mktemp -d)"
for d in scripts .serena prompts ai-memory .codex-workflow-src .codex-workflow-src-main; do
  if [ -d "${d}" ]; then
    if git ls-files -- "${d}/" 2>/dev/null | grep -q .; then
      # Directory contains tracked files — move only the untracked
      # files individually so the index stays consistent with the
      # working tree (moving tracked files causes "Entry … not
      # uptodate. Cannot merge." errors).
      while IFS= read -r f; do
        [ -n "${f}" ] && [ -e "${f}" ] && {
          mkdir -p "${RESOLVE_STASH}/$(dirname "${f}")"
          mv "${f}" "${RESOLVE_STASH}/${f}"
        }
      done < <(git ls-files --others -- "${d}/")
    else
      # Directory is entirely untracked — safe to move as a whole.
      mv "${d}" "${RESOLVE_STASH}/${d}"
    fi
  fi
done

# Safety net: remove any remaining untracked files (including
# .gitignore'd ones) that the loop above may have missed, preventing
# "Untracked working tree file … would be overwritten by merge."
git clean -ffdx -e .codex-workflow-src -e .codex-workflow-src-main 2>/dev/null || true

# Capture the merge exit code explicitly rather than swallowing
# it with `|| true`.  We must distinguish:
#   exit 0   — merge succeeded (base advanced, no conflicts left)
#              or already-up-to-date
#   exit 1   — merge produced content conflicts (expected path)
#   exit 128 — merge aborted before entering merge state (dirty
#              tree, missing ref, corrupt index, etc.) — SHOULD
#              be impossible because we reset the tracked-file
#              state at the top of this step, but if it ever
#              happens we must surface it instead of silently
#              running Codex against an empty conflict set.
_merge_exit=0
# Capture stderr so the hard-fail annotation below can surface git's
# real complaint (e.g. "refusing to merge unrelated histories") instead
# of a generic "investigate" message.
_merge_stderr_file="$(mktemp)"
trap 'rm -f "${_merge_stderr_file}"' EXIT INT TERM
git merge --no-commit --no-ff "origin/${BASE_BRANCH}" 2> "${_merge_stderr_file}" || _merge_exit=$?
if [ -s "${_merge_stderr_file}" ]; then
  sed 's/^/git merge stderr: /' "${_merge_stderr_file}"
fi

# Capture the set of actually-unmerged paths produced by this
# merge replay.  This is the authoritative allowlist the Codex
# resolver is permitted to modify: the resolver prompt
# explicitly restricts edits to files containing conflict
# markers, so anything the editor touches outside this set is
# a prompt violation (and in the past has been a sign of
# hallucinated output — see the downstream workflow-file guard
# and PR #912 post-mortem).  Persist the list so the post-exec
# validation step can diff against it.
RESOLVER_ALLOWLIST_FILE="${RUNTIME_DIR}/resolver_unmerged_allowlist.txt"
git diff --name-only --diff-filter=U | sort -u > "${RESOLVER_ALLOWLIST_FILE}" || true
_resolver_allowlist_count="$(wc -l < "${RESOLVER_ALLOWLIST_FILE}" | tr -d '[:space:]')"
echo "Resolver allowlist (unmerged paths at merge replay): ${_resolver_allowlist_count} entries (git merge exit=${_merge_exit})"
if [ "${_resolver_allowlist_count}" -gt 0 ]; then
  sed 's/^/ - /' "${RESOLVER_ALLOWLIST_FILE}" || true
fi

# When the allowlist is empty there is nothing for Codex to
# resolve.  Running Codex against an empty conflict set is how
# past runs have produced hallucinated edits to placeholder
# markers inside prompts/conflict-resolver.txt — the only file
# in the tree whose content resembles conflict markers.  Two
# sub-cases, distinguished by the merge exit code:
#
#   1. _merge_exit == 0: the merge either succeeded cleanly
#      (base advanced and the conflict was resolved upstream
#      between the detect step and this step, or the merge
#      auto-resolved the textual conflict) or reported
#      "Already up-to-date".  In both cases there is nothing
#      real to resolve, even if MERGE_HEAD is still present
#      because --no-commit left the merge state around.  Abort
#      the in-progress merge to clear MERGE_HEAD, restore the
#      stashed workflow dirs so later steps still have their
#      environment, clear MERGE_CONFLICT so the second half of
#      this split step (and downstream gates keyed off it)
#      treat the conflict as no longer present, and exit 0.
#
#   2. _merge_exit != 0: the merge failed for a non-conflict
#      reason — dirty tree, missing ref, corrupt index, etc.
#      We must NOT clear MERGE_CONFLICT here: that would let
#      the workflow silently continue as if the conflict were
#      resolved.  Fail loudly with an error annotation and
#      dump diagnostics so the root cause is visible.
if [ "${_resolver_allowlist_count}" -eq 0 ]; then
  if [ "${_merge_exit}" -eq 0 ]; then
    echo "::warning::Merge replay produced no unmerged paths (git merge exit=0) — skipping Codex resolver (nothing to resolve)."
    if [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
      git merge --abort 2>/dev/null || true
    fi
    for d in scripts .serena prompts ai-memory .codex-workflow-src .codex-workflow-src-main; do
      if [ -d "${RESOLVE_STASH}/${d}" ]; then
        cp -a "${RESOLVE_STASH}/${d}/." "${d}/" 2>/dev/null || cp -a "${RESOLVE_STASH}/${d}" "${d}"
      fi
    done
    rm -rf "${RESOLVE_STASH}"
    rm -f "${_merge_stderr_file}"
    git reset --hard HEAD 2>/dev/null || true
    echo "MERGE_CONFLICT=false" >> "$GITHUB_ENV"
    exit 0
  fi
  # Flatten captured stderr onto one line for the ::error:: annotation,
  # and route "refusing to merge unrelated histories" to a dedicated
  # message that names the two SHAs so operators can repair the branch
  # without re-reading the raw log.
  _merge_stderr_oneline="$(tr '\n' ' ' < "${_merge_stderr_file}" 2>/dev/null | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
  _merge_stderr_oneline="${_merge_stderr_oneline:-<git merge produced no stderr>}"
  _merge_stderr_oneline="$(printf '%s' "${_merge_stderr_oneline}" | sed 's/%/%25/g; s/\r/%0D/g')"
  if grep -qi 'refusing to merge unrelated histories' "${_merge_stderr_file}" 2>/dev/null; then
    _head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    _base_sha="$(git rev-parse --short "origin/${BASE_BRANCH}" 2>/dev/null || echo unknown)"
    echo "::error::Merge replay failed (exit ${_merge_exit}): HEAD (${_head_sha}) and origin/${BASE_BRANCH} (${_base_sha}) have no common ancestor. This PR's branch was likely force-pushed to an orphan root, or ${BASE_BRANCH} was force-pushed since this branch diverged. Manual repair required: rebase the PR branch onto a current ${BASE_BRANCH} ancestor, or recreate the branch from ${BASE_BRANCH} and re-apply the changes. git stderr: ${_merge_stderr_oneline}"
  else
    echo "::error::Merge replay failed with exit ${_merge_exit} before producing any unmerged paths. The clean-tree reset at the top of this step did not yield a tree git could merge — investigate. git stderr: ${_merge_stderr_oneline}"
  fi
  git status --porcelain 2>/dev/null || true
  git ls-files --others --exclude-standard 2>/dev/null | head -20 || true
  rm -f "${_merge_stderr_file}"
  exit 1
fi
# The stderr file is no longer needed past this point — the happy path
# has unmerged entries, which the resolver handles regardless of stderr.
rm -f "${_merge_stderr_file}"

# Defensive: `git merge --no-commit` auto-stages merged hunks (and
# conflict-marked hunks) into the index for every path both sides
# touched, including protected paths (prompts/, .serena/,
# .github/scripts/, .github/prompts/, ai-memory/) when the consumer
# repo has leaked tracked copies of workflow runtime helpers from an
# older buggy run.  The downstream `git add -u -- ':!prompts' …`
# exclusion does NOT unstage already-indexed paths, so without this
# reset the merge commit would carry protected-path changes and
# trip the commit-gate guard, wasting the whole review run.
# Skipped on the workflow source repo itself, where these paths are
# real source code.
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ]; then
  git reset -q HEAD -- 'prompts' '.serena' '.github/scripts' '.github/prompts' 'ai-memory' '.codex-workflow-src' '.codex-workflow-src-main' 2>/dev/null || true
  git checkout -- 'prompts' '.serena' '.github/scripts' '.github/prompts' 'ai-memory' '.codex-workflow-src' 2>/dev/null || true
fi

# Restore stashed dirs so Codex has its full environment.
for d in scripts .serena prompts ai-memory .codex-workflow-src .codex-workflow-src-main; do
  if [ -d "${RESOLVE_STASH}/${d}" ]; then
    cp -a "${RESOLVE_STASH}/${d}/." "${d}/" 2>/dev/null || cp -a "${RESOLVE_STASH}/${d}" "${d}"
  fi
done
rm -rf "${RESOLVE_STASH}"

# Enumerate the unmerged paths git is currently tracking so the
# resolver prompt can name every conflicted file explicitly.  Without
# this enumeration Codex tends to discover and fix only one file per
# invocation, leaving the rest of the conflicts to subsequent
# workflow runs.  Listing them up-front and requiring all of them be
# resolved in a single pass turns the resolver back into a one-shot
# operation.
CONFLICTED_FILES_RAW="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
if [ -n "${CONFLICTED_FILES_RAW}" ]; then
  CONFLICTED_FILES_COUNT="$(printf '%s\n' "${CONFLICTED_FILES_RAW}" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
  CONFLICTED_FILES_LIST="$(printf '%s\n' "${CONFLICTED_FILES_RAW}" | sed '/^$/d; s/^/          - /')"
else
  CONFLICTED_FILES_COUNT="0"
  CONFLICTED_FILES_LIST="          (git did not report any unmerged paths; scan the working tree for conflict markers and resolve every file that contains them)"
fi
echo "Conflict resolver: ${CONFLICTED_FILES_COUNT} unmerged path(s) to resolve in this run."

# Render the conflict resolver prompt from the template file under
# prompts/. The template body used to live inline as a heredoc in
# this run: block, but its size pushed the whole step past GitHub
# Actions' 21,000-char template-expression limit, which caused the
# entire review_autofix.yml to fail parsing from internal-review.yml
# (silent — no check_runs, no error in Actions UI). Keeping the
# prompt in a separate file under prompts/ lets it grow without
# ever re-approaching the limit.
#
# Two templates exist:
#   * conflict-resolver.txt — the generic one used everywhere
#     except orchestrator integration branches.
#   * integration-sync-conflict-resolver.txt — used when the PR
#     head ref matches `orchestrator/project-*`. Adds merged
#     sub-issue intent + intent fingerprints so the resolver
#     does not silently revert a merged sub-issue's work
#     during a sync conflict resolution.
DEFAULT_PROMPT_TPL="${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt"
INTEGRATION_PROMPT_TPL="${SUPPORT_PROMPTS_DIR}/integration-sync-conflict-resolver.txt"
PROMPT_TPL="${DEFAULT_PROMPT_TPL}"
IS_INTEGRATION_SYNC="false"
INTEGRATION_TRACKING_NUM=""
INTEGRATION_TRACKING_TITLE=""
INTEGRATION_TRACKING_BODY=""
INTEGRATION_MERGED_SUB_ISSUES_LIST=""
INTEGRATION_MERGED_SUB_ISSUE_COUNT="0"
INTEGRATION_FINGERPRINTS_JSON="{}"
case "${TARGET_BRANCH:-${HEAD_REF:-}}" in
  orchestrator/project-*)
    IS_INTEGRATION_SYNC="true"
    INTEGRATION_TRACKING_NUM="${TARGET_BRANCH#orchestrator/project-}"
    if [ ! -f "${INTEGRATION_PROMPT_TPL}" ]; then
      echo "::warning::integration-sync-conflict-resolver template missing at ${INTEGRATION_PROMPT_TPL}; falling back to generic conflict-resolver template (intent-injection disabled for this run)."
      IS_INTEGRATION_SYNC="false"
    else
      PROMPT_TPL="${INTEGRATION_PROMPT_TPL}"
    fi
    ;;
esac
if [ ! -f "${PROMPT_TPL}" ]; then
  echo "::error::Conflict resolver prompt template missing: ${PROMPT_TPL}"
  exit 1
fi

# Pull the orchestrator state comment for this tracking issue so we
# can render merged sub-issue intent + fingerprints into the prompt.
# Fail-open: any failure here just leaves the integration variables
# blank and still renders the integration template (the resolver
# will see a placeholder note and behave like the generic resolver
# for those slots).
if [ "${IS_INTEGRATION_SYNC}" = "true" ] && [[ "${INTEGRATION_TRACKING_NUM}" =~ ^[0-9]+$ ]]; then
  _ti_json="$(gh_retry gh api -H 'Accept: application/vnd.github+json' \
    "repos/${GITHUB_REPOSITORY}/issues/${INTEGRATION_TRACKING_NUM}" 2>/dev/null || echo '{}')"
  INTEGRATION_TRACKING_TITLE="$(printf '%s' "${_ti_json}" | jq -r '.title // ""' 2>/dev/null || echo "")"
  INTEGRATION_TRACKING_BODY="$(printf '%s' "${_ti_json}" | jq -r '.body // ""' 2>/dev/null || echo "")"
  unset _ti_json

  _ti_comments_raw="$(mktemp)"
  if gh_retry gh api --paginate \
    "repos/${GITHUB_REPOSITORY}/issues/${INTEGRATION_TRACKING_NUM}/comments?per_page=100" \
    > "${_ti_comments_raw}" 2>/dev/null; then
    _state_payload="$(jq -s '
      ([.[][] | select(.body | contains("ORCHESTRATOR_STATE_V1"))] // [])
      | last // {}
      | .body // ""
      | capture("ORCHESTRATOR_STATE_V1\\n(?<json>(.|\\n)*)\\nORCHESTRATOR_STATE_V1")
      | .json // ""
    ' "${_ti_comments_raw}" 2>/dev/null || echo '""')"
    _state_json="$(printf '%s' "${_state_payload}" | jq -r '.' 2>/dev/null || echo "")"
    if [ -n "${_state_json}" ]; then
      # Build the merged sub-issues list (id : github_issue : status)
      INTEGRATION_MERGED_SUB_ISSUES_LIST="$(printf '%s' "${_state_json}" | jq -r '
        [
          .waves[]?.issues[]?
          | select(.status == "merged")
          | "          - " + (.id // "?") + " (issue #" + ((.github_issue // 0) | tostring) + ")"
        ] | join("\n")
      ' 2>/dev/null || echo "")"
      INTEGRATION_MERGED_SUB_ISSUE_COUNT="$(printf '%s' "${_state_json}" | jq -r '
        [.waves[]?.issues[]? | select(.status == "merged")] | length
      ' 2>/dev/null || echo "0")"
      INTEGRATION_FINGERPRINTS_JSON="$(printf '%s' "${_state_json}" | jq -c '
        .merged_issue_fingerprints // {}
      ' 2>/dev/null || echo "{}")"
    fi
    unset _state_payload _state_json
  fi
  rm -f "${_ti_comments_raw}"
  unset _ti_comments_raw

  if [ -z "${INTEGRATION_MERGED_SUB_ISSUES_LIST}" ]; then
    INTEGRATION_MERGED_SUB_ISSUES_LIST="          (no merged sub-issues recorded in tracking-issue state — this typically means the integration branch is empty or state is not yet seeded)"
  fi
  # Stash the (potentially large) fingerprints JSON in a temp file
  # and render it from that path below to avoid passing large
  # payloads via process environment variables.
  INTEGRATION_FINGERPRINTS_FILE="${RUNTIME_DIR}/integration_fingerprints.json"
  printf '%s' "${INTEGRATION_FINGERPRINTS_JSON}" > "${INTEGRATION_FINGERPRINTS_FILE}"
  # Persist the conflicted-paths-to-fingerprint mapping for the
  # post-resolve verification step below. The verification step is
  # gated on IS_INTEGRATION_SYNC == 'true' so this file only matters
  # in the integration-sync path.
  INTEGRATION_BRANCH_NAME="${TARGET_BRANCH}"
  echo "INTEGRATION_FINGERPRINTS_FILE=${INTEGRATION_FINGERPRINTS_FILE}" >> "$GITHUB_ENV"
  echo "INTEGRATION_BRANCH_NAME=${INTEGRATION_BRANCH_NAME}" >> "$GITHUB_ENV"
  echo "INTEGRATION_TRACKING_NUM=${INTEGRATION_TRACKING_NUM}" >> "$GITHUB_ENV"
  echo "IS_INTEGRATION_SYNC=true" >> "$GITHUB_ENV"
fi

# Render the prompt template with substitutions. We pass placeholder
# names + their values via env so the python one-liner stays under
# GHA's 21,000-char per-step expression limit and avoids wrestling
# with shell quoting around the multi-line template body.
PROMPT_TPL="${PROMPT_TPL}" \
  CONFLICTED_FILES_COUNT="${CONFLICTED_FILES_COUNT}" \
  CONFLICTED_FILES_LIST="${CONFLICTED_FILES_LIST}" \
  INTEGRATION_BRANCH="${TARGET_BRANCH:-${HEAD_REF:-}}" \
  TRACKING_ISSUE_NUMBER="${INTEGRATION_TRACKING_NUM}" \
  TRACKING_ISSUE_TITLE="${INTEGRATION_TRACKING_TITLE}" \
  TRACKING_ISSUE_BODY="${INTEGRATION_TRACKING_BODY}" \
  MERGED_SUB_ISSUES_LIST="${INTEGRATION_MERGED_SUB_ISSUES_LIST}" \
  MERGED_SUB_ISSUE_COUNT="${INTEGRATION_MERGED_SUB_ISSUE_COUNT}" \
  INTEGRATION_FINGERPRINTS_FILE="${INTEGRATION_FINGERPRINTS_FILE:-}" \
  python3 -c "import os,sys; tpl=open(os.environ['PROMPT_TPL'],encoding='utf-8').read(); keys=['CONFLICTED_FILES_COUNT','CONFLICTED_FILES_LIST','INTEGRATION_BRANCH','TRACKING_ISSUE_NUMBER','TRACKING_ISSUE_TITLE','TRACKING_ISSUE_BODY','MERGED_SUB_ISSUES_LIST','MERGED_SUB_ISSUE_COUNT']; [tpl := tpl.replace('{{'+k+'}}', os.environ.get(k,'')) for k in keys]; p=os.environ.get('INTEGRATION_FINGERPRINTS_FILE',''); fp=(open(p,encoding='utf-8',errors='replace').read() if (p and os.path.isfile(p) and os.access(p, os.R_OK)) else '{}'); tpl=tpl.replace('{{INTENT_FINGERPRINTS_JSON}}', fp); sys.stdout.write(tpl)" \
  > "${CONFLICT_RESOLVER_PROMPT_FILE}"

# On the workflow source repo, snapshot the post-merge working-tree
# state before Codex resolves conflicts.  The commit logic below
# uses it to stage ONLY files Codex actually wrote during resolution,
# including executable-bit-only updates,
# discarding files that git merge auto-staged but Codex never
# touched.  The auto-merged hunks will replay naturally when the PR
# is eventually merged into BASE, so skipping them here is safe.
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
  PRE_RESOLVER_STATE_FILE="${RUNTIME_DIR}/pre_resolver_state.tsv"
  : > "${PRE_RESOLVER_STATE_FILE}"
  # `sort -zu` dedupes: after `git merge --no-commit`, conflicted
  # paths appear once per index stage (1/2/3) in `git ls-files -z`.
  # We only need to hash each working-tree file once.
  while IFS= read -r -d '' snap_path; do
    [ -f "${snap_path}" ] || continue
    snap_exec=0
    [ -x "${snap_path}" ] && snap_exec=1
    printf 'T\t%s\t%s\t%s\n' \
      "$(git hash-object -- "${snap_path}")" \
      "${snap_exec}" \
      "${snap_path}" >> "${PRE_RESOLVER_STATE_FILE}"
  done < <(git ls-files -z | sort -zu)
  while IFS= read -r snap_path; do
    [ -n "${snap_path}" ] && \
      printf 'U\t%s\n' "${snap_path}" >> "${PRE_RESOLVER_STATE_FILE}"
  done < <(git ls-files --others --exclude-standard)
  echo "Pre-resolver snapshot captured: $(wc -l < "${PRE_RESOLVER_STATE_FILE}") entries"

  # Capture the set of paths that actually have merge conflicts
  # right now, before the resolver runs.  This is the canonical
  # whitelist for "files the resolver is allowed to edit"; the
  # post-resolver guard (check_resolver_diff.sh) rejects any
  # touched file that is not in this set.
  CONFLICTED_PATHS_FILE="${RUNTIME_DIR}/conflicted_paths.txt"
  : > "${CONFLICTED_PATHS_FILE}"
  git ls-files --unmerged | awk '{print $4}' | sort -u >> "${CONFLICTED_PATHS_FILE}"
  # Belt-and-suspenders: also include any tracked file that
  # currently contains a literal git conflict marker.  This
  # catches paths git auto-merged but left with residual markers
  # (rare but possible with --no-ff).
  git grep -lE '^(<<<<<<< |>>>>>>> )' -- ':!*.md' 2>/dev/null \
    >> "${CONFLICTED_PATHS_FILE}" || true
  sort -u -o "${CONFLICTED_PATHS_FILE}" "${CONFLICTED_PATHS_FILE}"
  echo "Conflicted paths captured: $(wc -l < "${CONFLICTED_PATHS_FILE}") entries"
fi
