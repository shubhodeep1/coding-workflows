#!/usr/bin/env bash
# review_commit_changes.sh — stage + commit editor output in review_autofix.yml.
#
# Extracted from the "Commit changes" step of review_autofix.yml because a
# single `run:` block exceeded GitHub Actions' 21,000-char template-expression
# limit (the limit applies per-`run:` block because each block is compiled as
# an expression template). Keeping the logic in a file lets it grow without
# re-approaching the limit.
#
# Inputs (environment):
#   CAN_PUSH                          "true" if the branch is writable; other values short-circuit.
#   COMMITTED_FILES_FILE              Path where a per-line list of committed paths is written.
#   IS_WORKFLOW_SOURCE_REPO           "true" on the coding-workflows repo itself.
#   ALLOW_WORKFLOW_EDITS              "true" to allow editor changes under .github/workflows.
#   AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE
#                                    "false" to suppress commit/push because the
#                                    existing validation tail cannot finish safely.
#   AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON
#                                    Machine-readable reason for the findings-only
#                                    partial-finalize fallback.
#   WRITE_GUARDS_ENABLED              "true" to enforce write-guard policy; false logs a bypass and continues.
#   RUNTIME_DIR                       Ephemeral per-run directory.
#   PRE_EDITOR_STATE_FILE             Optional snapshot of pre-editor tree state.
#   PRE_EDITOR_DIFF_BASELINE_FILE     Optional pre-editor baseline diff file.
#   LAST_RUN_DIFF_FILE                Diff from previous autofix iteration.
#   EDITOR_SUMMARY_FILE               Editor-produced summary (used by overlap validation).
#   REVIEW_LEDGER_PATH                Path to review-issue ledger (defaults to .ai/review_issue_ledger/pr-${PR_NUMBER}.txt). Gitignored; persisted across autofix iterations via actions/cache in review_autofix.yml.
#   GH_PAT                            GitHub token used to rewrite the origin remote URL.
#   GITHUB_REPOSITORY                 owner/repo slug (auto-set by GitHub Actions).
#
# Outputs:
#   $GITHUB_ENV:     DID_COMMIT, LEDGER_ONLY_COMMIT, LEDGER_ONLY_COMMIT_STRICT.
#   $GITHUB_OUTPUT:  did_commit, ledger_only_commit, ledger_only_commit_strict.
#
#   LEDGER_ONLY_COMMIT signals "this commit should NOT trigger an rb_judge
#   rerun" and is `true` for BOTH (a) the commit's only tracked path is the
#   review-issue ledger, AND (b) the editor's per-file audit reports the
#   autofix loop has converged (applied=0, already_applied≥1).  Existing
#   consumers (auto-merge, retrigger-guard, continuation-dispatch skip) read
#   this flag.
#
#   LEDGER_ONLY_COMMIT_STRICT signals ONLY (a) — the commit's tracked paths
#   equal the ledger path.  Callers that need to know "did the editor's
#   productive edits actually land?" (the EDITOR_CHANGES_LOST detector in
#   review_autofix.yml) MUST read this strict flag; reading
#   LEDGER_ONLY_COMMIT causes a false positive when the audit-convergence
#   branch fires on a commit that DID land real source-file edits
#   (bitsafe.io PR #177 / run 25653654000).
#   ${COMMITTED_FILES_FILE}:  one "- <path>" line per committed file, or a single marker line.
#
# Failure modes:
#   - Exits non-zero on untracked-removal failure or overlap-validation block.
#   - Exits 0 (no-op) when CAN_PUSH != "true" or nothing is staged.

set -euo pipefail

_review_commit_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${_review_commit_script_dir}/write_guard.sh"

if [ -z "${COMMITTED_FILES_FILE:-}" ]; then
  if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
    COMMITTED_FILES_FILE="${RUNTIME_DIR}/committed_files.txt"
    echo "::warning::COMMITTED_FILES_FILE was unset; defaulting to ${COMMITTED_FILES_FILE}."
  else
    echo "::error::COMMITTED_FILES_FILE is required when RUNTIME_DIR is unavailable."
    exit 1
  fi
fi

# Diagnostic: working tree state at the start of this step.
# Pairs with the "checkpoint=editor_exit" group emitted at the
# end of review_apply_fixes.sh.  If editor_exit shows edits but
# commit_step_start shows a clean tree, the reversion happens
# at the step boundary (runner transition) and no logic inside
# this step can recover.  See PR #1255.
echo "::group::Working tree state (checkpoint=commit_step_start)"
printf 'timestamp=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
_cp_status="$(git status --porcelain 2>/dev/null || true)"
if [ -n "${_cp_status}" ]; then
  printf '%s\n' "${_cp_status}" | head -n 40 || true
  _cp_status_lines="$(printf '%s\n' "${_cp_status}" | wc -l | tr -d '[:space:]' || echo 0)"
  [ "${_cp_status_lines:-0}" -gt 40 ] && echo "[truncated: ${_cp_status_lines} total porcelain lines]"
else
  echo "(clean)"
fi
echo "--- git diff --stat HEAD ---"
_cp_diffstat="$(git diff --stat HEAD 2>/dev/null || true)"
if [ -n "${_cp_diffstat}" ]; then
  printf '%s\n' "${_cp_diffstat}" | head -n 40 || true
  _cp_diffstat_lines="$(printf '%s\n' "${_cp_diffstat}" | wc -l | tr -d '[:space:]' || echo 0)"
  [ "${_cp_diffstat_lines:-0}" -gt 40 ] && echo "[truncated: ${_cp_diffstat_lines} total diffstat lines]"
fi
echo "::endgroup::"

echo "DID_COMMIT=false" >> "$GITHUB_ENV"
echo "did_commit=false" >> "$GITHUB_OUTPUT"
echo "LEDGER_ONLY_COMMIT=false" >> "$GITHUB_ENV"
echo "ledger_only_commit=false" >> "$GITHUB_OUTPUT"
echo "LEDGER_ONLY_COMMIT_STRICT=false" >> "$GITHUB_ENV"
echo "ledger_only_commit_strict=false" >> "$GITHUB_OUTPUT"

if [ "${CAN_PUSH:-false}" != "true" ]; then
  echo "Skipping commit/push: branch is not writable from this workflow."
  echo "- commit skipped (branch is not writable from this workflow)" > "${COMMITTED_FILES_FILE}"
  exit 0
fi

if [ "${AUTOFIX_PARTIAL_FINALIZE_REQUESTED:-false}" = "true" ] && [ "${AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE:-true}" != "true" ]; then
  withheld_reason="${AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON:-validation_tail_incomplete}"
  echo "Partial finalize findings-only fallback: discarding local edits before commit (${withheld_reason})."
  git reset --hard HEAD >/dev/null 2>&1 || true
  # Keep this list aligned with any persisted review/runtime directories that
  # must survive a findings-only partial-finalize cleanup.
  git clean -ffdx \
    -e .ai \
    -e .serena \
    -e scripts \
    -e prompts \
    -e .github/ai \
    -e .github/scripts \
    -e .github/prompts \
    -e .codex-workflow-src \
    -e .codex-workflow-src-main >/dev/null 2>&1 || true
  echo "- none (edits withheld for safety: ${withheld_reason})" > "${COMMITTED_FILES_FILE}"
  exit 0
fi

rm -f "${COMMITTED_FILES_FILE}"

# Drop unchanged bootstrap-owned Serena runtime state before any
# untracked-file cleanup or staging. If the repo already owned the
# Serena project config, or Codex mutated it away from the bootstrap
# hash, leave the tree on disk and let the staging guards below exclude
# it from commits without deleting repo-owned content. Defense-in-depth:
# if the preexisting detector ever misclassifies the tree, never delete a
# tracked `.serena/` subtree.
if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && [ -f .serena/project.yml ]; then
  current_serena_project_hash="$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "${current_serena_project_hash}" ] && [ "${current_serena_project_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then
    if ! git ls-files --error-unmatch -- .serena >/dev/null 2>&1; then
      rm -rf .serena
    fi
  fi
fi

# On the workflow source repo the editor is legitimately allowed
# to create new files (e.g. new workflow-templates/** assets,
# tests, prompts).  The IS_WORKFLOW_SOURCE_REPO=true branch of
# the staging logic below reconciles against PRE_EDITOR_STATE_FILE
# and will only stage paths the editor actually touched, so a
# blanket rm here would delete the editor's legitimate new files
# before that reconciliation runs (observed in PR #1330 where
# newly-created template files were deleted here, producing
# "Editor claimed changes but no commit was produced").
NEW_FILES_BEFORE_COMMIT_FILE="$(mktemp)"
git ls-files --others --exclude-standard -z > "${NEW_FILES_BEFORE_COMMIT_FILE}"
if [ -s "${NEW_FILES_BEFORE_COMMIT_FILE}" ]; then
  if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
    echo "Preserving newly created files in workflow source repo:"
    while IFS= read -r -d '' created_file; do
      [ -n "${created_file}" ] || continue
      echo "- ${created_file}"
    done < "${NEW_FILES_BEFORE_COMMIT_FILE}"
  else
    echo "Removing newly created files before commit (editor may not create new files):"
    while IFS= read -r -d '' created_file; do
      [ -n "${created_file}" ] || continue
      # Preserve infrastructure dirs needed by the conflict resolver step.
      # They are cleaned up after conflict resolution (or are harmless
      # untracked files if no conflicts exist).
      case "${created_file}" in
        .serena|.serena/*) continue ;;
        scripts/*|prompts/*) continue ;;
        changelog.d/*.md)
          # Editor-created changelog fragments are a legitimate review
          # fix, not a stray artifact: CLAUDE.md §20 requires one
          # fragment per behaviour-changing PR, reviewers flag its
          # absence, and nothing in the pipeline machinery writes into
          # changelog.d/.  Deleting the fragment here left the tree
          # clean at commit time and fired a false EDITOR_CHANGES_LOST
          # dead end on every fragment-only review round
          # (tele-funtoken-msg-scoring#3763, review run 32732281452).
          # The consumer-repo staging below picks the file up via its
          # untracked-files `git add` pass.
          echo "Preserving editor-created changelog fragment: ${created_file}"
          continue ;;
      esac
      echo "- ${created_file}"
      if ! rm -rf -- "${created_file}"; then
        echo "Failed to remove newly created path: ${created_file}"
        rm -f "${NEW_FILES_BEFORE_COMMIT_FILE}"
        exit 1
      fi
    done < "${NEW_FILES_BEFORE_COMMIT_FILE}"
  fi
fi
rm -f "${NEW_FILES_BEFORE_COMMIT_FILE}"

REVIEW_WRITE_GUARD_PRESTAGE_FILE="$(mktemp "${TMPDIR:-/tmp}/review-write-guard-prestage.XXXXXX")"
{
  git diff --name-only --diff-filter=ACMRD HEAD || true
  git ls-files --others --exclude-standard || true
} | sed '/^$/d' | sort -u > "${REVIEW_WRITE_GUARD_PRESTAGE_FILE}"
if ! write_guard_check review_editor "${REVIEW_WRITE_GUARD_PRESTAGE_FILE}"; then
  rm -f "${REVIEW_WRITE_GUARD_PRESTAGE_FILE}"
  exit 1
fi
rm -f "${REVIEW_WRITE_GUARD_PRESTAGE_FILE}"

if [ "${ALLOW_WORKFLOW_EDITS:-false}" != "true" ] && [ -n "$(git status --porcelain .github/workflows)" ]; then
  echo "Workflow edits are not allowed; discarding .github/workflows changes."
  git restore --source=HEAD --staged --worktree -- .github/workflows || true
  git clean -f -d -- .github/workflows || true
fi

# Remove workflow-generated/fetched artifacts so they are never
# committed to caller repos.  Skip when running on coding-workflows
# itself — these files are actual source code there, not artifacts.
# NOTE: scripts/ and prompts/ are preserved here for the conflict
# resolver step (codex exec needs the model catalog) and for later
# notification steps (Telegram, labeling).
# They are cleaned up in the final "Cleanup temporary artifacts" step.
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ]; then
  for _artifact in pre_assembled_static.txt unattended_system_instructions.md ai_pipeline.md agents.md; do
    if git ls-files --error-unmatch -- "${_artifact}" >/dev/null 2>&1; then
      continue
    fi
    rm -f -- "${_artifact}"
  done
fi

git config user.name "codex-bot"
git config user.email "codex@users.noreply.github.com"
git rm -r --cached node_modules 2>/dev/null || true

# Diagnostic: working tree state immediately before the
# touched-file comparison loop.  If commit_step_start showed
# edits but pre_touched_loop shows a clean tree, the reversion
# happens inside this step (untracked cleanup, protected-path
# reset, artifact rm, or similar).  See PR #1255.
echo "::group::Working tree state (checkpoint=pre_touched_loop)"
printf 'timestamp=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
_cp_status="$(git status --porcelain 2>/dev/null || true)"
if [ -n "${_cp_status}" ]; then
  printf '%s\n' "${_cp_status}" | head -n 40 || true
  _cp_status_lines="$(printf '%s\n' "${_cp_status}" | wc -l | tr -d '[:space:]' || echo 0)"
  [ "${_cp_status_lines:-0}" -gt 40 ] && echo "[truncated: ${_cp_status_lines} total porcelain lines]"
else
  echo "(clean)"
fi
echo "--- git diff --stat HEAD ---"
_cp_diffstat="$(git diff --stat HEAD 2>/dev/null || true)"
if [ -n "${_cp_diffstat}" ]; then
  printf '%s\n' "${_cp_diffstat}" | head -n 40 || true
  _cp_diffstat_lines="$(printf '%s\n' "${_cp_diffstat}" | wc -l | tr -d '[:space:]' || echo 0)"
  [ "${_cp_diffstat_lines:-0}" -gt 40 ] && echo "[truncated: ${_cp_diffstat_lines} total diffstat lines]"
fi
echo "::endgroup::"

if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
  # On the workflow source repo, commit ONLY files the editor
  # actually wrote to.  Compared to the old pathspec-exclusion list,
  # this lets any script — including former "canonical helpers"
  # (memory_helpers.sh, review_run_reviewers.sh, etc.) — be modified
  # when legitimate, while still blocking files that git merge,
  # reviewer side-effects, or anything else happened to auto-stage
  # with content the editor never touched.
  CODEX_TOUCHED_FILE="${RUNTIME_DIR}/codex_touched_autofix.txt"
  : > "${CODEX_TOUCHED_FILE}"
  if [ -f "${PRE_EDITOR_STATE_FILE:-/nonexistent}" ]; then
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
            printf '%s\n' "${diff_path}" >> "${CODEX_TOUCHED_FILE}"
            continue
          fi
          new_sha="$(git hash-object -- "${diff_path}" 2>/dev/null || true)"
          if [ -x "${diff_path}" ]; then
            new_exec=1
          else
            new_exec=0
          fi
          if { [ -n "${new_sha}" ] && [ "${new_sha}" != "${old_sha}" ]; } || [ "${new_exec}" != "${old_exec}" ]; then
            printf '%s\n' "${diff_path}" >> "${CODEX_TOUCHED_FILE}"
          fi
          ;;
        U)
          printf '%s\n' "${diff_a}" >> "${PRE_UNTRACKED_LIST}"
          ;;
      esac
    done < "${PRE_EDITOR_STATE_FILE}"
    git ls-files --others --exclude-standard > "${POST_UNTRACKED_LIST}" || true
    # Also capture new files the editor already staged (status `A `):
    # `git ls-files --others` excludes cached/staged paths, so without
    # this second source, newly-created-but-staged files are invisible
    # to the touched-file detector and get wiped by `git read-tree HEAD`
    # below, producing a no-op commit and a false "Editor changes lost"
    # alert (see run 24608448620 / PR #1330).
    git diff --cached --diff-filter=A --name-only HEAD >> "${POST_UNTRACKED_LIST}" 2>/dev/null || true
    sort -o "${PRE_UNTRACKED_LIST}" "${PRE_UNTRACKED_LIST}"
    sort -o "${POST_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"
    comm -13 "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}" >> "${CODEX_TOUCHED_FILE}"
    rm -f "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"
    # Safety union: never silently drop a file that is genuinely
    # modified vs HEAD on disk at commit time.  The SHA-based
    # loop above can miss such files when the pre-editor snapshot
    # SHA happens to equal the post-editor SHA (e.g. the file was
    # already in its edited state at snapshot time, or the
    # workflow-support overlay perturbed pre-SHAs for unrelated
    # files).  Missing a real edit surfaces as EDITOR_CHANGES_LOST
    # even though the edit is on disk (see run 24604441953 where
    # a script edit was modified but dropped).
    # Exclude paths that were already dirty before the editor run
    # (e.g. support-script overlay mode-bit flips) so this union
    # cannot stage workflow bootstrap side-effects as editor output.
    if [ -f "${PRE_EDITOR_DIFF_BASELINE_FILE:-/nonexistent}" ]; then
      CURRENT_DIFF_LIST="$(mktemp "${RUNTIME_DIR}/current_diff.XXXXXX" 2>/dev/null || echo "${RUNTIME_DIR}/current_diff.$$")"
      git diff --name-only HEAD | sort -u > "${CURRENT_DIFF_LIST}" || true
      comm -13 "${PRE_EDITOR_DIFF_BASELINE_FILE}" "${CURRENT_DIFF_LIST}" >> "${CODEX_TOUCHED_FILE}" || true
      rm -f "${CURRENT_DIFF_LIST}"
    else
      git diff --name-only HEAD >> "${CODEX_TOUCHED_FILE}" || true
    fi
  else
    echo "::warning::pre-editor snapshot missing; falling back to git diff HEAD."
    git diff --name-only HEAD >> "${CODEX_TOUCHED_FILE}" || true
    git ls-files --others --exclude-standard >> "${CODEX_TOUCHED_FILE}" || true
  fi
  sort -u -o "${CODEX_TOUCHED_FILE}" "${CODEX_TOUCHED_FILE}"
  touched_count="$(wc -l < "${CODEX_TOUCHED_FILE}" | tr -d '[:space:]')"
  echo "Editor-touched files (${touched_count}):"
  sed 's/^/ - /' "${CODEX_TOUCHED_FILE}" || true
  # Rebuild the index so it contains HEAD + only editor-touched
  # files.  This discards any path that entered the index for
  # other reasons (reviewer artifacts, lingering merge state from
  # earlier retries, etc.).
  git read-tree HEAD
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
  done < "${CODEX_TOUCHED_FILE}"
  rm -f "${CODEX_TOUCHED_FILE}"
else
  # Build per-file exclusions from scripts/.gitignore when present.
  # If it is absent, keep exclusions empty so consumer-owned scripts/
  # changes are still staged.
  _ra_script_excludes=()
  if [ -f scripts/.gitignore ]; then
    while IFS= read -r _ign_entry; do
      [[ -z "${_ign_entry}" || "${_ign_entry}" == \#* ]] && continue
      _ra_script_excludes+=(":!scripts/${_ign_entry}")
    done < scripts/.gitignore
  fi
  git add -u -- ':!node_modules' "${_ra_script_excludes[@]}" ':!prompts' ':!ai-memory' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.github/prompts' ':!.github/scripts'
  git ls-files --others --exclude-standard -z -- ':!node_modules' "${_ra_script_excludes[@]}" ':!prompts' ':!ai-memory' ':!.serena' ':!.serena/**' ':!.codex-workflow-src' ':!.codex-workflow-src-main' ':!.github/ai' ':!.github/prompts' ':!.github/scripts' | xargs -0 -r git add --
fi

echo "Staged files before commit:"
STAGED_FILES="$(git diff --cached --name-only || true)"
printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
# Soft guardrail: if workflow runtime/helper artifacts leaked into
# the staging area (e.g. the editor wrote to a protected path, or
# the consumer branch has leaked tracked copies from an older run),
# unstage them and continue.  A previous hard `exit 1` here silently
# threw away reviewer+editor work on what is usually a recoverable
# condition.
PROTECTED_LEAKED=false
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^\.github/(prompts|scripts)/'; then
  echo "::warning::.github/prompts or .github/scripts was staged in consumer repo; unstaging protected paths and continuing."
  git reset -q HEAD -- '.github/prompts' '.github/scripts' 2>/dev/null || true
  PROTECTED_LEAKED=true
fi
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" != "true" ] && printf '%s\n' "${STAGED_FILES}" | grep -Eq '^(prompts/|\.github/scripts/|\.github/prompts/|ai-memory/|\.codex-workflow-src/|\.codex-workflow-src-main/)'; then
  echo "::warning::workflow runtime/helper artifacts were staged in consumer repo; unstaging protected paths and continuing."
  git reset -q HEAD -- 'prompts' '.github/scripts' '.github/prompts' 'ai-memory' '.codex-workflow-src' '.codex-workflow-src-main' 2>/dev/null || true
  PROTECTED_LEAKED=true
fi
if [ "${PROTECTED_LEAKED}" = "true" ]; then
  STAGED_FILES="$(git diff --cached --name-only || true)"
  echo "Staged files after protected-path reset:"
  printf '%s\n' "${STAGED_FILES}" | sed '/^$/d; s/^/ - /' || true
fi

REVIEW_WRITE_GUARD_STAGED_FILE="$(mktemp "${TMPDIR:-/tmp}/review-write-guard-staged.XXXXXX")"
printf '%s\n' "${STAGED_FILES}" | sed '/^$/d' > "${REVIEW_WRITE_GUARD_STAGED_FILE}"
if ! write_guard_check review_editor "${REVIEW_WRITE_GUARD_STAGED_FILE}"; then
  rm -f "${REVIEW_WRITE_GUARD_STAGED_FILE}"
  exit 1
fi
rm -f "${REVIEW_WRITE_GUARD_STAGED_FILE}"

if git diff --cached --quiet; then
  echo "No repository changes to commit."
  echo "- none" > "${COMMITTED_FILES_FILE}"
else
  OVERLAP_REPORT_FILE="$(mktemp)"
  OVERLAP_VALIDATION_STDERR_FILE="$(mktemp)"
  set +e
  PYTHONDONTWRITEBYTECODE=1 python3 - "${LAST_RUN_DIFF_FILE}" "${OVERLAP_REPORT_FILE}" 2>"${OVERLAP_VALIDATION_STDERR_FILE}" <<'PY'
import re
import subprocess
import sys
from collections import defaultdict

last_run_diff_path = sys.argv[1]
overlap_report_path = sys.argv[2]

hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
file_re = re.compile(r"^\+\+\+ b/(.+)$")
staged_cmd = ["git", "diff", "--cached", "--unified=0", "--no-color"]
staged_diff = subprocess.run(staged_cmd, check=True, capture_output=True, text=True).stdout.splitlines()

def parse_ranges(lines, use_side):
    ranges = defaultdict(list)
    current_file = None
    for line in lines:
        file_match = file_re.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue
        hunk_match = hunk_re.match(line)
        if not hunk_match or current_file is None:
            continue
        if use_side == "new":
            start = int(hunk_match.group(3))
            count = int(hunk_match.group(4) or "1")
        else:
            start = int(hunk_match.group(1))
            count = int(hunk_match.group(2) or "1")
        if count <= 0:
            continue
        end = start + count - 1
        ranges[current_file].append((start, end))
    return ranges

with open(last_run_diff_path, "r", encoding="utf-8", errors="replace") as fh:
    last_run_lines = fh.read().splitlines()

last_run_ranges = parse_ranges(last_run_lines, "new")
staged_ranges = parse_ranges(staged_diff, "old")

# Workflow-generated artifacts that are removed before commit should
# never trigger the oscillation guard.
EXCLUDED_FILES = {"pre_assembled_static.txt"}

# Non-code files where "Runtime failure path" is semantically
# meaningless — documentation, configs, licences, etc.
_NON_CODE_EXTS = {
    ".md", ".txt", ".rst", ".adoc",
    ".json", ".yaml", ".yml", ".toml",
    ".csv", ".tsv",
    ".lock",
}
_NON_CODE_BASENAMES = {"LICENSE", "LICENSE.txt", "NOTICE"}

def _is_non_code(path: str) -> bool:
    import os as _os
    basename = _os.path.basename(path)
    if basename in _NON_CODE_BASENAMES:
        return True
    _, ext = _os.path.splitext(basename)
    return ext.lower() in _NON_CODE_EXTS

overlaps = []
for file_path, staged_file_ranges in staged_ranges.items():
    if file_path in EXCLUDED_FILES:
        continue
    if _is_non_code(file_path):
        continue
    previous_ranges = last_run_ranges.get(file_path, [])
    if not previous_ranges:
        continue
    for staged_start, staged_end in staged_file_ranges:
        for previous_start, previous_end in previous_ranges:
            if staged_start <= previous_end and previous_start <= staged_end:
                overlaps.append(
                    f"{file_path}:{staged_start}-{staged_end} overlaps {previous_start}-{previous_end}"
                )
                break

with open(overlap_report_path, "w", encoding="utf-8") as out:
    for item in overlaps:
        out.write(item + "\n")

print(f"OVERLAP_COUNT={len(overlaps)}")
PY
  overlap_validation_exit_code="$?"
  set -e
  if [ "${overlap_validation_exit_code}" -eq 0 ]; then
    overlap_count="$(wc -l < "${OVERLAP_REPORT_FILE}" | tr -d '[:space:]')"
    if [ "${overlap_count}" -gt 0 ]; then
      echo "Detected staged edits overlapping hunks from the previous AI autofix run:"
      cat "${OVERLAP_REPORT_FILE}"
      _has_regression=false
      _has_runtime=false
      _is_fallback=false
      grep -qi 'Regression fingerprint:' "${EDITOR_SUMMARY_FILE}" && _has_regression=true || true
      grep -qi 'Runtime failure path:' "${EDITOR_SUMMARY_FILE}" && _has_runtime=true || true
      grep -Eqi 'editor failed before producing a validated summary|unavailable \(editor fallback\)' "${EDITOR_SUMMARY_FILE}" && _is_fallback=true || true
      if [ "${_has_regression}" != "true" ] || [ "${_has_runtime}" != "true" ]; then
        if [ "${_is_fallback}" = "true" ]; then
          echo "Overlap detected, but editor ran in fallback mode; allowing commit without strict metadata enforcement."
        else
          echo "Blocking commit: overlapping hunks require both 'Regression fingerprint:' and 'Runtime failure path:' in editor summary."
          rm -f "${OVERLAP_REPORT_FILE}" "${OVERLAP_VALIDATION_STDERR_FILE}"
          exit 1
        fi
      else
        echo "Overlap validation passed using editor summary metadata."
      fi
    fi
  else
    echo "Overlap validation script failed unexpectedly (exit ${overlap_validation_exit_code})."
    if [ -s "${OVERLAP_VALIDATION_STDERR_FILE}" ]; then
      echo "---- overlap validation stderr ----"
      cat "${OVERLAP_VALIDATION_STDERR_FILE}"
      echo "---- end overlap validation stderr ----"
    fi
    overlap_count="$(wc -l < "${OVERLAP_REPORT_FILE}" | tr -d '[:space:]' || echo "0")"
    if [ "${overlap_count}" -gt 0 ]; then
      echo "Blocking commit because overlap validation failed and overlap report is non-empty."
      rm -f "${OVERLAP_REPORT_FILE}" "${OVERLAP_VALIDATION_STDERR_FILE}"
      exit 1
    fi
    echo "Continuing because overlap report is empty (treating as OVERLAP_COUNT=0 fallback)."
  fi
  rm -f "${OVERLAP_REPORT_FILE}" "${OVERLAP_VALIDATION_STDERR_FILE}"

  git commit -m "[ai-autofix] apply PR fixes"
  {
    git diff-tree --no-commit-id --name-only -r -z HEAD \
      | while IFS= read -r -d '' changed_file; do
          printf -- '- %s\n' "${changed_file}"
        done
  } > "${COMMITTED_FILES_FILE}"
  git remote set-url origin "https://x-access-token:${GH_PAT}@github.com/${GITHUB_REPOSITORY}"
  # NOTE: do NOT push here. The push is deferred to the final
  # "Push all pending commits" step so that conflict resolution,
  # labeling, and auto-merge complete before the synchronize event
  # fires.  Pushing here causes a concurrency self-cancel race
  # that kills all subsequent steps.
  echo "DID_COMMIT=true" >> "$GITHUB_ENV"
  echo "did_commit=true" >> "$GITHUB_OUTPUT"

  # Detect ledger-only commits: the autofix bookkeeping ledger
  # (REVIEW_LEDGER_PATH, default .ai/review_issue_ledger/pr-<PR_NUMBER>.txt)
  # is updated by scripts/review_issue_ledger.sh on every review pass
  # regardless of whether the editor actually edited any repo file.
  # With the default path gitignored and persisted via actions/cache,
  # the ledger itself is never staged, so in the default configuration
  # this detector will not fire. Consumers that override
  # REVIEW_LEDGER_PATH to a tracked path still need the detection:
  # when the commit's only tracked path is the ledger, the editor
  # made no productive change and downstream "clean review" gates
  # (auto-merge, ready-to-merge label, telegram success) should
  # still fire. The safety gates (EDITOR_CHANGES_LOST,
  # EDITOR_NOOP_SUSPICIOUS) also need to run so the no-op claim is
  # still verified. See PR #1472 for the bug this addresses: the
  # self-triggered-autofix gate skip (PR #1459) short-circuits the
  # synchronize re-run that would otherwise enable auto-merge, so
  # auto-merge must be enabled in THIS run for ledger-only commits.
  # Normalize both sides of the path comparison so equivalent
  # relative-path spellings match. `git diff-tree --name-only`
  # emits canonical relative paths (no ./ prefix, no repeated
  # slashes, no trailing slash), but a consumer that sets
  # REVIEW_LEDGER_PATH to e.g. ./.ai/review_issue_ledger/pr-123.txt
  # or .ai//review_issue_ledger/pr-123.txt would otherwise silently
  # fail to match and leave the PR stuck in the auto-merge
  # state this step is designed to resolve. See PR #1476.
  normalize_rel_path() {
    printf '%s\n' "$1" | sed -e 's#^\(\./\)\+##' -e 's#//*#/#g' -e 's#/$##'
  }
  SAFE_PR_NUMBER="${PR_NUMBER:-0}"
  SAFE_PR_NUMBER="${SAFE_PR_NUMBER//[^0-9]/}"
  : "${SAFE_PR_NUMBER:=0}"
  LEDGER_PATH_FOR_DETECTOR="${REVIEW_LEDGER_PATH:-.ai/review_issue_ledger/pr-${SAFE_PR_NUMBER}.txt}"
  NORMALIZED_LEDGER_PATH_FOR_DETECTOR="$(normalize_rel_path "${LEDGER_PATH_FOR_DETECTOR}")"
  COMMIT_PATHS_RAW="$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null || true)"
  COMMIT_PATH_COUNT="$(printf '%s\n' "${COMMIT_PATHS_RAW}" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
  SINGLE_COMMIT_PATH="$(printf '%s\n' "${COMMIT_PATHS_RAW}" | sed '/^$/d')"
  NORMALIZED_SINGLE_COMMIT_PATH="$(normalize_rel_path "${SINGLE_COMMIT_PATH}")"
  if [ "${COMMIT_PATH_COUNT}" = "1" ] && [ "${NORMALIZED_SINGLE_COMMIT_PATH}" = "${NORMALIZED_LEDGER_PATH_FOR_DETECTOR}" ]; then
    echo "Commit contains only the review-issue ledger (${LEDGER_PATH_FOR_DETECTOR}); marking as editor no-op for downstream clean-review gates."
    echo "LEDGER_ONLY_COMMIT=true" >> "$GITHUB_ENV"
    echo "ledger_only_commit=true" >> "$GITHUB_OUTPUT"
    echo "LEDGER_ONLY_COMMIT_STRICT=true" >> "$GITHUB_ENV"
    echo "ledger_only_commit_strict=true" >> "$GITHUB_OUTPUT"
  else
    # Audit-driven convergence detector.  When every per-file audit entry
    # in the editor summary reports `issues applied: 0` AND the cumulative
    # `issues already applied` is ≥1, the editor's own bookkeeping says
    # "nothing new to do" — every reviewer finding was already satisfied
    # in a prior iteration.  Treat that as ledger-only-equivalent so the
    # autofix loop converges (skip rb_judge, fall through to auto-merge)
    # even when an unrelated file (e.g. the editor noting a follow-up
    # one-liner) sneaked into the commit.  Without this, the path-only
    # heuristic above fails open and the loop forces an rb_judge call
    # against a converged tree.
    #
    # Disable by setting AUTOFIX_AUDIT_CONVERGENCE_ENABLED=false.
    _audit_convergence_applies=false
    if [ "${AUTOFIX_AUDIT_CONVERGENCE_ENABLED:-true}" = "true" ] \
        && [ -n "${EDITOR_SUMMARY_FILE:-}" ] \
        && [ -s "${EDITOR_SUMMARY_FILE}" ]; then
      # Sum the per-file audit counters.  Lines look like
      #   "- … total issues listed: N, issues applied: A, issues already applied: AA, issues ignored: I"
      # The same regex anchors the existing arithmetic-mismatch check at
      # review_autofix.yml:3055-3079, so use the same tolerant matching.
      _audit_total_applied=0
      _audit_total_already=0
      _audit_total_lines=0
      while IFS= read -r _audit_line; do
        [ -n "${_audit_line}" ] || continue
        _t="$(printf '%s' "${_audit_line}" | grep -ioP 'total issues listed[^0-9]*\K[0-9]+' || true)"
        [ -n "${_t}" ] || continue
        _a="$(printf '%s' "${_audit_line}" | grep -ioP '(?<!already )issues applied[^0-9]*\K[0-9]+' || echo 0)"
        _aa="$(printf '%s' "${_audit_line}" | grep -ioP 'issues already applied[^0-9]*\K[0-9]+' || echo 0)"
        _audit_total_lines=$((_audit_total_lines + 1))
        _audit_total_applied=$((_audit_total_applied + ${_a:-0}))
        _audit_total_already=$((_audit_total_already + ${_aa:-0}))
      done < "${EDITOR_SUMMARY_FILE}"

      if [ "${_audit_total_lines}" -gt 0 ] \
          && [ "${_audit_total_applied}" -eq 0 ] \
          && [ "${_audit_total_already}" -ge 1 ]; then
        _audit_convergence_applies=true
        echo "Audit convergence detected: ${_audit_total_lines} reviewer file(s), applied=${_audit_total_applied}, already_applied=${_audit_total_already}. Treating as ledger-only-equivalent so the autofix loop converges."
      fi
    fi

    if [ "${_audit_convergence_applies}" = "true" ]; then
      echo "LEDGER_ONLY_COMMIT=true" >> "$GITHUB_ENV"
      echo "ledger_only_commit=true" >> "$GITHUB_OUTPUT"
      # Strict flag stays false: audit convergence is NOT the same as
      # "only the ledger was committed".  Callers that gate on whether
      # the editor's productive edits actually landed (the
      # EDITOR_CHANGES_LOST detector in review_autofix.yml) MUST read
      # the strict flag to avoid a false positive on commits that
      # contain real source-file diffs alongside a converged audit
      # (bitsafe.io PR #177 / run 25653654000).
      echo "LEDGER_ONLY_COMMIT_STRICT=false" >> "$GITHUB_ENV"
      echo "ledger_only_commit_strict=false" >> "$GITHUB_OUTPUT"
    else
      echo "LEDGER_ONLY_COMMIT=false" >> "$GITHUB_ENV"
      echo "ledger_only_commit=false" >> "$GITHUB_OUTPUT"
      echo "LEDGER_ONLY_COMMIT_STRICT=false" >> "$GITHUB_ENV"
      echo "ledger_only_commit_strict=false" >> "$GITHUB_OUTPUT"
    fi
  fi
fi
