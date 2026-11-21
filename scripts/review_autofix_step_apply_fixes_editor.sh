#!/usr/bin/env bash
# Body of the "Apply fixes with editor model" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

# On the workflow source repo, snapshot the working-tree state before
# the editor runs.  The "Commit changes" step below uses this snapshot
# to stage ONLY files the editor actually wrote to (computed by
# hashing each file pre/post plus executable-bit changes), instead of
# relying on hardcoded
# ':!scripts/<helper>' pathspec excludes that cannot unstage files
# already in the index.  This lets any script — including former
# "canonical helpers" like review_apply_fixes.sh — be legitimately
# modified, while still blocking files the editor never touched from
# riding along in the commit.
# Consumer repos use per-file exclusions from scripts/.gitignore
# so consumer-owned scripts (e.g. scripts/security/*.js) are staged.
#
# Both repo kinds: record which paths are ALREADY untracked before
# the editor runs.  scripts/review_commit_changes.sh uses this in
# consumer repos to tell an editor-created file (new since this
# snapshot: keep and stage it) from a pre-existing stray or
# runtime leftover (remove it).  Without it the consumer cleanup
# deleted every new file, which turned any review round whose fix
# was a new file (a db/contracts/*.yml contract, a regression
# test) into a deterministic EDITOR_CHANGES_LOST dead end
# (tele-funtoken-msg-scoring#4287).
PRE_EDITOR_UNTRACKED_FILE="${RUNTIME_DIR}/pre_editor_untracked.txt"
if git ls-files --others --exclude-standard -z | sort -zu > "${PRE_EDITOR_UNTRACKED_FILE}.tmp" \
    && mv -f -- "${PRE_EDITOR_UNTRACKED_FILE}.tmp" "${PRE_EDITOR_UNTRACKED_FILE}"; then
  echo "Pre-editor untracked snapshot captured: $(tr -cd '\0' < "${PRE_EDITOR_UNTRACKED_FILE}" | wc -c | tr -d '[:space:]') paths"
else
  rm -f -- "${PRE_EDITOR_UNTRACKED_FILE}.tmp" "${PRE_EDITOR_UNTRACKED_FILE}"
  echo "::warning::Pre-editor untracked snapshot capture failed; legacy delete-all fallback will apply."
fi
echo "PRE_EDITOR_UNTRACKED_FILE=${PRE_EDITOR_UNTRACKED_FILE}" >> "$GITHUB_ENV"
if [ "${IS_WORKFLOW_SOURCE_REPO:-false}" = "true" ]; then
  PRE_EDITOR_STATE_FILE="${RUNTIME_DIR}/pre_editor_state.tsv"
  : > "${PRE_EDITOR_STATE_FILE}"
  PRE_EDITOR_DIFF_BASELINE_FILE="${RUNTIME_DIR}/pre_editor_head_diff_paths.txt"
  git diff --name-only HEAD | sort -u > "${PRE_EDITOR_DIFF_BASELINE_FILE}" || true
  # `sort -zu` dedupes: in an active merge state, `git ls-files -z`
  # emits conflicted paths once per index stage (1/2/3).  The
  # autofix step should never run in merge state, but the guard is
  # cheap and makes the snapshot reusable from the resolver step.
  while IFS= read -r -d '' snap_path; do
    [ -f "${snap_path}" ] || [ -L "${snap_path}" ] || continue
    snap_exec=0
    if [ -L "${snap_path}" ]; then
      # Hash a symlink as git stores it (its link text), never the file it
      # points to: workflow-templates/CLAUDE.md -> ../CLAUDE.md otherwise
      # "changes" whenever CLAUDE.md does, and the touched-set check rejects
      # a correct resolution as out-of-scope (PR #4443, run 36120214576).
      snap_sha="$(printf '%s' "$(readlink -- "${snap_path}")" | git hash-object --stdin)"
    else
      [ -x "${snap_path}" ] && snap_exec=1
      snap_sha="$(git hash-object -- "${snap_path}")"
    fi
    printf 'T\t%s\t%s\t%s\n' \
      "${snap_sha}" \
      "${snap_exec}" \
      "${snap_path}" >> "${PRE_EDITOR_STATE_FILE}"
  done < <(git ls-files -z | sort -zu)
  while IFS= read -r snap_path; do
    [ -n "${snap_path}" ] && \
      printf 'U\t%s\n' "${snap_path}" >> "${PRE_EDITOR_STATE_FILE}"
  done < <(git ls-files --others --exclude-standard)
  echo "PRE_EDITOR_STATE_FILE=${PRE_EDITOR_STATE_FILE}" >> "$GITHUB_ENV"
  echo "PRE_EDITOR_DIFF_BASELINE_FILE=${PRE_EDITOR_DIFF_BASELINE_FILE}" >> "$GITHUB_ENV"
  echo "Pre-editor snapshot captured: $(wc -l < "${PRE_EDITOR_STATE_FILE}") entries"
fi

# Keep the editor's stderr for the failure fingerprint ("Assemble
# failure evidence"); it still reaches the step log through tee.
bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" 2> >(tee -a "${RUNTIME_DIR}/editor_stage_stderr.txt" >&2)

# Bounded in-step retry for first-iteration empty-output failures.
#
# When the editor crashes/times out on the very first autofix
# iteration (no prior `[ai-autofix]` commits on HEAD), the
# downstream "Post editor summary comment" step posts a "will
# retry" comment and exits non-zero, deferring the retry to
# the stall poller's next cron tick — typically 1–3 hours
# later, which directly contributes to ai:done elapsed time.
# On iteration 2+ an empty editor output is a legitimate
# convergence signal (no new defects), so the retry is
# gated to the first-iteration case only.
#
# Skipped when AUTOFIX_INSTEP_RETRY_ENABLED=false so consumers
# can opt out without redeploying the workflow.  Tune the
# cooldown via AUTOFIX_INSTEP_RETRY_SLEEP_SECS (default 30) so
# slower runner environments can give the editor more time to
# recover from transient crashes/timeouts without holding the
# job for an excessive fixed window.
_instep_retry_enabled="${AUTOFIX_INSTEP_RETRY_ENABLED:-true}"
_instep_retry_sleep="${AUTOFIX_INSTEP_RETRY_SLEEP_SECS:-30}"
if ! [[ "${_instep_retry_sleep}" =~ ^[0-9]+$ ]] || [ "${_instep_retry_sleep}" -lt 1 ] || [ "${_instep_retry_sleep}" -gt 600 ]; then
  echo "::warning::AUTOFIX_INSTEP_RETRY_SLEEP_SECS must be an integer in [1, 600]; defaulting to 30."
  _instep_retry_sleep=30
fi
# Detect whether the editor's just-written summary is unusable
# for downstream consumers (commit step, push step, validator).
# Two signals count, both of which mean "no productive editor
# output landed":
#   1. The summary file is missing or zero-length (the original
#      retry condition).
#   2. The summary file exists but contains the same fallback /
#      failure markers that the downstream "Validate editor no-op
#      disposition" step greps for to set EDITOR_NOOP_SUSPICIOUS.
#      `scripts/review_apply_fixes.sh` writes a ~600-byte fallback
#      summary when all editor attempts fail — non-empty on disk,
#      so the original `[ ! -s ... ]` test missed it. The marker
#      regex must stay in lockstep with the validate step's grep
#      (both reference "editor failed before producing" /
#      "unavailable (editor fallback)") — a divergence that lets
#      the fallback summary slip past the validator would
#      reintroduce the run-25126757724 cascade.
# The flag name is "unusable" rather than "empty" because case
# (2) leaves a non-empty summary on disk that nonetheless cannot
# drive a productive autofix commit.
_instep_retry_summary_unusable="false"
if [ ! -s "${EDITOR_SUMMARY_FILE:-/dev/null}" ]; then
  _instep_retry_summary_unusable="true"
elif grep -qiE 'editor failed before producing|unavailable \(editor fallback\)' "${EDITOR_SUMMARY_FILE}" 2>/dev/null; then
  _instep_retry_summary_unusable="true"
  # Wording deliberately omits the literal substring
  # "Editor summary contains failure/fallback markers" — the e2e
  # poller in test-and-mark-stable.yml greps live job logs for
  # the validator's `::warning::` annotation that uses that
  # phrase, and a retry-side echo carrying the same literal
  # would false-trigger the poller's early-exit before the
  # retry has had a chance to succeed (see PR #1798 review).
  echo "::notice::Editor summary present but matched fallback-marker regex — treating as unusable for in-step retry decision."
fi
if [ "${_instep_retry_enabled}" = "true" ] \
    && [ "${_instep_retry_summary_unusable}" = "true" ]; then
  # `git log` here intentionally walks HEAD on the already-
  # checked-out PR branch (the surrounding workflow checks
  # out the PR head before this step runs, see "Checkout PR
  # head branch" earlier in the job).  If this block is ever
  # extracted to a standalone script, the caller must ensure
  # the PR branch is current.
  # `grep -c` prints "0" AND exits 1 when there are no matches, so
  # `|| echo 0` would append a second "0" (yielding "0\n0") and the
  # subsequent equality check would silently fail — the retry would
  # never fire because the comparison is never the literal "0".
  # Use `|| true` instead so grep's own "0" output is the only value
  # captured.  See PR #1703 review comment by copilot-pull-request-reviewer.
  # The grouping (`^(A|B)`) is semantically equivalent to
  # `^A|^B` under standard ERE precedence, but is more
  # robust against refactors and reviewer-bot misreadings.
  _prior_autofix_count="$(git log --format='%s' -n 5 2>/dev/null \
    | grep -cE '^(\[ai-autofix\]|\[judge-fix\])' || true)"
  if [ "${_prior_autofix_count}" = "0" ]; then
    echo "::warning::Editor produced no summary on first iteration — retrying once after ${_instep_retry_sleep}s before falling through to noop-comment path."
    sleep "${_instep_retry_sleep}"
    bash "${SUPPORT_SCRIPTS_DIR}/review_apply_fixes.sh" 2> >(tee -a "${RUNTIME_DIR}/editor_stage_stderr.txt" >&2) || {
      echo "::warning::Editor in-step retry also failed; downstream noop-comment path will fire."
    }
  fi
fi
