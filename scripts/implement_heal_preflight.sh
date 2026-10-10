#!/usr/bin/env bash
# Refuse heal implementation unless the intake-authored, unedited scope is verifiable.
set -euo pipefail

heal_py="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
issue_json="${ISSUE_META_FILE:?}"

# Extra key=value log fields set by the support-ref check (expected/actual
# ref and SHA). Only script-validated values are ever placed here.
refuse_context=""

refuse()
{
	local reason="$1"
	# `detail` is always a fixed word chosen by this script; it splits the
	# broad `unavailable` reason into distinct causes without renaming it.
	local detail="${2:-}"
	local detail_field="" detail_text="${reason}" context_field=""
	if [ -n "${detail}" ]; then
		detail_field=" detail=${detail}"
		detail_text="${reason}: ${detail}"
	fi
	if [ -n "${refuse_context}" ]; then
		context_field=" ${refuse_context}"
	fi
	echo "HEAL_SCOPE_REFUSED issue=${ISSUE_NUMBER:-unknown} reason=${reason}${detail_field}${context_field}"
	# errexit is suspended in a function reached through `|| refuse`, so each
	# write is checked: a missing skip flag, marker or latch must fail the job.
	# Never allow another editor path to run after a failed authorization read.
	if ! echo 'SKIP_IMPLEMENT=true' >> "${GITHUB_ENV}"; then
		echo "::error::HEAL_SCOPE_REFUSED issue=${ISSUE_NUMBER:-unknown} reason=${reason} outcome=skip_flag_failed" >&2
		exit 1
	fi
	if ! gh issue comment "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" --body "Workflow heal scope verification failed (${detail_text}); implementation was refused. <!-- ai:workflow-heal-scope-unverified:v1 reason=${reason}${detail_field} -->" >/dev/null; then
		echo "::error::HEAL_SCOPE_REFUSED issue=${ISSUE_NUMBER:-unknown} reason=${reason} outcome=comment_failed" >&2
		exit 1
	fi
	# Comment first: the label event starts the reporter immediately, which
	# must already see the authenticated refusal marker to skip re-healing.
	if ! gh issue edit "${ISSUE_NUMBER}" --repo "${GITHUB_REPOSITORY}" --add-label 'ai:needs-human' >/dev/null; then
		echo "::error::HEAL_SCOPE_REFUSED issue=${ISSUE_NUMBER:-unknown} reason=${reason} outcome=label_failed" >&2
		exit 1
	fi
	exit 0
}

if [ "${1:-}" = refuse ] && [ "${2:-}" = out_of_heal_scope ]; then
	refuse out_of_heal_scope
fi
echo 'HEAL_ROUTE=false' >> "${GITHUB_ENV:?}"
# The checkout-context step empties ISSUE_META_FILE when its metadata fetch
# fails transiently (and continues). Re-read the issue once here so ordinary
# issues are not refused for a blip; a failed re-read still fails closed below.
if [ ! -s "${issue_json}" ] && [[ "${ISSUE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] && [[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	refetched_issue_json="$(mktemp "${RUNNER_TEMP:-/tmp}/heal-issue-meta.XXXXXXXX")"
	if gh api "repos/${GITHUB_REPOSITORY}/issues/${ISSUE_NUMBER}" > "${refetched_issue_json}" 2>/dev/null; then
		issue_json="${refetched_issue_json}"
	fi
fi
# Capture the classifier's exit status: a crash must not read as "not a heal
# issue" and send a heal issue down the ordinary host-editor route.
if ! heal_route="$(PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" heal-route --issue-json "${issue_json}")"; then
	echo "::error::HEAL_SCOPE_REFUSED issue=${ISSUE_NUMBER:-unknown} reason=classification_failed" >&2
	echo 'SKIP_IMPLEMENT=true' >> "${GITHUB_ENV}"
	exit 1
fi
if [ "${heal_route}" != true ]; then
	exit 0
fi

[[ "${ISSUE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || refuse malformed
[[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || refuse malformed
identity="$(gh api user --jq .login 2>/dev/null)" || refuse unavailable identity_unavailable
[ -n "${identity}" ] || refuse unavailable identity_empty
owner="${GITHUB_REPOSITORY%%/*}"
repo="${GITHUB_REPOSITORY#*/}"
# REST issues/<n> cannot provide lastEditedAt. This single GraphQL read binds
# body, labels, author and edit status to the same authoritative snapshot.
live="$(gh api graphql -f query='query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){body lastEditedAt author{login} labels(first:100){nodes{name}}}}}' -f owner="${owner}" -f repo="${repo}" -F number="${ISSUE_NUMBER}" 2>/dev/null)" || refuse unavailable issue_read_failed
verified="$(printf '%s' "${live}" | jq -c --arg login "${identity}" '{body: .data.repository.issue.body, last_edited_at: .data.repository.issue.lastEditedAt, author_login: .data.repository.issue.author.login, labels: [.data.repository.issue.labels.nodes[].name], pipeline_login: $login}' | PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" heal-scope verify --input-json /dev/stdin)" || refuse unavailable scope_verify_failed
status="$(printf '%s' "${verified}" | jq -r '.status')"
[ "${status}" = verified ] || refuse "${status}"

scope_file="${RUNNER_TEMP:-/tmp}/heal-scope-${GITHUB_RUN_ID:-local}.txt"
printf '%s' "${verified}" | jq -r '.paths[]' > "${scope_file}"
chmod 0444 "${scope_file}"
# The support checkout is separate from the editor's writable source copy.
support="${GITHUB_WORKSPACE:?}/.codex-workflow-src"
[ -d "${support}/scripts" ] || refuse unavailable support_missing
support_head="$(git -C "${support}" rev-parse HEAD 2>/dev/null)" || refuse unavailable support_head_unreadable
expected_ref="${SCRIPT_REF:-main}"
# Only a charset-checked ref is ever logged; anything else shows as `invalid`.
if [[ "${expected_ref}" =~ ^[A-Za-z0-9._/-]{1,100}$ ]]; then
	expected_ref_shown="${expected_ref}"
else
	expected_ref_shown="invalid"
fi
actual_sha_shown="unresolved"
if [[ "${support_head}" =~ ^[0-9a-f]{40}$ ]]; then
	actual_sha_shown="${support_head}"
fi
# The primary checkout of a SHA leaves a detached HEAD and a checkout of
# `stable` creates only refs/heads/stable, so a local `main` branch exists only
# when implement.yml's "Checkout workflow support source fallback" step ran.
# Report that fallback; it is still refused unless the SHAs really match.
support_fallback=false
if [ "${expected_ref}" != main ] && git -C "${support}" rev-parse --verify -q "refs/heads/main^{commit}" >/dev/null 2>&1; then
	support_fallback=true
fi
set_support_context()
{
	local expected_sha_shown="unresolved"
	if [[ "${1:-}" =~ ^[0-9a-f]{40}$ ]]; then
		expected_sha_shown="$1"
	fi
	refuse_context="expected_ref=${expected_ref_shown} expected_sha=${expected_sha_shown} actual_sha=${actual_sha_shown} fallback=${support_fallback}"
}
refuse_support_ref()
{
	local detail="$1"
	if [ "${support_fallback}" = true ]; then
		detail=support_ref_fallback
	fi
	echo "::error::Heal support ref check failed detail=${detail} ${refuse_context}" >&2
	refuse unavailable "${detail}"
}
expected_head=""
set_support_context ""
case "${expected_ref}" in
	# `stable` is both a branch and a release tag in coding-workflows, and
	# actions/checkout fetches both; a bare `stable` resolves to the tag
	# (refs/tags precedes refs/heads), which lags the checked-out branch tip.
	# Resolve the local branch actions/checkout created, unambiguously.
	main|stable) expected_head="$(git -C "${support}" rev-parse --verify -q "refs/heads/${expected_ref}^{commit}" 2>/dev/null)" || refuse_support_ref support_ref_unresolved ;;
	*)
		if [[ ! "${expected_ref}" =~ ^[0-9a-f]{40}$ ]]; then
			echo "::error::Heal support ref check failed detail=support_ref_invalid ${refuse_context}" >&2
			refuse unavailable support_ref_invalid
		fi
		expected_head="${expected_ref}"
		;;
esac
set_support_context "${expected_head}"
[ "${support_head}" = "${expected_head}" ] || refuse_support_ref support_ref_mismatch
echo "::notice::Heal support ref verified ${refuse_context}"
if [ "${support_fallback}" = true ]; then
	echo "::warning::Heal support source came from the main fallback checkout ${refuse_context}"
fi
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
