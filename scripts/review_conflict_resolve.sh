#!/usr/bin/env bash
# review_conflict_resolve.sh — run Codex resolver, validate, stage, and
# create the [ai-merge-resolve] commit for review_autofix.yml.
#
# Extracted from the "Run Codex resolver, validate, stage, commit" step of
# review_autofix.yml to keep the `run:` block under GitHub Actions' 21,000-
# char template-expression limit. Consumes artefacts produced by
# review_conflict_prepare.sh and shares that step's short-circuit conditions.
#
# Inputs (environment):
#   RUNTIME_DIR                     Ephemeral per-run directory.
#   CONFLICT_RESOLVER_PROMPT_FILE   Rendered resolver prompt.
#   CONFLICT_RESOLVER_SUMMARY_FILE  Path resolver summary is written to.
#   MODEL_EDITOR                    Codex model id used for resolution.
#   IS_WORKFLOW_SOURCE_REPO         "true" on the coding-workflows repo itself.
#   SUPPORT_SCRIPTS_DIR             Path to check_resolver_diff.sh / verify_integration_fingerprints.py.
#   IS_INTEGRATION_SYNC             "true" when acting on an orchestrator integration branch.
#   INTEGRATION_FINGERPRINTS_FILE   Fingerprints payload written by the prepare step.
#   INTEGRATION_BRANCH_NAME / TARGET_BRANCH  Branch identifiers used by the verifier.
#   GH_PAT                          GitHub token used to rewrite the origin remote URL.
#   GITHUB_REPOSITORY               owner/repo slug (auto-set).
#
# Outputs:
#   $GITHUB_ENV: CONFLICT_RESOLVED.
#   Creates a single [ai-merge-resolve] commit on success (push deferred).
#
# Failure modes:
#   - Exits 1 on resolver retry exhaustion, allowlist violation,
#     check_resolver_diff.sh failure, or integration-fingerprint hard violation.
#   - Exits 0 with CONFLICT_RESOLVED=false when no changes remain to commit.

set -euo pipefail

# Re-derive runtime paths set in the prepare step. Shell variables
# do not cross step boundaries; RUNTIME_DIR itself is in
# $GITHUB_ENV so it survives, and both paths are deterministic
# functions of RUNTIME_DIR.
PRE_RESOLVER_STATE_FILE="${RUNTIME_DIR}/pre_resolver_state.tsv"
CONFLICTED_PATHS_FILE="${RUNTIME_DIR}/conflicted_paths.txt"
RESOLVER_ALLOWLIST_FILE="${RUNTIME_DIR}/resolver_unmerged_allowlist.txt"

attempt=1
while [ "${attempt}" -le 3 ]; do
  tmp_output="$(mktemp)"
  if codex exec --model "${MODEL_EDITOR}" --full-auto < "${CONFLICT_RESOLVER_PROMPT_FILE}" > "${tmp_output}"; then
    if [ -s "${tmp_output}" ]; then
      mv "${tmp_output}" "${CONFLICT_RESOLVER_SUMMARY_FILE}"
      echo "Conflict resolver succeeded on attempt ${attempt}."
      break
    fi
  fi
  rm -f "${tmp_output}"
  if [ "${attempt}" -eq 3 ]; then
    echo "Conflict resolver failed after retries."
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done

# Remove root-level workflow-generated artifacts so they are never
# committed to caller repos.  Skip when running on coding-workflows
# itself — these files are actual source code there, not artifacts.
# NOTE: .serena/ and prompts/ are cleaned up in the final
# "Cleanup temporary artifacts" step because later notification steps
# (Telegram, labeling) still need scripts/tg_helpers.sh and
# scripts/label_helpers.sh.  prompts/ and .serena/ are excluded
# from git add via ':!.serena', ':!prompts' patterns; fetched
# scripts are excluded via the bootstrap-generated scripts/.gitignore.
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ]; then
  rm -f ./pre_assembled_static.txt
  rm -f codex_system_instructions.md ai_pipeline.md unattended_llm_system_instructions.md agents.md
fi

if [ -n "$(git status --porcelain)" ]; then
  git config user.name "codex-bot"
  git config user.email "codex@users.noreply.github.com"
  git rm -r --cached node_modules 2>/dev/null || true
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
    # On the workflow source repo, identify the files Codex actually
    # wrote during conflict resolution so we can surface them in
    # the job log and (below) stage them on top of the live merge
    # index.  Auto-merged files the editor did NOT touch stay in
    # the index as git merged them — we deliberately do NOT strip
    # them, because stripping them broke merge ancestry (see the
    # comment block below the pre-snapshot diff for the full
    # rationale).
    RESOLVER_TOUCHED_FILE="${RUNTIME_DIR}/codex_touched_resolver.txt"
    : > "${RESOLVER_TOUCHED_FILE}"
    if [ -f "${PRE_RESOLVER_STATE_FILE:-/nonexistent}" ]; then
      PRE_UNTRACKED_LIST="$(mktemp)"
      POST_UNTRACKED_LIST="$(mktemp)"
      while IFS=$'\t' read -r diff_kind diff_a diff_b diff_c; do
        case "${diff_kind}" in
          T)
            old_sha="${diff_a}"
            old_exec="${diff_b}"
            diff_path="${diff_c}"
            [ -z "${diff_path}" ] && continue
            if [ ! -e "${diff_path}" ]; then
              printf '%s\n' "${diff_path}" >> "${RESOLVER_TOUCHED_FILE}"
              continue
            fi
            new_sha="$(git hash-object -- "${diff_path}" 2>/dev/null || true)"
            if [ -x "${diff_path}" ]; then
              new_exec=1
            else
              new_exec=0
            fi
            if { [ -n "${new_sha}" ] && [ "${new_sha}" != "${old_sha}" ]; } || [ "${new_exec}" != "${old_exec}" ]; then
              printf '%s\n' "${diff_path}" >> "${RESOLVER_TOUCHED_FILE}"
            fi
            ;;
          U)
            printf '%s\n' "${diff_a}" >> "${PRE_UNTRACKED_LIST}"
            ;;
        esac
      done < "${PRE_RESOLVER_STATE_FILE}"
      git ls-files --others --exclude-standard > "${POST_UNTRACKED_LIST}" || true
      sort -o "${PRE_UNTRACKED_LIST}" "${PRE_UNTRACKED_LIST}"
      sort -o "${POST_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"
      comm -13 "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}" >> "${RESOLVER_TOUCHED_FILE}"
      rm -f "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"
    else
      echo "::warning::pre-resolver snapshot missing; falling back to full status."
      git ls-files --modified --others --exclude-standard >> "${RESOLVER_TOUCHED_FILE}" || true
    fi
    sort -u -o "${RESOLVER_TOUCHED_FILE}" "${RESOLVER_TOUCHED_FILE}"
    touched_count="$(wc -l < "${RESOLVER_TOUCHED_FILE}" | tr -d '[:space:]')"
    echo "Resolver-touched files (${touched_count}):"
    sed 's/^/ - /' "${RESOLVER_TOUCHED_FILE}" || true

    # Allowlist validation (hallucination guard):
    # The conflict-resolver prompt explicitly forbids editing
    # files that don't contain conflict markers.  Compare the
    # files Codex actually touched against the unmerged-paths
    # allowlist captured right after the merge replay.  Any
    # .github/workflows/*.y(a)ml file touched outside the
    # allowlist is treated as a hallucinated modification —
    # the highest-risk class of editor drift, because a
    # corrupt workflow file poisons every subsequent run's
    # bootstrap (see PR #912 post-mortem where the resolver
    # added 300 lines referencing files that never existed).
    # Non-workflow out-of-allowlist files emit a warning only,
    # since the merge-staged index may legitimately contain
    # auto-resolved files Codex lightly touched.
    if [ -s "${RESOLVER_ALLOWLIST_FILE:-/nonexistent}" ] || [ -f "${RESOLVER_ALLOWLIST_FILE:-/nonexistent}" ]; then
      WORKFLOW_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_workflow_violations.txt"
      OTHER_VIOLATIONS_FILE="${RUNTIME_DIR}/resolver_other_violations.txt"
      : > "${WORKFLOW_VIOLATIONS_FILE}"
      : > "${OTHER_VIOLATIONS_FILE}"
      while IFS= read -r _touched_path; do
        [ -z "${_touched_path}" ] && continue
        if grep -Fxq "${_touched_path}" "${RESOLVER_ALLOWLIST_FILE}"; then
          continue
        fi
        case "${_touched_path}" in
          .github/workflows/*.yml|.github/workflows/*.yaml)
            printf '%s\n' "${_touched_path}" >> "${WORKFLOW_VIOLATIONS_FILE}"
            ;;
          *)
            printf '%s\n' "${_touched_path}" >> "${OTHER_VIOLATIONS_FILE}"
            ;;
        esac
      done < "${RESOLVER_TOUCHED_FILE}"

      if [ -s "${OTHER_VIOLATIONS_FILE}" ]; then
        echo "::warning::Conflict resolver touched non-workflow files that were not in the unmerged set. This may be benign (e.g. auto-merged index updates) but is worth noting:"
        sed 's/^/ - /' "${OTHER_VIOLATIONS_FILE}" || true
      fi

      if [ -s "${WORKFLOW_VIOLATIONS_FILE}" ]; then
        echo "::error::Conflict resolver modified GitHub workflow files that were NOT in the unmerged set. This is the signature of a hallucinated [ai-merge-resolve] edit and must not be committed — a corrupt workflow file would break every subsequent run's bootstrap. Refusing to commit."
        echo "Unmerged paths captured before codex exec (allowlist):"
        if [ -s "${RESOLVER_ALLOWLIST_FILE}" ]; then
          sed 's/^/ - /' "${RESOLVER_ALLOWLIST_FILE}" || true
        else
          echo "  (allowlist is empty — the merge replay produced no unmerged paths)"
        fi
        echo "Workflow files touched by resolver but NOT in allowlist:"
        sed 's/^/ - /' "${WORKFLOW_VIOLATIONS_FILE}" || true
        exit 1
      fi
    else
      echo "::warning::Resolver allowlist file missing; skipping hallucination guard. Falling through to existing commit-gate protections."
    fi

    # Hard guardrail against hallucinated merge resolutions.
    # check_resolver_diff.sh enforces three invariants:
    #   1. touched ⊆ conflicted  — the resolver may only edit
    #      files that actually had merge markers.  This is what
    #      catches the PR #912 failure mode where the resolver
    #      added 300 lines + references to nonexistent helper
    #      scripts under the guise of "merge resolution".
    #   2. bash -n / py_compile / json.load on every touched
    #      file — catches truncated heredocs and similar.
    #   3. Workflow → script reference integrity for any
    #      modified .github/workflows/*.yml — would have caught
    #      build_repo_overview.sh / protected_paths.txt.
    # On failure: skip the merge-resolve commit and exit 1 so
    # the run goes to ai:review-blocked instead of pushing a
    # broken commit that breaks every subsequent autofix run.
    if [ ! -f "${CONFLICTED_PATHS_FILE:-/nonexistent}" ]; then
      echo "::error::Conflicted-paths snapshot missing; refusing to create [ai-merge-resolve] commit without resolver validation."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      rm -f "${RESOLVER_TOUCHED_FILE}"
      exit 1
    fi

    if [ ! -x "${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh" ]; then
      echo "::error::check_resolver_diff.sh is missing or not executable; refusing to create [ai-merge-resolve] commit without resolver validation."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      rm -f "${RESOLVER_TOUCHED_FILE}"
      exit 1
    fi

    if ! "${SUPPORT_SCRIPTS_DIR}/check_resolver_diff.sh" \
        --conflicted-set "${CONFLICTED_PATHS_FILE}" \
        --touched-set    "${RESOLVER_TOUCHED_FILE}" \
        --repo-root      "${PWD}"; then
      echo "::error::Conflict resolver output failed validation; skipping [ai-merge-resolve] commit."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      rm -f "${RESOLVER_TOUCHED_FILE}"
      exit 1
    fi

    # Stage the editor's conflict resolutions on top of the
    # existing merge index.  Crucially, we KEEP .git/MERGE_HEAD
    # in place AND we do NOT reset the index to HEAD.  Both
    # are required for correctness:
    #
    # 1. Keeping MERGE_HEAD makes `git commit` below produce a
    #    real merge commit with two parents (HEAD and base).
    #    Without the second parent, git's merge-base walker
    #    rewinds to the original merge base on every subsequent
    #    `git merge base` attempt and re-raises the identical
    #    conflict — observed on PR #908, where the previous
    #    single-parent resolve commit left the PR in
    #    mergeable_state=dirty with the same conflict line.
    #
    # 2. NOT resetting the index preserves git merge's
    #    auto-merged content for files the editor did not
    #    touch.  An earlier attempt at this fix did `git
    #    read-tree HEAD` to keep the commit's first-parent
    #    diff minimal, but that silently reverted base-side
    #    changes to auto-merged files: when the PR was
    #    eventually merged into BASE, git saw PR HEAD's old
    #    content as "theirs" against base's unchanged content
    #    and applied the revert.  Keeping the merged index
    #    lets base's changes flow through the merge commit
    #    unchanged, so the eventual merge-to-BASE is a clean
    #    fast-forward-equivalent.
    #
    # The previous per-file `git add`/`git rm` loop below still
    # runs on top of the merge index: for conflicted paths
    # whose conflict markers the editor removed, `git add`
    # replaces the unmerged index entries with the editor's
    # resolved content.  Auto-merged paths the editor did not
    # touch stay in the index as git merged them.
    git rm -r --cached --ignore-unmatch -- node_modules 2>/dev/null || true
    while IFS= read -r touched_path; do
      [ -z "${touched_path}" ] && continue
      case "${touched_path}" in
        node_modules|node_modules/*|*/node_modules|*/node_modules/*) continue ;;
      esac
      if [ -e "${touched_path}" ]; then
        git add -- "${touched_path}" 2>/dev/null || true
      else
        git rm -q -- "${touched_path}" 2>/dev/null || true
      fi
    done < "${RESOLVER_TOUCHED_FILE}"
    rm -f "${RESOLVER_TOUCHED_FILE}"
  else
    # Build per-file exclusions from scripts/.gitignore when present.
    # If it is absent, keep exclusions empty so consumer-owned scripts/
    # changes are still staged.
    _rs_script_excludes=()
    if [ -f scripts/.gitignore ]; then
      while IFS= read -r _ign_entry; do
        [[ -z "${_ign_entry}" || "${_ign_entry}" == \#* ]] && continue
        _rs_script_excludes+=(":!scripts/${_ign_entry}")
      done < scripts/.gitignore
    fi
    git add -u -- ':!node_modules' "${_rs_script_excludes[@]}" ':!prompts' ':!.serena' ':!ai-memory' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.github/prompts' ':!.github/scripts'
    git ls-files --others --exclude-standard -z -- ':!node_modules' "${_rs_script_excludes[@]}" ':!prompts' ':!.serena' ':!ai-memory' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.github/ai' ':!.github/prompts' ':!.github/scripts' | xargs -0 -r git add --
  fi
  echo "Staged files before commit:"
  STAGED_FILES="$(git diff --cached --name-only || true)"
  printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
  # Soft guardrail: if workflow runtime/helper artifacts leaked into
  # the staging area (e.g. merge auto-stage of conflicted paths,
  # codex exec writing to a protected path, or leaked tracked files
  # on the consumer branch), unstage them and continue.  A previous
  # hard `exit 1` here silently threw away reviewer+editor work on
  # what is usually a recoverable condition.
  PROTECTED_LEAKED=false
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^\.github/(prompts|scripts)/'; then
    echo "::warning::.github/prompts or .github/scripts was staged in consumer repo; unstaging protected paths and continuing."
    git reset -q HEAD -- '.github/prompts' '.github/scripts' 2>/dev/null || true
    PROTECTED_LEAKED=true
  fi
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^(prompts/|\.serena/|\.github/scripts/|\.github/prompts/|ai-memory/|\.codex-workflow-src/|\.codex-workflow-src-main/)'; then
    echo "::warning::workflow runtime/helper artifacts were staged in consumer repo; unstaging protected paths and continuing."
    git reset -q HEAD -- 'prompts' '.serena' '.github/scripts' '.github/prompts' 'ai-memory' '.codex-workflow-src' '.codex-workflow-src-main' 2>/dev/null || true
    PROTECTED_LEAKED=true
  fi
  if [ "${PROTECTED_LEAKED}" = "true" ]; then
    STAGED_FILES="$(git diff --cached --name-only || true)"
    echo "Staged files after protected-path reset:"
    printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
    if git diff --cached --quiet; then
      echo "No repository changes remain after protected-path reset; skipping merge-resolve commit."
      echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
      exit 0
    fi
  fi
  if git diff --cached --quiet; then
    echo "No staged merge resolution changes remain; skipping merge-resolve commit."
    echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
    exit 0
  fi

  # ============================================================
  # Integration-sync intent fingerprint verification (#1057
  # auto-heal hardening).
  #
  # When this resolver run is acting on an orchestrator
  # integration branch (IS_INTEGRATION_SYNC=true was exported
  # by the prepare step), the orchestrator state has captured
  # intent fingerprints for every sub-issue PR that has merged
  # into this branch.  Verify the resolver's output preserves
  # those fingerprints BEFORE creating the [ai-merge-resolve]
  # commit:
  #
  #   - must_contain[]: every regex MUST still match its file
  #     in the post-resolve tree.  A failed match means the
  #     resolver removed a line the merged sub-issue added —
  #     a silent intent regression.
  #
  #   - must_not_contain[]: no regex may match its file in
  #     the post-resolve tree.  A successful match means the
  #     resolver re-introduced a line the merged sub-issue
  #     deleted — a silent intent regression.
  #
  # Going-forward only (Q4:A): sub-issues merged before
  # fingerprinting was enabled have no entries in the
  # fingerprints JSON and are silently skipped.  Fail-open on
  # any plumbing error: a missing fingerprints file or
  # unparseable JSON logs a warning and lets the resolver
  # commit through (the existing check_resolver_diff.sh
  # guards still apply).  Verification failures, however, are
  # HARD errors — the whole point is to surface silent
  # regressions before they ship.
  #
  # #5 silent-regression detector: as a belt-and-braces signal
  # for the "resolver took the other side verbatim" failure
  # mode, also emit a warning if the post-resolve tree
  # contains strictly fewer total must_contain markers than
  # were captured.  The hard match check above already
  # rejects any specific drop, but the count delta is a
  # cheap way to catch coordinated regressions in the logs.
  if [ "${IS_INTEGRATION_SYNC:-false}" = "true" ]; then
    if [ -z "${INTEGRATION_FINGERPRINTS_FILE:-}" ] || [ ! -f "${INTEGRATION_FINGERPRINTS_FILE}" ]; then
      echo "::warning::Integration fingerprint verification skipped — INTEGRATION_FINGERPRINTS_FILE missing. Resolver commit allowed but downstream sub-issue intent regressions will not be caught by this guard."
    else
      _fp_size="$(wc -c < "${INTEGRATION_FINGERPRINTS_FILE}" 2>/dev/null || echo 0)"
      if [ "${_fp_size}" -le 2 ]; then
        echo "Integration fingerprint verification: no fingerprints recorded for any merged sub-issue (${INTEGRATION_FINGERPRINTS_FILE} is empty/{}). Skipping verification."
      else
        echo "Integration fingerprint verification: checking resolver output against captured intent fingerprints..."
        # Delegate to scripts/verify_integration_fingerprints.py
        # (optional bootstrap entry — see OPTIONAL_BOOTSTRAP_SCRIPTS).
        # Exit codes:
        #   0 — all fingerprints satisfied
        #   1 — at least one fingerprint violation (HARD error)
        #   2 — plumbing failure (file missing / unparseable):
        #       FAIL OPEN — warn and continue.
        # Older consumer script_refs may not have the verifier yet;
        # in that case the bootstrap step warns and continues, and
        # we fall open here too.
        _fp_verifier_exit=0
        if [ ! -f "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" ]; then
          echo "::warning::verify_integration_fingerprints.py not bootstrapped on this script_ref; integration fingerprint verification skipped (fail-open). Resolver commit allowed."
          _fp_verifier_exit=2
        else
          INTEGRATION_BRANCH_NAME="${INTEGRATION_BRANCH_NAME:-${TARGET_BRANCH:-}}" \
            python3 "${SUPPORT_SCRIPTS_DIR}/verify_integration_fingerprints.py" \
              "${INTEGRATION_FINGERPRINTS_FILE}" || _fp_verifier_exit=$?
        fi
        case "${_fp_verifier_exit}" in
          0)
            ;;
          1)
            # Hard violation — surface as a structured error
            # and bail out of the resolver step.  The merge
            # state is left intact so the orchestrator's
            # next poll tick re-enters heal_integration_-
            # branch_conflict, which (per Q3:A default
            # INTEGRATION_SYNC_CONFLICT_MAX_RETRIES=1) will
            # immediately escalate to the integration judge.
            echo "::error::Aborting [ai-merge-resolve] commit: integration fingerprint verification rejected the resolver output."
            echo "CONFLICT_RESOLVED=false" >> "$GITHUB_ENV"
            exit 1
            ;;
          2|*)
            echo "::warning::Integration fingerprint verification could not run (exit ${_fp_verifier_exit}); continuing without intent guard for this commit."
            ;;
        esac
      fi
    fi
  fi

  git commit -m "[ai-merge-resolve] resolve merge conflicts"
  git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"
  # NOTE: push deferred to final "Push all pending commits" step.
  echo "CONFLICT_RESOLVED=true" >> "$GITHUB_ENV"
  echo "Conflicts resolved and committed (push deferred)"
else
  echo "No conflict resolution changes to commit"
fi

