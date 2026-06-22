#!/usr/bin/env bash
# implement_commit_changes.sh — stage + commit implement-phase editor output.
#
# Extracted from the "Commit changes" step of .github/workflows/implement.yml
# because the inline `run:` block exceeded GitHub Actions' per-step template
# expression size limit. Keeping the logic in a helper preserves the existing
# commit/output contract without re-approaching that ceiling.

set -euo pipefail

STEP_NAME="Commit changes"
STEP_STDERR_FILE="$(mktemp)"
CAPTURE_FILE=""
if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ] && [ -w "${RUNTIME_DIR}" ]; then
  CAPTURE_FILE="${RUNTIME_DIR}/post_codex_validation_errors.txt"
fi

capture_step_failure() {
  local failed_command="$1"
  [ -n "${CAPTURE_FILE}" ] || return 0
  {
    printf '===== %s =====\n' "${STEP_NAME}"
    printf 'Command: %s\n' "${failed_command}"
    if [ -s "${STEP_STDERR_FILE}" ]; then
      cat "${STEP_STDERR_FILE}"
    else
      echo "(no stderr output)"
    fi
    printf '\n'
  } >> "${CAPTURE_FILE}" || true
}

FAILED_COMMAND=""
LAST_COMMAND=""
trap 'LAST_COMMAND="${BASH_COMMAND}"' DEBUG
trap 'FAILED_COMMAND="${BASH_COMMAND}"' ERR
on_step_exit() {
  local exit_code="$?"
  trap - DEBUG ERR EXIT
  set +e
  exec 2>&3 || true
  exec 3>&- || true
  wait || true
  if [ "${exit_code}" -ne 0 ]; then
    local failed_command="${FAILED_COMMAND:-${LAST_COMMAND:-unknown command}}"
    capture_step_failure "${failed_command}"
  fi
  rm -f "${STEP_STDERR_FILE}"
  exit "${exit_code}"
}
trap on_step_exit EXIT
exec 3>&2
exec 2> >(tee "${STEP_STDERR_FILE}" >&3)

# Remove workflow-generated/fetched artifacts BEFORE checking for
# changes so they don't cause false-positive "file changes" detection.
# The manifest tracks exactly which files were fetched — caller-repo
# files that were never touched are safe. Refuse the cleanup batch if
# the manifest contains any path outside repo-relative cleanup targets.
rm -f ./pre_assembled_static.txt
fetched_manifest_path="${FETCHED_MANIFEST:-}"
if [ -n "${fetched_manifest_path}" ] && [ -f "${fetched_manifest_path}" ]; then
  safe_fetched_paths=()
  unsafe_fetched_paths=""
  while IFS= read -r fetched_file; do
    [ -n "${fetched_file}" ] || continue
    case "${fetched_file}" in
      /*|../*|*/../*|..|*/..)
        unsafe_fetched_paths="${unsafe_fetched_paths}${unsafe_fetched_paths:+$'\n'}${fetched_file}"
        ;;
      *)
        safe_fetched_paths+=("${fetched_file}")
        ;;
    esac
  done < "${fetched_manifest_path}"
  if [ -n "${unsafe_fetched_paths}" ]; then
    unsafe_fetched_count="$(printf '%s\n' "${unsafe_fetched_paths}" | sed '/^$/d' | wc -l | tr -d ' ')"
    echo "::error::Refusing to continue: fetched-manifest contains unsafe cleanup path(s)."
    printf '%s\n' "${unsafe_fetched_paths}" | sed 's/^/  - /'
    {
      echo "destructive_commit_blocked=unsafe-fetched-manifest"
      echo "destructive_commit_count=${unsafe_fetched_count}"
      echo 'destructive_commit_deletions<<__DCD_EOF__'
      printf '%s\n' "${unsafe_fetched_paths}"
      echo '__DCD_EOF__'
    } >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if [ "${#safe_fetched_paths[@]}" -gt 0 ]; then
    rm -f -- "${safe_fetched_paths[@]}"
  fi
fi

if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && [ -f .serena/project.yml ]; then
  current_serena_project_hash="$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "${current_serena_project_hash}" ] && [ "${current_serena_project_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then
    if ! git ls-files --error-unmatch -- .serena >/dev/null 2>&1; then
      rm -rf .serena
    fi
  fi
fi

porcelain_status="$(git status --porcelain)"
if [ -z "${porcelain_status}" ]; then
  echo "No repository changes were produced by Codex implementation (worktree clean after artifact cleanup)."
  echo "did_commit=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
echo "Worktree changes before staging (post artifact-cleanup):"
# shellcheck disable=SC2001
echo "${porcelain_status}" | sed 's/^/  /'

git config user.name "codex-bot"
git config user.email "codex@users.noreply.github.com"
git rm -r --cached node_modules 2>/dev/null || true
# Template-owned paths (prompts/, .github/prompts/,
# .github/scripts/) are fetched from coding-workflows at runtime in
# consumer repos, and must NOT be committed there — any edit to them
# in a consumer wrapper would either be a no-op (overwritten next run)
# or leak a stale copy back into the caller's tree.  In the canonical
# `shubhodeep1/coding-workflows` repository these paths ARE the source of truth
# and must be editable, so the excludes are dropped.
#
# NOTE: scripts/ is NOT blanket-excluded here. Runtime-fetched
# infrastructure helpers are filtered separately so consumer repos can
# still commit their own scripts/ changes when required.
wf_source="shubhodeep1/coding-workflows"
is_self_repo="false"
if [ "${GITHUB_REPOSITORY:-}" = "${wf_source}" ]; then
  is_self_repo="true"
fi
add_u_excludes=(':!node_modules' ':!.codex-workflow-src' ':!.codex-workflow-src-main')
add_o_excludes=(':!node_modules' ':!.github/ai' ':!.codex-workflow-src' ':!.codex-workflow-src-main')
if [ "${is_self_repo}" = "false" ]; then
  # Build per-file exclusions from the runtime-generated scripts/.gitignore
  # instead of a blanket ':!scripts'.  The blanket exclusion previously
  # blocked ALL consumer-repo-owned files under scripts/ (e.g.
  # scripts/security/check-npm-audit.js), causing silent no-op commits.
  # The .gitignore lists exactly the fetched coding-workflows helper
  # scripts that must NOT be committed to consumer repos.
  if [ -f scripts/.gitignore ]; then
    while IFS= read -r _ign_entry; do
      [[ -z "${_ign_entry}" || "${_ign_entry}" == \#* ]] && continue
      add_u_excludes+=(":!scripts/${_ign_entry}")
      add_o_excludes+=(":!scripts/${_ign_entry}")
    done < scripts/.gitignore
  else
    # Fallback: if .gitignore was not created (bootstrap failure), use
    # blanket exclusion for safety — same as the legacy behaviour.
    add_u_excludes+=(':!scripts')
    add_o_excludes+=(':!scripts')
  fi
  # Keep runtime artifact paths out of consumer-repo commits for
  # both tracked updates (-u) and untracked additions.
  add_u_excludes+=(':!prompts' ':!ai-memory' ':!.github/prompts' ':!.github/scripts')
  add_o_excludes+=(':!prompts' ':!ai-memory' ':!.github/prompts' ':!.github/scripts')
fi
git add -u -- "${add_u_excludes[@]}"
git ls-files --others --exclude-standard -z -- "${add_o_excludes[@]}" | xargs -0 -r git add --
if [ "${is_self_repo}" = "false" ] && [ -f scripts/.gitignore ]; then
  while IFS= read -r fetched_script; do
    case "${fetched_script}" in ''|'#'*|'.gitignore') continue ;; esac
    git reset -q HEAD -- "scripts/${fetched_script}" 2>/dev/null || true
  done < scripts/.gitignore
fi
echo "Staged files before commit:"
git diff --cached --name-only | sed 's/^/ - /' || true
if [ "${is_self_repo}" = "false" ] && git diff --cached --name-only | grep -E '^\.github/(prompts|scripts)/'; then
  echo "Error: .github/prompts or .github/scripts is staged"
  exit 1
fi

# -----------------------------------------------------------------
# Destructive-commit guard (P2 — defense-in-depth for PRs #917/#931)
#
# Before committing, inspect the staged deletion set and refuse
# the commit if either:
#
#   (a) any deletion touches the canonical workflow-source list
#       (agents.md, ai_pipeline.md, unattended_system_instructions.md,
#        CLAUDE.md, prompts/**, scripts/**, .github/ai/**) and
#       ALLOW_WORKFLOW_EDITS is
#       not set to 'true'. Even on self-repo runs, deleting any
#       of these files is almost never what an issue asked for;
#       legitimate workflow-editing issues must opt in.
#
#   (b) the total number of staged deletions exceeds the
#       effective bulk-delete threshold and ALLOW_BULK_DELETE
#       is not set to 'true'. The effective threshold is
#       BULK_DELETE_THRESHOLD (default 3) for any commit that
#       includes at least one non-.md deletion, and
#       BULK_DELETE_THRESHOLD_MD (default 100) for commits whose
#       staged deletions are all .md files. This relaxes the
#       cap for docs/scratchpad cleanups (e.g. analysis/*.md
#       backlog purges) while keeping the strict cap whenever
#       any source file is staged for deletion. Canonical .md
#       files (agents.md, ai_pipeline.md, CLAUDE.md,
#       unattended_system_instructions.md) remain covered by
#       the canonical-source check above regardless of the
#       threshold.
#
# On rejection, `destructive_commit_blocked` is written to the
# step output with the reason ('canonical-source' or
# 'bulk-delete'); the `destructive_commit_deletions` output
# carries the deletion list (newline-separated), and the
# bulk-delete path also emits the active threshold label/value
# so downstream steps (P3: label + TG alert) can act on it
# before exiting.
# -----------------------------------------------------------------
deleted_staged="$(git diff --cached --diff-filter=D --name-only || true)"
if [ -n "${deleted_staged}" ]; then
  canonical_deletions="$(printf '%s\n' "${deleted_staged}" \
    | grep -E '^(agents\.md|ai_pipeline\.md|unattended_system_instructions\.md|CLAUDE\.md|prompts/|scripts/|\.github/ai/|\.github/scripts/)' \
    || true)"
  total_deletions="$(printf '%s\n' "${deleted_staged}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if ! [[ "${total_deletions}" =~ ^[0-9]+$ ]]; then
    echo "::warning::Non-numeric staged deletion count '${total_deletions}'; forcing fail-closed bulk-delete handling."
    total_deletions=999999
  fi
  # Count non-.md deletions to decide whether the lenient
  # markdown-only threshold applies. Case-insensitive match so
  # .MD / .Md are treated the same as .md.
  non_md_deletions="$(printf '%s\n' "${deleted_staged}" | sed '/^$/d' | grep -ivE '\.md$' || true)"
  non_md_count="$(printf '%s\n' "${non_md_deletions}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if ! [[ "${non_md_count}" =~ ^[0-9]+$ ]]; then
    echo "::warning::Non-numeric non-markdown deletion count '${non_md_count}'; using the strict threshold path."
    non_md_count=1
  fi
  threshold="${BULK_DELETE_THRESHOLD:-3}"
  if ! [[ "${threshold}" =~ ^[0-9]+$ ]]; then
    echo "::warning::Invalid BULK_DELETE_THRESHOLD='${threshold}'; defaulting to 3."
    threshold=3
  fi
  md_threshold="${BULK_DELETE_THRESHOLD_MD:-100}"
  if ! [[ "${md_threshold}" =~ ^[0-9]+$ ]]; then
    echo "::warning::Invalid BULK_DELETE_THRESHOLD_MD='${md_threshold}'; defaulting to 100."
    md_threshold=100
  fi
  # When every staged deletion is a .md file, apply the
  # lenient markdown-only threshold; otherwise fall back to
  # the strict mixed-content threshold so any source-file
  # deletion still trips at the original limit.
  if [ "${non_md_count}" -eq 0 ]; then
    effective_threshold="${md_threshold}"
    threshold_label="BULK_DELETE_THRESHOLD_MD"
  else
    effective_threshold="${threshold}"
    threshold_label="BULK_DELETE_THRESHOLD"
  fi

  if [ -n "${canonical_deletions}" ] && [ "${ALLOW_WORKFLOW_EDITS:-false}" != "true" ]; then
    echo "::error::Refusing to commit: ${total_deletions} staged deletion(s) include canonical workflow-source files and ALLOW_WORKFLOW_EDITS is not 'true'."
    echo "Canonical-source deletions blocked:"
    printf '%s\n' "${canonical_deletions}" | sed 's/^/  - /'
    # Emit outputs BEFORE exiting so downstream P3 step can read them.
    {
      echo "destructive_commit_blocked=canonical-source"
      echo "destructive_commit_count=${total_deletions}"
      echo 'destructive_commit_deletions<<__DCD_EOF__'
      printf '%s\n' "${deleted_staged}"
      echo '__DCD_EOF__'
    } >> "$GITHUB_OUTPUT"
    exit 1
  fi

  if [ "${total_deletions}" -gt "${effective_threshold}" ] && [ "${ALLOW_BULK_DELETE:-false}" != "true" ]; then
    echo "::error::Refusing to commit: ${total_deletions} staged deletions exceeds ${threshold_label}=${effective_threshold} (non-md deletions=${non_md_count}) and ALLOW_BULK_DELETE is not 'true'."
    echo "Deletions blocked by bulk-delete threshold:"
    printf '%s\n' "${deleted_staged}" | sed 's/^/  - /'
    {
      echo "destructive_commit_blocked=bulk-delete"
      echo "destructive_commit_count=${total_deletions}"
      echo "destructive_commit_threshold_label=${threshold_label}"
      echo "destructive_commit_effective_threshold=${effective_threshold}"
      echo 'destructive_commit_deletions<<__DCD_EOF__'
      printf '%s\n' "${deleted_staged}"
      echo '__DCD_EOF__'
    } >> "$GITHUB_OUTPUT"
    exit 1
  fi
fi

# >>> files_touched scope-enforcement guard (commit) >>>
# Mirror of the destructive-commit guard above, for scope drift:
# reject when the staged change set includes paths the issue's
# files_touched allowlist does not cover. Operates on the same real
# index that was just staged for the destructive guard. Fails OPEN —
# no allowlist, master toggle off, or a missing helper all
# log-and-allow. On a violation the commit is neither created nor
# pushed; the "Destructive-commit guard — label + alert on rejection"
# step labels the issue ai:scope-blocked and alerts.
if [ "${ENFORCE_FILES_TOUCHED:-true}" != "true" ]; then
  echo "::notice::files_touched scope guard disabled (ENFORCE_FILES_TOUCHED='${ENFORCE_FILES_TOUCHED:-true}')."
else
  scope_staged="$(git diff --cached --name-only --diff-filter=ACMRD || true)"
  if [ -n "${scope_staged}" ]; then
    scope_staged_file="$(mktemp "${TMPDIR:-/tmp}/implement-scope-staged.XXXXXX")"
    scope_allowlist_file="$(mktemp "${TMPDIR:-/tmp}/implement-scope-allowlist.XXXXXX")"
    printf '%s\n' "${scope_staged}" > "${scope_staged_file}"
    scope_violations=""
    scope_rc=0
    if [ -f scripts/files_touched_scope_guard.py ]; then
      set +e
      scope_violations="$(python3 scripts/files_touched_scope_guard.py \
        --issue-body-file "${ISSUE_BODY_FILE:-}" \
        --staged-file "${scope_staged_file}" \
        --allowlist-out "${scope_allowlist_file}")"
      scope_rc=$?
      set -e
    else
      scope_rc=127
    fi
    rm -f "${scope_staged_file}"
    case "${scope_rc}" in
      0)
        echo "files_touched scope guard: all staged paths fall within the issue allowlist."
        ;;
      10)
        echo "::notice::files_touched scope guard skipped: issue declares no files_touched allowlist."
        ;;
      20)
        scope_count="$(printf '%s\n' "${scope_violations}" | sed '/^$/d' | wc -l | tr -d ' ')"
        scope_allowlist="$(sed '/^$/d' "${scope_allowlist_file}" 2>/dev/null || true)"
        if [ "${ALLOW_OUT_OF_SCOPE_FILES:-false}" = "true" ]; then
          echo "::warning::files_touched scope guard: ${scope_count} staged path(s) outside the allowlist, but ALLOW_OUT_OF_SCOPE_FILES=true — allowing."
          printf '%s\n' "${scope_violations}" | sed '/^$/d;s/^/  - /'
        else
          echo "::error::Refusing to commit: ${scope_count} staged path(s) fall outside the issue's files_touched allowlist and ALLOW_OUT_OF_SCOPE_FILES is not 'true'."
          printf '%s\n' "${scope_violations}" | sed '/^$/d;s/^/  - /'
          {
            echo "scope_violation_blocked=out-of-scope"
            echo "scope_violation_count=${scope_count}"
            echo 'scope_violation_files<<__SVF_EOF__'
            printf '%s\n' "${scope_violations}" | sed '/^$/d'
            echo '__SVF_EOF__'
            echo 'scope_violation_allowlist<<__SVA_EOF__'
            printf '%s\n' "${scope_allowlist}" | sed '/^$/d'
            echo '__SVA_EOF__'
          } >> "$GITHUB_OUTPUT"
          rm -f "${scope_allowlist_file}"
          exit 1
        fi
        ;;
      *)
        echo "::warning::files_touched scope guard failed open (helper exit ${scope_rc}); staged change set not scope-checked this run."
        ;;
    esac
    rm -f "${scope_allowlist_file}"
  fi
fi
# <<< files_touched scope-enforcement guard (commit) <<<

# If every Codex-produced change was filtered out by the path exclusions
# above (or stripped by the fetched-manifest cleanup earlier in this step),
# there is nothing to commit.  Treat this as a no-op rather than letting
# `git commit` fail — the "Handle no-op implementation" step below will
# re-label the issue and post an explanatory comment.
if [ -z "$(git diff --cached --name-only)" ]; then
  if [ "${is_self_repo}" = "false" ]; then
    echo "All Codex changes were filtered out by staging exclusions or fetched-manifest cleanup; nothing to commit."
  else
    echo "No staged changes after fetched-manifest cleanup; nothing to commit."
  fi
  # Log what Codex created that didn't make it past the staging
  # filters, for the no-op handler and the pathspec hard-fail (Guard
  # 1) further below.
  #
  # The setup steps clone the runtime support checkout into the
  # worktree as untracked, gitignore-free directories, so a raw
  # `git status --porcelain` ALWAYS lists
  #   ?? .codex-workflow-src/
  #   ?? .codex-workflow-src-main/
  # on every consumer-repo run, whether or not Codex touched them. (In
  # this repo those paths are gitignored and never appear, which is why
  # the false-positive only ever bit consumer repos.) Those bare
  # untracked entries are the normal every-run state: they must NOT be
  # reported as "excluded changes" here, nor trip Guard 1 below —
  # doing so produced false ai:needs-human escalations on genuine
  # no-ops (e.g. a task where Codex edited a file then reverted it).
  # Strip exactly those whole-line entries at the source so
  # remaining_changes carries only real survivors.
  #
  # The support-source directories are nested Git checkouts, so the
  # outer repo's porcelain never descends into them: even after Codex
  # edits a file inside .codex-workflow-src/, the outer status still
  # shows only "?? .codex-workflow-src/". After stripping those benign
  # top-level markers, query each support checkout's own Git status and
  # splice any dirty inner paths back into remaining_changes so Guard 1
  # still trips on real writes inside the runtime-fetched checkout.
  remaining_changes="$(git status --porcelain 2>/dev/null \
    | grep -vxE '\?\? \.codex-workflow-src(-main)?/?' || true)"
  for support_checkout in .codex-workflow-src .codex-workflow-src-main; do
    if [ -e "${support_checkout}/.git" ]; then
      if support_checkout_changes="$(git -C "${support_checkout}" status --porcelain 2>/dev/null)"; then
        support_checkout_changes="$(printf '%s\n' "${support_checkout_changes}" | sed "s#^\\(..\\) #\\1 ${support_checkout}/#")"
      else
        echo "::warning::Failed to query nested support checkout status for ${support_checkout}; treating it as clean."
        support_checkout_changes=""
      fi
      if [ -n "${support_checkout_changes}" ]; then
        remaining_changes="${remaining_changes}${remaining_changes:+$'\n'}${support_checkout_changes}"
      fi
    fi
  done
  if [ -n "${remaining_changes}" ]; then
    echo "::warning::Files present in worktree but excluded from staging:"
# shellcheck disable=SC2001
    echo "${remaining_changes}" | sed 's/^/  /'
    echo "If these are legitimate consumer-repo files, the pathspec exclusions in the commit step may be too broad."
    # Pass to Handle no-op step for inclusion in the issue comment
    {
      echo "remaining_changes<<__RC_EOF__"
      echo "${remaining_changes}"
      echo "__RC_EOF__"
    } >> "$GITHUB_OUTPUT"
  fi
  echo "did_commit=false" >> "$GITHUB_OUTPUT"
  exit 0
fi
git commit -m "AI implementation for issue #${ISSUE_NUMBER}"
echo "did_commit=true" >> "$GITHUB_OUTPUT"
