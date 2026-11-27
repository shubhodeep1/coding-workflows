#!/usr/bin/env bash
# operator_step_daily_tick.sh: step body of "Tick observable operator steps"
# in .github/workflows/workflow-log-analysis.yml (job `collect-logs`, daily
# `0 6 * * *` schedule only). Pipeline-human-wait-elimination plan, Phase 9
# (#10118).
#
# Runs `scripts/operator_step_issue.py tick --observable` against the commit
# the `stable` tag points at, so operator steps whose condition the pipeline
# can observe (a merged PR in stable, Allow auto-merge on, a recent successful
# run with the credentials, a conditional step) are ticked and an empty
# tracker is closed. Never run by hand.
#
# Fail-open: always exits 0. Logs under the `OPERATOR_STEP_TICK` prefix:
#   op=daily outcome=skip reason=disabled|no_pat|helper_missing|no_stable_tag
#   op=daily outcome=error reason=helper_exit rc=<n>
#
# Env (all defaulted here, CLAUDE.md section 8):
#   OPERATOR_STEP_DAILY_TICK_ENABLED  default `true`; `false` = no git or API call.
#   OPERATOR_STEP_DAILY_TICK_HELPER   default: the sibling operator_step_issue.py.
#   GH_TOKEN                          GH_PAT; empty = skip (the trusted login is the PAT login).
#   GITHUB_REPOSITORY                 set by Actions.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
enabled="$(printf '%s' "${OPERATOR_STEP_DAILY_TICK_ENABLED:-true}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
helper="${OPERATOR_STEP_DAILY_TICK_HELPER:-${SCRIPT_DIR}/operator_step_issue.py}"

if [ "${enabled}" = "false" ]; then
	echo "OPERATOR_STEP_TICK op=daily outcome=skip reason=disabled"
	exit 0
fi
if [ -z "${GH_TOKEN:-}" ]; then
	echo "::notice::OPERATOR_STEP_TICK op=daily outcome=skip reason=no_pat"
	exit 0
fi
if [ -L "${helper}" ] || [ ! -f "${helper}" ]; then
	echo "::notice::OPERATOR_STEP_TICK op=daily outcome=skip reason=helper_missing"
	exit 0
fi

# Commit history only (no trees or blobs), as tick_operator_steps in
# scripts/promote_main_cycle.sh does: the ancestor checks run in local git.
# A full clone rejects --unshallow, so ask first.
fetch_args=(--quiet --no-tags --filter=tree:0)
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null || true)" = "true" ]; then
	fetch_args+=(--unshallow)
fi
if ! git fetch "${fetch_args[@]}" origin "+refs/tags/stable:refs/tags/stable" >/dev/null 2>&1; then
	echo "::warning::OPERATOR_STEP_TICK op=daily: commit history fetch failed; entries whose commits are missing stay open."
fi

stable_sha="$(git rev-parse --verify -q 'refs/tags/stable^{commit}' 2>/dev/null || true)"
if ! [[ "${stable_sha}" =~ ^[0-9a-f]{40}$ ]]; then
	echo "::notice::OPERATOR_STEP_TICK op=daily outcome=skip reason=no_stable_tag"
	exit 0
fi

set +e
PYTHONDONTWRITEBYTECODE=1 python3 "${helper}" tick --repo "${GITHUB_REPOSITORY:-}" --stable-sha "${stable_sha}" --repo-dir "${PWD}" --observable
tick_rc=$?
set -e
if [ "${tick_rc}" -ne 0 ]; then
	echo "::warning::OPERATOR_STEP_TICK op=daily outcome=error reason=helper_exit rc=${tick_rc}"
fi
exit 0
