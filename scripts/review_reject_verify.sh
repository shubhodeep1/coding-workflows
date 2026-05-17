#!/usr/bin/env bash
set -euo pipefail

trim_ascii_whitespace()
{
	local value="$1"
	value="${value#"${value%%[![:space:]]*}"}"
	value="${value%"${value##*[![:space:]]}"}"
	printf '%s' "${value}"
}

sanitize_numeric_identifier()
{
	local value="$1"
	local fallback="$2"
	value="$(trim_ascii_whitespace "${value}")"
	if [[ "${value}" =~ ^[0-9]+$ ]]; then
		printf '%s' "${value}"
		return 0
	fi
	printf '%s' "${fallback}"
}

CONSOLIDATOR_REJECT_SCHEMA_ENABLED="${CONSOLIDATOR_REJECT_SCHEMA_ENABLED:-false}"
RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
REVIEW_ISSUES_FILE="${REVIEW_ISSUES_FILE:-${RUNTIME_DIR}/review_issues.txt}"
PR_DIFF_FILE="${PR_DIFF_FILE:-${RUNTIME_DIR}/pr_diff.patch}"
LINKED_ISSUE_CONTEXT_FILE="${LINKED_ISSUE_CONTEXT_FILE:-${RUNTIME_DIR}/linked_issue_context.txt}"
PR_NUMBER="$(sanitize_numeric_identifier "${PR_NUMBER:-unknown}" "unknown")"

ROUND_NUMBER_BASE="$(trim_ascii_whitespace "${ROUND_NUMBER:-}")"
AUTOFIX_ITERATION_VALUE="$(trim_ascii_whitespace "${AUTOFIX_ITERATION:-}")"
if [[ "${ROUND_NUMBER_BASE}" =~ ^[0-9]+$ ]]; then
	CURRENT_ROUND="$((ROUND_NUMBER_BASE + 1))"
elif [[ "${AUTOFIX_ITERATION_VALUE}" =~ ^[0-9]+$ ]]; then
	CURRENT_ROUND="${AUTOFIX_ITERATION_VALUE}"
else
	CURRENT_ROUND="1"
fi

ARTIFACT_DIR=".ai/review_runtime/pr-${PR_NUMBER}/round-${CURRENT_ROUND}"
ARTIFACT_PATH="${ARTIFACT_DIR}/verified_rejections.json"
mkdir -p "${ARTIFACT_DIR}"

if ! PYTHONDONTWRITEBYTECODE=1 python3 - \
	"${REVIEW_ISSUES_FILE}" \
	"${PR_DIFF_FILE}" \
	"${LINKED_ISSUE_CONTEXT_FILE}" \
	"${ARTIFACT_PATH}" \
	"${PR_NUMBER}" \
	"${CURRENT_ROUND}" \
	"${CONSOLIDATOR_REJECT_SCHEMA_ENABLED}" <<'PY'
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path


REVIEW_ISSUES_FILE = Path(sys.argv[1])
PR_DIFF_FILE = Path(sys.argv[2])
LINKED_ISSUE_CONTEXT_FILE = Path(sys.argv[3])
ARTIFACT_PATH = Path(sys.argv[4])
PR_NUMBER = sys.argv[5]
CURRENT_ROUND = int(sys.argv[6])
SCHEMA_ENABLED = sys.argv[7].strip().lower() in {"1", "true", "yes", "on"}
REPO_ROOT = Path.cwd()

REJECT_EVIDENCE_HEADERS = (
	"EVIDENCE_DIFF_HUNK",
	"EVIDENCE_FILES_TOUCHED",
	"EVIDENCE_RUNTIME_PATH",
	"EVIDENCE_SPEC_QUOTE",
	"EVIDENCE_PRIOR_ROUND",
)
MULTILINE_HEADERS = {
	"EVIDENCE",
	"CURRENT_CODE",
	"SUGGESTED_APPROACH",
	"NOTES",
	"REVERSAL_REASON",
	"UNRECOGNISED",
	*REJECT_EVIDENCE_HEADERS,
}
FIELD_ORDER = [
	"FILE",
	"LINES",
	"LENS",
	"SEVERITY",
	"FLAGGED_BY",
	"CLASSIFICATION",
	"REJECTION_KIND",
	"MERGED_FROM",
	"EVIDENCE_DIFF_HUNK",
	"EVIDENCE_FILES_TOUCHED",
	"EVIDENCE_RUNTIME_PATH",
	"EVIDENCE_SPEC_QUOTE",
	"EVIDENCE_PRIOR_ROUND",
	"REVERSAL_REASON",
	"EVIDENCE",
	"CURRENT_CODE",
	"SUGGESTED_APPROACH",
	"NOTES",
	"PARSER_TAGS",
	"UNRECOGNISED",
]


def squish(value: str, limit: int | None = None) -> str:
	text = re.sub(r"\s+", " ", value).strip()
	if limit is not None and len(text) > limit:
		text = text[: max(limit - 3, 0)].rstrip() + "..."
	return text


def parse_issue_id(start_marker: str) -> str:
	match = re.match(r"^===\s+ISSUE\s+(.+?)\s+===$", start_marker)
	if match:
		return match.group(1).strip()
	return start_marker.strip("= ")


def parse_line_spec(spec: str) -> tuple[int, int] | None:
	match = re.match(r"^(\d+)(?:-(\d+))?$", spec.strip())
	if not match:
		return None
	start = int(match.group(1))
	end = int(match.group(2) or match.group(1))
	if start < 1 or end < start:
		return None
	return start, end


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


def sticky_line_bucket() -> int:
	raw = os.getenv("STICKY_LINE_BUCKET", "5").strip()
	return int(raw) if raw.isdigit() else 5


def parse_evidence_map(text: str) -> dict[str, list[str]]:
	data: dict[str, list[str]] = {}
	for raw in text.splitlines():
		line = raw.strip()
		if not line or ":" not in line:
			continue
		key, value = line.split(":", 1)
		data.setdefault(key.strip(), []).append(value.strip())
	return data


def first_value(mapping: dict[str, list[str]], key: str) -> str:
	values = mapping.get(key, [])
	return values[0] if values else ""


def extract_files_touched(text: str) -> list[str] | None:
	lines = text.splitlines()
	for idx, raw in enumerate(lines):
		stripped = raw.strip()
		if stripped not in {"files_touched:", "- files_touched:"}:
			continue
		base_indent = len(raw) - len(raw.lstrip(" "))
		entries: list[str] = []
		for candidate in lines[idx + 1 :]:
			if not candidate.strip():
				if entries:
					break
				continue
			indent = len(candidate) - len(candidate.lstrip(" "))
			if indent <= base_indent:
				break
			match = re.match(r"^\s*-\s+(.+?)\s*$", candidate)
			if not match:
				break
			entry = match.group(1).strip()
			if entry:
				entries.append(entry)
		return entries or None
	return None


def normalize_patch_path(path: str) -> str | None:
	path = path.strip()
	if path[:1] in {'"', "'"}:
		try:
			parts = shlex.split(path)
		except ValueError:
			return None
		if len(parts) != 1:
			return None
		path = parts[0]
	if path == "/dev/null":
		return None
	if path.startswith(("a/", "b/")):
		return path[2:]
	return path


def parse_diff_git_paths(line: str) -> tuple[str | None, str | None]:
	try:
		parts = shlex.split(line)
	except ValueError:
		return None, None
	if len(parts) < 4 or parts[0] != "diff" or parts[1] != "--git":
		return None, None
	return normalize_patch_path(parts[2]), normalize_patch_path(parts[3])


def parse_patch(text: str) -> tuple[bool, dict[str, dict[str, object]]]:
	if "diff --git " not in text:
		return False, {}
	files: dict[str, dict[str, object]] = {}
	current_path: str | None = None
	current_old_path: str | None = None
	current_lines: list[str] = []
	hunks: list[tuple[int, int]] = []
	current_deleted = False
	current_has_file_markers = False

	def flush() -> None:
		nonlocal current_path, current_old_path, current_lines, hunks, current_deleted, current_has_file_markers
		path = current_path or current_old_path
		if not path:
			current_path = None
			current_old_path = None
			current_lines = []
			hunks = []
			current_deleted = False
			current_has_file_markers = False
			return
		files[path] = {
			"text": "\n".join(current_lines),
			"hunks": list(hunks),
			"deleted": current_deleted,
			"metadata_only": not current_deleted and not hunks and not current_has_file_markers,
		}
		current_path = None
		current_old_path = None
		current_lines = []
		hunks = []
		current_deleted = False
		current_has_file_markers = False

	for line in text.splitlines():
		if line.startswith("diff --git "):
			flush()
			current_lines = [line]
			current_old_path, current_path = parse_diff_git_paths(line)
			current_deleted = False
			current_has_file_markers = False
			continue
		current_lines.append(line)
		if line.startswith("--- "):
			current_old_path = normalize_patch_path(line[4:])
			current_has_file_markers = True
			continue
		if line.startswith("+++ "):
			current_path = normalize_patch_path(line[4:])
			current_deleted = current_path is None
			current_has_file_markers = True
			continue
		match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
		if match and (current_path is not None or current_old_path is not None):
			start = int(match.group(1))
			length = int(match.group(2) or "1")
			end = start if length == 0 else start + length - 1
			hunks.append((start, end))
	flush()
	return bool(files), files


def render_blocks(blocks: list[dict[str, object]]) -> str:
	rendered: list[str] = []
	for block in blocks:
		fields: dict[str, str] = block["fields"]  # type: ignore[assignment]
		rendered.append(block["start"])  # type: ignore[arg-type]
		emitted: set[str] = set()
		for header in FIELD_ORDER:
			if header not in fields:
				continue
			value = fields[header]
			emitted.add(header)
			if header in MULTILINE_HEADERS:
				if header == "REVERSAL_REASON" and "\n" not in value:
					compact = squish(value)
					rendered.append(f"{header}: {compact}" if compact else f"{header}:")
				else:
					rendered.append(f"{header}:")
					if value:
						for line in value.splitlines():
							rendered.append(f"  {line}")
					else:
						rendered.append("  ")
			else:
				compact = squish(value)
				rendered.append(f"{header}: {compact}" if compact else f"{header}:")
		for header in block["order"]:  # type: ignore[index]
			if header in emitted or header not in fields:
				continue
			value = fields[header]
			if header in MULTILINE_HEADERS:
				if header == "REVERSAL_REASON" and "\n" not in value:
					compact = squish(value)
					rendered.append(f"{header}: {compact}" if compact else f"{header}:")
				else:
					rendered.append(f"{header}:")
					if value:
						for line in value.splitlines():
							rendered.append(f"  {line}")
					else:
						rendered.append("  ")
			else:
				compact = squish(value)
				rendered.append(f"{header}: {compact}" if compact else f"{header}:")
		rendered.append(block["end"])  # type: ignore[arg-type]
		rendered.append("")
	return "\n".join(rendered).rstrip() + ("\n" if rendered else "")


def verify_already_fixed(block: dict[str, object], diff_available: bool, patches: dict[str, dict[str, object]]) -> tuple[str, str]:
	if not diff_available:
		return "inconclusive", "PR diff was unavailable, so the cited diff hunk could not be verified."
	evidence = parse_evidence_map(block["fields"].get("EVIDENCE_DIFF_HUNK", ""))  # type: ignore[index]
	file_path = first_value(evidence, "file")
	line_spec = first_value(evidence, "lines")
	excerpt = first_value(evidence, "excerpt")
	parsed = parse_line_spec(line_spec)
	if not file_path or parsed is None:
		return "inconclusive", "Parsed rejection evidence was incomplete after the parser stage."
	display_path = normalize_patch_path(file_path) or file_path
	patch = patches.get(file_path)
	if patch is None:
		patch = patches.get(display_path)
	if patch is None:
		return "does-not-support", f"PR diff does not touch {display_path}."
	if bool(patch.get("metadata_only")) or (not patch.get("deleted") and not patch.get("hunks")):
		return "inconclusive", f"PR diff touches {display_path} but does not contain verifiable line hunks for the cited diff hunk."
	if bool(patch.get("deleted")):
		if excerpt and excerpt not in str(patch.get("text", "")):
			return "does-not-support", f"PR diff for {display_path} does not contain the cited excerpt."
		return "support", f"PR diff deletes {display_path}, which covers the cited fix."
	start, end = parsed
	for hunk_start, hunk_end in patch.get("hunks", []):
		if hunk_end < hunk_start:
			continue
		if not (end < hunk_start or hunk_end < start):
			if excerpt and excerpt not in str(patch.get("text", "")):
				return "does-not-support", f"PR diff for {display_path} does not contain the cited excerpt."
			return "support", f"PR diff contains a matching hunk for {display_path}:{line_spec}."
	return "does-not-support", f"PR diff does not contain a hunk covering {display_path}:{line_spec}."


def verify_out_of_scope(block: dict[str, object], linked_issue_text: str) -> tuple[str, str]:
	evidence = parse_evidence_map(block["fields"].get("EVIDENCE_FILES_TOUCHED", ""))  # type: ignore[index]
	cited_path = first_value(evidence, "cited_path")
	if not cited_path:
		return "inconclusive", "Parsed out-of-scope evidence was incomplete after the parser stage."
	files_touched = extract_files_touched(linked_issue_text)
	if not files_touched:
		return "inconclusive", "Linked issue files_touched metadata was unavailable, so scope could not be verified."
	if cited_path in files_touched:
		return "does-not-support", f"Linked issue files_touched explicitly includes {cited_path}."
	return "support", f"Linked issue files_touched does not include {cited_path}."


def verify_prior_round(block: dict[str, object], repo_root: Path, pr_number: str) -> tuple[str, str]:
	evidence = parse_evidence_map(block["fields"].get("EVIDENCE_PRIOR_ROUND", ""))  # type: ignore[index]
	round_value = first_value(evidence, "round")
	issue_id = first_value(evidence, "issue_id")
	prior_kind = first_value(evidence, "rejection_kind")
	sticky = first_value(evidence, "sticky").lower()
	if not round_value or not issue_id or not prior_kind:
		return "inconclusive", "Parsed prior-round evidence was incomplete after the parser stage."
	if not pr_number.isdigit():
		return "inconclusive", "PR number was not numeric, so prior-round artifact lookup was skipped."
	if sticky != "true":
		return "does-not-support", "already-rejected-with-evidence requires sticky: true."
	if not round_value.isdigit() or int(round_value) < 1:
		return "does-not-support", "already-rejected-with-evidence cites an invalid prior round."
	prior_artifact = repo_root / ".ai" / "review_runtime" / f"pr-{pr_number}" / f"round-{int(round_value)}" / "verified_rejections.json"
	if not prior_artifact.exists():
		return "inconclusive", f"Prior-round verifier artifact {prior_artifact} is unavailable."
	try:
		payload = json.loads(prior_artifact.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return "inconclusive", f"Prior-round verifier artifact {prior_artifact} could not be parsed."
	for row in payload.get("results", []):
		if not isinstance(row, dict):
			continue
		if str(row.get("issue_id", "")) != issue_id:
			continue
		if str(row.get("rejection_kind", "")) != prior_kind:
			return "does-not-support", "Prior-round verifier artifact disagrees on rejection kind."
		if str(row.get("verdict", "")) != "support":
			return "does-not-support", "Prior-round verifier artifact did not support the cited rejection."
		if str(row.get("file", "")) != str(block["fields"].get("FILE", "")):  # type: ignore[index]
			return "does-not-support", "Prior-round verifier artifact points at a different file."
		prior_lines = str(row.get("lines", ""))
		current_lines = str(block["fields"].get("LINES", ""))  # type: ignore[index]
		if prior_lines != current_lines:
			prior_range = parse_line_spec(prior_lines)
			current_range = parse_line_spec(current_lines)
			if prior_range is None or current_range is None:
				return "does-not-support", "Prior-round verifier artifact points at a different line range."
			if line_range_distance(*prior_range, *current_range) > sticky_line_bucket():
				return "does-not-support", "Prior-round verifier artifact points at a different line range."
		return "support", f"Prior-round verifier artifact still supports issue {issue_id}."
	return "does-not-support", f"Prior-round verifier artifact does not contain issue {issue_id}."


def parse_blocks(text: str) -> list[dict[str, object]]:
	blocks: list[dict[str, object]] = []
	current: dict[str, object] | None = None
	current_header: str | None = None
	for raw in text.splitlines():
		line = raw.rstrip("\n")
		if line.startswith("=== END ") and line.endswith(" ==="):
			if current is not None:
				current["end"] = line
				blocks.append(current)
			current = None
			current_header = None
			continue
		if line.startswith("=== ") and line.endswith(" ==="):
			current = {
				"start": line,
				"end": "",
				"fields": {},
				"order": [],
			}
			current_header = None
			continue
		if current is None:
			continue
		match = re.match(r"^([A-Z_]+):[ \t]*(.*)$", line)
		if match:
			header = match.group(1)
			value = match.group(2)
			fields: dict[str, str] = current["fields"]  # type: ignore[assignment]
			if header not in fields:
				current["order"].append(header)  # type: ignore[index]
			fields[header] = value
			current_header = header if header in MULTILINE_HEADERS else None
			continue
		if current_header is not None:
			fields = current["fields"]  # type: ignore[assignment]
			value = line[2:] if line.startswith("  ") else line
			fields[current_header] = f"{fields[current_header]}\n{value}" if fields[current_header] else value
	return blocks


review_issues_text = REVIEW_ISSUES_FILE.read_text(encoding="utf-8") if REVIEW_ISSUES_FILE.exists() else ""
linked_issue_text = LINKED_ISSUE_CONTEXT_FILE.read_text(encoding="utf-8") if LINKED_ISSUE_CONTEXT_FILE.exists() else ""
diff_text = PR_DIFF_FILE.read_text(encoding="utf-8") if PR_DIFF_FILE.exists() else ""
diff_available, patch_data = parse_patch(diff_text)
blocks = parse_blocks(review_issues_text)
results: list[dict[str, str | int | bool]] = []
mutated = False

if SCHEMA_ENABLED:
	for block in blocks:
		fields: dict[str, str] = block["fields"]  # type: ignore[assignment]
		classification = fields.get("CLASSIFICATION", "")
		rejection_kind = fields.get("REJECTION_KIND", "")
		if classification != "non-actionable" or not rejection_kind:
			continue
		issue_id = parse_issue_id(str(block["start"]))
		if rejection_kind == "already-fixed":
			verdict, reason = verify_already_fixed(block, diff_available, patch_data)
		elif rejection_kind == "out-of-scope":
			verdict, reason = verify_out_of_scope(block, linked_issue_text)
		elif rejection_kind == "already-rejected-with-evidence":
			verdict, reason = verify_prior_round(block, REPO_ROOT, PR_NUMBER)
		else:
			verdict = "inconclusive"
			reason = f"{rejection_kind} remains pending the Phase C PR-2 LLM verifier."

		result = {
			"issue_id": issue_id,
			"file": fields.get("FILE", ""),
			"lines": fields.get("LINES", ""),
			"rejection_kind": rejection_kind,
			"verdict": verdict,
			"reason": reason,
			"classification_before": classification,
			"classification_after": classification,
		}

		if verdict == "support":
			print(f"CONSOLIDATOR_REJECT_VERIFIED issue={issue_id} kind={rejection_kind} verdict=support")
		elif verdict == "does-not-support":
			fields["CLASSIFICATION"] = "must-fix"
			fields["REVERSAL_REASON"] = squish(reason, 400)
			result["classification_after"] = "must-fix"
			mutated = True
			print(f"CONSOLIDATOR_REJECT_VERIFIED issue={issue_id} kind={rejection_kind} verdict=does-not-support")
			print(f"CONSOLIDATOR_REJECT_REVERSED issue={issue_id} kind={rejection_kind} reason={squish(reason, 160)}")
		else:
			print(f"CONSOLIDATOR_REJECT_VERIFIER_INCONCLUSIVE issue={issue_id} kind={rejection_kind} reason={squish(reason, 160)}")

		results.append(result)

artifact = {
	"pr_number": PR_NUMBER,
	"round": CURRENT_ROUND,
	"schema_enabled": SCHEMA_ENABLED,
	"results": results,
}
ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

if mutated:
	REVIEW_ISSUES_FILE.write_text(render_blocks(blocks), encoding="utf-8")
PY
then
	printf 'CONSOLIDATOR_REJECT_VERIFIER_FAIL reason=python_error round=%s path=%s\n' \
		"${CURRENT_ROUND}" "${ARTIFACT_PATH}" >&2
	exit 1
fi

exit 0
