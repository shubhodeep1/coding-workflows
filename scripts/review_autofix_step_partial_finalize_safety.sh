#!/usr/bin/env bash
# Body of the "Decide partial-finalize validation/push safety" step in
# .github/workflows/review_autofix.yml, moved out of the workflow to keep it
# under GitHub's 512,000-byte workflow file limit (a larger file never
# starts runs). The step sources this file in its own shell, so the step's
# if:, env: and continue-on-error: stay in the workflow; edit those there.
set -euo pipefail

validation_tail_can_complete="true"
edits_withheld_for_safety="false"
withheld_reason="none"

if [ "${AUTOFIX_PARTIAL_FINALIZE_REQUESTED:-false}" = "true" ]; then
  workflow_timeout_source_path=".codex-workflow-src/.github/workflows/review_autofix.yml"
  if [ ! -f "${workflow_timeout_source_path}" ]; then
    workflow_timeout_source_path=".github/workflows/review_autofix.yml"
  fi
  if ! read -r job_timeout_minutes resolver_timeout_minutes < <(PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 python3 -c 'from pathlib import Path; import sys, yaml; workflow = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}; jobs = workflow.get("jobs") or {}; codex_agent_job = jobs.get("codex-agent") or {}; job_timeout_minutes = codex_agent_job.get("timeout-minutes"); resolver_timeout_minutes = next((step.get("timeout-minutes") for step in (codex_agent_job.get("steps") or []) if isinstance(step, dict) and step.get("name") == "Run Codex resolver, validate, stage, commit"), None); sys.exit(1) if not isinstance(job_timeout_minutes, int) or not isinstance(resolver_timeout_minutes, int) else print(job_timeout_minutes, resolver_timeout_minutes)' "${workflow_timeout_source_path}"); then
    job_timeout_minutes="240"
    resolver_timeout_minutes="170"
  fi
  # Requiring a full resolver window plus 10 minutes of
  # smoke/detect/push slack keeps the partial-publish path inside the
  # workflow's existing validation envelope instead of publishing
  # edits that the current run cannot fully re-validate before the
  # hard timeout.
  job_timeout_total_secs=$(( job_timeout_minutes * 60 ))
  partial_finalize_validation_minimum_secs=$(( (resolver_timeout_minutes * 60) + 600 ))
  now_epoch="$(date +%s)"
  budget_start_epoch="${CODEX_RUN_BUDGET_START_EPOCH:-${JOB_START_EPOCH:-}}"
  hard_timeout_remaining_secs=0

  if [[ "${budget_start_epoch}" =~ ^[0-9]+$ ]] && [ "${budget_start_epoch}" -gt 0 ] && [ "${now_epoch}" -ge "${budget_start_epoch}" ]; then
    job_deadline_epoch=$(( budget_start_epoch + job_timeout_total_secs ))
    hard_timeout_remaining_secs=$(( job_deadline_epoch - now_epoch ))
    if [ "${hard_timeout_remaining_secs}" -lt 0 ]; then
      hard_timeout_remaining_secs=0
    fi
  fi

  if [ "${hard_timeout_remaining_secs}" -lt "${partial_finalize_validation_minimum_secs}" ]; then
    validation_tail_can_complete="false"
    edits_withheld_for_safety="true"
    withheld_reason="insufficient_budget_for_validation_tail"
    echo "Partial finalize publish-safety gate: findings-only fallback (hard_timeout_remaining_secs=${hard_timeout_remaining_secs}, minimum_required_secs=${partial_finalize_validation_minimum_secs})."
  else
    echo "Partial finalize publish-safety gate: validated tail remains available (hard_timeout_remaining_secs=${hard_timeout_remaining_secs}, minimum_required_secs=${partial_finalize_validation_minimum_secs})."
  fi
fi

{
  echo "AUTOFIX_PARTIAL_FINALIZE_VALIDATION_TAIL_CAN_COMPLETE=${validation_tail_can_complete}"
  echo "AUTOFIX_PARTIAL_FINALIZE_EDITS_WITHHELD_FOR_SAFETY=${edits_withheld_for_safety}"
  echo "AUTOFIX_PARTIAL_FINALIZE_WITHHELD_REASON=${withheld_reason}"
} >> "$GITHUB_ENV"
