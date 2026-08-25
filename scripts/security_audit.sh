#!/usr/bin/env bash
# security_audit.sh — Run the default-branch OWASP Top 10 + STRIDE audit.

set -euo pipefail

security_audit_require_cmd() {
	local cmd_name="${1:?command name required}"
	command -v "${cmd_name}" >/dev/null 2>&1 || {
		echo "${cmd_name} is required but not installed" >&2
		exit 1
	}
}

security_audit_flag_enabled() {
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

SECURITY_AUDIT_ENABLED="${SECURITY_AUDIT_ENABLED:-true}"
if ! security_audit_flag_enabled "${SECURITY_AUDIT_ENABLED}"; then
	echo "security-audit: SECURITY_AUDIT_ENABLED=${SECURITY_AUDIT_ENABLED}; skipping."
	exit 0
fi

# Skip the whole audit when HEAD matches the last audited commit recorded on
# the tracker issue (log-only skip; no issue comment).
SECURITY_AUDIT_SKIP_IF_UNCHANGED="${SECURITY_AUDIT_SKIP_IF_UNCHANGED:-true}"
# Scope the audit to commits since the last audited commit when possible;
# first runs and history rewrites fail open to the full default-branch scope.
SECURITY_AUDIT_INCREMENTAL="${SECURITY_AUDIT_INCREMENTAL:-true}"

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"

[[ "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]] || {
	echo "GITHUB_REPOSITORY must be in owner/repo format" >&2
	exit 1
}

SECURITY_AUDIT_CONFIDENCE_GATE="${SECURITY_AUDIT_CONFIDENCE_GATE:-8}"
if ! [[ "${SECURITY_AUDIT_CONFIDENCE_GATE}" =~ ^[0-9]+$ ]] \
	|| [ "${SECURITY_AUDIT_CONFIDENCE_GATE}" -lt 1 ] \
	|| [ "${SECURITY_AUDIT_CONFIDENCE_GATE}" -gt 10 ]; then
	echo "SECURITY_AUDIT_CONFIDENCE_GATE must be an integer from 1 to 10" >&2
	exit 1
fi

SECURITY_AUDIT_FP_EXCLUSIONS="${SECURITY_AUDIT_FP_EXCLUSIONS:-scripts/security_audit_fp_exclusions.json}"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "${REPO_ROOT}"

# Consumer-called runs (workflow-templates/ai-security-audit.yml wrapper) stage
# this repo's scripts/prompts outside the audited checkout and point
# SECURITY_AUDIT_SUPPORT_DIR at that staged tree. Source-repo runs leave it
# unset so support files resolve from the audited checkout itself,
# byte-identical to the pre-consumer behaviour.
SECURITY_AUDIT_SUPPORT_DIR="${SECURITY_AUDIT_SUPPORT_DIR:-${REPO_ROOT}}"

# Resolve the exclusion catalog: a copy in the audited repository wins (so a
# consumer can pin its own catalog at the default relative path); otherwise a
# relative path falls back to the staged support tree.
if [ ! -f "${SECURITY_AUDIT_FP_EXCLUSIONS}" ] \
	&& [[ "${SECURITY_AUDIT_FP_EXCLUSIONS}" != /* ]] \
	&& [ -f "${SECURITY_AUDIT_SUPPORT_DIR}/${SECURITY_AUDIT_FP_EXCLUSIONS}" ]; then
	SECURITY_AUDIT_FP_EXCLUSIONS="${SECURITY_AUDIT_SUPPORT_DIR}/${SECURITY_AUDIT_FP_EXCLUSIONS}"
fi

[ -f "${SECURITY_AUDIT_FP_EXCLUSIONS}" ] || {
	echo "SECURITY_AUDIT_FP_EXCLUSIONS not found: ${SECURITY_AUDIT_FP_EXCLUSIONS}" >&2
	exit 1
}

export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

security_audit_require_cmd bash
security_audit_require_cmd codex
security_audit_require_cmd gh
security_audit_require_cmd python3

[ -f "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/label_helpers.sh" ] || {
	echo "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/label_helpers.sh is required" >&2
	exit 1
}

# shellcheck disable=SC1091
source "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/label_helpers.sh"

# label_helpers.sh only finds gh_helpers.sh relative to the current working
# directory; consumer-called runs execute from the audited checkout, so
# re-source the staged gh_helpers.sh to restore the real gh_retry wrapper
# (re-sourcing is a no-op redefinition on source-repo runs).
if [ -f "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/gh_helpers.sh" ]; then
	# shellcheck disable=SC1091
	source "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/gh_helpers.sh"
fi

ensure_label_exists "ai:security-audit" "${GITHUB_REPOSITORY}"
ensure_label_exists "ai:security" "${GITHUB_REPOSITORY}"

TRACKER_TITLE="AI Security Audit Tracker"
TRACKER_MARKER="<!-- ai:security-audit-tracker:v1 -->"
FOLLOWUP_MARKER_PREFIX="<!-- ai:security-finding:"
LAST_SHA_MARKER_PREFIX="<!-- ai:security-audit-last-sha:"
MAX_FOLLOWUP_ISSUES_PER_WEEK="3"
# Past this many changed files an incremental diff stops being cheaper than a
# full audit, so the scope resolver falls back to the full default-branch scope.
SECURITY_AUDIT_INCREMENTAL_MAX_FILES="200"

SECURITY_AUDIT_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/security-audit.XXXXXX")"
trap 'rm -rf "${SECURITY_AUDIT_RUNTIME_DIR}"' EXIT

TRACKER_BODY_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-issue-body.md"
TRACKER_CANDIDATES_JSON="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-candidates.json"
TRACKER_SELECTION_ENV="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-selection.env"
RENDERED_PROMPT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/prompt.txt"
CODEX_OUTPUT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/codex-output.json"
FILTERED_FINDINGS_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/filtered-findings.json"
FILTER_SUMMARY_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/filter-summary.json"
EXISTING_FOLLOWUPS_JSON="${SECURITY_AUDIT_RUNTIME_DIR}/existing-followups.json"
TRACKER_COMMENT_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-comment.md"
FOLLOWUP_INDEX_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/followup-index.tsv"
FOLLOWUP_SUMMARY_ENV="${SECURITY_AUDIT_RUNTIME_DIR}/followup-summary.env"
FOLLOWUP_BODY_DIR="${SECURITY_AUDIT_RUNTIME_DIR}/followups"
CHANGED_FILES_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/changed-files.txt"
TRACKER_BODY_WITH_SHA_FILE="${SECURITY_AUDIT_RUNTIME_DIR}/tracker-issue-body-with-sha.md"

cat > "${TRACKER_BODY_FILE}" <<EOF
${TRACKER_MARKER}
# ${TRACKER_TITLE}

This issue is managed by \`.github/workflows/security-audit.yml\`.

It collects weekly and ad-hoc default-branch OWASP Top 10 + STRIDE audit results.
Follow-up issues created from this tracker use the additive label \`ai:security\`.
EOF

# No existing prefetch/cache exists in this standalone workflow. One bulk
# issue-list call covers tracker discovery without per-issue follow-up probes.
gh_retry gh issue list \
	--repo "${GITHUB_REPOSITORY}" \
	--state all \
	--label "ai:security-audit" \
	--limit 50 \
		--json number,title,body,state,url > "${TRACKER_CANDIDATES_JSON}"

python3 - "${TRACKER_CANDIDATES_JSON}" "${TRACKER_MARKER}" "${LAST_SHA_MARKER_PREFIX}" > "${TRACKER_SELECTION_ENV}" <<'PY'
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

candidates_path = Path(sys.argv[1])
marker = sys.argv[2]
last_sha_marker_prefix = sys.argv[3]

try:
	candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
	raise SystemExit(f"failed to load tracker candidates: {exc}")

selected = None
for candidate in candidates:
	if not isinstance(candidate, dict):
		continue
	body = str(candidate.get("body") or "")
	if marker not in body:
		continue
	selected = candidate
	if str(candidate.get("state") or "").upper() == "OPEN":
		break

number = ""
state = ""
last_audited_sha = ""
if isinstance(selected, dict):
	number = str(selected.get("number") or "").strip()
	state = str(selected.get("state") or "").strip()
	# The tracker-discovery list call above already returns the body, so the
	# last-audited-commit marker is parsed without any additional API call.
	last_sha_match = re.search(
		re.escape(last_sha_marker_prefix) + r"([0-9a-fA-F]{7,40}) -->",
		str(selected.get("body") or ""),
	)
	if last_sha_match is not None:
		last_audited_sha = last_sha_match.group(1).strip().lower()

print(f"TRACKER_NUMBER={shlex.quote(number)}")
print(f"TRACKER_STATE={shlex.quote(state)}")
print(f"LAST_AUDITED_SHA={shlex.quote(last_audited_sha)}")
PY

# shellcheck disable=SC1090
source "${TRACKER_SELECTION_ENV}"

if [ -z "${TRACKER_NUMBER}" ]; then
	TRACKER_URL="$(gh_retry gh issue create \
		--repo "${GITHUB_REPOSITORY}" \
		--title "${TRACKER_TITLE}" \
		--label "ai:security-audit" \
		--body-file "${TRACKER_BODY_FILE}")"
	TRACKER_NUMBER="${TRACKER_URL##*/}"
else
	if [ "$(printf '%s' "${TRACKER_STATE}" | tr '[:lower:]' '[:upper:]')" = "CLOSED" ]; then
		gh_retry gh issue reopen "${TRACKER_NUMBER}" --repo "${GITHUB_REPOSITORY}"
	fi
	gh_retry gh issue edit "${TRACKER_NUMBER}" --repo "${GITHUB_REPOSITORY}" --add-label "ai:security-audit"
fi

# --- Scope resolution: skip-if-unchanged + incremental diff scope ---------
# HEAD_SHA is empty when the checkout is not a git repository; both gates
# then fail open to the historical full-scope audit.
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo "")"

AUDIT_SCOPE_MODE="full"
AUDIT_SCOPE_REASON="no last-audited commit recorded on the tracker"
: > "${CHANGED_FILES_FILE}"

if [ -z "${HEAD_SHA}" ]; then
	AUDIT_SCOPE_REASON="checkout is not a git repository; scope gates fail open to a full audit"
elif [ -n "${LAST_AUDITED_SHA}" ]; then
	if [ "${LAST_AUDITED_SHA}" = "${HEAD_SHA}" ]; then
		if security_audit_flag_enabled "${SECURITY_AUDIT_SKIP_IF_UNCHANGED}"; then
			echo "security-audit: skipping — HEAD ${HEAD_SHA} unchanged since last audit (tracker=#${TRACKER_NUMBER})."
			exit 0
		fi
		AUDIT_SCOPE_REASON="HEAD unchanged since last audit but SECURITY_AUDIT_SKIP_IF_UNCHANGED is disabled"
	elif security_audit_flag_enabled "${SECURITY_AUDIT_INCREMENTAL}"; then
		if git cat-file -e "${LAST_AUDITED_SHA}^{commit}" 2>/dev/null \
			&& git merge-base --is-ancestor "${LAST_AUDITED_SHA}" "${HEAD_SHA}" 2>/dev/null; then
			git diff --name-only "${LAST_AUDITED_SHA}..${HEAD_SHA}" > "${CHANGED_FILES_FILE}"
			CHANGED_FILE_COUNT="$(grep -c . "${CHANGED_FILES_FILE}" 2>/dev/null || true)"
			if ! [[ "${CHANGED_FILE_COUNT}" =~ ^[0-9]+$ ]]; then
				AUDIT_SCOPE_REASON="could not count changed files since ${LAST_AUDITED_SHA}; falling back to a full audit"
			elif [ "${CHANGED_FILE_COUNT}" -eq 0 ]; then
				if security_audit_flag_enabled "${SECURITY_AUDIT_SKIP_IF_UNCHANGED}"; then
					echo "security-audit: skipping — no content changes between ${LAST_AUDITED_SHA} and ${HEAD_SHA} (tracker=#${TRACKER_NUMBER})."
					exit 0
				fi
				AUDIT_SCOPE_REASON="empty diff since last audit but SECURITY_AUDIT_SKIP_IF_UNCHANGED is disabled"
			elif [ "${CHANGED_FILE_COUNT}" -gt "${SECURITY_AUDIT_INCREMENTAL_MAX_FILES}" ]; then
				AUDIT_SCOPE_REASON="${CHANGED_FILE_COUNT} changed files exceed the incremental cap of ${SECURITY_AUDIT_INCREMENTAL_MAX_FILES}; falling back to a full audit"
			else
				AUDIT_SCOPE_MODE="incremental"
				AUDIT_SCOPE_REASON="${CHANGED_FILE_COUNT} files changed since last audited commit ${LAST_AUDITED_SHA}"
			fi
		else
			AUDIT_SCOPE_REASON="last audited commit ${LAST_AUDITED_SHA} is missing or not an ancestor of HEAD (history rewrite?); falling back to a full audit"
		fi
	else
		AUDIT_SCOPE_REASON="SECURITY_AUDIT_INCREMENTAL is disabled"
	fi
fi
echo "security-audit: scope=${AUDIT_SCOPE_MODE} (${AUDIT_SCOPE_REASON})"

{
	bash "${SECURITY_AUDIT_SUPPORT_DIR}/scripts/render_prompt.sh" "${SECURITY_AUDIT_SUPPORT_DIR}/prompts/mode-security-audit.txt"
	echo
	echo "Current UTC date: $(date -u +%F)"
	if [ "${AUDIT_SCOPE_MODE}" = "incremental" ]; then
		echo "Audit scope: INCREMENTAL — commits ${LAST_AUDITED_SHA}..${HEAD_SHA} on the default branch."
		echo "Files changed since the last audited commit (every finding MUST cite one of these files):"
		sed 's/^/- /' "${CHANGED_FILES_FILE}"
		echo "You may read any file in the repository to trace cross-file impact (callers, configuration, trust boundaries), but only emit findings whose cited file appears in the changed list above; findings citing unchanged files are dropped by the post-filter."
	else
		echo "Audit scope: repository checkout at default-branch HEAD."
	fi
} > "${RENDERED_PROMPT_FILE}"

codex --ask-for-approval never \
	-c model_verbosity=low \
	-c include_apply_patch_tool=true \
	exec \
	--skip-git-repo-check \
	--model "openai/gpt-5.6-sol" \
	--sandbox read-only < "${RENDERED_PROMPT_FILE}" > "${CODEX_OUTPUT_FILE}"

python3 - \
	"${REPO_ROOT}" \
	"${CODEX_OUTPUT_FILE}" \
	"${SECURITY_AUDIT_FP_EXCLUSIONS}" \
	"${SECURITY_AUDIT_CONFIDENCE_GATE}" \
	"${FILTERED_FINDINGS_FILE}" \
	"${FILTER_SUMMARY_FILE}" \
	"${AUDIT_SCOPE_MODE}" \
	"${CHANGED_FILES_FILE}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

repo_root = Path(sys.argv[1]).resolve()
codex_output_path = Path(sys.argv[2])
exclusions_path = Path(sys.argv[3])
confidence_gate = int(sys.argv[4])
filtered_findings_path = Path(sys.argv[5])
summary_path = Path(sys.argv[6])
audit_scope_mode = sys.argv[7]
changed_files_path = Path(sys.argv[8])

# Incremental scope is enforced here deterministically: even if the model
# ignores the prompt's changed-file restriction, out-of-scope findings never
# reach the tracker or follow-up issues.
changed_files: set[str] = set()
if audit_scope_mode == "incremental":
	try:
		changed_files = {
			line.strip()
			for line in changed_files_path.read_text(encoding="utf-8").splitlines()
			if line.strip()
		}
	except OSError as exc:
		raise SystemExit(f"unable to read changed-files list: {exc}")

severity_rank = {
	"critical": 0,
	"high": 1,
	"medium": 2,
	"low": 3,
}
allowed_exact_fields = {"finding_id", "owasp_or_stride_category", "severity", "file"}
allowed_contains_fields = {"exploit_scenario", "recommendation"}


def fail(message: str) -> None:
	raise SystemExit(message)


def load_json(path: Path, *, label: str):
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except OSError as exc:
		fail(f"unable to read {label}: {exc}")
	except json.JSONDecodeError as exc:
		fail(f"invalid JSON in {label}: {exc}")


def normalize_exclusions(raw_exclusions: object) -> list[dict[str, object]]:
	if not isinstance(raw_exclusions, dict):
		fail("security-audit exclusions must be a JSON object")
	if raw_exclusions.get("schema_version") != "security_audit_fp_exclusions.v1":
		fail("unsupported security-audit exclusions schema_version")
	rules = raw_exclusions.get("rules")
	if not isinstance(rules, list):
		fail("security-audit exclusions rules must be a JSON array")
	normalized_rules: list[dict[str, object]] = []
	for rule in rules:
		if not isinstance(rule, dict):
			fail("each security-audit exclusion rule must be an object")
		rule_id = str(rule.get("id") or "").strip()
		reason = str(rule.get("reason") or "").strip()
		fields = rule.get("fields") or {}
		contains = rule.get("contains") or {}
		if not rule_id or not reason:
			fail("security-audit exclusion rules require non-empty id and reason")
		if not isinstance(fields, dict) or not isinstance(contains, dict):
			fail(f"security-audit exclusion rule {rule_id} must use object fields/contains matchers")
		normalized_fields: dict[str, str] = {}
		for key, value in fields.items():
			if key not in allowed_exact_fields:
				fail(f"security-audit exclusion rule {rule_id} uses unsupported exact field {key}")
			if not isinstance(value, str) or not value.strip():
				fail(f"security-audit exclusion rule {rule_id} must use non-empty string exact matches")
			normalized_fields[key] = value.strip()
		normalized_contains: dict[str, list[str]] = {}
		for key, value in contains.items():
			if key not in allowed_contains_fields:
				fail(f"security-audit exclusion rule {rule_id} uses unsupported contains field {key}")
			if isinstance(value, str):
				values = [value]
			elif isinstance(value, list):
				values = value
			else:
				fail(f"security-audit exclusion rule {rule_id} must use string or string-list contains values")
			needles: list[str] = []
			for needle in values:
				if not isinstance(needle, str) or not needle.strip():
					fail(f"security-audit exclusion rule {rule_id} contains entries must be non-empty strings")
				needles.append(needle.strip().lower())
			normalized_contains[key] = needles
		normalized_rules.append(
			{
				"id": rule_id,
				"reason": reason,
				"fields": normalized_fields,
				"contains": normalized_contains,
			}
		)
	return normalized_rules


def normalize_finding(raw_finding: object) -> tuple[dict[str, object] | None, str | None]:
	if not isinstance(raw_finding, dict):
		return None, "finding must be an object"
	finding_id = str(raw_finding.get("finding_id") or "").strip()
	category = str(raw_finding.get("owasp_or_stride_category") or "").strip()
	severity = str(raw_finding.get("severity") or "").strip().lower()
	exploit_scenario = str(raw_finding.get("exploit_scenario") or "").strip()
	recommendation = str(raw_finding.get("recommendation") or "").strip()
	file_value = str(raw_finding.get("file") or "").strip()
	confidence = raw_finding.get("confidence")
	line = raw_finding.get("line")

	if not finding_id:
		return None, "finding_id is required"
	if not category:
		return None, f"{finding_id}: owasp_or_stride_category is required"
	if severity not in severity_rank:
		return None, f"{finding_id}: severity must be one of critical/high/medium/low"
	if isinstance(confidence, bool) or not isinstance(confidence, int) or confidence < 1 or confidence > 10:
		return None, f"{finding_id}: confidence must be an integer from 1 to 10"
	if isinstance(line, bool) or not isinstance(line, int) or line < 1:
		return None, f"{finding_id}: line must be a positive integer"
	if not exploit_scenario:
		return None, f"{finding_id}: exploit_scenario is required"
	if not recommendation:
		return None, f"{finding_id}: recommendation is required"
	if not file_value:
		return None, f"{finding_id}: file is required"

	try:
		candidate_path = PurePosixPath(file_value)
	except Exception as exc:
		return None, f"{finding_id}: invalid file path: {exc}"

	if candidate_path.is_absolute() or ".." in candidate_path.parts:
		return None, f"{finding_id}: file must be a repository-relative path"

	normalized_file = candidate_path.as_posix()
	if normalized_file.startswith("./"):
		normalized_file = normalized_file[2:]
	if not normalized_file or normalized_file == ".":
		return None, f"{finding_id}: file must resolve to a repository file"

	resolved_path = (repo_root / normalized_file).resolve()
	try:
		resolved_path.relative_to(repo_root)
	except ValueError:
		return None, f"{finding_id}: file escapes repository root"
	if not resolved_path.is_file():
		return None, f"{finding_id}: file does not exist in checkout"

	try:
		line_count = len(resolved_path.read_text(encoding="utf-8", errors="replace").splitlines())
	except OSError as exc:
		return None, f"{finding_id}: unable to read file: {exc}"
	if line_count < 1:
		return None, f"{finding_id}: file is empty and cannot back a concrete line reference"
	if line > line_count:
		return None, f"{finding_id}: line {line} exceeds file length {line_count}"

	return (
		{
			"finding_id": finding_id,
			"owasp_or_stride_category": category,
			"severity": severity,
			"confidence": confidence,
			"file": normalized_file,
			"line": line,
			"exploit_scenario": exploit_scenario,
			"recommendation": recommendation,
		},
		None,
	)


def matching_exclusion_rule(finding: dict[str, object], rules: list[dict[str, object]]) -> str | None:
	for rule in rules:
		rule_fields = rule["fields"]
		rule_contains = rule["contains"]
		matched = True
		for field_name, expected_value in rule_fields.items():
			if str(finding.get(field_name) or "") != str(expected_value):
				matched = False
				break
		if not matched:
			continue
		for field_name, needles in rule_contains.items():
			haystack = str(finding.get(field_name) or "").lower()
			if not all(needle in haystack for needle in needles):
				matched = False
				break
		if matched:
			return str(rule["id"])
	return None


raw_findings = load_json(codex_output_path, label="Codex output")
exclusion_rules = normalize_exclusions(load_json(exclusions_path, label="security-audit exclusions"))

if not isinstance(raw_findings, list):
	fail("security-audit Codex output must be a JSON array")

kept_findings: list[dict[str, object]] = []
invalid_findings: list[dict[str, str]] = []
excluded_findings: list[dict[str, str]] = []
low_confidence_findings: list[str] = []
out_of_scope_findings: list[str] = []
seen_ids: set[str] = set()

for raw_finding in raw_findings:
	normalized_finding, error_message = normalize_finding(raw_finding)
	if normalized_finding is None:
		invalid_findings.append({"error": error_message or "invalid finding"})
		continue
	finding_id = str(normalized_finding["finding_id"])
	if finding_id in seen_ids:
		invalid_findings.append({"error": f"{finding_id}: duplicate finding_id"})
		continue
	seen_ids.add(finding_id)
	if audit_scope_mode == "incremental" and str(normalized_finding["file"]) not in changed_files:
		out_of_scope_findings.append(finding_id)
		continue
	if int(normalized_finding["confidence"]) < confidence_gate:
		low_confidence_findings.append(finding_id)
		continue
	rule_id = matching_exclusion_rule(normalized_finding, exclusion_rules)
	if rule_id is not None:
		excluded_findings.append({"finding_id": finding_id, "rule_id": rule_id})
		continue
	kept_findings.append(normalized_finding)

kept_findings.sort(
	key=lambda finding: (
		severity_rank[str(finding["severity"])],
		-int(finding["confidence"]),
		str(finding["file"]),
		int(finding["line"]),
		str(finding["finding_id"]),
	)
)

filtered_findings_path.write_text(
	json.dumps(kept_findings, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
	encoding="utf-8",
)
summary_path.write_text(
	json.dumps(
		{
			"kept": len(kept_findings),
			"suppressed_low_confidence": len(low_confidence_findings),
			"suppressed_excluded": len(excluded_findings),
			"suppressed_invalid": len(invalid_findings),
			"suppressed_out_of_scope": len(out_of_scope_findings),
			"low_confidence_finding_ids": low_confidence_findings,
			"excluded": excluded_findings,
			"invalid": invalid_findings,
			"out_of_scope_finding_ids": out_of_scope_findings,
		},
		ensure_ascii=True,
		indent=2,
		sort_keys=True,
	)
	+ "\n",
	encoding="utf-8",
)
PY

# Standalone workflow: no cycle-local issue cache exists here. Fetch existing
# follow-up issues once and reuse the result for weekly-cap accounting + dedupe.
gh_retry gh issue list \
	--repo "${GITHUB_REPOSITORY}" \
	--state all \
	--label "ai:security" \
	--limit 200 \
		--json number,title,body,createdAt,url > "${EXISTING_FOLLOWUPS_JSON}"

python3 - \
	"${FILTERED_FINDINGS_FILE}" \
	"${FILTER_SUMMARY_FILE}" \
	"${EXISTING_FOLLOWUPS_JSON}" \
	"${TRACKER_NUMBER}" \
	"${TRACKER_COMMENT_FILE}" \
	"${FOLLOWUP_BODY_DIR}" \
	"${FOLLOWUP_INDEX_FILE}" \
	"${FOLLOWUP_SUMMARY_ENV}" \
	"${SECURITY_AUDIT_CONFIDENCE_GATE}" \
	"${SECURITY_AUDIT_FP_EXCLUSIONS}" \
	"${FOLLOWUP_MARKER_PREFIX}" \
	"${MAX_FOLLOWUP_ISSUES_PER_WEEK}" \
	"${AUDIT_SCOPE_MODE}" \
	"${HEAD_SHA}" \
	"${LAST_AUDITED_SHA}" <<'PY'
from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

findings_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
existing_followups_path = Path(sys.argv[3])
tracker_number = sys.argv[4]
tracker_comment_path = Path(sys.argv[5])
followup_body_dir = Path(sys.argv[6])
followup_index_path = Path(sys.argv[7])
followup_summary_env_path = Path(sys.argv[8])
confidence_gate = sys.argv[9]
exclusions_path = sys.argv[10]
followup_marker_prefix = sys.argv[11]
max_followups_per_week = int(sys.argv[12])
audit_scope_mode = sys.argv[13]
head_sha = sys.argv[14].strip()
last_audited_sha = sys.argv[15].strip()


def load_json(path: Path, *, label: str):
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SystemExit(f"unable to load {label}: {exc}")


def parse_dt(value: object) -> datetime | None:
	if not isinstance(value, str) or not value.strip():
		return None
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
	except ValueError:
		return None


def truncate_title(title: str) -> str:
	normalized = " ".join(title.split())
	if len(normalized) <= 250:
		return normalized
	return normalized[:247] + "..."


findings = load_json(findings_path, label="filtered findings")
summary = load_json(summary_path, label="filter summary")
existing_followups = load_json(existing_followups_path, label="existing follow-up issues")

if not isinstance(findings, list) or not isinstance(summary, dict) or not isinstance(existing_followups, list):
	raise SystemExit("security-audit summary generation received invalid JSON payloads")

marker_regex = re.compile(re.escape(followup_marker_prefix) + r"([^>]+) -->")
existing_finding_ids: set[str] = set()
weekly_existing_count = 0
now_utc = datetime.now(timezone.utc)
week_start = (now_utc - timedelta(days=now_utc.weekday())).date()

for issue in existing_followups:
	if not isinstance(issue, dict):
		continue
	body = str(issue.get("body") or "")
	match = marker_regex.search(body)
	if match is None:
		continue
	finding_id = match.group(1).strip()
	if finding_id:
		existing_finding_ids.add(finding_id)
	created_at = parse_dt(issue.get("createdAt"))
	if created_at is not None and created_at.date() >= week_start:
		weekly_existing_count += 1

remaining_weekly_capacity = max(0, max_followups_per_week - weekly_existing_count)
planned_followups: list[dict[str, object]] = []
skipped_existing_count = 0
skipped_weekly_cap_count = 0

for finding in findings:
	if not isinstance(finding, dict):
		continue
	finding_id = str(finding.get("finding_id") or "").strip()
	if finding_id in existing_finding_ids:
		skipped_existing_count += 1
		continue
	if len(planned_followups) >= remaining_weekly_capacity:
		skipped_weekly_cap_count += 1
		continue
	planned_followups.append(finding)

if audit_scope_mode == "incremental" and last_audited_sha:
	scope_line = f"- Audit scope: incremental (`{last_audited_sha}`..`{head_sha}`)"
else:
	scope_line = "- Audit scope: full default-branch checkout"

comment_lines = [
	f"## {now_utc.date().isoformat()} Security audit",
	"",
	scope_line,
	f"- Audited commit: `{head_sha or 'n/a'}`",
	f"- Confidence gate: `>= {confidence_gate}`",
	f"- Exclusion catalog: `{exclusions_path}`",
	f"- Findings surfaced: {len(findings)}",
	f"- Suppressed low-confidence findings: {int(summary.get('suppressed_low_confidence', 0))}",
	f"- Suppressed excluded findings: {int(summary.get('suppressed_excluded', 0))}",
	f"- Suppressed invalid findings: {int(summary.get('suppressed_invalid', 0))}",
	f"- Suppressed out-of-scope findings: {int(summary.get('suppressed_out_of_scope', 0))}",
	f"- Existing follow-up issues this UTC week: {weekly_existing_count}",
	f"- New follow-up issues planned this run: {len(planned_followups)}",
	f"- Findings skipped because a marked follow-up issue already exists: {skipped_existing_count}",
	f"- Findings deferred by the weekly cap: {skipped_weekly_cap_count}",
]

if findings:
	comment_lines.extend(["", "### Findings", ""])
	for idx, finding in enumerate(findings, 1):
		comment_lines.extend(
			[
				f"{idx}. **[{finding['severity']} | {finding['confidence']}/10]** `{finding['file']}:{finding['line']}` — `{finding['owasp_or_stride_category']}`",
				f"   - Finding ID: `{finding['finding_id']}`",
				f"   - Exploit scenario: {finding['exploit_scenario']}",
				f"   - Recommendation: {finding['recommendation']}",
			]
		)
else:
	comment_lines.extend(["", "No findings met the confidence gate after exclusions."])

tracker_comment_path.write_text("\n".join(comment_lines) + "\n", encoding="utf-8")

followup_body_dir.mkdir(parents=True, exist_ok=True)
index_lines: list[str] = []
for idx, finding in enumerate(planned_followups):
	title = truncate_title(
		f"[security-audit] {finding['finding_id']}: {finding['severity']} {finding['file']}:{finding['line']}"
	)
	body_path = followup_body_dir / f"followup-{idx}.md"
	body_lines = [
		f"{followup_marker_prefix}{finding['finding_id']} -->",
		f"Refs #{tracker_number}",
		"",
		"Generated by `.github/workflows/security-audit.yml`.",
		"",
		f"- Category: `{finding['owasp_or_stride_category']}`",
		f"- Severity: `{finding['severity']}`",
		f"- Confidence: `{finding['confidence']}/10`",
		f"- Location: `{finding['file']}:{finding['line']}`",
		"",
		"## Exploit scenario",
		str(finding["exploit_scenario"]),
		"",
		"## Recommendation",
		str(finding["recommendation"]),
	]
	body_path.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
	index_lines.append(f"{body_path}\t{title}\n")

followup_index_path.write_text("".join(index_lines), encoding="utf-8")
followup_summary_env_path.write_text(
	"\n".join(
		[
			f"SURVIVING_FINDINGS_COUNT={shlex.quote(str(len(findings)))}",
			f"FOLLOWUP_CREATE_COUNT={shlex.quote(str(len(planned_followups)))}",
		]
	)
	+ "\n",
	encoding="utf-8",
)
PY

gh_retry gh issue comment "${TRACKER_NUMBER}" \
	--repo "${GITHUB_REPOSITORY}" \
	--body-file "${TRACKER_COMMENT_FILE}"

while IFS=$'\t' read -r FOLLOWUP_BODY_PATH FOLLOWUP_TITLE; do
	[ -n "${FOLLOWUP_BODY_PATH}" ] || continue
	gh_retry gh issue create \
		--repo "${GITHUB_REPOSITORY}" \
		--title "${FOLLOWUP_TITLE}" \
		--label "ai:security" \
		--body-file "${FOLLOWUP_BODY_PATH}" >/dev/null
done < "${FOLLOWUP_INDEX_FILE}"

if [ -n "${HEAD_SHA}" ]; then
	# Persist the audited HEAD SHA on the tracker body so the next run can
	# skip when unchanged or diff-scope against it. One extra `gh issue edit`
	# per completed audit; reads are free because the tracker-discovery
	# `gh issue list` above already returns the body (§15). Runs only after
	# the comment and follow-ups posted, so a failed run re-audits the same
	# range instead of silently advancing the marker.
	{
		cat "${TRACKER_BODY_FILE}"
		echo
		echo "Last audited commit (managed automatically; do not edit):"
		echo "${LAST_SHA_MARKER_PREFIX}${HEAD_SHA} -->"
	} > "${TRACKER_BODY_WITH_SHA_FILE}"
	gh_retry gh issue edit "${TRACKER_NUMBER}" \
		--repo "${GITHUB_REPOSITORY}" \
		--body-file "${TRACKER_BODY_WITH_SHA_FILE}"
fi

# shellcheck disable=SC1090
source "${FOLLOWUP_SUMMARY_ENV}"

echo "security-audit: tracker=#${TRACKER_NUMBER} findings=${SURVIVING_FINDINGS_COUNT} followups_created=${FOLLOWUP_CREATE_COUNT}"
