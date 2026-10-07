#!/usr/bin/env bash
# Body of the review_autofix.yml "Record Claude pool capacity" step (sourced by
# the step; moved out to keep the workflow under GitHub's 512,000-byte limit).
set -uo pipefail
helper="${SUPPORT_SCRIPTS_DIR:-}/provider_outage.py"
if [ ! -f "${helper}" ]; then
  echo "PROVIDER_OUTAGE op=capacity outcome=skip reason=helper_missing"
  exit 0
fi
alert_file="$(mktemp)"
if [ "${CLAUDE_POOL_STEP_REASON}" = "all_gated" ]; then
  PYTHONDONTWRITEBYTECODE=1 python3 "${helper}" open --repo "${REPOSITORY}" --kind capacity --provider claude-pool --status all_gated --alert-out "${alert_file}" || true
  { echo "CLAUDE_POOL_REASON=all_gated"; echo "PROVIDER_OUTAGE_CAPACITY_ALERTED=true"; } >> "$GITHUB_ENV"
else
  PYTHONDONTWRITEBYTECODE=1 python3 "${helper}" close --repo "${REPOSITORY}" --kind capacity --alert-out "${alert_file}" || true
fi
if [ -s "${alert_file}" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh" ]; then
  source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"
  tg_send_msg "$(cat "${alert_file}")" "WARNING" >/dev/null || true
fi
rm -f -- "${alert_file}"
