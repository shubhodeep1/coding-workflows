#!/usr/bin/env bash
# implement_staged_support_workspace.sh — give the implement editor the
# branch's own copies of the staged support helpers (self-repo only).
#
# Usage:
#   bash implement_staged_support_workspace.sh restore     # before the editor launches
#   bash implement_staged_support_workspace.sh reinstall   # after the editor exits
#
# Background. In this repository the "Stage workflow support files" step of
# implement.yml installs SCRIPT_REF's (the default branch's) copies of the
# runtime helpers over the checkout's tracked files, and workspace_init.sh
# copies that staged checkout into the editor workspace. When the issue's
# branch is an orchestrator integration branch that diverged from SCRIPT_REF,
# the editor therefore sees and edits SCRIPT_REF's version of a helper, not
# the branch's. scripts/implement_commit_changes.sh then has to re-base that
# edit onto the branch's version with a 3-way merge, and a conflict fails
# closed into ai:needs-human. Security-pass fix issue #4113 (project #3965)
# hit exactly that: scripts/codex_helpers.sh differed from main by 477 lines,
# the editor edited main's copy, the merge conflicted, and the issue stopped
# for a human (implement run 35072286584).
#
# `restore` runs in the editor workspace right before the editor launches:
# every STAGED_SUPPORT_LEDGER path whose workspace content still equals the
# installed copy (STAGED_SUPPORT_BASE_DIR) is put back to HEAD's version
# (or removed when HEAD does not track it, i.e. staging recreated a file the
# branch deleted), and the path is recorded in STAGED_SUPPORT_EDITOR_HEAD_LEDGER.
# The editor then edits the branch's own file, so its edit is a plain edit.
#
# `reinstall` runs right after the editor exits: every recorded path the
# editor left untouched (workspace still equals HEAD) gets the installed
# copy back, so the workflow's later steps that still read helpers from the
# workspace see exactly what they saw before this script ran. Paths the
# editor edited, recreated or deleted are left alone; the commit helper reads
# STAGED_SUPPORT_EDITOR_HEAD_LEDGER and commits them as ordinary editor
# changes instead of attempting the 3-way re-base.
#
# Trust boundary: this only changes which version of a helper the editor is
# shown inside the workspace it already controls. The staged copies under
# IMPLEMENT_STAGED_SUPPORT_RUN_DIR and the GITHUB_WORKSPACE checkout are not
# touched, and nothing here runs code from the workspace.
#
# Contract:
#   - No-op (exit 0) when STAGED_SUPPORT_LEDGER is unset or absent: consumer
#     repos never set the ledger, and the staged helpers there are runtime
#     artifacts excluded at commit time.
#   - Fails closed (exit 1) on an unsafe ledger path or a missing base copy,
#     matching implement_commit_changes.sh, so an inconsistent inventory never
#     silently leaves SCRIPT_REF copies in the editor's view.
#   - `reinstall` only acts on editor-head ledger entries that are also in
#     STAGED_SUPPORT_LEDGER. `restore` is the only writer of the editor-head
#     ledger and it only records staged paths, so any other entry was written
#     by something else that inherited STAGED_SUPPORT_EDITOR_HEAD_LEDGER from
#     the job environment (the editor's own pytest run of
#     tests/test_implement_post_codex_recovery.py appended its fixture path
#     `scripts/helper.sh` during implement runs 35614385686, 35628923735,
#     35642366131 and 35656715219 and the reinstall failed closed on it).
#     Such an entry cannot be a SCRIPT_REF copy, so it is logged as
#     IMPLEMENT_STAGED_SUPPORT_EDITOR_HEAD_LEDGER_UNKNOWN_PATH and skipped;
#     the commit helper never consults the editor-head ledger for a path
#     outside the staged inventory either.
#   - Every decision is logged under the IMPLEMENT_STAGED_SUPPORT_EDITOR_*
#     keys; the summary line is IMPLEMENT_STAGED_SUPPORT_EDITOR_<MODE>.
#   - No GitHub API calls; git operations are local to the workspace.
set -euo pipefail

mode="${1:-}"
case "${mode}" in
  restore|reinstall) ;;
  *)
    echo "usage: $0 restore|reinstall" >&2
    exit 2
    ;;
esac

staged_support_ledger="${STAGED_SUPPORT_LEDGER:-}"
staged_support_base_dir="${STAGED_SUPPORT_BASE_DIR:-}"
if [ -z "${staged_support_ledger}" ] || [ ! -f "${staged_support_ledger}" ]; then
  echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED mode=${mode} reason=no_ledger"
  exit 0
fi
if [ -z "${staged_support_base_dir}" ] || [ ! -d "${staged_support_base_dir}" ]; then
  echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${staged_support_base_dir:-<unset>}; refusing to change the editor's view of staged support files without the installed-content baseline."
  exit 1
fi
editor_head_ledger="${STAGED_SUPPORT_EDITOR_HEAD_LEDGER:-}"
if [ -z "${editor_head_ledger}" ]; then
  editor_head_ledger="$(dirname -- "${staged_support_ledger}")/staged_support_editor_head.txt"
fi

_unsafe_path() {
  case "$1" in
    /*|../*|*/../*|..|*/..|.|./) return 0 ;;
  esac
  return 1
}

if [ "${mode}" = "restore" ]; then
  # The implementation and syntax-repair loops both call restore. Keep the
  # ledger cumulative so a helper edited by the implementation loop still
  # takes the plain HEAD-edit commit path after the repair loop runs.
  : >> "${editor_head_ledger}"
  restored=0
  removed=0
  skipped=0
  while IFS= read -r staged_path; do
    [ -n "${staged_path}" ] || continue
    if _unsafe_path "${staged_path}"; then
      echo "::error::IMPLEMENT_STAGED_SUPPORT_LEDGER_INVALID path=${staged_path} reason=unsafe_path"
      exit 1
    fi
    staged_base="${staged_support_base_dir}/${staged_path}"
    if [ ! -f "${staged_base}" ]; then
      echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${staged_path}; refusing to change the editor's view of an unverifiable support-ref copy."
      exit 1
    fi
    if [ ! -f "${staged_path}" ]; then
      echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED_PATH path=${staged_path} reason=absent_before_editor"
      skipped=$((skipped + 1))
      continue
    fi
    staged_mode="$(stat -c '%a' -- "${staged_path}" 2>/dev/null || true)"
    staged_base_mode="$(stat -c '%a' -- "${staged_base}" 2>/dev/null || true)"
    if ! cmp -s -- "${staged_path}" "${staged_base}" || [ -z "${staged_mode}" ] || [ "${staged_mode}" != "${staged_base_mode}" ]; then
      # Something between staging and the editor launch already changed this
      # copy; leave it for the commit helper's existing re-base path.
      echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED_PATH path=${staged_path} reason=modified_before_editor"
      skipped=$((skipped + 1))
      continue
    fi
    if git cat-file -e "HEAD:${staged_path}" >/dev/null 2>&1; then
      git restore --source=HEAD --worktree -- "${staged_path}"
      echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORED path=${staged_path}"
      restored=$((restored + 1))
    else
      rm -f -- "${staged_path}"
      echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORED path=${staged_path} state=absent-in-head"
      removed=$((removed + 1))
    fi
    if ! grep -Fqx -- "${staged_path}" "${editor_head_ledger}"; then
      printf '%s\n' "${staged_path}" >> "${editor_head_ledger}"
    fi
  done < "${staged_support_ledger}"
  echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORE restored=${restored} removed=${removed} skipped=${skipped} head_ledger=${editor_head_ledger}"
  exit 0
fi

# reinstall
if [ ! -f "${editor_head_ledger}" ]; then
  echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED mode=reinstall reason=no_head_ledger"
  exit 0
fi
reinstalled=0
edited=0
unknown=0
while IFS= read -r staged_path; do
  [ -n "${staged_path}" ] || continue
  if _unsafe_path "${staged_path}"; then
    echo "::error::IMPLEMENT_STAGED_SUPPORT_LEDGER_INVALID path=${staged_path} reason=unsafe_path"
    exit 1
  fi
  if ! grep -Fqx -- "${staged_path}" "${staged_support_ledger}"; then
    echo "::warning::IMPLEMENT_STAGED_SUPPORT_EDITOR_HEAD_LEDGER_UNKNOWN_PATH path=${staged_path} reason=not_in_staged_ledger; not a staged support copy, leaving the workspace path alone."
    unknown=$((unknown + 1))
    continue
  fi
  staged_base="${staged_support_base_dir}/${staged_path}"
  if [ ! -f "${staged_base}" ]; then
    echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${staged_path}; cannot reinstall the support-ref copy."
    exit 1
  fi
  if git cat-file -e "HEAD:${staged_path}" >/dev/null 2>&1; then
    # `git diff --quiet HEAD` compares content and mode, and fails (non-zero)
    # when the path was deleted from the worktree.
    if [ -f "${staged_path}" ] && git diff --quiet HEAD -- "${staged_path}" 2>/dev/null; then
      staged_base_mode="$(stat -c '%a' -- "${staged_base}" 2>/dev/null || true)"
      if [ -z "${staged_base_mode}" ]; then
        echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${staged_path} reason=mode_unavailable; cannot reinstall the support-ref copy."
        exit 1
      fi
      install -m "${staged_base_mode}" -- "${staged_base}" "${staged_path}"
      echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALLED path=${staged_path}"
      reinstalled=$((reinstalled + 1))
    else
      echo "IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD path=${staged_path}"
      edited=$((edited + 1))
    fi
  elif [ ! -e "${staged_path}" ]; then
    staged_base_mode="$(stat -c '%a' -- "${staged_base}" 2>/dev/null || true)"
    if [ -z "${staged_base_mode}" ]; then
      echo "::error::IMPLEMENT_STAGED_SUPPORT_BASE_MISSING path=${staged_path} reason=mode_unavailable; cannot reinstall the support-ref copy."
      exit 1
    fi
    mkdir -p -- "$(dirname -- "${staged_path}")"
    install -m "${staged_base_mode}" -- "${staged_base}" "${staged_path}"
    echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALLED path=${staged_path} state=absent-in-head"
    reinstalled=$((reinstalled + 1))
  else
    echo "IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD path=${staged_path} state=recreated-by-editor"
    edited=$((edited + 1))
  fi
done < "${editor_head_ledger}"
echo "IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALL reinstalled=${reinstalled} edited_from_head=${edited} unknown=${unknown}"
exit 0
