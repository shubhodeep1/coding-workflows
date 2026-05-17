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

sanitize_positive_integer()
{
	local value="$1"
	local fallback="$2"
	value="$(trim_ascii_whitespace "${value}")"
	if [[ "${value}" =~ ^[1-9][0-9]*$ ]]; then
		printf '%s' "${value}"
		return 0
	fi
	printf '%s' "${fallback}"
}

CONSOLIDATOR_REJECT_SCHEMA_ENABLED="${CONSOLIDATOR_REJECT_SCHEMA_ENABLED:-false}"
CONSOLIDATOR_REJECT_VERIFIER_ENABLED="$(trim_ascii_whitespace "${CONSOLIDATOR_REJECT_VERIFIER_ENABLED:-false}")"
CONSOLIDATOR_REJECT_VERIFIER_REASONING="$(trim_ascii_whitespace "${CONSOLIDATOR_REJECT_VERIFIER_REASONING:-low}")"
case "${CONSOLIDATOR_REJECT_VERIFIER_REASONING}" in
	xhigh|high|medium|low|none) ;;
	*) CONSOLIDATOR_REJECT_VERIFIER_REASONING="low" ;;
esac
CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX="$(sanitize_positive_integer "${CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX:-8}" "8")"
RUNTIME_DIR="${RUNTIME_DIR:?RUNTIME_DIR is required}"
REVIEW_ISSUES_FILE="${REVIEW_ISSUES_FILE:-${RUNTIME_DIR}/review_issues.txt}"
PR_DIFF_FILE="${PR_DIFF_FILE:-${RUNTIME_DIR}/pr_diff.patch}"
LINKED_ISSUE_CONTEXT_FILE="${LINKED_ISSUE_CONTEXT_FILE:-${RUNTIME_DIR}/linked_issue_context.txt}"
REJECT_VERIFIER_PROMPT_FILE="${REJECT_VERIFIER_PROMPT_FILE:-${SUPPORT_PROMPTS_DIR:-prompts}/consolidator-reject-verifier.txt}"
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
	"${CONSOLIDATOR_REJECT_SCHEMA_ENABLED}" \
	"${CONSOLIDATOR_REJECT_VERIFIER_ENABLED}" \
	"${CONSOLIDATOR_REJECT_VERIFIER_REASONING}" \
	"${CONSOLIDATOR_REJECT_VERIFIER_BATCH_MAX}" \
	"${REJECT_VERIFIER_PROMPT_FILE}" <<'PY'
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REVIEW_ISSUES_FILE = Path(sys.argv[1])
PR_DIFF_FILE = Path(sys.argv[2])
LINKED_ISSUE_CONTEXT_FILE = Path(sys.argv[3])
ARTIFACT_PATH = Path(sys.argv[4])
PR_NUMBER = sys.argv[5]
CURRENT_ROUND = int(sys.argv[6])
SCHEMA_ENABLED = sys.argv[7].strip().lower() in {"1", "true", "yes", "on"}
LLM_VERIFIER_ENABLED = sys.argv[8].strip().lower() in {"1", "true", "yes", "on"}
LLM_VERIFIER_REASONING = sys.argv[9].strip() or "low"
try:
	LLM_VERIFIER_BATCH_MAX = max(int(sys.argv[10]), 1)
except ValueError:
	LLM_VERIFIER_BATCH_MAX = 8
PROMPT_TEMPLATE = Path(sys.argv[11])
REPO_ROOT = Path.cwd()

LLM_REJECTION_KINDS = {"reviewer-wrong", "spec-doesnt-support"}
VERDICT_VALUES = {"support", "does-not-support", "inconclusive"}
LLM_VERIFIER_MODEL = "openai/gpt-5.4-mini"
LLM_VERIFIER_TIMEOUT_SECS = 120
LLM_VERIFIER_REASON_MAX_CHARS = 200

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


def chunked(items: list[dict[str, object]], size: int) -> list[list[dict[str, object]]]:
	return [items[idx : idx + size] for idx in range(0, len(items), size)]


def llm_prompt_missing_reason(prompt_path: Path) -> str:
	return f"LLM reject verifier prompt {prompt_path} is unavailable."


def llm_batch_fail_results(batch: list[dict[str, object]], reason_code: str, reason_text: str) -> dict[str, tuple[str, str]]:
	first_issue = batch[0]["issue_id"] if batch else "none"
	print(f"CONSOLIDATOR_REJECT_VERIFIER_FAIL reason={reason_code} first_issue={first_issue} batch_size={len(batch)}")
	reason_text = squish(reason_text, LLM_VERIFIER_REASON_MAX_CHARS)
	return {str(item["issue_id"]): ("inconclusive", reason_text) for item in batch}


def llm_unexpected_error_results(batch: list[dict[str, object]]) -> dict[str, tuple[str, str]]:
	return llm_batch_fail_results(
		batch,
		"unexpected_exception",
		"LLM reject verifier hit an unexpected internal error.",
	)


def upsert_toml_string_key(text: str, key: str, value: str) -> str:
	replacement = f'{key} = "{value}"'
	pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
	if pattern.search(text):
		return pattern.sub(replacement, text, count=1)
	if text and not text.endswith("\n"):
		text += "\n"
	return text + replacement + "\n"


def prepare_llm_codex_home(reasoning: str) -> Path:
	root = Path(os.environ.get("RUNNER_TEMP") or REPO_ROOT / ".ai" / "tmp") / "codex_home_reject_verify"
	root.mkdir(parents=True, exist_ok=True)
	codex_home = Path(tempfile.mkdtemp(prefix="reject-verifier.", dir=str(root)))
	try:
		source: Path | None = None
		if os.environ.get("CODEX_HOME") and Path(os.environ["CODEX_HOME"]).is_dir():
			source = Path(os.environ["CODEX_HOME"])
		elif (Path.home() / ".codex").is_dir():
			source = Path.home() / ".codex"
		if source is not None:
			try:
				shutil.copytree(source, codex_home, dirs_exist_ok=True)
			except OSError:
				pass
		config_paths = [codex_home / "config.toml", codex_home / ".codex" / "config.toml"]
		config_written = False
		for cfg in config_paths:
			if not cfg.exists():
				continue
			try:
				text = cfg.read_text(encoding="utf-8")
			except OSError:
				text = ""
			text = upsert_toml_string_key(text, "model_reasoning_effort", reasoning)
			text = upsert_toml_string_key(text, "sandbox_mode", "read-only")
			try:
				cfg.parent.mkdir(parents=True, exist_ok=True)
				cfg.write_text(text, encoding="utf-8")
			except OSError:
				continue
			config_written = True
		if not config_written:
			cfg = codex_home / "config.toml"
			try:
				cfg.write_text(
					upsert_toml_string_key(
						upsert_toml_string_key("", "model_reasoning_effort", reasoning),
						"sandbox_mode",
						"read-only",
					),
					encoding="utf-8",
				)
			except OSError:
				pass
		return codex_home
	except Exception:
		shutil.rmtree(codex_home, ignore_errors=True)
		raise


def build_llm_prompt_text(template_text: str, batch: list[dict[str, object]]) -> str:
	items: list[dict[str, str]] = []
	for item in batch:
		fields: dict[str, str] = item["fields"]  # type: ignore[assignment]
		payload = {
			"issue_id": str(item["issue_id"]),
			"rejection_kind": str(item["rejection_kind"]),
			"FILE": fields.get("FILE", ""),
			"LINES": fields.get("LINES", ""),
			"EVIDENCE": fields.get("EVIDENCE", ""),
			"CURRENT_CODE": fields.get("CURRENT_CODE", ""),
		}
		if item["rejection_kind"] == "reviewer-wrong":
			payload["EVIDENCE_RUNTIME_PATH"] = fields.get("EVIDENCE_RUNTIME_PATH", "")
		elif item["rejection_kind"] == "spec-doesnt-support":
			payload["EVIDENCE_SPEC_QUOTE"] = fields.get("EVIDENCE_SPEC_QUOTE", "")
		items.append(payload)
	return template_text.rstrip() + "\n\nINPUT_PAYLOAD\n" + json.dumps({"items": items}, indent=2) + "\n"


def validate_llm_batch_output(payload: object, batch: list[dict[str, object]]) -> dict[str, tuple[str, str]]:
	if not isinstance(payload, dict):
		raise ValueError("invalid_top_level")
	rows = payload.get("results")
	if not isinstance(rows, list):
		raise ValueError("missing_results")
	expected = {str(item["issue_id"]): str(item["rejection_kind"]) for item in batch}
	validated: dict[str, tuple[str, str]] = {}
	for row in rows:
		if not isinstance(row, dict):
			raise ValueError("invalid_row")
		issue_id = str(row.get("issue_id", "")).strip()
		rejection_kind = str(row.get("rejection_kind", "")).strip()
		verdict = str(row.get("verdict", "")).strip()
		reason = squish(str(row.get("reason", "")), LLM_VERIFIER_REASON_MAX_CHARS)
		if issue_id not in expected:
			raise ValueError("unexpected_issue_id")
		if issue_id in validated:
			raise ValueError("duplicate_issue_id")
		if rejection_kind != expected[issue_id]:
			raise ValueError("rejection_kind_mismatch")
		if verdict not in VERDICT_VALUES:
			raise ValueError("invalid_verdict")
		if not reason:
			raise ValueError("missing_reason")
		validated[issue_id] = (verdict, reason)
	if set(validated) != set(expected):
		raise ValueError("missing_rows")
	return validated


def run_llm_verifier_batch(batch: list[dict[str, object]], template_text: str) -> dict[str, tuple[str, str]]:
	if not batch:
		return {}
	codex_bin = shutil.which("codex")
	if not codex_bin:
		return llm_batch_fail_results(
			batch,
			"missing_codex",
			"codex CLI is unavailable, so the LLM reject verifier could not run.",
		)
	timeout_bin = shutil.which("timeout")
	prompt_text = build_llm_prompt_text(template_text, batch)
	codex_home: Path | None = None
	try:
		codex_home = prepare_llm_codex_home(LLM_VERIFIER_REASONING)
		cmd = [
			codex_bin,
			"--ask-for-approval",
			"never",
			"-c",
			"model_verbosity=low",
			"-c",
			f"model_reasoning_effort={LLM_VERIFIER_REASONING}",
			"-c",
			"include_apply_patch_tool=true",
			"exec",
			"--model",
			LLM_VERIFIER_MODEL,
			"--sandbox",
			"read-only",
		]
		if timeout_bin:
			cmd = [
				timeout_bin,
				"--signal=TERM",
				"--kill-after=30s",
				"--",
				str(LLM_VERIFIER_TIMEOUT_SECS),
			] + cmd
		env = os.environ.copy()
		env["CODEX_HOME"] = str(codex_home)
		completed = subprocess.run(
			cmd,
			input=prompt_text,
			text=True,
			capture_output=True,
			env=env,
			timeout=None if timeout_bin else LLM_VERIFIER_TIMEOUT_SECS,
		)
	except subprocess.TimeoutExpired:
		return llm_batch_fail_results(
			batch,
			"timeout",
			f"LLM reject verifier timed out after {LLM_VERIFIER_TIMEOUT_SECS}s.",
		)
	finally:
		if codex_home is not None:
			shutil.rmtree(codex_home, ignore_errors=True)

	if completed.returncode in {124, 137}:
		return llm_batch_fail_results(
			batch,
			"timeout",
			f"LLM reject verifier timed out after {LLM_VERIFIER_TIMEOUT_SECS}s.",
		)
	if completed.returncode != 0:
		return llm_batch_fail_results(
			batch,
			f"exit_{completed.returncode}",
			f"LLM reject verifier exited with status {completed.returncode}.",
		)
	stdout_text = completed.stdout.strip()
	if not stdout_text:
		return llm_batch_fail_results(
			batch,
			"empty_output",
			"LLM reject verifier returned empty output.",
		)
	try:
		payload = json.loads(stdout_text)
		return validate_llm_batch_output(payload, batch)
	except (json.JSONDecodeError, ValueError) as exc:
		reason_code = "malformed_json" if isinstance(exc, json.JSONDecodeError) else str(exc)
		reason_text = (
			"LLM reject verifier returned malformed JSON."
			if isinstance(exc, json.JSONDecodeError)
			else "LLM reject verifier returned incomplete or malformed results."
		)
		return llm_batch_fail_results(batch, reason_code, reason_text)


def verify_llm_rejections(items: list[dict[str, object]]) -> dict[str, tuple[str, str]]:
	results: dict[str, tuple[str, str]] = {}
	if not items:
		return results
	if not LLM_VERIFIER_ENABLED:
		for item in items:
			results[str(item["issue_id"])] = (
				"inconclusive",
				"LLM reject verifier is disabled by CONSOLIDATOR_REJECT_VERIFIER_ENABLED=false.",
			)
		return results
	if not PROMPT_TEMPLATE.exists():
		for batch in chunked(items, LLM_VERIFIER_BATCH_MAX):
			results.update(llm_batch_fail_results(batch, "missing_prompt", llm_prompt_missing_reason(PROMPT_TEMPLATE)))
		return results
	try:
		template_text = PROMPT_TEMPLATE.read_text(encoding="utf-8")
	except OSError:
		for batch in chunked(items, LLM_VERIFIER_BATCH_MAX):
			results.update(llm_batch_fail_results(batch, "prompt_read_error", llm_prompt_missing_reason(PROMPT_TEMPLATE)))
		return results
	for batch in chunked(items, LLM_VERIFIER_BATCH_MAX):
		try:
			results.update(run_llm_verifier_batch(batch, template_text))
		except Exception:
			results.update(llm_unexpected_error_results(batch))
	return results


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
		if str(row.get("lines", "")) != str(block["fields"].get("LINES", "")):  # type: ignore[index]
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
	llm_pending: list[dict[str, object]] = []
	for block in blocks:
		fields: dict[str, str] = block["fields"]  # type: ignore[assignment]
		classification = fields.get("CLASSIFICATION", "")
		rejection_kind = fields.get("REJECTION_KIND", "")
		if classification != "non-actionable" or not rejection_kind:
			continue
		issue_id = parse_issue_id(str(block["start"]))
		block["issue_id"] = issue_id
		if rejection_kind in LLM_REJECTION_KINDS:
			llm_pending.append({
				"issue_id": issue_id,
				"rejection_kind": rejection_kind,
				"fields": fields,
			})

	try:
		llm_results = verify_llm_rejections(llm_pending)
	except Exception:
		llm_results = llm_unexpected_error_results(llm_pending)

	for block in blocks:
		fields = block["fields"]  # type: ignore[assignment]
		classification = fields.get("CLASSIFICATION", "")
		rejection_kind = fields.get("REJECTION_KIND", "")
		if classification != "non-actionable" or not rejection_kind:
			continue
		issue_id = str(block.get("issue_id") or parse_issue_id(str(block["start"])))
		if rejection_kind == "already-fixed":
			verdict, reason = verify_already_fixed(block, diff_available, patch_data)
		elif rejection_kind == "out-of-scope":
			verdict, reason = verify_out_of_scope(block, linked_issue_text)
		elif rejection_kind == "already-rejected-with-evidence":
			verdict, reason = verify_prior_round(block, REPO_ROOT, PR_NUMBER)
		elif rejection_kind in LLM_REJECTION_KINDS:
			verdict, reason = llm_results.get(
				issue_id,
				("inconclusive", "LLM reject verifier returned no result for this issue."),
			)
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
