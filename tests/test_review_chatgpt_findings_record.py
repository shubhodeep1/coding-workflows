#!/usr/bin/env python3
"""Review findings record (ChatGPT reviewer trial plan Phase 3, Refs #8969).

scripts/review_findings_record.py writes review_findings_record.json (schema
version 1) after the editor summary in review_autofix.yml. These tests pin the
payload, the disabled-slot shape, the fail-open behaviour, the hostile-input
handling and the workflow / CI / docs wiring.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "review_findings_record.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGENTS_FRAGMENT = REPO_ROOT / "agents.d" / "chatgpt-review-findings-record.md"
CHANGELOG_FRAGMENT = REPO_ROOT / "changelog.d" / "chatgpt-review-findings-record.md"

PANEL = "\n".join([
	"anthropic/claude-opus-5",
	"openai/gpt-6-luna",
	"google/gemini-4-pro",
	"x-ai/grok-5",
	"deepseek/deepseek-v4",
])
SECRET_TAIL = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SENTINEL = "sentinel-secret-value-9f8e7d6c5b4a"
TOP_LEVEL_KEYS = {
	"schema_version",
	"record",
	"generated_at",
	"repository",
	"pr_number",
	"run_id",
	"run_attempt",
	"head_sha",
	"round",
	"tier",
	"chatgpt_slot",
	"slots",
	"consolidator",
	"issues",
	"editor",
	"warnings",
}
ISSUES_TEXT = """=== ISSUE 001 ===
FILE: scripts/foo.sh
LINES: 10-20
LENS: correctness
SEVERITY: high
FLAGGED_BY: chatgpt/gpt-6-sol, anthropic/claude-opus-5
CLASSIFICATION: actionable
MERGED_FROM: 003, 004
EVIDENCE_DIFF_HUNK:
  EVIDENCE_BODY_MARKER diff text
EVIDENCE:
  FILE: injected/path.sh
CURRENT_CODE:
  CURRENT_CODE_BODY_MARKER token=ghp_""" + SECRET_TAIL + """
SUGGESTED_APPROACH:
  SUGGESTED_APPROACH_BODY_MARKER
=== END ISSUE 001 ===

=== ISSUE 002 ===
FILE: docs/bar.md
LINES: 5
LENS: docs
SEVERITY: low
FLAGGED_BY: google/gemini-4-pro
CLASSIFICATION: actionable
EVIDENCE:
  something
CURRENT_CODE:
  code
SUGGESTED_APPROACH:
  fix
=== END ISSUE 002 ===
"""
EDITOR_TEXT = """Changes made:
- Fixed the null check in scripts/foo.sh (issue 001).
Change status:
- edited
Already satisfied (suggested but already present):
- none
Ignored suggestions (with short reason):
- ISSUE 002: not applicable, reviewer pasted ghp_""" + SECRET_TAIL + """ by mistake
Reviewer files processed:
- review_openai_gpt-6-luna.txt mentions issue 002
"""


def _write(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _fixture(tmp_path: Path, *, issues: bool = True, editor: bool = True) -> dict:
	prev = tmp_path / "prev"
	runtime = tmp_path / "runtime"
	prev.mkdir()
	runtime.mkdir()
	_write(prev / "status_review_anthropic_claude-opus-5.txt", "success\n")
	_write(prev / "status_review_chatgpt_gpt-6-sol.txt", "skipped_pool\n")
	_write(prev / "status_review_google_gemini-4-pro.txt", "failed\n")
	_write(prev / "status_review_x-ai_grok-5.txt", "timeout\n")
	# deepseek has no status file -> not_selected
	if issues:
		_write(runtime / "review_issues.txt", ISSUES_TEXT)
	if editor:
		_write(runtime / "editor_summary.txt", EDITOR_TEXT)
	return {
		"PREVIOUS_REVIEWS_DIR": str(prev),
		"RUNTIME_DIR": str(runtime),
		"REVIEW_TIER": "full",
		"REVIEW_TIER_REASON": "default",
		"REVIEWER_MODELS": PANEL,
		"REVIEWER_CHATGPT_SLOT_RESOLVED": "true",
		"REVIEWER_CHATGPT_MODEL_RESOLVED": "gpt-6-sol",
		"REVIEWER_CHATGPT_REPLACES_RESOLVED": "openai/gpt-6-luna",
		"GITHUB_REPOSITORY": "owner/repo",
		"PR_NUMBER": "42",
		"GITHUB_RUN_ID": "123456",
		"GITHUB_RUN_ATTEMPT": "2",
		"INITIAL_HEAD_SHA": "a" * 40,
		"AUTOFIX_ITERATION_METRIC": "3",
	}


def _run(env_extra: dict, output: Path, script: Path = SCRIPT) -> subprocess.CompletedProcess:
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"GH_TOKEN": SENTINEL,
		"OPENROUTER_API_KEY": SENTINEL,
	}
	env.update(env_extra)
	return subprocess.run(
		[sys.executable, str(script), "write", "--output", str(output)],
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def _slot(record: dict, slug: str) -> dict:
	return next(slot for slot in record["slots"] if slot["slug"] == slug)


def test_full_record_with_slot_on(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	result = _run(env, output)
	assert result.returncode == 0, result.stderr
	raw = output.read_text(encoding="utf-8")
	record = json.loads(raw)
	assert set(record) == TOP_LEVEL_KEYS
	assert record["schema_version"] == 1
	assert record["record"] == "review_findings_record"
	assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", record["generated_at"])
	assert record["repository"] == "owner/repo"
	assert record["pr_number"] == 42
	assert record["run_id"] == "123456"
	assert record["run_attempt"] == "2"
	assert record["head_sha"] == "a" * 40
	assert record["round"] == "3"
	assert record["tier"] == {"name": "full", "reason": "default"}
	assert record["chatgpt_slot"] == {"enabled": True, "slug": "chatgpt/gpt-6-sol", "status": "skipped_pool"}
	slugs = [slot["slug"] for slot in record["slots"]]
	assert slugs == ["anthropic/claude-opus-5", "chatgpt/gpt-6-sol", "google/gemini-4-pro", "x-ai/grok-5", "deepseek/deepseek-v4"]
	assert _slot(record, "anthropic/claude-opus-5")["status"] == "ok"
	assert _slot(record, "chatgpt/gpt-6-sol")["status"] == "skipped_pool"
	assert _slot(record, "google/gemini-4-pro")["status"] == "failed"
	assert _slot(record, "x-ai/grok-5") == {"slug": "x-ai/grok-5", "safe_name": "x-ai_grok-5", "pass": "review", "status": "failed", "raw_status": "timeout"}
	assert _slot(record, "deepseek/deepseek-v4") == {"slug": "deepseek/deepseek-v4", "safe_name": "deepseek_deepseek-v4", "pass": "review", "status": "not_selected", "raw_status": None}
	assert record["consolidator"] == {"ran": True, "issue_count": 2, "flagged_by_dropped": 0}
	first, second = record["issues"]
	assert first["id"] == "001"
	assert first["file"] == "scripts/foo.sh"
	assert first["lines"] == "10-20"
	assert first["severity"] == "high"
	assert first["classification"] == "actionable"
	assert first["flagged_by"] == ["chatgpt/gpt-6-sol", "anthropic/claude-opus-5"]
	assert first["flagged_by_unverified"] is False
	assert first["merged_from"] == ["003", "004"]
	assert first["editor"] == {"disposition": "applied", "override": False, "reason": None}
	assert second["merged_from"] == []
	assert second["editor"]["disposition"] == "ignored"
	assert second["editor"]["override"] is False
	assert "ghp_[redacted]" in second["editor"]["reason"]
	assert record["editor"] == {"ran": True, "change_status": "edited", "override_count": 0}
	assert record["warnings"] == []
	for forbidden in (SECRET_TAIL, "EVIDENCE_BODY_MARKER", "CURRENT_CODE_BODY_MARKER", "SUGGESTED_APPROACH_BODY_MARKER", "injected/path.sh", SENTINEL):
		assert forbidden not in raw
		assert forbidden not in result.stdout
	assert "REVIEW_FINDINGS_RECORD: outcome=written issues=2 slots=5 chatgpt_slot=enabled warnings=none" in result.stdout
	assert oct(output.stat().st_mode & 0o777) == oct(0o644)
	assert not list(output.parent.glob(".review_findings_record.*"))


def test_switch_off_carries_disabled_state(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	env["REVIEWER_CHATGPT_SLOT_RESOLVED"] = "false"
	prev = Path(env["PREVIOUS_REVIEWS_DIR"])
	(prev / "status_review_chatgpt_gpt-6-sol.txt").unlink()
	_write(prev / "status_review_openai_gpt-6-luna.txt", "success\n")
	_write(Path(env["RUNTIME_DIR"]) / "review_issues.txt", ISSUES_TEXT.replace("chatgpt/gpt-6-sol", "openai/gpt-6-luna"))
	output = prev / "review_findings_record.json"
	result = _run(env, output)
	assert result.returncode == 0
	raw = output.read_text(encoding="utf-8")
	record = json.loads(raw)
	assert record["chatgpt_slot"] == {"enabled": False, "slug": None, "status": None}
	assert "chatgpt/" not in raw
	assert _slot(record, "openai/gpt-6-luna")["status"] == "ok"
	assert "chatgpt_slot=disabled" in result.stdout


@pytest.mark.parametrize("override", [{}, {"REVIEWER_CHATGPT_SLOT_RESOLVED": ""}, {"REVIEWER_CHATGPT_MODEL_RESOLVED": "Bad Model"}, {"REVIEWER_CHATGPT_REPLACES_RESOLVED": "nobody/none"}])
def test_slot_off_variants_keep_disabled_shape(tmp_path: Path, override: dict) -> None:
	env = _fixture(tmp_path)
	if not override:
		for key in ("REVIEWER_CHATGPT_SLOT_RESOLVED", "REVIEWER_CHATGPT_MODEL_RESOLVED", "REVIEWER_CHATGPT_REPLACES_RESOLVED"):
			env.pop(key)
	env.update(override)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert record["chatgpt_slot"] == {"enabled": False, "slug": None, "status": None}
	assert any(slot["slug"] == "openai/gpt-6-luna" for slot in record["slots"])
	# The leftover chatgpt status file is still recorded, without a slug.
	assert {"slug": None, "safe_name": "chatgpt_gpt-6-sol", "pass": "review", "status": "skipped_pool", "raw_status": "skipped_pool"} in record["slots"]


def test_missing_inputs_give_partial_record(tmp_path: Path) -> None:
	env = _fixture(tmp_path, issues=False, editor=False)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	result = _run(env, output)
	assert result.returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert record["warnings"] == ["editor_summary_missing", "issues_file_missing"]
	assert record["consolidator"] == {"ran": False, "issue_count": 0, "flagged_by_dropped": 0}
	assert record["editor"] == {"ran": False, "change_status": "unknown", "override_count": 0}
	assert "REVIEW_FINDINGS_RECORD: outcome=partial" in result.stdout
	assert "::warning::REVIEW_FINDINGS_RECORD: outcome=partial" in result.stdout


def test_lite_tier_without_issues_file_is_not_a_warning(tmp_path: Path) -> None:
	env = _fixture(tmp_path, issues=False)
	env["REVIEW_TIER"] = "lite"
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	result = _run(env, output)
	assert result.returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert "issues_file_missing" not in record["warnings"]
	assert record["consolidator"]["ran"] is False
	assert record["tier"]["name"] == "lite"


def test_tier_falls_back_to_runtime_file(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	env.pop("REVIEW_TIER")
	_write(Path(env["RUNTIME_DIR"]) / "review_tier.txt", "standard\n")
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	assert json.loads(output.read_text(encoding="utf-8"))["tier"]["name"] == "standard"


def test_pass1_prefix_when_no_review_pass(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	prev = Path(env["PREVIOUS_REVIEWS_DIR"])
	for path in prev.glob("status_review_*.txt"):
		path.rename(prev / path.name.replace("status_review_", "status_pass1_"))
	output = prev / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert {slot["pass"] for slot in record["slots"]} == {"pass1"}
	assert _slot(record, "anthropic/claude-opus-5")["status"] == "ok"


def test_override_bullet_marks_ignored(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	editor = EDITOR_TEXT.replace(
		"- ISSUE 002: not applicable",
		"- CONSOLIDATOR_OVERRIDDEN: 002 — consolidator guidance wrong\n- other: not applicable",
	)
	_write(Path(env["RUNTIME_DIR"]) / "editor_summary.txt", editor)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert record["issues"][1]["editor"]["disposition"] == "ignored"
	assert record["issues"][1]["editor"]["override"] is True
	assert record["editor"]["override_count"] == 1


def test_symlink_output_fails_open(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	target = tmp_path / "target.txt"
	target.write_text("untouched", encoding="utf-8")
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	output.symlink_to(target)
	result = _run(env, output)
	assert result.returncode == 0
	assert "::warning::REVIEW_FINDINGS_RECORD: outcome=failed reason=OutputPathRefused" in result.stdout
	assert target.read_text(encoding="utf-8") == "untouched"
	assert output.is_symlink()
	assert not list(output.parent.glob(".review_findings_record.*"))


def test_parent_not_a_directory_fails_open(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	blocker = tmp_path / "blocker"
	blocker.write_text("x", encoding="utf-8")
	result = _run(env, blocker / "review_findings_record.json")
	assert result.returncode == 0
	assert "outcome=failed" in result.stdout


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_directory_fails_open(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	locked = tmp_path / "locked"
	locked.mkdir()
	locked.chmod(0o500)
	try:
		result = _run(env, locked / "review_findings_record.json")
	finally:
		locked.chmod(0o700)
	assert result.returncode == 0
	assert "outcome=failed reason=PermissionError" in result.stdout


def test_no_previous_reviews_dir_is_skipped(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	env.pop("PREVIOUS_REVIEWS_DIR")
	output = tmp_path / "review_findings_record.json"
	result = _run(env, output)
	assert result.returncode == 0
	assert "REVIEW_FINDINGS_RECORD: outcome=skipped reason=no_previous_reviews_dir" in result.stdout
	assert not output.exists()


def test_malformed_command_line_exits_2(tmp_path: Path) -> None:
	result = subprocess.run([sys.executable, str(SCRIPT), "write"], capture_output=True, text=True, check=False)
	assert result.returncode == 2


def test_symlinked_issues_file_is_refused(tmp_path: Path) -> None:
	env = _fixture(tmp_path, issues=False)
	real = tmp_path / "real_issues.txt"
	real.write_text(ISSUES_TEXT, encoding="utf-8")
	(Path(env["RUNTIME_DIR"]) / "review_issues.txt").symlink_to(real)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert "issues_file_refused" in record["warnings"]
	assert record["issues"] == []


def test_hostile_input_is_dropped(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	env["PR_NUMBER"] = "42; rm -rf /"
	env["INITIAL_HEAD_SHA"] = "not-a-sha"
	env["REVIEW_TIER"] = "ultra"
	env["GITHUB_REPOSITORY"] = "owner/repo$(id)"
	hostile = ISSUES_TEXT.replace(
		"FLAGGED_BY: google/gemini-4-pro",
		'FLAGGED_BY: google/gemini-4-pro, evil"};$(id), UPPER/Case',
	).replace("SEVERITY: low", "SEVERITY: low; drop table").replace("FILE: docs/bar.md", "FILE: docs/\x1bbar.md")
	_write(Path(env["RUNTIME_DIR"]) / "review_issues.txt", hostile)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	result = _run(env, output)
	assert result.returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert record["pr_number"] is None
	assert record["head_sha"] is None
	assert record["repository"] is None
	assert record["tier"]["name"] is None
	for code in ("invalid_pr_number", "invalid_head_sha", "invalid_tier", "invalid_repository", "invalid_issue_file"):
		assert code in record["warnings"]
	second = record["issues"][1]
	assert second["flagged_by"] == ["google/gemini-4-pro"]
	assert second["severity"] is None
	assert second["file"] is None
	assert record["consolidator"]["flagged_by_dropped"] == 2
	assert SENTINEL not in output.read_text(encoding="utf-8")
	assert SENTINEL not in result.stdout + result.stderr


def test_unverified_flagged_by_marker(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	text = ISSUES_TEXT.replace(
		"FLAGGED_BY: google/gemini-4-pro\n",
		"FLAGGED_BY: unknown\n",
	).replace("=== END ISSUE 002 ===", "PARSER_TAGS: EVIDENCE_UNVERIFIED\n=== END ISSUE 002 ===")
	_write(Path(env["RUNTIME_DIR"]) / "review_issues.txt", text)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	second = json.loads(output.read_text(encoding="utf-8"))["issues"][1]
	assert second["flagged_by"] == ["unknown"]
	assert second["flagged_by_unverified"] is True


def test_issue_cap(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	blocks = "".join(f"=== ISSUE {n:03d} ===\nFILE: a.sh\nLINES: 1\n=== END ISSUE {n:03d} ===\n" for n in range(1, 503))
	_write(Path(env["RUNTIME_DIR"]) / "review_issues.txt", blocks)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert len(record["issues"]) == 500
	assert "issues_truncated" in record["warnings"]


def test_redactor_unavailable_drops_reason(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	isolated = tmp_path / "isolated"
	isolated.mkdir()
	script_copy = isolated / SCRIPT.name
	shutil.copy2(SCRIPT, script_copy)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	result = _run(env, output, script=script_copy)
	assert result.returncode == 0
	raw = output.read_text(encoding="utf-8")
	record = json.loads(raw)
	assert record["issues"][1]["editor"] == {"disposition": "ignored", "override": False, "reason": None}
	assert "redactor_unavailable" in record["warnings"]
	assert SECRET_TAIL not in raw


def test_production_token_shapes_are_kept(tmp_path: Path) -> None:
	# review_parse_consolidator.sh emits bundle basenames and reviewer_N, never
	# vendor/model slugs; tier reasons carry "<=" and lenses are prose.
	env = _fixture(tmp_path)
	env["REVIEW_TIER"] = "standard"
	env["REVIEW_TIER_REASON"] = "code_<=200_loc"
	text = ISSUES_TEXT.replace(
		"FLAGGED_BY: chatgpt/gpt-6-sol, anthropic/claude-opus-5",
		"FLAGGED_BY: review_chatgpt_gpt-6-sol, anthropic_claude-opus-5, reviewer_3",
	).replace("LENS: correctness", "LENS: CORRECTNESS & LOGIC").replace("LENS: docs", "LENS: DOCS COVERAGE (DIATAXIS)")
	_write(Path(env["RUNTIME_DIR"]) / "review_issues.txt", text)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	first, second = record["issues"]
	assert first["flagged_by"] == ["chatgpt/gpt-6-sol", "anthropic/claude-opus-5", "reviewer_3"]
	assert first["lens"] == "CORRECTNESS & LOGIC"
	assert second["lens"] == "DOCS COVERAGE (DIATAXIS)"
	assert record["consolidator"]["flagged_by_dropped"] == 0
	assert record["tier"] == {"name": "standard", "reason": "code_<=200_loc"}
	assert record["warnings"] == []


def test_empty_issues_file_is_not_a_clean_round(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	runtime = Path(env["RUNTIME_DIR"])
	_write(runtime / "review_issues.txt", "")
	_write(runtime / "parser_stats.txt", "parsed_blocks=0\nparse_failed=1\n")
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert record["consolidator"] == {"ran": False, "issue_count": 0, "flagged_by_dropped": 0}
	assert "issues_file_empty" in record["warnings"]
	assert "consolidator_parse_failed" in record["warnings"]


def test_override_count_only_counts_ignored_override_bullets(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	editor = EDITOR_TEXT.replace(
		"- ISSUE 002: not applicable",
		"- CONSOLIDATOR_OVERRIDDEN: no-issue-id — advisory guidance wrong\n- ISSUE 002: not applicable",
	) + "Notes: CONSOLIDATOR_OVERRIDDEN: 001 mentioned outside the section\n"
	_write(Path(env["RUNTIME_DIR"]) / "editor_summary.txt", editor)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert record["editor"]["override_count"] == 1
	assert record["issues"][0]["editor"]["override"] is False


def test_passthrough_blocks_warn_and_unmatched_tokens_keep_prefix(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	text = ISSUES_TEXT.replace(
		"FLAGGED_BY: google/gemini-4-pro",
		"FLAGGED_BY: google/gemini-4-pro, review_unknown_model",
	) + "=== ISSUE PASSTHROUGH 001 ===\nFILE: other.sh\nLINES: 3\nCLASSIFICATION: unclassified\n=== END ISSUE PASSTHROUGH 001 ===\n"
	_write(Path(env["RUNTIME_DIR"]) / "review_issues.txt", text)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	record = json.loads(output.read_text(encoding="utf-8"))
	assert [issue["id"] for issue in record["issues"]] == ["001", "002"]
	assert "passthrough_blocks_skipped" in record["warnings"]
	assert record["issues"][1]["flagged_by"] == ["google/gemini-4-pro", "review_unknown_model"]


def test_parser_stats_override_without_runtime_dir(tmp_path: Path) -> None:
	env = _fixture(tmp_path)
	runtime = Path(env.pop("RUNTIME_DIR"))
	env["REVIEW_ISSUES_FILE"] = str(runtime / "review_issues.txt")
	env["EDITOR_SUMMARY_FILE"] = str(runtime / "editor_summary.txt")
	stats = tmp_path / "stats.txt"
	stats.write_text("parse_failed=1\n", encoding="utf-8")
	env["PARSER_STATS_FILE"] = str(stats)
	output = Path(env["PREVIOUS_REVIEWS_DIR"]) / "review_findings_record.json"
	assert _run(env, output).returncode == 0
	assert "consolidator_parse_failed" in json.loads(output.read_text(encoding="utf-8"))["warnings"]


def _step_block(text: str, name: str) -> str:
	start = text.index(f"\n      - name: {name}\n")
	end = text.find("\n      - name: ", start + 1)
	if end == -1:
		end = len(text)
	return text[start:end]


def test_workflow_steps_contract() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	upload_logs = _step_block(text, "Upload per-reviewer logs (always)")
	logs_if = re.search(r"\n        if: (.*)\n", upload_logs).group(1)
	write_step = _step_block(text, "Write review findings record")
	upload_step = _step_block(text, "Upload review findings record")
	for block in (write_step, upload_step):
		assert f"\n        if: {logs_if}\n" in block
		assert "\n        continue-on-error: true\n" in block
		assert "secrets." not in block
		assert "GH_TOKEN" not in block
	assert "review_findings_record.py" in write_step
	assert "${SUPPORT_SCRIPTS_DIR:-}/review_findings_record.py" in write_step
	assert ".codex-workflow-src/scripts/review_findings_record.py" in write_step
	assert "outcome=skipped reason=helper_unavailable" in write_step
	assert "REVIEWER_CHATGPT_SLOT_RESOLVED: ${{ steps.ai_engine.outputs.chatgpt_slot || '' }}" in write_step
	assert "AUTOFIX_ITERATION_METRIC: ${{ steps.retrigger_guard.outputs.autofix_iteration || '' }}" in write_step
	assert "uses: actions/upload-artifact@v6" in upload_step
	assert "name: reviewer-findings-${{ github.run_id }}-${{ github.run_attempt }}" in upload_step
	assert "path: ${{ env.PREVIOUS_REVIEWS_DIR }}/review_findings_record.json" in upload_step
	assert "retention-days: 30" in upload_step
	assert "if-no-files-found: ignore" in upload_step
	order = [
		text.index("\n      - name: Post editor summary comment\n"),
		text.index("\n      - name: Write review findings record\n"),
		text.index("\n      - name: Upload review findings record\n"),
		text.index("\n      - name: Clear Serena after editor\n"),
	]
	assert order == sorted(order)
	assert len(WORKFLOW.read_bytes()) < 480_000


def test_ci_and_docs_wiring() -> None:
	assert "tests/test_review_chatgpt_findings_record.py" in CI.read_text(encoding="utf-8")
	fragment = AGENTS_FRAGMENT.read_text(encoding="utf-8")
	assert '<!-- agents: section="Review pipeline consolidator + ledger contract" -->' in fragment
	assert '<!-- agents: section="Stable log prefixes (contractual)" -->' in fragment
	assert "LOG_PREFIX.name=REVIEW_FINDINGS_RECORD" in fragment
	assert "Refs #8969" in CHANGELOG_FRAGMENT.read_text(encoding="utf-8")
