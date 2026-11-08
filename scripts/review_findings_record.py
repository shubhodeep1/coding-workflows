#!/usr/bin/env python3
"""Review findings record (ChatGPT account reviewer trial plan Phase 3, Refs #8969).

Writes ``review_findings_record.json`` (schema version 1) for one review
round so operators can compare the ChatGPT reviewer slot with the rest of the
panel during the trial. Inputs are files the review job already wrote:

- the per-slot status files ``${PREVIOUS_REVIEWS_DIR}/status_<pass>_<safe>.txt``
  written by ``scripts/review_run_reviewers.sh``;
- the parsed consolidator output ``${REVIEW_ISSUES_FILE}`` (default
  ``${RUNTIME_DIR}/review_issues.txt``) written by
  ``scripts/review_parse_consolidator.sh``;
- the editor summary ``${EDITOR_SUMMARY_FILE}`` (default
  ``${RUNTIME_DIR}/editor_summary.txt``).

Every input is treated as untrusted (LLM / PR influenced). Only header fields
are kept and every value is checked against an allowlist pattern; evidence,
code and suggestion bodies are never copied, and editor reasons go through
``workflow_failure_heal.redact_secrets`` (dropped when the redactor cannot be
imported). The script never reads credentials, pool outputs or reviewer
``.log`` files, and logs only exception class names.

The helper is fail-open: any failure prints a ``REVIEW_FINDINGS_RECORD:
outcome=failed`` warning and exits 0. Only a malformed command line exits 2.

Usage: review_findings_record.py write --output <path>
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1
RECORD_NAME = "review_findings_record"
LOG_PREFIX = "REVIEW_FINDINGS_RECORD"
READ_LIMIT_BYTES = 4 * 1024 * 1024
STATUS_READ_LIMIT_BYTES = 4096
MAX_ISSUES = 500
MAX_FILE_PATH_LEN = 512
MAX_REASON_LEN = 300
# Bound the text handed to the redactor before the final cap so a token that
# straddles the 300-character cut is still recognised as a whole.
MAX_REASON_PRE_REDACT_LEN = 1000

TIER_NAMES = ("lite", "standard", "full", "disabled")
PR_NUMBER_RE = re.compile(r"^[0-9]{1,10}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_ID_RE = re.compile(r"^[0-9]{1,20}$")
RUN_ATTEMPT_RE = re.compile(r"^[0-9]{1,6}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# Tier reasons from review_run_reviewers.sh carry "<=" (e.g. code_<=200_loc).
TIER_REASON_RE = re.compile(r"^[A-Za-z0-9_.:<>=-]{1,64}$")
# Consolidator lens names are prose, e.g. "CORRECTNESS & LOGIC" or
# "DOCS COVERAGE (DIATAXIS)" (prompts/review-consolidator.txt).
LENS_RE = re.compile(r"^[A-Za-z0-9 &/()_.:-]{1,64}$")
# review_parse_consolidator.sh keeps only KNOWN_REVIEWERS tokens: bundle file
# basenames ("review_<safe_name>" or "<safe_name>") and "reviewer_N".
REVIEWER_TOKEN_PREFIXES = ("review_", "pass1_")
LINES_RE = re.compile(r"^[A-Za-z0-9_.:,-]{1,64}$")
ISSUE_ID_RE = re.compile(r"^[0-9]{1,6}$")
MERGED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SLUG_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9._:-]+$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
# Mirrors reviewer_chatgpt_slot_resolve in scripts/review_run_reviewers.sh.
CHATGPT_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
CHATGPT_REPLACES_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9._-]+$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

ISSUE_START_RE = re.compile(r"^=== ISSUE ([0-9]+) ===$")
ISSUE_END_RE = re.compile(r"^=== END ISSUE [0-9]+ ===$")
# review_parse_consolidator.sh appends these for reviewer anchors the
# consolidator did not cover; they are not recorded, only counted as a warning.
PASSTHROUGH_START_RE = re.compile(r"^=== ISSUE PASSTHROUGH [0-9]+ ===$")
HEADER_RE = re.compile(r"^([A-Z_]+):(?:[ \t](.*))?$")
ISSUE_HEADER_KEYS = (
	"FILE",
	"LINES",
	"LENS",
	"SEVERITY",
	"FLAGGED_BY",
	"CLASSIFICATION",
	"REJECTION_KIND",
	"MERGED_FROM",
	"PARSER_TAGS",
)
# review_parse_consolidator.sh writes FLAGGED_BY: unknown and the
# EVIDENCE_UNVERIFIED parser tag when no FLAGGED_BY entry named a known reviewer.
FLAGGED_BY_UNVERIFIED_VALUE = "unknown"
FLAGGED_BY_UNVERIFIED_TAG = "EVIDENCE_UNVERIFIED"

# Editor summary sections fixed by scripts/review_apply_fixes.sh.
EDITOR_SECTION_CHANGES = "Changes made:"
EDITOR_SECTION_STATUS = "Change status:"
EDITOR_SECTION_ALREADY = "Already satisfied (suggested but already present):"
EDITOR_SECTION_IGNORED = "Ignored suggestions (with short reason):"
EDITOR_SECTION_HEADING_RE = re.compile(r"^[A-Z][A-Za-z0-9 ()/_-]{0,80}:$")
EDITOR_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
OVERRIDE_RE = re.compile(r"CONSOLIDATOR_OVERRIDDEN:\s*([A-Za-z0-9_-]+)")


class OutputPathRefused(Exception):
	"""The output path is a symlink or its parent is not a directory."""


def _env(name: str) -> str:
	return (os.environ.get(name) or "").strip()


def _token(value: str | None, pattern: re.Pattern[str], max_len: int = 64) -> str | None:
	if value is None:
		return None
	value = value.strip()
	if not value or len(value) > max_len or not pattern.match(value):
		return None
	return value


def _read_bounded(path: str, limit: int = READ_LIMIT_BYTES) -> tuple[str | None, str | None]:
	"""Return (text, None) or (None, "missing"|"refused"|"oversized")."""
	if not path:
		return None, "missing"
	try:
		st = os.lstat(path)
	except FileNotFoundError:
		return None, "missing"
	except OSError:
		return None, "refused"
	if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
		return None, "refused"
	flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
	try:
		fd = os.open(path, flags)
	except OSError:
		return None, "refused"
	try:
		fst = os.fstat(fd)
		if not stat.S_ISREG(fst.st_mode):
			return None, "refused"
		if fst.st_size > limit:
			return None, "oversized"
		chunks = []
		remaining = limit + 1
		while remaining > 0:
			chunk = os.read(fd, min(65536, remaining))
			if not chunk:
				break
			chunks.append(chunk)
			remaining -= len(chunk)
		data = b"".join(chunks)
	finally:
		os.close(fd)
	if len(data) > limit:
		return None, "oversized"
	return data.decode("utf-8", errors="replace"), None


def _safe_name(slug: str) -> str:
	# Same as `tr '/.:' '___'` in scripts/review_run_reviewers.sh.
	return slug.translate(str.maketrans("/.:", "___"))


def resolve_panel() -> tuple[list[str], str | None]:
	"""Return the review panel and the ChatGPT slot slug (None when off).

	Mirrors normalize_reviewer_model_list and reviewer_chatgpt_slot_resolve in
	scripts/review_run_reviewers.sh, so no panel file has to be added there.
	"""
	panel: list[str] = []
	raw = os.environ.get("REVIEWER_MODELS") or ""
	for entry in raw.replace(",", "\n").splitlines():
		entry = entry.strip()
		if entry and SLUG_RE.match(entry) and len(entry) <= 128 and entry not in panel:
			panel.append(entry)
	if _env("REVIEWER_CHATGPT_SLOT_RESOLVED") != "true":
		return panel, None
	model = _env("REVIEWER_CHATGPT_MODEL_RESOLVED")
	replaces = _env("REVIEWER_CHATGPT_REPLACES_RESOLVED")
	if not CHATGPT_MODEL_RE.match(model) or not CHATGPT_REPLACES_RE.match(replaces):
		return panel, None
	if replaces not in panel:
		# Phase 2 logs swap=skipped reason=replaces_not_in_panel; the slot
		# never ran, so the record carries the disabled state.
		return panel, None
	chatgpt_slug = "chatgpt/" + model
	panel[panel.index(replaces)] = chatgpt_slug
	return panel, chatgpt_slug


def _map_status(raw: str) -> str:
	if raw == "success":
		return "ok"
	if raw == "skipped_pool":
		return "skipped_pool"
	return "failed"


def collect_slot_statuses(previous_reviews_dir: str, panel: list[str], warnings: set[str]) -> list[dict]:
	review_paths = sorted(glob.glob(os.path.join(glob.escape(previous_reviews_dir), "status_review_*.txt")))
	pass_prefix = "review"
	status_paths = review_paths
	if not review_paths:
		pass_prefix = "pass1"
		status_paths = sorted(glob.glob(os.path.join(glob.escape(previous_reviews_dir), "status_pass1_*.txt")))
	if not status_paths:
		warnings.add("no_status_files")
	file_prefix = "status_" + pass_prefix + "_"
	existing: dict[str, str] = {}
	for path in status_paths:
		base = os.path.basename(path)
		name = base[len(file_prefix):-len(".txt")]
		existing[name] = path

	def slot_entry(slug: str | None, safe_name: str) -> dict:
		path = existing.get(safe_name)
		if path is None:
			return {"slug": slug, "safe_name": safe_name, "pass": pass_prefix, "status": "not_selected", "raw_status": None}
		text, err = _read_bounded(path, STATUS_READ_LIMIT_BYTES)
		if err is not None:
			warnings.add("status_file_unreadable")
			return {"slug": slug, "safe_name": safe_name, "pass": pass_prefix, "status": "failed", "raw_status": "unknown"}
		raw = (text or "").strip()
		raw_token = _token(raw, TOKEN_RE) or "unknown"
		return {"slug": slug, "safe_name": safe_name, "pass": pass_prefix, "status": _map_status(raw), "raw_status": raw_token}

	slots = []
	panel_safe_names = set()
	for slug in panel:
		safe_name = _safe_name(slug)
		panel_safe_names.add(safe_name)
		slots.append(slot_entry(slug, safe_name))
	for safe_name in sorted(existing):
		if safe_name in panel_safe_names:
			continue
		if not SAFE_NAME_RE.match(safe_name):
			warnings.add("invalid_slot_name")
			continue
		slots.append(slot_entry(None, safe_name))
	return slots


def _clean_text(value: str, max_len: int) -> str | None:
	value = value.strip()
	if not value or len(value) > max_len or CONTROL_CHARS_RE.search(value) or not value.isprintable():
		return None
	return value


def _flagged_by_token(entry: str, safe_to_slug: dict[str, str]) -> str | None:
	"""Return the recorded form of one FLAGGED_BY entry, or None to drop it."""
	if len(entry) > 160:
		return None
	if SLUG_RE.match(entry):
		return entry
	if not SAFE_NAME_RE.match(entry):
		return None
	name = entry
	for prefix in REVIEWER_TOKEN_PREFIXES:
		if name.startswith(prefix) and len(name) > len(prefix):
			name = name[len(prefix):]
			break
	return safe_to_slug.get(name, entry)


def parse_review_issues(text: str, warnings: set[str], safe_to_slug: dict[str, str] | None = None) -> tuple[list[dict], int]:
	safe_to_slug = safe_to_slug or {}
	issues: list[dict] = []
	flagged_by_dropped = 0
	current: dict | None = None
	current_id = ""
	truncated = False

	def finish(block: dict, issue_id: str) -> None:
		nonlocal flagged_by_dropped
		flagged_by: list[str] = []
		unverified = False
		for entry in (block.get("FLAGGED_BY") or "").split(","):
			entry = entry.strip()
			if not entry:
				continue
			if entry == FLAGGED_BY_UNVERIFIED_VALUE:
				unverified = True
				if entry not in flagged_by:
					flagged_by.append(entry)
			else:
				token = _flagged_by_token(entry, safe_to_slug)
				if token is None:
					flagged_by_dropped += 1
				elif token not in flagged_by:
					flagged_by.append(token)
		tags = [tag.strip() for tag in (block.get("PARSER_TAGS") or "").split(",")]
		if FLAGGED_BY_UNVERIFIED_TAG in tags:
			unverified = True
		merged_from: list[str] = []
		for entry in re.split(r"[,\s]+", block.get("MERGED_FROM") or ""):
			if entry and MERGED_ID_RE.match(entry) and entry not in merged_from:
				merged_from.append(entry)
		file_value = None
		if block.get("FILE"):
			file_value = _clean_text(block["FILE"], MAX_FILE_PATH_LEN)
			if file_value is None:
				warnings.add("invalid_issue_file")
		issues.append({
			"id": issue_id,
			"file": file_value,
			"lines": _token(block.get("LINES"), LINES_RE),
			"lens": _token(block.get("LENS"), LENS_RE),
			"severity": _token(block.get("SEVERITY"), TOKEN_RE),
			"classification": _token(block.get("CLASSIFICATION"), TOKEN_RE),
			"rejection_kind": _token(block.get("REJECTION_KIND"), TOKEN_RE),
			"flagged_by": flagged_by,
			"flagged_by_unverified": unverified,
			"merged_from": merged_from,
		})

	for line in text.splitlines():
		if PASSTHROUGH_START_RE.match(line):
			warnings.add("passthrough_blocks_skipped")
		start = ISSUE_START_RE.match(line)
		if start:
			if current is not None:
				finish(current, current_id)
				current = None
			if len(issues) >= MAX_ISSUES:
				truncated = True
				break
			if ISSUE_ID_RE.match(start.group(1)):
				current = {}
				current_id = start.group(1)
			else:
				warnings.add("invalid_issue_id")
			continue
		if current is None:
			continue
		if ISSUE_END_RE.match(line):
			finish(current, current_id)
			current = None
			continue
		# Indented lines are EVIDENCE_* / CURRENT_CODE / SUGGESTED_APPROACH /
		# NOTES / UNRECOGNISED bodies: untrusted code and prose, never copied.
		if not line or line[0] in " \t":
			continue
		header = HEADER_RE.match(line)
		if not header:
			continue
		key = header.group(1)
		if key in ISSUE_HEADER_KEYS and key not in current:
			current[key] = header.group(2) or ""
	if current is not None and not truncated:
		if len(issues) < MAX_ISSUES:
			finish(current, current_id)
		else:
			truncated = True
	if truncated:
		warnings.add("issues_truncated")
	return issues, flagged_by_dropped


def _load_redactor():
	script_dir = os.path.dirname(os.path.abspath(__file__))
	if script_dir not in sys.path:
		sys.path.insert(0, script_dir)
	try:
		import workflow_failure_heal  # noqa: PLC0415

		return workflow_failure_heal.redact_secrets
	except Exception:  # noqa: BLE001 - fail closed: reasons are dropped
		return None


def _issue_id_pattern(issue_id: str) -> re.Pattern[str]:
	# Best-effort match of an issue id inside a free-text editor bullet: the id
	# as written ("001"), or the number after an issue/ISSUE prefix or `#`
	# with any leading zeros ("issue 1", "ISSUE-001", "#1"). A bare number
	# without padding is not matched so a "line 2" mention is not issue 2.
	number = str(int(issue_id))
	return re.compile(
		r"(?<![A-Za-z0-9_])(?:" + re.escape(issue_id) + r"|(?:issue[\s_:#-]*|#)0*" + number + r")(?![A-Za-z0-9_])",
		re.IGNORECASE,
	)


def _same_issue_id(token: str, issue_id: str) -> bool:
	if token == issue_id:
		return True
	return token.isdigit() and len(token) <= 6 and int(token) == int(issue_id)


def parse_editor_summary(text: str, issue_ids: list[str], warnings: set[str]) -> dict:
	sections: dict[str, list[str]] = {
		EDITOR_SECTION_CHANGES: [],
		EDITOR_SECTION_STATUS: [],
		EDITOR_SECTION_ALREADY: [],
		EDITOR_SECTION_IGNORED: [],
	}
	current: str | None = None
	override_count = 0
	for line in text.splitlines():
		stripped = line.strip()
		if line[:1] not in (" ", "\t") and EDITOR_SECTION_HEADING_RE.match(stripped):
			current = stripped if stripped in sections else None
			continue
		if current is None:
			continue
		bullet = EDITOR_BULLET_RE.match(line)
		if bullet:
			sections[current].append(bullet.group(1).strip())
			# Count only override bullets in the Ignored section (the editor
			# contract), including "no-issue-id" overrides.
			if current == EDITOR_SECTION_IGNORED and OVERRIDE_RE.search(bullet.group(1)):
				override_count += 1

	change_status = "unknown"
	for value in sections[EDITOR_SECTION_STATUS]:
		value = value.strip().strip("`").strip().lower()
		if value in ("edited", "not-edited"):
			change_status = value
		break

	redactor = None
	redactor_loaded = False
	per_issue: dict[str, dict] = {}
	for issue_id in issue_ids:
		pattern = _issue_id_pattern(issue_id)
		disposition = "unknown"
		override = False
		reason_text: str | None = None
		for bullet in sections[EDITOR_SECTION_IGNORED]:
			override_match = OVERRIDE_RE.search(bullet)
			if override_match and _same_issue_id(override_match.group(1), issue_id):
				disposition = "ignored"
				override = True
				reason_text = bullet
				break
		if disposition == "unknown":
			for section, value in (
				(EDITOR_SECTION_CHANGES, "applied"),
				(EDITOR_SECTION_ALREADY, "already_satisfied"),
				(EDITOR_SECTION_IGNORED, "ignored"),
			):
				hit = next((b for b in sections[section] if pattern.search(b)), None)
				if hit is not None:
					disposition = value
					if value == "ignored":
						reason_text = hit
					break
		reason = None
		if disposition == "ignored" and reason_text:
			cleaned = CONTROL_CHARS_RE.sub("", reason_text)[:MAX_REASON_PRE_REDACT_LEN]
			if not redactor_loaded:
				redactor = _load_redactor()
				redactor_loaded = True
			if redactor is None:
				warnings.add("redactor_unavailable")
			else:
				reason = (redactor(cleaned)[:MAX_REASON_LEN].strip()) or None
		per_issue[issue_id] = {"disposition": disposition, "override": override, "reason": reason}
	return {"ran": True, "change_status": change_status, "override_count": override_count, "per_issue": per_issue}


def _identity_field(record: dict, warnings: set[str], key: str, env_name: str, pattern: re.Pattern[str], max_len: int = 64) -> None:
	raw = _env(env_name)
	value = _token(raw, pattern, max_len)
	if raw and value is None:
		warnings.add("invalid_" + key)
	record[key] = value


def build_record(previous_reviews_dir: str) -> dict:
	warnings: set[str] = set()
	runtime_dir = _env("RUNTIME_DIR")
	record: dict = {
		"schema_version": SCHEMA_VERSION,
		"record": RECORD_NAME,
		"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
	}
	_identity_field(record, warnings, "repository", "GITHUB_REPOSITORY", REPOSITORY_RE, 200)
	_identity_field(record, warnings, "pr_number", "PR_NUMBER", PR_NUMBER_RE)
	if record["pr_number"] is not None:
		record["pr_number"] = int(record["pr_number"])
	_identity_field(record, warnings, "run_id", "GITHUB_RUN_ID", RUN_ID_RE)
	_identity_field(record, warnings, "run_attempt", "GITHUB_RUN_ATTEMPT", RUN_ATTEMPT_RE)
	_identity_field(record, warnings, "head_sha", "INITIAL_HEAD_SHA", SHA_RE)
	_identity_field(record, warnings, "round", "AUTOFIX_ITERATION_METRIC", TOKEN_RE)

	tier_raw = _env("REVIEW_TIER")
	if not tier_raw and runtime_dir:
		tier_text, _ = _read_bounded(os.path.join(runtime_dir, "review_tier.txt"), STATUS_READ_LIMIT_BYTES)
		tier_raw = (tier_text or "").strip()
	tier_name = tier_raw if tier_raw in TIER_NAMES else None
	if not tier_raw:
		warnings.add("tier_unknown")
	elif tier_name is None:
		warnings.add("invalid_tier")
	tier_reason_raw = _env("REVIEW_TIER_REASON")
	tier_reason = _token(tier_reason_raw, TIER_REASON_RE)
	if tier_reason_raw and tier_reason is None:
		warnings.add("invalid_tier_reason")
	record["tier"] = {"name": tier_name, "reason": tier_reason}

	panel, chatgpt_slug = resolve_panel()
	slots = collect_slot_statuses(previous_reviews_dir, panel, warnings)
	record["slots"] = slots
	if chatgpt_slug is None:
		record["chatgpt_slot"] = {"enabled": False, "slug": None, "status": None}
	else:
		chatgpt_status = next((slot["status"] for slot in slots if slot["slug"] == chatgpt_slug), "not_selected")
		record["chatgpt_slot"] = {"enabled": True, "slug": chatgpt_slug, "status": chatgpt_status}

	# The lite tier disables the consolidator (REVIEW_CONSOLIDATOR_ENABLED=0),
	# so a missing issues file is expected there and is not a warning.
	consolidator_expected = tier_name != "lite" and _env("REVIEW_CONSOLIDATOR_ENABLED") != "0"
	issues_path = _env("REVIEW_ISSUES_FILE") or (os.path.join(runtime_dir, "review_issues.txt") if runtime_dir else "")
	issues_text, issues_err = _read_bounded(issues_path)
	issues: list[dict] = []
	flagged_by_dropped = 0
	safe_to_slug = {slot["safe_name"]: slot["slug"] for slot in slots if slot["slug"]}
	consolidator_ran = False
	if issues_text is not None and issues_text.strip():
		consolidator_ran = True
		issues, flagged_by_dropped = parse_review_issues(issues_text, warnings, safe_to_slug)
	elif issues_text is not None:
		# review_parse_consolidator.sh truncates the file on a parse failure or
		# when no issue markers were found; do not record that as a clean round.
		if consolidator_expected:
			warnings.add("issues_file_empty")
	elif issues_err == "missing":
		if consolidator_expected:
			warnings.add("issues_file_missing")
	else:
		warnings.add("issues_file_" + issues_err)
	stats_path = _env("PARSER_STATS_FILE") or (os.path.join(runtime_dir, "parser_stats.txt") if runtime_dir else "")
	if stats_path:
		stats_text, _ = _read_bounded(stats_path, STATUS_READ_LIMIT_BYTES)
		if stats_text and re.search(r"(?m)^parse_failed=1$", stats_text):
			warnings.add("consolidator_parse_failed")
	record["consolidator"] = {"ran": consolidator_ran, "issue_count": len(issues), "flagged_by_dropped": flagged_by_dropped}

	editor_path = _env("EDITOR_SUMMARY_FILE") or (os.path.join(runtime_dir, "editor_summary.txt") if runtime_dir else "")
	editor_text, editor_err = _read_bounded(editor_path)
	issue_ids = [issue["id"] for issue in issues]
	if editor_text is not None and editor_text.strip():
		editor = parse_editor_summary(editor_text, issue_ids, warnings)
	else:
		if editor_err in (None, "missing"):
			warnings.add("editor_summary_missing")
		else:
			warnings.add("editor_summary_" + editor_err)
		editor = {"ran": False, "change_status": "unknown", "override_count": 0, "per_issue": {}}
	for issue in issues:
		issue["editor"] = editor["per_issue"].get(issue["id"], {"disposition": "unknown", "override": False, "reason": None})
	record["issues"] = issues
	record["editor"] = {"ran": editor["ran"], "change_status": editor["change_status"], "override_count": editor["override_count"]}
	record["warnings"] = sorted(warnings)
	return record


def write_atomic(path: str, data: dict) -> None:
	parent = os.path.dirname(os.path.abspath(path))
	if os.path.islink(path) or not os.path.isdir(parent) or os.path.islink(parent):
		raise OutputPathRefused()
	payload = json.dumps(data, sort_keys=True, indent=2) + "\n"
	fd, tmp_path = tempfile.mkstemp(prefix=".review_findings_record.", suffix=".tmp", dir=parent)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			handle.write(payload)
			handle.flush()
			os.fsync(handle.fileno())
		os.chmod(tmp_path, 0o644)
		os.replace(tmp_path, path)
		tmp_path = ""
	finally:
		if tmp_path:
			try:
				os.unlink(tmp_path)
			except OSError:
				pass


def run_write(output: str) -> None:
	previous_reviews_dir = _env("PREVIOUS_REVIEWS_DIR")
	if not previous_reviews_dir or not os.path.isdir(previous_reviews_dir):
		print(f"{LOG_PREFIX}: outcome=skipped reason=no_previous_reviews_dir", flush=True)
		return
	record = build_record(previous_reviews_dir)
	write_atomic(output, record)
	warnings = record["warnings"]
	outcome = "partial" if warnings else "written"
	warnings_text = ",".join(warnings) if warnings else "none"
	chatgpt_state = "enabled" if record["chatgpt_slot"]["enabled"] else "disabled"
	print(
		f"{LOG_PREFIX}: outcome={outcome} issues={len(record['issues'])} slots={len(record['slots'])} "
		f"chatgpt_slot={chatgpt_state} warnings={warnings_text}",
		flush=True,
	)
	if outcome == "partial":
		print(f"::warning::{LOG_PREFIX}: outcome=partial warnings={warnings_text}", flush=True)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Write the review findings record (schema version 1).")
	sub = parser.add_subparsers(dest="command", required=True)
	write_parser = sub.add_parser("write", help="build and write review_findings_record.json")
	write_parser.add_argument("--output", required=True)
	args = parser.parse_args(argv)
	try:
		run_write(args.output)
	except Exception as exc:  # noqa: BLE001 - fail open; never log the message
		print(f"::warning::{LOG_PREFIX}: outcome=failed reason={type(exc).__name__}", flush=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
