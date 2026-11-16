#!/usr/bin/env python3
"""Tests for scripts/plan_scope_block_route.sh (issue #9534).

A planning block whose only cause is missing files_touched scope is routed to
the unblock judge with a trusted guard-rejection marker the ledger parses; a
human /answer releases ai:scope-blocked only for a planning rejection.
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
_SPEC = importlib.util.spec_from_file_location("unblock_ledger_for_route_tests", LEDGER_PATH)
ledger = importlib.util.module_from_spec(_SPEC)
sys.modules["unblock_ledger_for_route_tests"] = ledger
_SPEC.loader.exec_module(ledger)

# Same sanitizer as the parse_plan step of .github/workflows/plan.yml.
FENCE_SANITIZER = r"s{(^|\n)([ \t]*((?:```|~~~))[^\n]*\n.*?\n[ \t]*\3[ \t]*(?=\n|$))}{$1}gms;"
PLANNING_HEADER = "🚨 **files_touched scope guard rejected this planning run.**"
ISSUE_BODY = "Do the thing.\n\nfiles_touched:\n  - src/app.py\n  - tests/**\n"


def _sanitize(text: str, tmp_path: Path) -> Path:
	raw = tmp_path / "codex_output.txt"
	raw.write_text(text, encoding="utf-8")
	out = tmp_path / "codex_output_parse.txt"
	result = subprocess.run(["perl", "-0pe", FENCE_SANITIZER, str(raw)], capture_output=True, text=True, check=True)
	out.write_text(result.stdout, encoding="utf-8")
	return out


def _outputs(path: Path) -> dict[str, str]:
	values: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		key, _, value = line.partition("=")
		values[key] = value
	return values


def _route(tmp_path: Path, plan_output: str, *, body: str = ISSUE_BODY, meta_extra: dict | None = None,
		env_extra: dict | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
	parse_file = _sanitize(plan_output, tmp_path)
	meta = {"number": 77, "body": body, "labels": [{"name": "ai:planning"}]}
	meta.update(meta_extra or {})
	meta_file = tmp_path / "issue_meta.json"
	meta_file.write_text(json.dumps(meta), encoding="utf-8")
	output_file = tmp_path / "github_output"
	output_file.write_text("", encoding="utf-8")
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"CODEX_OUTPUT_PARSE_FILE": str(parse_file),
		"ISSUE_NUMBER": "77",
		"ISSUE_META_FILE": str(meta_file),
		"GITHUB_RUN_ID": "4242",
		"GITHUB_OUTPUT": str(output_file),
	}
	env.update(env_extra or {})
	result = subprocess.run(["bash", str(SCRIPT), "route"], env=env, capture_output=True, text=True, timeout=60, check=False)
	return result, _outputs(output_file)


def _blocked(*paths: str) -> str:
	return "BLOCKED: needed files are outside files_touched\n" + "".join(f"SCOPE_MISSING: {p}\n" for p in paths)


def test_valid_scope_lines_route_with_a_marker_the_ledger_parses(tmp_path: Path) -> None:
	result, out = _route(tmp_path, _blocked("scripts/deliver.sh", "`docs/guide.md`", "scripts/deliver.sh"))
	assert result.returncode == 0, result.stderr
	assert out["scope_routed"] == "true"
	match = ledger.REJECTION_RE.fullmatch(out["scope_marker"])
	assert match, out["scope_marker"]
	assert match.group("item") == "77" and match.group("guard") == "scope" and match.group("reason") == "out-of-scope"
	assert match.group("run") == "4242" and match.group("count") == "2" and match.group("truncated") == "false"
	assert json.loads(base64.b64decode(match.group("paths"))) == ["scripts/deliver.sh", "docs/guide.md"]
	assert "PLAN_SCOPE_BLOCK outcome=routed issue=77 paths=2" in result.stdout
	# The ledger reads it as an overridable rejection from the trusted login.
	comment = {"user": {"login": "bot"}, "body": f"Planning blocked: human input required.\n\n{PLANNING_HEADER}\n{out['scope_marker']}"}
	rejection = ledger.latest_rejection([comment], "bot", 77, "scope-blocked")
	assert rejection["status"] == "ok" and rejection["paths"] == ["scripts/deliver.sh", "docs/guide.md"]


@pytest.mark.parametrize(
	("plan_output", "reason"),
	[
		("BLOCKED: needs a credential\n", "no_scope_lines"),
		(_blocked("src/*.py"), "invalid_path"),
		(_blocked("../etc/passwd"), "invalid_path"),
		(_blocked("/etc/passwd"), "invalid_path"),
		(_blocked("docs/"), "invalid_path"),
		("BLOCKED: x\nSCOPE_MISSING: two words.md\n", "invalid_path"),
		(_blocked(*[f"docs/f{i}.md" for i in range(21)]), "too_many_paths"),
		(_blocked("src/app.py"), "already_in_scope"),
		(_blocked("tests/test_new.py"), "already_in_scope"),
		(_blocked("docs/a.md", "src/app.py"), "already_in_scope"),
	],
)
def test_refused_scope_lines_are_not_routed(tmp_path: Path, plan_output: str, reason: str) -> None:
	result, out = _route(tmp_path, plan_output)
	assert result.returncode == 0, result.stderr
	assert out == {"scope_routed": "false", "scope_marker": ""}
	assert f"outcome=not_routed issue=77" in result.stdout
	assert result.stdout.rstrip().endswith(f"reason={reason}"), result.stdout


def test_twenty_paths_still_route(tmp_path: Path) -> None:
	_, out = _route(tmp_path, _blocked(*[f"docs/f{i}.md" for i in range(20)]))
	assert out["scope_routed"] == "true"


def test_no_files_touched_block_pull_request_and_disabled_are_not_routed(tmp_path: Path) -> None:
	(tmp_path / "a").mkdir()
	result, out = _route(tmp_path / "a", _blocked("docs/a.md"), body="no scope here")
	assert out["scope_routed"] == "false" and "reason=no_files_touched" in result.stdout
	(tmp_path / "b").mkdir()
	result, out = _route(tmp_path / "b", _blocked("docs/a.md"), meta_extra={"pull_request": {"url": "x"}})
	assert out["scope_routed"] == "false" and "reason=not_an_issue" in result.stdout
	(tmp_path / "c").mkdir()
	result, out = _route(tmp_path / "c", _blocked("docs/a.md"), env_extra={"PLAN_SCOPE_BLOCK_ROUTING_ENABLED": "FALSE"})
	assert out["scope_routed"] == "false" and "reason=disabled" in result.stdout
	(tmp_path / "d").mkdir()
	result, out = _route(tmp_path / "d", _blocked("docs/a.md"), env_extra={"ISSUE_NUMBER": "0"})
	assert out["scope_routed"] == "false" and "reason=invalid_input" in result.stdout


def test_scope_lines_inside_a_fence_are_ignored(tmp_path: Path) -> None:
	result, out = _route(tmp_path, "BLOCKED: x\n```\nSCOPE_MISSING: docs/a.md\n```\n")
	assert out["scope_routed"] == "false" and "reason=no_scope_lines" in result.stdout


def _release(tmp_path: Path, comments: object, labels: list[str], *, raw_comments: str | None = None) -> subprocess.CompletedProcess[str]:
	meta_file = tmp_path / "issue_meta.json"
	meta_file.write_text(json.dumps({"number": 77, "labels": [{"name": n} for n in labels]}), encoding="utf-8")
	comments_file = tmp_path / "issue_comments.json"
	comments_file.write_text(raw_comments if raw_comments is not None else json.dumps(comments), encoding="utf-8")
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"ISSUE_NUMBER": "77",
		"ISSUE_META_FILE": str(meta_file),
		"ISSUE_COMMENTS_FILE": str(comments_file),
		"PLAN_SCOPE_BLOCK_ROUTING_ENABLED": "false",
	}
	return subprocess.run(["bash", str(SCRIPT), "answer-release"], env=env, capture_output=True, text=True, timeout=60, check=False)


def _marker(guard: str = "scope", item: int = 77) -> str:
	encoded = base64.b64encode(json.dumps(["docs/a.md"], separators=(",", ":")).encode()).decode()
	return f"<!-- ai:guard-rejection:v1 item={item} guard={guard} reason=out-of-scope run=9 count=1 truncated=false paths={encoded} -->"


def _planning_comment(association: str = "OWNER") -> dict:
	return {"author_association": association, "user": {"login": "owner", "type": "User"},
		"body": f"Planning blocked: human input required.\n\nRun: x\n\n{PLANNING_HEADER}\n{_marker()}"}


def _implement_comment() -> dict:
	return {"author_association": "NONE", "user": {"login": "bot[bot]", "type": "Bot"},
		"body": f"🚨 **files_touched scope guard rejected this implementation run.**\n\nPaths.\n{_marker()}"}


def test_answer_release_true_for_newest_trusted_planning_marker(tmp_path: Path) -> None:
	result = _release(tmp_path, [_implement_comment(), _planning_comment()], ["ai:blocked", "ai:scope-blocked"])
	assert result.returncode == 0
	assert result.stdout.strip() == "release=true"
	assert "PLAN_SCOPE_BLOCK outcome=released issue=77" in result.stderr


def test_answer_release_reads_concatenated_paginated_pages(tmp_path: Path) -> None:
	raw = json.dumps([_implement_comment()]) + "\n" + json.dumps([_planning_comment()])
	result = _release(tmp_path, None, ["ai:blocked", "ai:scope-blocked"], raw_comments=raw)
	assert result.stdout.strip() == "release=true"


@pytest.mark.parametrize(
	("comments", "labels", "reason"),
	[
		([_planning_comment(), _implement_comment()], ["ai:blocked", "ai:scope-blocked"], "newest_not_planning"),
		([_planning_comment("NONE")], ["ai:blocked", "ai:scope-blocked"], "no_rejection"),
		([_planning_comment()], ["ai:scope-blocked"], "labels_absent"),
		([_planning_comment()], ["ai:blocked"], "labels_absent"),
		([], ["ai:blocked", "ai:scope-blocked"], "no_rejection"),
	],
)
def test_answer_release_keeps_the_latch(tmp_path: Path, comments: list, labels: list[str], reason: str) -> None:
	result = _release(tmp_path, comments, labels)
	assert result.stdout.strip() == "release=false"
	assert f"outcome=kept issue=77 reason={reason}" in result.stderr


def test_answer_release_fails_closed_on_unreadable_comments(tmp_path: Path) -> None:
	result = _release(tmp_path, None, ["ai:blocked", "ai:scope-blocked"], raw_comments="{not json")
	assert result.stdout.strip() == "release=false"
	assert "reason=unreadable" in result.stderr


def test_answer_release_ignores_a_header_only_implement_comment_as_newer(tmp_path: Path) -> None:
	header_only = {"author_association": "OWNER", "user": {"type": "User"},
		"body": "🚨 **Issue scope-lock rejected this implementation run.**\n\nno marker"}
	result = _release(tmp_path, [_planning_comment(), header_only], ["ai:blocked", "ai:scope-blocked"])
	assert result.stdout.strip() == "release=false"
