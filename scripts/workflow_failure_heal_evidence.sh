#!/usr/bin/env bash
# Collect bounded, authenticated diagnostics before a heal editor is started.
set -euo pipefail
heal_py="${WORKFLOW_HEAL_PY:-scripts/workflow_failure_heal.py}"
out="${HEAL_EVIDENCE_FILE:-${RUNTIME_DIR:-/tmp}/heal_evidence.md}"
max_bytes="${WORKFLOW_HEAL_EVIDENCE_MAX_BYTES:-24000}"
max_runs="${WORKFLOW_HEAL_EVIDENCE_MAX_RUNS:-3}"
[[ "${max_bytes}" =~ ^[1-9][0-9]{0,5}$ ]] && [ "${max_bytes}" -ge 256 ] || max_bytes=24000
[[ "${max_runs}" =~ ^[1-9][0-9]?$ ]] || max_runs=3
max_runs=$((max_runs < 3 ? max_runs : 3))
mkdir -p "$(dirname "${out}")"
stub()
{
	printf '=== BEGIN UNTRUSTED WORKFLOW FAILURE EVIDENCE ===\n(evidence unavailable: %s)\n=== END UNTRUSTED WORKFLOW FAILURE EVIDENCE ===\n' "$1" > "${out}"
	echo "WORKFLOW_HEAL_EVIDENCE runs=0 jobs=0 bytes=$(wc -c < "${out}") outcome=unavailable"
	exit 0
}
[[ "${GITHUB_REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || stub invalid_repository
[[ "${ISSUE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || stub invalid_issue
login="$(gh api user --jq .login 2>/dev/null)" || stub identity_unavailable
owner="${GITHUB_REPOSITORY%%/*}"
repo="${GITHUB_REPOSITORY#*/}"
live="$(gh api graphql -f query='query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){body lastEditedAt author{login} labels(first:100){nodes{name}}}}}' -f owner="${owner}" -f repo="${repo}" -F number="${ISSUE_NUMBER}" 2>/dev/null)" || stub issue_unavailable
verification="$(printf '%s' "${live}" | jq -c --arg login "${login}" '{body:.data.repository.issue.body,last_edited_at:.data.repository.issue.lastEditedAt,author_login:.data.repository.issue.author.login,labels:[.data.repository.issue.labels.nodes[].name],pipeline_login:$login}' | PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" heal-scope verify --input-json /dev/stdin)" || stub scope_unverified
[ "$(printf '%s' "${verification}" | jq -r .status)" = verified ] || stub scope_unverified
scratch="$(mktemp -d "${RUNNER_TEMP:-/tmp}/heal-evidence.XXXXXXXX")"
trap 'rm -rf -- "${scratch}"' EXIT
jobs_count=0
runs_count=0
: > "${scratch}/content"
while IFS= read -r ref; do
	[ "${runs_count}" -lt "${max_runs}" ] || break
	case "${ref}" in
		"${GITHUB_REPOSITORY}":[1-9]*) ;;
		*) continue ;;
	esac
	run_id="${ref##*:}"
	[[ "${run_id}" =~ ^[1-9][0-9]*$ ]] || continue
	runs_count=$((runs_count + 1))
	if ! gh api --method GET "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs" -F per_page=100 > "${scratch}/jobs.json" 2>/dev/null; then
		continue
	fi
	PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" select-evidence-jobs --jobs-json "${scratch}/jobs.json" --kind autofix_failure --limit 3 > "${scratch}/selected.json" || continue
	while IFS= read -r job_id; do
		[[ "${job_id}" =~ ^[1-9][0-9]*$ ]] || continue
		# No gh_retry: it would buffer raw stdout (including credentials) on disk.
		if gh api --allow-escape-sequences "repos/${GITHUB_REPOSITORY}/actions/jobs/${job_id}/logs" 2>/dev/null \
			| PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" redact-stream > "${scratch}/redacted"; then
			printf '\nRun %s job %s (UNTRUSTED):\n' "${run_id}" "${job_id}" >> "${scratch}/content"
			PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" filter-log --log-file "${scratch}/redacted" --max-bytes 6000 >> "${scratch}/content"
			jobs_count=$((jobs_count + 1))
		fi
	done < <(jq -r '.[].id' "${scratch}/selected.json")
done < <(printf '%s' "${verification}" | jq -r '.runs[]')
[ "${jobs_count}" -gt 0 ] || stub logs_unavailable
{
	echo '=== BEGIN UNTRUSTED WORKFLOW FAILURE EVIDENCE ==='
	# Bounded before publication; filtered content was already redacted before disk.
	head -c "$((max_bytes - 128))" "${scratch}/content"
	echo
	echo '=== END UNTRUSTED WORKFLOW FAILURE EVIDENCE ==='
} > "${out}"
echo "WORKFLOW_HEAL_EVIDENCE runs=${runs_count} jobs=${jobs_count} bytes=$(wc -c < "${out}") outcome=written"
