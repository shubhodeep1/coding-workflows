#!/usr/bin/env bash
# Body of the "Stage codex logs for upload (failure or empty-editor)" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail
# Stage everything under RUNNER_TEMP so the Cleanup step
# below cannot race the upload — it rm -rf's RUNTIME_DIR.
# The upload step's path: glob is also more predictable when
# rooted at a single dir we own.
STAGE_DIR="${RUNNER_TEMP:-/tmp}/codex-review-autofix-failure-logs"
rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}/runtime" "${STAGE_DIR}/previous_reviews" "${STAGE_DIR}/codex-home-log"

if [ -d "${RUNTIME_DIR:-}" ]; then
  # Top-level prompts, summaries, and diagnostic context. The
  # editor_* and conflict_resolver_* files are the codex stdout
  # / synthesised summaries; floor_tags / review_issues /
  # ledger_status / consolidator_raw / parser_stats are the
  # advisory editor inputs declared at the top of
  # scripts/review_apply_fixes.sh — without them a cold post-
  # mortem can't tell whether bad consolidator/parser output
  # was the upstream cause of an empty editor turn.
  # pr_check_runs_context.txt and reviewer_consensus.txt let
  # us reconstruct what context the editor saw without re-
  # fetching from the API. summariser_{pass1,review}.log hold
  # each consensus-summariser attempt's stderr tail (issue
  # #4653: exit 0 with empty stdout had no uploaded trace).
  for f in editor_summary.txt editor_prompt.txt editor_prompt_body.txt \
           pr_editor_comment.txt reviewer_consensus.txt \
           reviewer_prompt.txt reviewer_prompt_body.txt \
           reviewer_prompt_pass1.txt reviewer_prompt_pass2.txt \
           cross_pollination_summary.txt \
           reviewer_bundle.txt reviewer_manifest.txt \
           conflict_resolver_summary.txt conflict_resolver_prompt.txt \
           committed_files.txt pr_check_runs_context.txt \
           pr_meta.json pr_all_comments_context.txt \
           linked_issue_context.txt memory_context.txt \
           pr_diff.patch original_pr_diff.patch \
           last_run_diff.patch last_run_changed_files.txt \
           pr_changed_files.txt last_commit_stat.txt \
           last_run_diff_stat.txt \
           symbol_diff_summary.txt \
           floor_tags.txt review_issues.txt ledger_status.txt \
           consolidator_raw.txt parser_stats.txt \
           summariser_pass1.log summariser_review.log; do
    [ -f "${RUNTIME_DIR}/${f}" ] && \
      cp -f "${RUNTIME_DIR}/${f}" "${STAGE_DIR}/runtime/${f}" || true
  done
fi

# Per-attempt editor stdout / stderr — the primary signal for
# the empty-output failure. review_apply_fixes.sh writes
# editor_attempt_${attempt}.txt (codex stdout) and
# editor_attempt_${attempt}.err (codex stderr) for every retry,
# plus editor_attempt_${attempt}_changes_lost.txt when the
# claimed-changes-lost guard fires. The reviewer-side .log /
# .txt / status_* files plus pass1/pass2 two-pass logs and
# consensus_pass1.txt cover the reviewer-fanout failure modes
# (all written by review_run_reviewers.sh into
# PREVIOUS_REVIEWS_DIR — note: reviewers produce .log not
# .err, the .err extension is editor-only). Without these the
# only remaining trace of why a codex stage produced 0 bytes
# is the truncated stderr in the live job log.
if [ -d "${PREVIOUS_REVIEWS_DIR:-}" ]; then
  for f in "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.txt \
           "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*.err \
           "${PREVIOUS_REVIEWS_DIR}"/editor_attempt_*_changes_lost.txt \
           "${PREVIOUS_REVIEWS_DIR}"/review_*.txt \
           "${PREVIOUS_REVIEWS_DIR}"/review_*.log \
           "${PREVIOUS_REVIEWS_DIR}"/pass1_*.txt \
           "${PREVIOUS_REVIEWS_DIR}"/pass1_*.log \
           "${PREVIOUS_REVIEWS_DIR}"/pass2_*.txt \
           "${PREVIOUS_REVIEWS_DIR}"/pass2_*.log \
           "${PREVIOUS_REVIEWS_DIR}"/status_*.txt \
           "${PREVIOUS_REVIEWS_DIR}"/consensus_pass1.txt \
           "${PREVIOUS_REVIEWS_DIR}"/cache_probe_*.txt \
           "${PREVIOUS_REVIEWS_DIR}"/cache_probe_*.log; do
    [ -f "${f}" ] && cp -f "${f}" "${STAGE_DIR}/previous_reviews/" || true
  done
fi

# Codex CLI's own session log directory carries the raw
# OpenRouter wire trace (request/response bodies, finish_reason,
# tool-router rejections). The editor stage runs under the
# default ${CODEX_HOME:-$HOME/.codex}, which survives until
# this step. Reviewer-fanout and summariser stages run under
# ephemeral ${RUNNER_TEMP}/codex_home_{reviewers,summariser,
# consolidator,...} dirs that those scripts rm -rf themselves
# before this step runs (see review_run_reviewers.sh L816,
# L958, L970, L1019 and summarize_reviewer_consensus.sh L211
# trap), so this glob captures editor wire logs but NOT
# reviewer/summariser ones — those are reconstructed from the
# PREVIOUS_REVIEWS_DIR/.log files copied above. Preserving
# reviewer wire logs would require a script-side change
# (logged as a follow-up). Copy unconditionally with no
# mtime filter so multi-attempt failures don't lose the
# earliest session log to a sliding window; retention-days
# on the upload step bounds storage cost.
if [ -d "${CODEX_HOME:-$HOME/.codex}/log" ]; then
  cp -rf "${CODEX_HOME:-$HOME/.codex}/log/." "${STAGE_DIR}/codex-home-log/" 2>/dev/null || true
fi

echo "CODEX_REVIEW_AUTOFIX_FAILURE_LOG_DIR=${STAGE_DIR}" >> "$GITHUB_ENV"
echo "Staged $(find "${STAGE_DIR}" -type f | wc -l) file(s) for review_autofix failure-log upload."
