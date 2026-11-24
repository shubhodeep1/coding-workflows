#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Preflight destructive-commit guard" step (id preflight_destructive_guard) of .github/workflows/implement.yml,
# moved out of the workflow for #9678 to keep implement.yml well under the
# 480,000-byte workflow file size guard (tests/test_workflow_file_size_limit.py).
# Runs after the editor's writes are copied back: it projects the staged
# deletion set and the files_touched scope check onto a temporary index and
# fails fast before the commit tail.
#
# The step keeps its name:, id:, if:, env: and continue-on-error: in the
# workflow and sources this file in the step shell, only from the trusted
# pre-editor copy in IMPLEMENT_STAGED_SUPPORT_RUN_DIR; it never runs a
# workspace copy. Pass values through the step env: (GitHub does not
# substitute workflow expressions inside a script file).
set -euo pipefail

TEMP_INDEX_FILE="$(mktemp "${TMPDIR:-/tmp}/implement-preflight-index.XXXXXX")"
VIRTUAL_CLEANUP_FILE="$(mktemp "${TMPDIR:-/tmp}/implement-preflight-cleanup.XXXXXX")"
cleanup() {
  rm -f "${TEMP_INDEX_FILE}" "${VIRTUAL_CLEANUP_FILE}"
}
trap cleanup EXIT
rm -f "${TEMP_INDEX_FILE}"

export GIT_INDEX_FILE="${TEMP_INDEX_FILE}"
git read-tree HEAD

: > "${VIRTUAL_CLEANUP_FILE}"
drop_bootstrap_serena="false"

add_virtual_cleanup_path() {
  local cleanup_path="${1#./}"
  [ -n "${cleanup_path}" ] || return 0
  printf '%s\n' "${cleanup_path}" >> "${VIRTUAL_CLEANUP_FILE}"
  git rm -r -q --cached --ignore-unmatch -- "${cleanup_path}" 2>/dev/null || true
}

is_virtual_cleanup_path() {
  local candidate="${1#./}"
  if grep -Fqx -- "${candidate}" "${VIRTUAL_CLEANUP_FILE}" 2>/dev/null; then
    return 0
  fi
  if [ "${drop_bootstrap_serena}" = "true" ]; then
    case "${candidate}" in
      .serena|.serena/*)
        return 0
        ;;
    esac
  fi
  return 1
}

add_virtual_cleanup_path "pre_assembled_static.txt"
if [ -n "${FETCHED_MANIFEST:-}" ] && [ -f "${FETCHED_MANIFEST}" ]; then
  while IFS= read -r fetched_file; do
    [ -n "${fetched_file}" ] || continue
    add_virtual_cleanup_path "${fetched_file}"
  done < "${FETCHED_MANIFEST}"
fi

if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && [ -f .serena/project.yml ]; then
  current_serena_project_hash="$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "${current_serena_project_hash}" ] && [ "${current_serena_project_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then
    if ! git ls-files --error-unmatch -- .serena >/dev/null 2>&1; then
      drop_bootstrap_serena="true"
    fi
  fi
fi

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
add_u_excludes=(':!node_modules' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.ai/.workspace_source_manifest.txt')
add_o_excludes=(':!node_modules' ':!.github/ai' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.ai/.workspace_source_manifest.txt')
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
if git ls-files --error-unmatch -- .ai/.workspace_source_manifest.txt >/dev/null 2>&1 && [ ! -e .ai/.workspace_source_manifest.txt ] && [ ! -L .ai/.workspace_source_manifest.txt ]; then
  git rm --cached --quiet -- .ai/.workspace_source_manifest.txt
  echo "IMPLEMENT_GENERATED_MANIFEST_UNTRACKED path=.ai/.workspace_source_manifest.txt"
fi
while IFS= read -r -d '' untracked_candidate; do
  [ -n "${untracked_candidate}" ] || continue
  if is_virtual_cleanup_path "${untracked_candidate}"; then
    continue
  fi
  git add -- "${untracked_candidate}"
done < <(git ls-files --others --exclude-standard -z -- "${add_o_excludes[@]}")
if [ "${is_self_repo}" = "false" ] && [ -f scripts/.gitignore ]; then
  while IFS= read -r fetched_script; do
    case "${fetched_script}" in ''|'#'*|'.gitignore') continue ;; esac
    git reset -q HEAD -- "scripts/${fetched_script}" 2>/dev/null || true
  done < scripts/.gitignore
fi
# Align with the later workflow-protection step so preflight does
# not count workflow-file deletions that will be restored before the
# real commit path when workflow edits are disallowed.
if [ "${ALLOW_WORKFLOW_EDITS:-false}" != "true" ] && git cat-file -e HEAD:.github/workflows >/dev/null 2>&1; then
  git reset -q HEAD -- .github/workflows 2>/dev/null || true
fi

deleted_staged="$(git diff --cached --diff-filter=D --name-only || true)"
if [ -n "${deleted_staged}" ]; then
  canonical_deletions="$(printf '%s\n' "${deleted_staged}" \
    | grep -E '^(agents\.md|ai_pipeline\.md|unattended_system_instructions\.md|CLAUDE\.md|prompts/|scripts/|\.github/ai/|\.github/scripts/)' \
    || true)"
  unblock_protected_deletions="$(printf '%s\n' "${deleted_staged}" | grep -iE '^(\.github|\.claude|workflow-templates)(/|$)' || true)"
  total_deletions="$(printf '%s\n' "${deleted_staged}" | sed '/^$/d' | wc -l | tr -d ' ')"
  # Count non-.md deletions to decide whether the lenient
  # markdown-only threshold applies. Case-insensitive match so
  # .MD / .Md are treated the same as .md.
  non_md_deletions="$(printf '%s\n' "${deleted_staged}" | sed '/^$/d' | grep -ivE '\.md$' || true)"
  non_md_count="$(printf '%s\n' "${non_md_deletions}" | sed '/^$/d' | wc -l | tr -d ' ')"
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
    {
      echo "destructive_commit_blocked=canonical-source"
      echo "destructive_commit_count=${total_deletions}"
      echo 'destructive_commit_deletions<<__DCD_EOF__'
      printf '%s\n' "${deleted_staged}"
      echo '__DCD_EOF__'
    } >> "$GITHUB_OUTPUT"
    exit 1
  fi

  # The unblock judge's one-shot override (plan Phase 7, Q12) lifts the
  # bulk-delete threshold for this run only, and never when a canonical
  # workflow source or protected automation path is among the deletions.
  unblock_bulk_override_applies="false"
  if [ "${UNBLOCK_BULK_DELETE_OVERRIDE:-false}" = "true" ] && [ -z "${canonical_deletions}" ] &&
    [ -z "${unblock_protected_deletions}" ] &&
    printf '%s\n' "${deleted_staged}" | jq -R -s -e --argjson paths "${UNBLOCK_BULK_DELETE_PATHS:-null}" 'all(split("\n")[] | select(length > 0); . as $path | $paths | index($path) != null)' >/dev/null 2>&1; then
    unblock_bulk_override_applies="true"
    echo "UNBLOCK_BULK_DELETE_OVERRIDE applied deletions=${total_deletions}"
  fi
  if [ "${UNBLOCK_BULK_DELETE_OVERRIDE:-false}" = "true" ] && [ -n "${unblock_protected_deletions}" ]; then
    echo "UNBLOCK_BULK_DELETE_OVERRIDE outcome=skip reason=protected_automation_paths"
  fi
  if [ "${total_deletions}" -gt "${effective_threshold}" ] && [ "${ALLOW_BULK_DELETE:-false}" != "true" ] && [ "${unblock_bulk_override_applies}" != "true" ]; then
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

# Project the staged-support restore into the temporary index before
# scope enforcement. Only untouched installed copies are reset;
# editor-modified or deleted support files remain scope-checked.
if [ "${is_self_repo}" = "true" ]; then
  if [ -z "${STAGED_SUPPORT_LEDGER:-}" ] || [ ! -f "${STAGED_SUPPORT_LEDGER:-}" ]; then
    echo "::error::IMPLEMENT_STAGED_SUPPORT_LEDGER_MISSING path=${STAGED_SUPPORT_LEDGER:-<unset>}; refusing preflight without the staged-support inventory."
    {
      echo "staged_support_rebase_conflict=true"
      echo "staged_support_rebase_conflict_files=${STAGED_SUPPORT_LEDGER:-STAGED_SUPPORT_LEDGER}"
    } >> "$GITHUB_OUTPUT"
    exit 1
  fi
  if [ -z "${STAGED_SUPPORT_BASE_DIR:-}" ] || [ ! -d "${STAGED_SUPPORT_BASE_DIR:-}" ]; then
    echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${STAGED_SUPPORT_BASE_DIR:-<unset>}; refusing preflight without the installed-content baseline."
    {
      echo "staged_support_rebase_conflict=true"
      echo "staged_support_rebase_conflict_files=${STAGED_SUPPORT_BASE_DIR:-STAGED_SUPPORT_BASE_DIR}"
    } >> "$GITHUB_OUTPUT"
    exit 1
  fi
  while IFS= read -r preflight_staged_support_path; do
    [ -n "${preflight_staged_support_path}" ] || continue
    case "${preflight_staged_support_path}" in
      /*|../*|*/../*|..|*/..)
        echo "::error::IMPLEMENT_STAGED_SUPPORT_LEDGER_INVALID path=${preflight_staged_support_path} reason=unsafe_path"
        {
          echo "staged_support_rebase_conflict=true"
          echo "staged_support_rebase_conflict_files=${preflight_staged_support_path}"
        } >> "$GITHUB_OUTPUT"
        exit 1
        ;;
    esac
    preflight_staged_support_base="${STAGED_SUPPORT_BASE_DIR}/${preflight_staged_support_path}"
    if [ ! -f "${preflight_staged_support_base}" ]; then
      echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${preflight_staged_support_path}; refusing preflight without the installed-content baseline."
      {
        echo "staged_support_rebase_conflict=true"
        echo "staged_support_rebase_conflict_files=${preflight_staged_support_path}"
      } >> "$GITHUB_OUTPUT"
      exit 1
    fi
    preflight_staged_support_mode="$(stat -c '%a' -- "${preflight_staged_support_path}" 2>/dev/null || true)"
    preflight_staged_support_base_mode="$(stat -c '%a' -- "${preflight_staged_support_base}" 2>/dev/null || true)"
    if [ -f "${preflight_staged_support_path}" ] \
      && cmp -s -- "${preflight_staged_support_path}" "${preflight_staged_support_base}" \
      && [ -n "${preflight_staged_support_mode}" ] \
      && [ "${preflight_staged_support_mode}" = "${preflight_staged_support_base_mode}" ]; then
      git reset -q HEAD -- "${preflight_staged_support_path}" 2>/dev/null \
        || git rm -q --cached --ignore-unmatch -- "${preflight_staged_support_path}" 2>/dev/null \
        || true
    fi
  done < "${STAGED_SUPPORT_LEDGER}"
fi

# >>> automation-path grant guard (preflight) >>>
# core.quotePath=false keeps non-ASCII names literal; names Git still
# C-quotes (control characters, quotes, backslashes) start with '"' and
# are denied below rather than slipping past the prefix match.
automation_staged="$(git -c core.quotePath=false diff --cached --name-only --no-renames --diff-filter=ACMDT || true)"
automation_paths="$(printf '%s\n' "${automation_staged}" | grep -iE '^"?(\.github|\.claude|scripts|prompts|workflow-templates)(/|$)' || true)"
if [ -n "${automation_paths}" ]; then
  automation_staged_file="$(mktemp "${TMPDIR:-/tmp}/implement-automation-staged.XXXXXX")"
  printf '%s\n' "${automation_paths}" > "${automation_staged_file}"
  automation_reason="grant_unavailable"
  automation_denied="${automation_paths}"
  automation_rc=127
  if grep -q '^"' <<< "${automation_paths}"; then
    automation_rc=30
    automation_reason="quoted_path"
  elif [ -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/files_touched_scope_guard.py" ]; then
    automation_error_file="$(mktemp "${TMPDIR:-/tmp}/implement-automation-error.XXXXXX")"
    automation_rc=0
    automation_denied="$(python3 "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/files_touched_scope_guard.py" \
      --check-automation-paths --staged-file "${automation_staged_file}" \
      --automation-grant-file "${AUTOMATION_PATH_GRANT_FILE:-${RUNTIME_DIR:-/nonexistent}/automation_path_grant.json}" \
      --allow-workflow-edits "${ALLOW_WORKFLOW_EDITS:-false}" --issue-number "${ISSUE_NUMBER}" \
      2> "${automation_error_file}")" || automation_rc=$?
    if [ "${automation_rc}" -eq 30 ]; then
      automation_reason="$(grep -m1 '^reason=' "${automation_error_file}" | cut -d= -f2 || true)"
    fi
    rm -f "${automation_error_file}"
  fi
  rm -f "${automation_staged_file}"
  if [ "${automation_rc}" -ne 0 ]; then
    if [ "${automation_rc}" -ne 30 ] || [ -z "${automation_denied}" ]; then
      automation_denied="${automation_paths}"
      automation_reason="guard_error"
    fi
    automation_count="$(printf '%s\n' "${automation_denied}" | wc -l | tr -d ' ')"
    automation_granted="$(jq -r '.paths[]? | strings' "${AUTOMATION_PATH_GRANT_FILE:-${RUNTIME_DIR:-/nonexistent}/automation_path_grant.json}" 2>/dev/null || true)"
    {
      echo 'scope_violation_blocked=automation-path'
      echo "scope_violation_count=${automation_count}"
      echo 'scope_violation_files<<__SVF_EOF__'
      printf '%s\n' "${automation_denied}"
      echo '__SVF_EOF__'
      echo 'scope_violation_allowlist<<__SVA_EOF__'
      printf 'reason=%s\n%s\n' "${automation_reason}" "${automation_granted}"
      echo '__SVA_EOF__'
    } >> "$GITHUB_OUTPUT"
    echo "::error::Automation-path grant denied ${automation_count} staged path(s)."
    echo "IMPLEMENT_AUTOMATION_PATH_GUARD mode=check outcome=rejected reason=${automation_reason} issue=${ISSUE_NUMBER} count=${automation_count}"
    exit 1
  fi
  echo "IMPLEMENT_AUTOMATION_PATH_GUARD mode=check outcome=granted issue=${ISSUE_NUMBER} count=0"
fi
# <<< automation-path grant guard (preflight) <<<

# >>> files_touched scope-enforcement guard (preflight) >>>
# Mirror of the destructive-commit guard above, for scope drift:
# reject when the staged change set includes paths the issue's
# files_touched allowlist does not cover. Shares the same projected
# staged index the destructive guard just inspected (GIT_INDEX_FILE
# is still the temp index here). Fails OPEN — no allowlist, master
# toggle off, or a missing helper all log-and-allow, matching the
# missing-allowlist posture used elsewhere in this workflow.
if [ "${ENFORCE_FILES_TOUCHED:-true}" != "true" ] && [ "${HEAL_ROUTE:-false}" != true ]; then
  echo "::notice::files_touched scope guard disabled (ENFORCE_FILES_TOUCHED='${ENFORCE_FILES_TOUCHED:-true}')."
else
  scope_staged="$(git diff --cached --name-only --diff-filter=ACMRD || true)"
  if [ -n "${scope_staged}" ]; then
    scope_staged_file="$(mktemp "${TMPDIR:-/tmp}/implement-scope-staged.XXXXXX")"
    scope_allowlist_file="$(mktemp "${TMPDIR:-/tmp}/implement-scope-allowlist.XXXXXX")"
    printf '%s\n' "${scope_staged}" > "${scope_staged_file}"
    scope_violations=""
    scope_rc=0
    scope_guard_extra_args=()
    if [ "${HEAL_ROUTE:-false}" = true ]; then
      scope_guard_extra_args+=(--strict-allowlist)
    fi
    if [ -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/files_touched_scope_guard.py" ]; then
      set +e
      scope_violations="$(python3 "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/files_touched_scope_guard.py" \
        "$(if [ "${HEAL_ROUTE:-false}" = true ]; then printf '%s' '--allowlist-file'; else printf '%s' '--issue-body-file'; fi)" \
        "$(if [ "${HEAL_ROUTE:-false}" = true ]; then printf '%s' "${HEAL_SCOPE_FILE:?}"; else printf '%s' "${ISSUE_BODY_FILE:-}"; fi)" \
        "${scope_guard_extra_args[@]}" \
        --issue-number "${ISSUE_NUMBER:-}" \
        --issue-meta-file "${ISSUE_META_FILE:-}" --pipeline-login "${IMPLEMENT_SCOPE_PIPELINE_LOGIN:-}" \
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
        if [ "${HEAL_ROUTE:-false}" = true ]; then
          echo '::error::Verified heal scope is empty; refusing scope check.' >&2
          exit 1
        fi
        echo "::notice::files_touched scope guard skipped: issue declares no files_touched allowlist."
        ;;
      20)
        scope_count="$(printf '%s\n' "${scope_violations}" | sed '/^$/d' | wc -l | tr -d ' ')"
        scope_allowlist="$(sed '/^$/d' "${scope_allowlist_file}" 2>/dev/null || true)"
        if [ "${ALLOW_OUT_OF_SCOPE_FILES:-false}" = "true" ] && [ "${HEAL_ROUTE:-false}" != true ]; then
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
        if [ "${HEAL_ROUTE:-false}" = true ]; then
          echo "::error::Heal scope guard failed with exit ${scope_rc}; refusing scope check." >&2
          exit 1
        fi
        echo "::warning::files_touched scope guard failed open (helper exit ${scope_rc}); staged change set not scope-checked this run."
        ;;
    esac
    rm -f "${scope_allowlist_file}"
  fi
fi
# <<< files_touched scope-enforcement guard (preflight) <<<
