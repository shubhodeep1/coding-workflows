#!/usr/bin/env bash
# Body of the review_autofix.yml "Record model-provider outage" step (sourced by
# the step). Issue #6633: a review/autofix failure confirmed as a model-provider
# outage opens (or reuses) the one repo-wide ai:provider-outage tracker; only
# the run that opens it sends the CRITICAL alert, which names the provider, the
# status and the secret name OPENROUTER_API_KEY, never its value. Fail-open.
set -euo pipefail
provider_outage_helper="${SUPPORT_SCRIPTS_DIR:-}/provider_outage.py"
if [ ! -f "${provider_outage_helper}" ]; then
  echo "PROVIDER_OUTAGE op=record outcome=skip reason=helper_missing"
  exit 0
fi
provider_outage_status="${AUTOFIX_PROVIDER_STATUS:-unknown}"
[[ "${provider_outage_status}" =~ ^[a-z0-9_]{1,20}$ ]] || provider_outage_status="unknown"
provider_outage_alert_file="$(mktemp)"
PYTHONDONTWRITEBYTECODE=1 python3 "${provider_outage_helper}" open --repo "${REPOSITORY}" --kind outage --provider openrouter \
  --status "${provider_outage_status}" --alert-out "${provider_outage_alert_file}" || true
if [ -s "${provider_outage_alert_file}" ] && [ -f "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh" ]; then
  source "${SUPPORT_SCRIPTS_DIR}/tg_helpers.sh"
  tg_send_msg "$(cat "${provider_outage_alert_file}")"$'\n'"PR: ${GITHUB_SERVER_URL:-https://github.com}/${REPOSITORY}/pull/${PR_NUMBER:-}" "CRITICAL" >/dev/null || true
fi
rm -f -- "${provider_outage_alert_file}"
