#!/usr/bin/env bash
# Collect bounded, authenticated diagnostics before a heal editor is started.
# P1 step 5 (docs/plans/heal-evidence-reproduction-and-trends-plan.md): when
# the scope-verified issue body has an `## Evidence pack` section, it is put
# first inside the same byte budget and the per-job tails shrink to fit.
# Without a pack the output is byte-identical to the legacy bundle.
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
status="$(printf '%s' "${verification}" | jq -r .status)"
# #7444: like the implement preflight, accept a body that only the pipeline
# login edited with an unchanged scope marker (heal-scope verify-edits).
if [ "${status}" = edited ]; then
	case "$(printf '%s' "${HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		''|true)
			verification="$(gh api graphql -f query='query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){body author{login} labels(first:100){nodes{name}} userContentEdits(first:100){totalCount pageInfo{hasNextPage} nodes{editor{login} deletedAt diff}}}}}' -f owner="${owner}" -f repo="${repo}" -F number="${ISSUE_NUMBER}" 2>/dev/null \
				| jq -c --arg login "${login}" '.data.repository.issue as $i | {body: $i.body, author_login: $i.author.login, labels: [$i.labels.nodes[].name], pipeline_login: $login, edits: $i.userContentEdits.nodes, edits_total: $i.userContentEdits.totalCount, edits_has_next_page: $i.userContentEdits.pageInfo.hasNextPage}' \
				| PYTHONDONTWRITEBYTECODE=1 python3 "${heal_py}" heal-scope verify-edits --input-json /dev/stdin)" || stub scope_unverified
			status="$(printf '%s' "${verification}" | jq -r .status)"
			;;
		false) ;;
		*) echo "::warning::HEAL_PLAN_GRANT_BODY_EDIT_FIX_ENABLED has invalid value; treating as off" ;;
	esac
fi
[ "${status}" = verified ] || stub scope_unverified
scratch="$(mktemp -d "${RUNNER_TEMP:-/tmp}/heal-evidence.XXXXXXXX")"
trap 'rm -rf -- "${scratch}"' EXIT
jobs_count=0
runs_count=0
: > "${scratch}/content"
# The pack comes only from the body already fetched above (no new API call)
# and only after the scope check passed. The awk reads all of its input
# (no early exit) so pipefail cannot see a SIGPIPE upstream.
: > "${scratch}/pack_content"
printf '%s' "${live}" | jq -r '.data.repository.issue.body // ""' | tr -d '\r' \
	| awk 'done{next} on && /^## /{done=1; next} /^## Evidence pack[[:space:]]*$/{on=1} on{print}' > "${scratch}/pack" || : > "${scratch}/pack"
if [ -s "${scratch}/pack" ]; then
	{
		printf '\nHeal issue evidence pack (UNTRUSTED, copied from the verified issue body):\n'
		cat "${scratch}/pack"
	} > "${scratch}/pack_content"
fi
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
if [ "${jobs_count}" -eq 0 ]; then
	# A pack is still evidence when no job log could be read.
	[ -s "${scratch}/pack_content" ] || stub logs_unavailable
	printf '\n(job logs unavailable: logs_unavailable)\n' >> "${scratch}/content"
fi
# A log line that repeats a fence delimiter must not close the evidence fence.
sed -E 's/={3}([[:space:]]*(BEGIN|END)[[:space:]]+UNTRUSTED)/= = =\1/Ig' "${scratch}/content" > "${scratch}/fenced"
sed -E 's/={3}([[:space:]]*(BEGIN|END)[[:space:]]+UNTRUSTED)/= = =\1/Ig' "${scratch}/pack_content" > "${scratch}/pack_fenced"
# Pack first, then the tails: the single cut below keeps the pack whole
# whenever it fits and shrinks the tails to the remaining budget.
cat "${scratch}/pack_fenced" "${scratch}/fenced" > "${scratch}/combined"
{
	echo '=== BEGIN UNTRUSTED WORKFLOW FAILURE EVIDENCE ==='
	# Bounded before publication; filtered content was already redacted before disk.
	head -c "$((max_bytes - 128))" "${scratch}/combined"
	echo
	echo '=== END UNTRUSTED WORKFLOW FAILURE EVIDENCE ==='
} > "${out}"
pack_suffix=""
pack_bytes="$(wc -c < "${scratch}/pack_fenced" | tr -d '[:space:]')"
if [ "${pack_bytes}" -gt 0 ]; then
	pack_suffix=" pack_bytes=${pack_bytes}"
	[ "${pack_bytes}" -le "$((max_bytes - 128))" ] || pack_suffix="${pack_suffix} pack_truncated=true"
fi
echo "WORKFLOW_HEAL_EVIDENCE runs=${runs_count} jobs=${jobs_count} bytes=$(wc -c < "${out}") outcome=written${pack_suffix}"
