#!/usr/bin/env bash
set -euo pipefail

is_truthy()
{
	local normalized=""
	normalized="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
	case "${normalized}" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

sticky_log_noop()
{
	local reason="${1:-unknown}"
	shift || true
	if [ "$#" -gt 0 ]; then
		printf 'STICKY_ANNOTATOR_NOOP reason=%s %s\n' "${reason}" "$*"
	else
		printf 'STICKY_ANNOTATOR_NOOP reason=%s\n' "${reason}"
	fi
}

RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"

if ! is_truthy "${STICKY_FINDINGS_ENABLED:-false}"; then
	exit 0
fi

STICKY_LINE_BUCKET="${STICKY_LINE_BUCKET:-5}"
if ! [[ "${STICKY_LINE_BUCKET}" =~ ^[0-9]+$ ]]; then
	STICKY_LINE_BUCKET=5
fi

if [ -z "${PR_NUMBER:-}" ] || ! [[ "${PR_NUMBER}" =~ ^[0-9]+$ ]] || [ -z "${AUTOFIX_ITERATION:-}" ] || ! [[ "${AUTOFIX_ITERATION}" =~ ^[0-9]+$ ]] || [ "${AUTOFIX_ITERATION}" -le 1 ]; then
	sticky_log_noop "missing_prior_context" "pr=${PR_NUMBER:-missing} round=${AUTOFIX_ITERATION:-missing}"
	exit 0
fi

prior_round="$((AUTOFIX_ITERATION - 1))"
REVIEWER_BUNDLE_FILE="${REVIEWER_BUNDLE_FILE:-${RUNTIME_DIR}/reviewer_bundle.txt}"
PRIOR_CONSOLIDATOR_PARSED_FILE="${PRIOR_CONSOLIDATOR_PARSED_FILE:-.ai/review_runtime/pr-${PR_NUMBER}/round-${prior_round}/consolidator_parsed.txt}"
STICKY_FINDINGS_JSON_FILE="${STICKY_FINDINGS_JSON_FILE:-.ai/review_runtime/pr-${PR_NUMBER}/round-${AUTOFIX_ITERATION}/sticky_findings.json}"
STICKY_FINDINGS_PRIORS_FILE="${STICKY_FINDINGS_PRIORS_FILE:-${RUNTIME_DIR}/sticky_findings_priors.txt}"

rm -f "${STICKY_FINDINGS_JSON_FILE}" "${STICKY_FINDINGS_PRIORS_FILE}" 2>/dev/null || true

if [ ! -s "${PRIOR_CONSOLIDATOR_PARSED_FILE}" ]; then
	sticky_log_noop "prior_artifact_missing" "source=${PRIOR_CONSOLIDATOR_PARSED_FILE}"
	exit 0
fi

if [ ! -s "${REVIEWER_BUNDLE_FILE}" ]; then
	sticky_log_noop "reviewer_bundle_missing" "source=${REVIEWER_BUNDLE_FILE}"
	exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
	sticky_log_noop "python_unavailable"
	exit 0
fi

mkdir -p "$(dirname "${STICKY_FINDINGS_JSON_FILE}")" "$(dirname "${STICKY_FINDINGS_PRIORS_FILE}")"

match_count="$(PYTHONDONTWRITEBYTECODE=1 python3 - "${PRIOR_CONSOLIDATOR_PARSED_FILE}" "${REVIEWER_BUNDLE_FILE}" "${STICKY_FINDINGS_JSON_FILE}" "${STICKY_FINDINGS_PRIORS_FILE}" "${STICKY_LINE_BUCKET}" "${AUTOFIX_ITERATION}" "${prior_round}" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import defaultdict


prior_path, bundle_path, json_path, priors_path, line_bucket_s, current_round_s, prior_round_s = sys.argv[1:8]
line_bucket = int(line_bucket_s)
current_round = int(current_round_s)
prior_round = int(prior_round_s)


def squish(value: object, limit: int | None = None) -> str:
	text = re.sub(r"\s+", " ", str(value)).strip()
	if limit is not None and len(text) > limit:
		text = text[: max(limit - 3, 0)].rstrip() + "..."
	return text


def normalize_file(path: object) -> str:
	text = squish(path)
	text = text.strip('`"')
	text = text.replace('\\', '/')
	if text.startswith('./'):
		text = text[2:]
	text = re.sub(r':[0-9]+(?:-[0-9]+)?$', '', text)
	text = re.sub(r'[,:;]+$', '', text)
	return text


def normalize_symptom(symptom: object) -> str:
	text = squish(symptom).lower()
	while True:
		updated = re.sub(r'^(?:issue|bug)\s*:\s*', '', text)
		if updated == text:
			break
		text = updated
	return text


def identity_key(file_path: str, symptom: str) -> str:
	payload = f"{normalize_file(file_path)}:{normalize_symptom(symptom)}"
	return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]


def parse_line_spec(value: str) -> tuple[int, int] | None:
	if not isinstance(value, str):
		return None
	text = squish(value)
	for pattern in (
		r'(?i)\blines?\s+(\d+)(?:\s*-\s*(\d+))?\b',
		r'^(\d+)(?:\s*-\s*(\d+))?$',
	):
		match = re.search(pattern, text)
		if not match:
			continue
		start = int(match.group(1))
		end = int(match.group(2) or match.group(1))
		if start < 1 or end < start:
			return None
		return start, end
	return None


def line_range_distance(
	left_start: int,
	left_end: int,
	right_start: int,
	right_end: int,
) -> int:
	if left_end < right_start:
		return right_start - left_end
	if right_end < left_start:
		return left_start - right_end
	return 0


def reviewer_from_path(path: str) -> str:
	name = os.path.basename(squish(path))
	if '.' in name:
		name = name.rsplit('.', 1)[0]
	return name or 'unknown_reviewer'


def parse_prior_issues(path: str) -> list[dict[str, object]]:
	issues: list[dict[str, object]] = []
	start_re = re.compile(r'^=== ISSUE (.+) ===$')
	end_re = re.compile(r'^=== END ISSUE (.+) ===$')
	header_re = re.compile(r'^([A-Z_]+):\s*(.*)$')
	multiline_fields = {'EVIDENCE', 'NOTES'}
	current_id: str | None = None
	current_field: str | None = None
	current_fields: dict[str, object] = {}

	def extract_prior_symptom(value: object) -> str:
		if isinstance(value, list):
			lines = value
		else:
			lines = str(value).splitlines()
		for raw in lines:
			text = squish(raw)
			text = re.sub(r'^[^>]+>\s*', '', text)
			if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
				text = text[1:-1].strip()
			if text:
				return text
		return ''

	def finalize(issue_id: str, fields: dict[str, object]) -> None:
		file_path = normalize_file(fields.get('FILE', ''))
		line_spec = squish(fields.get('LINES', ''))
		classification = squish(fields.get('CLASSIFICATION', ''))
		if not file_path or classification not in {'non-actionable', 'nice-to-have', 'unclassified'}:
			return
		parsed_lines = parse_line_spec(line_spec)
		if parsed_lines is None:
			return
		prior_symptom = extract_prior_symptom(fields.get('EVIDENCE', ''))
		if not prior_symptom:
			return
		notes_value = fields.get('NOTES', '')
		if isinstance(notes_value, list):
			notes_text = '\n'.join(notes_value)
		else:
			notes_text = str(notes_value)
		issues.append(
			{
				'issue_id': squish(issue_id),
				'file': file_path,
				'line_start': parsed_lines[0],
				'line_end': parsed_lines[1],
				'classification': classification,
				'rejection_kind': squish(fields.get('REJECTION_KIND', '')),
				'notes': squish(notes_text, 240),
				'identity_key': identity_key(file_path, prior_symptom),
			}
		)

	with open(path, 'r', encoding='utf-8') as handle:
		for raw_line in handle:
			line = raw_line.rstrip('\n')
			start_match = start_re.match(line)
			if start_match:
				current_id = start_match.group(1)
				current_field = None
				current_fields = {}
				continue

			end_match = end_re.match(line)
			if end_match:
				if current_id is not None and current_id == end_match.group(1):
					finalize(current_id, current_fields)
				current_id = None
				current_field = None
				current_fields = {}
				continue

			if current_id is None:
				continue

			header_match = header_re.match(line)
			if header_match:
				header = header_match.group(1)
				value = header_match.group(2)
				if header in multiline_fields:
					current_fields[header] = [value]
					current_field = header
				else:
					current_fields[header] = value
					current_field = None
				continue

			if current_field is not None:
				current_fields.setdefault(current_field, []).append(line)

	return issues


def parse_reviewer_findings(path: str) -> list[dict[str, object]]:
	findings: list[dict[str, object]] = []
	in_content = False
	source_path = ''
	content_lines: list[str] = []

	def finalize_chunk(reviewer: str, chunk_lines: list[str]) -> dict[str, object] | None:
		file_path = ''
		line_ref = ''
		symptom = ''
		for raw in chunk_lines:
			stripped = raw.strip()
			lower = stripped.lower()
			if lower.startswith('file:'):
				file_path = normalize_file(stripped.split(':', 1)[1])
			elif lower.startswith('line or code reference:'):
				line_ref = stripped.split(':', 1)[1].strip()
			elif lower.startswith('code:') and not line_ref:
				line_ref = stripped.split(':', 1)[1].strip()
			elif lower.startswith('line:') and not line_ref:
				line_ref = stripped.split(':', 1)[1].strip()
			elif lower.startswith('problem:'):
				symptom = stripped.split(':', 1)[1].strip()
		parsed_line_ref = line_ref
		path_match = None
		if line_ref:
			path_match = re.search(r'([A-Za-z0-9_./-]+):([0-9]+(?:\s*-\s*[0-9]+)?)\s*$', line_ref)
		if not file_path and path_match:
			file_path = normalize_file(path_match.group(1))
		if path_match:
			parsed_line_ref = path_match.group(2)
		parsed_lines = parse_line_spec(parsed_line_ref)
		if not file_path or not symptom or parsed_lines is None:
			return None
		return {
			'file': file_path,
			'line_start': parsed_lines[0],
			'line_end': parsed_lines[1],
			'symptom': squish(symptom, 240),
			'identity_key': identity_key(file_path, symptom),
			'reviewer': reviewer,
		}

	def finalize_content_block() -> None:
		reviewer = reviewer_from_path(source_path)
		current_chunk: list[str] = []
		for raw in content_lines:
			if raw.strip().lower().startswith('file:'):
				if current_chunk:
					parsed = finalize_chunk(reviewer, current_chunk)
					if parsed is not None:
						findings.append(parsed)
				current_chunk = [raw]
			elif current_chunk:
				current_chunk.append(raw)
		if current_chunk:
			parsed = finalize_chunk(reviewer, current_chunk)
			if parsed is not None:
				findings.append(parsed)

	with open(path, 'r', encoding='utf-8') as handle:
		for raw_line in handle:
			line = raw_line.rstrip('\n')
			if line.startswith('FILE_PATH:'):
				source_path = line.split(':', 1)[1].strip()
				in_content = False
				content_lines = []
				continue
			if line == 'CONTENT_START':
				in_content = True
				content_lines = []
				continue
			if line == 'CONTENT_END':
				if in_content:
					finalize_content_block()
				in_content = False
				content_lines = []
				continue
			if in_content:
				content_lines.append(line)

	return findings


prior_issues = parse_prior_issues(prior_path)
current_findings = parse_reviewer_findings(bundle_path)

prior_by_key: dict[str, list[dict[str, object]]] = defaultdict(list)
for issue in prior_issues:
	prior_by_key[str(issue['identity_key'])].append(issue)

for rows in prior_by_key.values():
	rows.sort(key=lambda row: (int(row['line_start']), str(row['issue_id'])))

aggregated: dict[tuple[str, str], dict[str, object]] = {}
for finding in current_findings:
	identity = str(finding['identity_key'])
	finding_start = int(finding['line_start'])
	finding_end = int(finding['line_end'])

	def candidate_key(issue: dict[str, object]) -> tuple[int, int, str]:
		return (
			line_range_distance(
				int(issue['line_start']),
				int(issue['line_end']),
				finding_start,
				finding_end,
			),
			abs(int(issue['line_start']) - finding_start),
			str(issue['issue_id']),
		)

	candidates = [
		issue
		for issue in prior_by_key.get(identity, [])
		if candidate_key(issue)[0] <= line_bucket
	]
	if not candidates:
		continue
	best = min(candidates, key=candidate_key)
	best_key = candidate_key(best)
	aggregate_key = (identity, str(best['issue_id']))
	entry = aggregated.get(aggregate_key)
	if entry is None:
		entry = {
			'identity_key': identity,
			'file': str(best['file']),
			'current_line': finding_start,
			'current_line_match_key': best_key,
			'current_lines': {finding_start},
			'current_symptom': str(finding['symptom']),
			'current_reviewers': {str(finding['reviewer'])},
			'prior_issue_id': str(best['issue_id']),
			'prior_line_start': int(best['line_start']),
			'prior_line_end': int(best['line_end']),
			'prior_classification': str(best['classification']),
			'prior_rejection_kind': str(best['rejection_kind']),
			'prior_notes': str(best['notes']),
			'sticky': True,
		}
		aggregated[aggregate_key] = entry
	else:
		entry['current_lines'].add(finding_start)
		entry['current_reviewers'].add(str(finding['reviewer']))
		if best_key < tuple(entry['current_line_match_key']):
			entry['current_line'] = finding_start
			entry['current_line_match_key'] = best_key
			entry['current_symptom'] = str(finding['symptom'])

matches: list[dict[str, object]] = []
for entry in sorted(aggregated.values(), key=lambda row: (str(row['file']), int(row['prior_line_start']), str(row['prior_issue_id']), str(row['identity_key']))):
	current_lines = sorted(int(value) for value in entry['current_lines'])
	current_reviewers = sorted(str(value) for value in entry['current_reviewers'])
	matches.append(
		{
			'identity_key': str(entry['identity_key']),
			'file': str(entry['file']),
			'current_line': int(entry['current_line']),
			'current_lines': current_lines,
			'current_symptom': squish(entry['current_symptom'], 200),
			'current_reviewers': current_reviewers,
			'prior_issue_id': str(entry['prior_issue_id']),
			'prior_line_start': int(entry['prior_line_start']),
			'prior_line_end': int(entry['prior_line_end']),
			'prior_classification': str(entry['prior_classification']),
			'prior_rejection_kind': str(entry['prior_rejection_kind']),
			'prior_notes': squish(entry['prior_notes'], 240),
			'sticky': True,
		}
	)

if not matches:
	print('0')
	sys.exit(0)

payload = {
	'current_round': current_round,
	'prior_round': prior_round,
	'line_bucket': line_bucket,
	'matches': matches,
}
with open(json_path, 'w', encoding='utf-8') as handle:
	json.dump(payload, handle, indent=2, sort_keys=True)
	handle.write('\n')

with open(priors_path, 'w', encoding='utf-8') as handle:
	handle.write('<sticky_findings_priors>\n')
	handle.write('source: prior_round_consolidator_parsed\n')
	handle.write(f'current_round: {current_round}\n')
	handle.write(f'prior_round: {prior_round}\n')
	handle.write(f'line_bucket: {line_bucket}\n')
	handle.write('matches:\n')
	for row in matches:
		handle.write(f"- identity_key: {row['identity_key']}\n")
		handle.write(f"  file: {row['file']}\n")
		handle.write(f"  current_line: {row['current_line']}\n")
		handle.write(f"  current_lines: {', '.join(str(value) for value in row['current_lines'])}\n")
		handle.write(f"  current_symptom: {row['current_symptom']}\n")
		handle.write(f"  reviewers: {', '.join(row['current_reviewers'])}\n")
		handle.write(f"  prior_issue_id: {row['prior_issue_id']}\n")
		handle.write(f"  prior_lines: {row['prior_line_start']}-{row['prior_line_end']}\n")
		handle.write(f"  prior_classification: {row['prior_classification']}\n")
		if row['prior_rejection_kind']:
			handle.write(f"  prior_rejection_kind: {row['prior_rejection_kind']}\n")
		handle.write(f"  prior_notes: {row['prior_notes']}\n")
		handle.write('  sticky: true\n')
	handle.write('</sticky_findings_priors>\n')

for row in matches:
	print(
		f"STICKY_FINDING_DETECTED issue={row['prior_issue_id']} file={row['file']} "
		f"current_line={row['current_line']} prior_line={row['prior_line_start']} identity_key={row['identity_key']}",
		file=sys.stderr,
	)

print(str(len(matches)))
PY
)" || {
	rm -f "${STICKY_FINDINGS_JSON_FILE}" "${STICKY_FINDINGS_PRIORS_FILE}" 2>/dev/null || true
	sticky_log_noop "annotator_failed" "source=${PRIOR_CONSOLIDATOR_PARSED_FILE}"
	exit 0
}

if ! [[ "${match_count}" =~ ^[0-9]+$ ]] || [ "${match_count}" -le 0 ] || [ ! -s "${STICKY_FINDINGS_JSON_FILE}" ] || [ ! -s "${STICKY_FINDINGS_PRIORS_FILE}" ]; then
	rm -f "${STICKY_FINDINGS_JSON_FILE}" "${STICKY_FINDINGS_PRIORS_FILE}" 2>/dev/null || true
	exit 0
fi

printf 'STICKY_FINDING_DETECTED count=%s source=%s output=%s\n' \
	"${match_count}" \
	"${PRIOR_CONSOLIDATOR_PARSED_FILE}" \
	"${STICKY_FINDINGS_JSON_FILE}"
