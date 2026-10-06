#!/usr/bin/env bash
# Refuse heal implementation unless the intake-authored, unedited scope is verifiable.
set -euo pipefail

heal_py="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
issue_json="${ISSUE_META_FILE:?}"

refuse()
{
	local reason="$1"
	echo "HEAL_SCOPE_REFUSED issue=${ISSUE_NUMBER:-unknown} reason=${reason}"
	# Never allow another editor path to run after a failed authorization read.
	echo 'SKIP_IMPLEMENT=true' >> "${GITHUB_ENV}"
	gh issue comment "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" --body "Workflow heal scope verification failed (${reason}); implementation was refused. <!-- ai:workflow-heal-scope-unverified:v1 reason=${reason} -->" >/dev/null
	# Comment first: the label event starts the reporter immediately, which
	# must already see the authenticated refusal marker to skip re-healing.
	gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" --add-label 'ai:needs-human' >/dev/null
	exit 0
}

if [ "${1:-}" = refuse ] && [ "${2:-}" = out_of_heal_scope ]; then
	refuse out_of_heal_scope
fi
echo 'HEAL_ROUTE=false' >> "${GITHUB_ENV:?}"
if [ "$(PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" heal-route --issue-json "${issue_json}")" != true ]; then
	exit 0
fi

[[ "${ISSUE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || refuse malformed
[[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || refuse malformed
identity="$(gh api user --jq .login 2>/dev/null)" || refuse unavailable
[ -n "${identity}" ] || refuse unavailable
owner="${GITHUB_REPOSITORY%%/*}"
repo="${GITHUB_REPOSITORY#*/}"
# REST issues/<n> cannot provide lastEditedAt. This single GraphQL read binds
# body, labels, author and edit status to the same authoritative snapshot.
live="$(gh api graphql -f query='query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){body lastEditedAt author{login} labels(first:100){nodes{name}}}}}' -f owner="${owner}" -f repo="${repo}" -F number="${ISSUE_NUMBER}" 2>/dev/null)" || refuse unavailable
verified="$(printf '%s' "${live}" | jq -c --arg login "${identity}" '{body: .data.repository.issue.body, last_edited_at: .data.repository.issue.lastEditedAt, author_login: .data.repository.issue.author.login, labels: [.data.repository.issue.labels.nodes[].name], pipeline_login: $login}' | PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" heal-scope verify --input-json /dev/stdin)" || refuse unavailable
status="$(printf '%s' "${verified}" | jq -r '.status')"
[ "${status}" = verified ] || refuse "${status}"

scope_file="${RUNNER_TEMP:-/tmp}/heal-scope-${GITHUB_RUN_ID:-local}.txt"
printf '%s' "${verified}" | jq -r '.paths[]' > "${scope_file}"
chmod 0444 "${scope_file}"
# The support checkout is separate from the editor's writable source copy.
support="${GITHUB_WORKSPACE:?}/.codex-workflow-src"
[ -d "${support}/scripts" ] || refuse unavailable
support_head="$(git -C "${support}" rev-parse HEAD 2>/dev/null)" || refuse unavailable
expected_ref="${SCRIPT_REF:-main}"
case "${expected_ref}" in
	main|stable) expected_head="$(git -C "${support}" rev-parse "${expected_ref}^{commit}" 2>/dev/null)" || refuse unavailable ;;
	*) [[ "${expected_ref}" =~ ^[0-9a-f]{40}$ ]] || refuse unavailable; expected_head="${expected_ref}" ;;
esac
[ "${support_head}" = "${expected_head}" ] || refuse unavailable
trusted_root="${RUNNER_TEMP:-/tmp}/heal-trusted-support-${GITHUB_RUN_ID:-local}"
mkdir -p "${trusted_root}"
cp -a "${support}/scripts" "${trusted_root}/scripts"
chmod -R a-w "${trusted_root}/scripts"
echo 'HEAL_ROUTE=true' >> "${GITHUB_ENV}"
echo "HEAL_SCOPE_FILE=${scope_file}" >> "${GITHUB_ENV}"
echo "HEAL_TRUSTED_SUPPORT_DIR=${trusted_root}/scripts" >> "${GITHUB_ENV}"
HEAL_EVIDENCE_FILE="${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/heal_evidence.md" \
	WORKFLOW_HEAL_PY="${trusted_root}/scripts/workflow_failure_heal.py" \
	bash "${trusted_root}/scripts/workflow_failure_heal_evidence.sh"
