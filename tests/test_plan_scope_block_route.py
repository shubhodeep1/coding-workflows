#!/usr/bin/env python3
"""scripts/plan_scope_block_route.sh routes a missing-scope planning block (issue #9534).

#9503 stopped at planning because the planner needed two files outside its
judge-written files_touched list, and the issue waited about 6 hours for a
person. The planner now lists such files on `SCOPE_MISSING:` lines; the route
script validates them and writes the trusted guard-rejection marker that
scripts/unblock_ledger.py turns into an `override_guard` offer. These tests run
the real script against fixture files and a fake GITHUB_OUTPUT.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "plan_scope_block_route.sh"
LEDGER_PATH = REPO_ROOT / "scripts" / "unblock_ledger.py"
_SPEC = importlib.util.spec_from_file_location("unblock_ledger_for_plan_scope_route", LEDGER_PATH)
ledger = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = ledger
_SPEC.loader.exec_module(ledger)

ISSUE = 41
RUN_ID = "4365"
BODY = "Fix the report.\n\nfiles_touched:\n  - .github/workflows/workflow-log-analysis.yml\n  - docs/**\n"


def _run(tmp_path: Path, output: str, *, body: str = BODY, meta: dict | None = None, **env_extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
	parse_file = tmp_path / "codex_output_parse.txt"
	parse_file.write_text(output, encoding="utf-8")
	meta_file = tmp_path / "issue_meta.json"
	meta_file.write_text(json.dumps(meta if meta is not None else {"number": ISSUE, "body": body}), encoding="utf-8")
	out_file = tmp_path / "github_output"
	out_file.write_text("", encoding="utf-8")
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": str(tmp_path),
		"CODEX_OUTPUT_PARSE_FILE": str(parse_file),
		"ISSUE_NUMBER": str(ISSUE),
		"ISSUE_META_FILE": str(meta_file),
		"GITHUB_RUN_ID": RUN_ID,
		"GITHUB_OUTPUT": str(out_file),
		"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	env.update(env_extra)
	result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False, timeout=60)
	outputs = {}
	for line in out_file.read_text(encoding="utf-8").splitlines():
		key, _, value = line.partition("=")
		outputs[key] = value
	return result, outputs


def _blocked(*paths: str) -> str:
	return "BLOCKED: needed files are outside files_touched\n" + "".join(f"SCOPE_MISSING: {path}\n" for path in paths)


def _assert_not_routed(result: subprocess.CompletedProcess[str], outputs: dict, reason: str) -> None:
	assert result.returncode == 0, result.stderr
	assert outputs == {"scope_routed": "false", "scope_marker": "", "scope_paths_count": "0"}, outputs
	assert f"PLAN_SCOPE_BLOCK outcome=not_routed issue={ISSUE} paths=0 reason={reason}" in result.stdout, result.stdout


def test_valid_scope_lines_produce_a_marker_the_ledger_parses(tmp_path: Path) -> None:
	paths = ["scripts/workflow_log_analysis_deliver_report.sh", "tests/test_workflow_log_analysis_report_delivery.py"]
	result, outputs = _run(tmp_path, _blocked(*paths, paths[0]))
	assert result.returncode == 0, result.stderr
	assert outputs["scope_routed"] == "true"
	assert outputs["scope_paths_count"] == "2"
	assert f"PLAN_SCOPE_BLOCK outcome=routed issue={ISSUE} paths=2" in result.stdout
	marker = outputs["scope_marker"]
	match = ledger.REJECTION_RE.fullmatch(marker)
	assert match, marker
	assert match.group("item") == str(ISSUE) and match.group("guard") == "scope"
	assert match.group("reason") == "out-of-scope" and match.group("run") == RUN_ID
	assert match.group("count") == "2" and match.group("truncated") == "false"
	assert json.loads(base64.b64decode(match.group("paths"))) == paths
	comments = [{"user": {"login": "bot"}, "body": "🚨 **files_touched scope guard rejected this planning run.**\n\nPlanning blocked.\n\n" + marker}]
	rejection = ledger.latest_rejection(comments, "bot", ISSUE, "scope-blocked")
	assert rejection["status"] == "ok" and rejection["paths"] == paths


def test_marker_matches_the_implement_guard_builder(tmp_path: Path) -> None:
	# Same encoding as guard_rejection_marker() in implement_handle_guard_block.sh.
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"))
	expected = subprocess.run(
		["bash", "-c", "printf '%s' tests/test_a.py | jq -R -s -c 'split(\"\\n\") | map(select(length > 0))' | jq -c '.[:100]' | base64 | tr -d '\\n'"],
		capture_output=True, text=True, check=True,
	).stdout
	assert outputs["scope_marker"].endswith(f" paths={expected} -->"), (outputs, expected)


def test_no_scope_lines(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, "BLOCKED: a private credential is required\n")
	_assert_not_routed(result, outputs, "no_scope_lines")


def test_scope_lines_inside_a_fence_are_ignored(tmp_path: Path) -> None:
	output = "BLOCKED: example only\n```\nSCOPE_MISSING: tests/test_a.py\n```\n"
	result, outputs = _run(tmp_path, output)
	_assert_not_routed(result, outputs, "no_scope_lines")


@pytest.mark.parametrize("path", ["tests/*.py", "docs/../scripts/x.sh", "/etc/passwd", "tests/", "tests/caf\u00e9.py", "a\\b.py", "tests/{a,b}.py",
	"`tests/a.py", "tests/'a'.py", '"tests/a.py"', "tests/$x.py", "``tests/a.py``"])
def test_invalid_paths_are_not_routed(tmp_path: Path, path: str) -> None:
	result, outputs = _run(tmp_path, _blocked("tests/test_ok.py", path))
	_assert_not_routed(result, outputs, "invalid_path")


def test_one_pair_of_backticks_is_stripped(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, _blocked("`tests/test_a.py`"))
	assert result.returncode == 0, result.stderr
	assert outputs["scope_routed"] == "true"
	match = ledger.REJECTION_RE.fullmatch(outputs["scope_marker"])
	assert match and json.loads(base64.b64decode(match.group("paths"))) == ["tests/test_a.py"]


def test_a_scope_line_with_two_tokens_is_invalid(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, "BLOCKED: x\nSCOPE_MISSING: tests/a.py tests/b.py\n")
	_assert_not_routed(result, outputs, "invalid_path")


def test_more_than_twenty_paths(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, _blocked(*[f"tests/test_{index}.py" for index in range(21)]))
	_assert_not_routed(result, outputs, "too_many_paths")
	result, outputs = _run(tmp_path, _blocked(*[f"tests/test_{index}.py" for index in range(20)]))
	assert outputs["scope_routed"] == "true" and outputs["scope_paths_count"] == "20"


@pytest.mark.parametrize("path", [".github/workflows/workflow-log-analysis.yml", "docs/guide/a.md"])
def test_a_path_already_in_scope_is_not_routed(tmp_path: Path, path: str) -> None:
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py", path))
	_assert_not_routed(result, outputs, "already_in_scope")


def test_no_files_touched_block(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), body="No scope list here.")
	_assert_not_routed(result, outputs, "no_scope_block")


@pytest.mark.parametrize("value", ["false", "FALSE", " false "])
def test_disabled(tmp_path: Path, value: str) -> None:
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), PLAN_SCOPE_BLOCK_ROUTING_ENABLED=value)
	_assert_not_routed(result, outputs, "disabled")


def test_waiting_answer_is_not_routed(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), WAITING_PARSED="true")
	_assert_not_routed(result, outputs, "waiting")


def test_a_pull_request_is_not_an_issue(tmp_path: Path) -> None:
	meta = {"number": ISSUE, "body": BODY, "pull_request": {"url": "x"}}
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), meta=meta)
	_assert_not_routed(result, outputs, "not_an_issue")
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), meta={"number": ISSUE + 1, "body": BODY})
	_assert_not_routed(result, outputs, "not_an_issue")


def test_unreadable_metadata_fails_open(tmp_path: Path) -> None:
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), meta={"number": ISSUE, "body": None})
	_assert_not_routed(result, outputs, "no_scope_block")
	result, outputs = _run(tmp_path, _blocked("tests/test_a.py"), SUPPORT_SCRIPTS_DIR=str(tmp_path / "missing"))
	# Falls back to the guard module next to the script itself.
	assert outputs["scope_routed"] == "true"


def test_script_makes_no_api_calls() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert "gh " not in text and "curl" not in text and "api.github.com" not in text
