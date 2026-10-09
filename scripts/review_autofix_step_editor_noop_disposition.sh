#!/usr/bin/env bash
# Body of the "Validate editor no-op disposition" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

EDITOR_NOOP_SUSPICIOUS="false"
# Additive refusal-specific signal (per CLAUDE.md §6 — do NOT
# rename EDITOR_NOOP_SUSPICIOUS; add alongside). Set when the
# fallback summary contains the "model refused (safety filter)"
# sentinel that review_apply_fixes.sh writes after detecting an
# OpenAI-style refusal. Lets downstream consumers (Telegram
# alerts, operators) distinguish "model refused — re-run" from
# generic "no-op suspicious — manual review".
EDITOR_NOOP_REFUSAL="false"
# Additive recoverable-failure signal (CLAUDE.md §6 — add
# alongside, never rename). Set when the summary is the
# `recoverable_failure` partial-finalize fallback that
# review_apply_fixes.sh writes after every editor attempt
# failed (provider/broker error, timeout, empty output). Lets
# the Telegram step say "editor failed on every attempt" with
# the real provider error instead of the generic "editor
# claimed no changes needed" wording, which is wrong for this
# case: the editor never produced output at all.
EDITOR_NOOP_RECOVERABLE_FAILURE="false"

if [ ! -s "${EDITOR_SUMMARY_FILE:-}" ]; then
  echo "::warning::Editor summary file is missing or empty — editor never produced output."
  EDITOR_NOOP_SUSPICIOUS="true"
else
  # ── Check 1: Detect editor fallback/failure markers ──
  # The fallback summary (review_apply_fixes.sh) writes known
  # sentinel phrases when all editor attempts fail.  These must
  # never reach auto-merge.
  if grep -qiE 'editor failed before producing|unavailable \(editor fallback\)' "${EDITOR_SUMMARY_FILE}"; then
    echo "::warning::Editor summary contains failure/fallback markers — editor never completed a validated review."
    EDITOR_NOOP_SUSPICIOUS="true"
  fi

  # ── Check 1b: Detect refusal-specific sentinel ──
  # When the editor model returns a safety-policy refusal,
  # review_apply_fixes.sh writes "Runtime failure path: -
  # model refused (safety filter)" into the fallback summary
  # (§20.10). Set both flags here because the partial-finalize
  # refusal summary does not carry Check 1's failure markers,
  # and Check 2 is skipped when REVIEWERS_SUCCESSFUL=0. The
  # refusal flag selects the more specific downstream alert.
  if grep -qiE 'model refused \(safety filter\)' "${EDITOR_SUMMARY_FILE}"; then
    echo "::notice::Editor returned a safety-policy refusal — re-run is likely to succeed once the provider-side cache expires."
    EDITOR_NOOP_SUSPICIOUS="true"
    EDITOR_NOOP_REFUSAL="true"
  fi

  # ── Check 1c: Detect recoverable-failure partial-finalize sentinel ──
  # When all editor attempts fail, review_apply_fixes.sh writes
  # the recoverable_failure partial-finalize summary whose
  # "Runtime failure path:" line carries the sentinel below.
  # This is the same class of failure Check 1 exists for (the
  # editor never completed a validated review), so it sets
  # EDITOR_NOOP_SUSPICIOUS=true itself instead of relying on
  # Check 2: Check 2 runs only when REVIEWERS_SUCCESSFUL > 0, and
  # the editor can run with zero successful reviewers when every
  # reviewer slot was skipped fail-open (skipped_open /
  # skipped_unmapped, scripts/review_run_reviewers.sh) — in that
  # state the summary used to leave EDITOR_NOOP_SUSPICIOUS=false,
  # so the "Telegram editor-noop-suspicious warning" alert never
  # fired and the merge-conflict detect/resolver chain gated on
  # `EDITOR_NOOP_SUSPICIOUS != 'true'` still ran with nothing to
  # land (§20.10). With reviewers > 0 the result is unchanged
  # (Check 2 flagged the fallback-only audit section anyway).
  # The additive flag classifies the cause for the alert.
  # Observed on PR #4077 (runs 34692519987, 34700918528,
  # 34702442346): the editor's broker returned HTTP 429 on every
  # attempt and the operator was told the editor "claimed no
  # changes needed". The sentinel must stay verbatim in lockstep
  # with review_apply_fixes.sh (tests/test_review_autofix_editor_noop_cascade_contract.py).
  # The soft-deadline fallback summary ("partial finalize
  # requested at the soft deadline before another editor
  # attempt") is deliberately NOT matched here: it means the run
  # budget was exhausted, not that the editor failed.
  if grep -qiE 'partial finalize requested after a recoverable editor failure' "${EDITOR_SUMMARY_FILE}"; then
    echo "::notice::Editor stopped after a recoverable failure on every attempt — no editor output was produced; see the editor step log for the provider error."
    EDITOR_NOOP_SUSPICIOUS="true"
    EDITOR_NOOP_RECOVERABLE_FAILURE="true"
  fi

  # ── Check 2: Reviewer audit sanity ──
  # When reviewer execution succeeded (REVIEWERS_SUCCESSFUL > 0)
  # but the editor claims no changes, verify the audit section has
  # real entries and the arithmetic adds up. The check itself
  # lives in scripts/validate_editor_audit.sh so the orchestrator
  # poll's noop-suspicious recovery sweep can call the exact
  # same regex/arithmetic logic when deciding whether a
  # force-merge fallback is safe — keeping the two paths in
  # lockstep prevents drift between "what the workflow flagged
  # as noop-suspicious" and "what the poller considers an
  # audit-healthy fallback candidate".
  if [ "${EDITOR_NOOP_SUSPICIOUS}" = "false" ] && [ "${REVIEWERS_SUCCESSFUL:-0}" -gt 0 ]; then
    # shellcheck disable=SC1091
    source "${SUPPORT_SCRIPTS_DIR}/validate_editor_audit.sh"
    _vea_rc=0
    validate_editor_audit_arithmetic \
      "${EDITOR_SUMMARY_FILE}" \
      "${REVIEWERS_SUCCESSFUL:-0}" \
      || _vea_rc=$?
    if [ "${_vea_rc}" -ne 0 ]; then
      EDITOR_NOOP_SUSPICIOUS="true"
    fi
  fi
fi

echo "EDITOR_NOOP_SUSPICIOUS=${EDITOR_NOOP_SUSPICIOUS}" >> "$GITHUB_ENV"
echo "EDITOR_NOOP_REFUSAL=${EDITOR_NOOP_REFUSAL}" >> "$GITHUB_ENV"
echo "EDITOR_NOOP_RECOVERABLE_FAILURE=${EDITOR_NOOP_RECOVERABLE_FAILURE}" >> "$GITHUB_ENV"
