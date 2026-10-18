#!/usr/bin/env bash
#
# security_audit_upstream_report.sh
#
# Runs as the "Report synced-file security findings upstream" step of
# .github/workflows/security-audit.yml in consumer repositories (never in
# coding-workflows), from the trusted support checkout. Plan
# docs/plans/synced-file-security-findings-heal-intake-plan.md, Phase 2
# (issue #7623, project #7604). It:
#
#   1. Reads the audit's upstream-managed findings (high/critical findings on
#      synced files that are byte-identical to the release), exported by
#      scripts/security_audit.sh to SECURITY_AUDIT_UPSTREAM_MANAGED_EXPORT.
#   2. Keeps only six fields per finding (file, line, category, severity,
#      confidence, finding_id): the exploit scenario and the recommendation
#      never leave the consumer, because coding-workflows is public (Q245).
#      Drops malformed entries, de-duplicates within the run by
#      (file, line, category) and by finding_id, and caps the list at 50.
#   3. Builds one `synced_security_finding` report, attaches the job's OIDC
#      report identity and sends one `repository_dispatch` (event type
#      `workflow-failure-heal`) to coding-workflows. The heal intake
#      de-duplicates repeats across runs and consumers by fingerprint into
#      occurrence comments, so nothing is remembered here.
#
# API calls (CLAUDE.md §15): one dispatch per run, no reads. The audit step
# already holds every input; the OIDC token comes from the Actions runtime.
#
# The script never fails the job: every exit is 0 and every outcome is a
# stable log line prefixed SECURITY_AUDIT_UPSTREAM_REPORT. Log lines carry
# fixed tokens and counts only; no model-written text is printed.
#
# Env:
#   GITHUB_REPOSITORY                        owner/repo of the consumer (required)
#   GH_TOKEN                                 token (GH_PAT) able to dispatch to coding-workflows
#   GITHUB_RUN_ID, GITHUB_SERVER_URL         the audit run
#   REPORT_WRAPPER_SHA                       the coding-workflows commit the audit ran at
#   WORKFLOW_JOB_JSON                        toJSON(job); its workflow_ref must end in @REPORT_WRAPPER_SHA
#   SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED   unset/empty/true on (default), false off, other values off with a warning
#   SECURITY_AUDIT_UPSTREAM_MANAGED_EXPORT   export file (default ${RUNNER_TEMP}/security-audit-upstream-managed.json)
#   WORKFLOW_HEAL_ENABLED                    "false" disables (default on)
#   WORKFLOW_HEAL_UPSTREAM_REPO              dispatch target (default shubhodeep1/coding-workflows)
#   WORKFLOW_HEAL_PY                         path of workflow_failure_heal.py (default: beside this script)
#   CLAUDE_CAPACITY_WAIT                     "true" skips (the audit produced no result)

set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1

log()
{
	echo "SECURITY_AUDIT_UPSTREAM_REPORT $*"
}

summary_line()
{
	local summary="${GITHUB_STEP_SUMMARY:-}"
	if [ -n "${summary}" ] && [ ! -L "${summary}" ]; then
		echo "- Synced-file security findings upstream: $*" >> "${summary}" 2>/dev/null || true
	fi
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEAL_PY="${WORKFLOW_HEAL_PY:-${script_dir}/workflow_failure_heal.py}"
UPSTREAM_REPO="${WORKFLOW_HEAL_UPSTREAM_REPO:-shubhodeep1/coding-workflows}"
REPO="${GITHUB_REPOSITORY:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
EXPORT_FILE="${SECURITY_AUDIT_UPSTREAM_MANAGED_EXPORT:-${RUNNER_TEMP:-/tmp}/security-audit-upstream-managed.json}"
MAX_EXPORT_BYTES=1048576

switch="${SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED-true}"
case "${switch,,}" in
	""|true) ;;
	false)
		log "skip reason=disabled"
		exit 0
		;;
	*)
		echo "::warning::SECURITY_AUDIT_UPSTREAM_REPORT_ENABLED has an unrecognised value; reporting is off."
		log "skip reason=invalid_switch"
		exit 0
		;;
esac
heal_switch="${WORKFLOW_HEAL_ENABLED:-true}"
if [ "${heal_switch,,}" = "false" ]; then
	log "skip reason=workflow_heal_disabled"
	exit 0
fi
if [ "${CLAUDE_CAPACITY_WAIT:-}" = "true" ]; then
	log "skip reason=capacity_wait"
	exit 0
fi
if ! [[ "${REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || ! [[ "${UPSTREAM_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
	log "skip reason=missing_context"
	exit 0
fi
if [ "${REPO,,}" = "${UPSTREAM_REPO,,}" ]; then
	log "skip reason=self_repo"
	exit 0
fi
if [ -L "${EXPORT_FILE}" ]; then
	log "skip reason=export_symlink"
	exit 0
fi
if [ ! -f "${EXPORT_FILE}" ]; then
	log "skip reason=no_summary"
	exit 0
fi
export_bytes="$(wc -c < "${EXPORT_FILE}" 2>/dev/null | tr -d ' ')"
if ! [[ "${export_bytes}" =~ ^[0-9]+$ ]] || [ "${export_bytes}" -gt "${MAX_EXPORT_BYTES}" ]; then
	log "skip reason=export_oversized"
	exit 0
fi
if [ -z "${GH_TOKEN:-}" ]; then
	log "skip reason=token_missing"
	exit 0
fi
WRAPPER_SHA="${REPORT_WRAPPER_SHA:-}"
JOB_WORKFLOW_REF="$(WORKFLOW_JOB_JSON="${WORKFLOW_JOB_JSON:-}" python3 -I -B -c 'import json, os
try:
	value = json.loads(os.environ.get("WORKFLOW_JOB_JSON") or "{}").get("workflow_ref")
except Exception:
	value = None
print(value if isinstance(value, str) else "")' 2>/dev/null || true)"
if ! [[ "${WRAPPER_SHA}" =~ ^[0-9a-f]{40}$ ]] || [ "${JOB_WORKFLOW_REF##*@}" != "${WRAPPER_SHA}" ]; then
	# The intake rejects a report whose identity ref is not this SHA
	# (release_mismatch), so an @stable or branch caller sends nothing.
	log "skip reason=wrapper_not_pinned"
	exit 0
fi
if ! [[ "${RUN_ID}" =~ ^[1-9][0-9]{0,19}$ ]]; then
	log "skip reason=missing_context"
	exit 0
fi
if [ ! -f "${HEAL_PY}" ] || [ -L "${HEAL_PY}" ]; then
	log "skip reason=helper_missing"
	exit 0
fi

if [ -f "${script_dir}/gh_helpers.sh" ] && [ ! -L "${script_dir}/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${script_dir}/gh_helpers.sh" 2>/dev/null || true
fi
type gh_retry >/dev/null 2>&1 || gh_retry() { "$@"; }

REPORT_DIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/security-audit-upstream-report.XXXXXX")" || {
	log "skip reason=tmpdir_unavailable"
	exit 0
}
trap 'rm -rf "${REPORT_DIR}"' EXIT
PAYLOAD_FILE="${REPORT_DIR}/payload.json"
COUNTS_FILE="${REPORT_DIR}/counts.txt"

# Selection and payload in one isolated interpreter. The category check uses
# the support module's own mapping when it has one, so a single category the
# intake cannot map does not reject the whole report.
if ! python3 -I -B - "${EXPORT_FILE}" "${PAYLOAD_FILE}" "${COUNTS_FILE}" "${HEAL_PY}" "${REPO}" "${WRAPPER_SHA}" \
		"${SERVER_URL}/${REPO}/actions/runs/${RUN_ID}" "${RUN_ID}" <<'PY' 2>/dev/null
import json
import os
import re
import sys

export_path, payload_path, counts_path, heal_py, repo, wrapper_sha, run_url, run_id = sys.argv[1:9]
MAX_FINDINGS = 50
FILE_RE = re.compile(r"^[A-Za-z0-9._/+-]{1,300}$")
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
CATEGORY_RE = re.compile(r"^[A-Za-z0-9 :()/._,&'+-]{1,200}$")
SEVERITY_RANK = {"critical": 0, "high": 1}

normalize_category = None
try:
	sys.path.insert(0, os.path.dirname(os.path.abspath(heal_py)))
	import workflow_failure_heal as heal  # noqa: E402

	normalize_category = getattr(heal, "normalize_synced_security_category", None)
	schema_version = getattr(heal, "SCHEMA_VERSION", "workflow_failure_heal.v1")
except Exception:
	schema_version = "workflow_failure_heal.v1"


def as_int(value, low, high):
	if isinstance(value, bool) or not isinstance(value, int):
		return None
	return value if low <= value <= high else None


def file_ok(value):
	if not isinstance(value, str) or not FILE_RE.fullmatch(value):
		return False
	if value.startswith("/") or "//" in value:
		return False
	return not any(part in ("", ".", "..") for part in value.split("/"))


with open(export_path, encoding="utf-8") as handle:
	data = json.load(handle)
entries = data.get("upstream_managed") if isinstance(data, dict) else None
if not isinstance(entries, list):
	entries = []

low_severity = dropped = deduped = 0
selected = []
seen_locations = set()
seen_ids = set()
for item in entries:
	if not isinstance(item, dict):
		dropped += 1
		continue
	severity = item.get("severity")
	severity = severity.strip().lower() if isinstance(severity, str) else ""
	if severity not in SEVERITY_RANK:
		low_severity += 1
		continue
	file = item.get("file")
	line = as_int(item.get("line"), 1, 1_000_000)
	confidence = as_int(item.get("confidence"), 1, 10)
	finding_id = item.get("finding_id")
	category = item.get("owasp_or_stride_category")
	category = category.strip() if isinstance(category, str) else ""
	if (
		not file_ok(file)
		or line is None
		or confidence is None
		or not isinstance(finding_id, str)
		or not ID_RE.fullmatch(finding_id)
		or not CATEGORY_RE.match(category)
	):
		dropped += 1
		continue
	if normalize_category is not None:
		# Publish only the intake's canonical token, never the model's wording.
		try:
			category = normalize_category(category)
		except Exception:
			dropped += 1
			continue
	location = (file, line, category)
	if location in seen_locations or finding_id in seen_ids:
		deduped += 1
		continue
	seen_locations.add(location)
	seen_ids.add(finding_id)
	selected.append({
		"file": file,
		"line": line,
		"category": category,
		"severity": severity,
		"confidence": confidence,
		"finding_id": finding_id,
	})

selected.sort(key=lambda f: (SEVERITY_RANK[f["severity"]], f["file"], f["line"], f["finding_id"]))
capped = max(0, len(selected) - MAX_FINDINGS)
selected = selected[:MAX_FINDINGS]

with open(counts_path, "w", encoding="utf-8") as handle:
	handle.write(f"findings={len(selected)} dropped={dropped} deduped={deduped} capped={capped} below_high={low_severity}\n")
if selected:
	payload = {
		"schema_version": schema_version,
		"source_repo": repo,
		"source_kind": "synced_security_finding",
		"wrapper_sha": wrapper_sha,
		"reporter_run_url": run_url,
		"reporter_run_id": run_id,
		"run_refs": [],
		"findings": selected,
	}
	with open(payload_path, "w", encoding="utf-8") as handle:
		json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
PY
then
	log "skip reason=export_unreadable"
	summary_line "skipped (export unreadable)"
	exit 0
fi

COUNTS="$(head -1 "${COUNTS_FILE}" 2>/dev/null | tr -cd 'a-z_=0-9 ')"
FINDINGS="$(sed -n 's/^findings=\([0-9]*\).*/\1/p' <<< "${COUNTS}")"
if [ ! -s "${PAYLOAD_FILE}" ] || [ "${FINDINGS:-0}" = "0" ]; then
	log "outcome=none ${COUNTS:-findings=0}"
	summary_line "none to report"
	exit 0
fi

if grep -q 'synced_security_finding' "${HEAL_PY}" 2>/dev/null; then
	if ! python3 "${HEAL_PY}" validate-payload --payload-json "${PAYLOAD_FILE}" >/dev/null 2>&1; then
		log "skip reason=payload_invalid findings=${FINDINGS}"
		summary_line "skipped (payload rejected locally, ${FINDINGS} findings)"
		exit 0
	fi
else
	log "prevalidate=unavailable findings=${FINDINGS}"
fi

# The intake requires a verified identity for this kind whatever its other
# settings say, so a report without one is not sent.
IDENTITY_FILE="${REPORT_DIR}/report_identity.jwt"
DISPATCH_FILE="${REPORT_DIR}/dispatch.json"
IDENTITY_STATUS=""
if grep -q 'request-report-identity' "${HEAL_PY}" 2>/dev/null; then
	IDENTITY_STATUS="$(python3 "${HEAL_PY}" request-report-identity --out "${IDENTITY_FILE}" 2>/dev/null || echo "identity=absent reason=helper_failed")"
	IDENTITY_STATUS="$(printf '%s' "${IDENTITY_STATUS}" | head -1 | tr -cd 'A-Za-z0-9_=. -')"
fi
if [ ! -s "${IDENTITY_FILE}" ]; then
	log "skip reason=identity_unavailable findings=${FINDINGS} ${IDENTITY_STATUS:-identity=absent}"
	summary_line "skipped (no OIDC identity, ${FINDINGS} findings)"
	exit 0
fi
if ! python3 "${HEAL_PY}" wrap-dispatch --payload-json "${PAYLOAD_FILE}" --report-identity-file "${IDENTITY_FILE}" > "${DISPATCH_FILE}" 2>/dev/null; then
	log "skip reason=dispatch_envelope_failed findings=${FINDINGS}"
	summary_line "skipped (dispatch body failed, ${FINDINGS} findings)"
	exit 0
fi
DISPATCH_ERROR_FILE="${REPORT_DIR}/dispatch_error.txt"
if ! gh_retry gh api -X POST "repos/${UPSTREAM_REPO}/dispatches" --input "${DISPATCH_FILE}" >/dev/null 2> "${DISPATCH_ERROR_FILE}"; then
	detail="$(head -c 200 "${DISPATCH_ERROR_FILE}" 2>/dev/null | tr '\r\n' '  ' | tr -cd '[:print:]' | sed 's/::/: :/g')"
	log "outcome=failed reason=dispatch_denied findings=${FINDINGS} upstream=${UPSTREAM_REPO} detail=${detail}"
	echo "::warning::Synced-file security findings could not be dispatched to ${UPSTREAM_REPO}; the next audit retries."
	summary_line "dispatch failed (${FINDINGS} findings)"
	exit 0
fi
log "outcome=dispatched ${COUNTS} upstream=${UPSTREAM_REPO} run=${RUN_ID}"
summary_line "dispatched ${FINDINGS} finding(s) to ${UPSTREAM_REPO}"
exit 0
