#!/usr/bin/env bash
# Body of the review_autofix.yml "Defer review on Claude capacity" and "Record
# Claude capacity wait" steps (sourced by the step; issue #7370, Claude
# capacity wait Phase 3). CLAUDE_CAPACITY_WAIT_STEP_MODE selects the mode:
#
#   defer   The pool step gated every account (all_gated). With the capacity
#           wait on (CLAUDE_POOL_CAPACITY_WAIT_ENABLED, scripts/ai_engine.sh)
#           it names the failure claude_capacity and fails the step, so every
#           success() step skips and no codex/OpenCode fallback runs. With
#           the wait off it does nothing and the existing fallback runs.
#   record  After such a deferral: append one `wait` fallback record (unless
#           one exists) and run scripts/ai_engine_fallback_report.sh for this
#           PR, which records the wait on the capacity tracker, labels the PR
#           ai:claude-capacity-wait and posts the item marker. The hourly
#           capacity-resume pass (scripts/provider_outage.py) re-dispatches
#           the review once an account is usable again.
set -euo pipefail
_cw_mode="${CLAUDE_CAPACITY_WAIT_STEP_MODE:-}"
_cw_dir="${SUPPORT_SCRIPTS_DIR:-}"
if [ ! -f "${_cw_dir}/ai_engine.sh" ]; then
  _cw_dir="${GITHUB_WORKSPACE:-.}/.codex-workflow-src/scripts"
fi
case "${_cw_mode}" in
  defer)
    if [ ! -f "${_cw_dir}/ai_engine.sh" ]; then
      echo "::warning::AI_ENGINE_CAPACITY_WAIT role=REVIEW outcome=skip reason=support_missing"
      exit 0
    fi
    # shellcheck source=ai_engine.sh
    source "${_cw_dir}/ai_engine.sh"
    if ! ai_engine_capacity_wait_enabled; then
      echo "AI_ENGINE_CAPACITY_WAIT role=REVIEW outcome=skip reason=wait_disabled"
      exit 0
    fi
    echo "AUTOFIX_FAILURE_REASON=claude_capacity" >> "${GITHUB_ENV}"
    echo "AI_ENGINE_CAPACITY_WAIT role=REVIEW outcome=deferred reason=all_gated pr=${PR_NUMBER:-}"
    echo "::notice::Every Claude pool account is at its usage gate; this review waits for capacity instead of falling back."
    exit 1
    ;;
  record)
    _cw_records="${AI_ENGINE_FALLBACK_RECORDS_FILE:-${RUNNER_TEMP:-/tmp}/ai-engine-fallback-records.tsv}"
    if [ -L "${_cw_records}" ]; then
      echo "::warning::AI_ENGINE_CAPACITY_WAIT role=REVIEW outcome=skip reason=records_symlink"
      exit 0
    fi
    if ! grep -qs $'\twait\t' "${_cw_records}"; then
      if [ -f "${_cw_dir}/ai_engine.sh" ]; then
        # shellcheck source=ai_engine.sh
        ( source "${_cw_dir}/ai_engine.sh" && CLAUDE_POOL_CAPACITY_WAIT_ENABLED=true ai_engine_fallback REVIEW_EDITOR all_gated ) || true
      fi
    fi
    _cw_report="${_cw_dir}/ai_engine_fallback_report.sh"
    [ -f "${_cw_report}" ] || _cw_report="${GITHUB_WORKSPACE:-.}/.codex-workflow-src/scripts/ai_engine_fallback_report.sh"
    if [ ! -f "${_cw_report}" ] || [ -L "${_cw_report}" ]; then
      echo "::warning::AI_ENGINE_CAPACITY_WAIT role=REVIEW outcome=skip reason=report_missing"
      exit 0
    fi
    _cw_pr="${PR_NUMBER:-}"
    _cw_kind=pr
    [[ "${_cw_pr}" =~ ^[1-9][0-9]{0,9}$ ]] || { _cw_kind=run; _cw_pr=""; }
    CLAUDE_CAPACITY_WAIT_ITEM_KIND="${_cw_kind}" CLAUDE_CAPACITY_WAIT_ITEM_NUMBER="${_cw_pr}" \
      CLAUDE_CAPACITY_WAIT_STAGE=review WORKFLOW_HEAL_ENABLED=false AI_ENGINE_FALLBACK_REPORT_WAITS_ONLY=true bash "${_cw_report}" || true
    exit 0
    ;;
  *)
    echo "::warning::AI_ENGINE_CAPACITY_WAIT role=REVIEW outcome=skip reason=invalid_mode"
    exit 0
    ;;
esac
