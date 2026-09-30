#!/usr/bin/env python3
"""Contract for `.claude/scripts/edit_comment.py` (CLAUDE.md §23.I helpers)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / ".claude" / "scripts" / "edit_comment.py"
TEMPLATE_SCRIPT_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "edit_comment.py"
SETTINGS_PATHS = [REPO_ROOT / ".claude" / "settings.json", REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"]

BODY = "<!-- ai:claude-issue-progress:v1 -->\n**Stage:** security-pass 1/5\n**Waiting on:** run 1\n"


def _load():
	spec = importlib.util.spec_from_file_location("edit_comment", SCRIPT_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


ec = _load()


@pytest.fixture
def github(monkeypatch):
	state = {"body": BODY, "patches": []}

	def gh_api(path):
		assert path == "repos/o/r/issues/comments/5"
		return {"body": state["body"], "updated_at": "2026-09-27T00:00:00Z"}

	def patch(repo, comment_id, body):
		state["patches"].append((repo, comment_id, body))
		state["body"] = body
		return {"updated_at": "2026-09-27T01:00:00Z", "html_url": "https://github.com/o/r/issues/1#issuecomment-5"}

	monkeypatch.setattr(ec.check_in_status, "gh_api", gh_api)
	monkeypatch.setattr(ec, "_patch_comment", patch)
	return state


def _write(tmp_path, data):
	path = tmp_path / "reps.json"
	path.write_text(json.dumps(data), encoding="utf-8")
	return str(path)


def test_replaces_each_old_exactly_once_and_patches(github, tmp_path):
	reps = _write(tmp_path, [{"old": "security-pass 1/5", "new": "validation 1/3"}, {"old": "run 1", "new": "run 2"}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 0
	assert github["patches"] == [("o/r", 5, BODY.replace("security-pass 1/5", "validation 1/3").replace("run 1", "run 2"))]


def test_missing_or_ambiguous_old_writes_nothing(github, tmp_path, capsys):
	for old in ("not there", "**"):
		reps = _write(tmp_path, [{"old": old, "new": "x"}])
		assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 1
		assert "expected exactly 1" in json.loads(capsys.readouterr().out)["error"]
	assert github["patches"] == []


def test_dry_run_prints_and_does_not_write(github, tmp_path, capsys):
	reps = _write(tmp_path, [{"old": "run 1", "new": "run 9"}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps, "--dry-run"]) == 0
	out = json.loads(capsys.readouterr().out)
	assert out["updated"] is False and "run 9" in out["body"]
	assert github["patches"] == []


def test_unchanged_body_is_not_patched(github, tmp_path, capsys):
	reps = _write(tmp_path, [{"old": "run 1", "new": "run 1"}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 0
	assert json.loads(capsys.readouterr().out)["reason"] == "body unchanged"
	assert github["patches"] == []


def test_body_file_replaces_the_whole_body(github, tmp_path):
	body_file = tmp_path / "body.md"
	body_file.write_text("new body\n", encoding="utf-8")
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)]) == 0
	assert github["patches"][-1][2] == "new body\n"


@pytest.mark.parametrize(
	"data",
	[[], {"old": "a", "new": "b"}, [{"old": "", "new": "b"}], [{"old": "a"}], [{"old": "a", "new": "b", "x": 1}], [{"old": 1, "new": "b"}]],
)
def test_malformed_replacements_are_rejected(github, tmp_path, data):
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", _write(tmp_path, data)]) == 1
	assert github["patches"] == []


def test_bad_repo_and_id_are_rejected(github, tmp_path):
	reps = _write(tmp_path, [{"old": "run 1", "new": "run 2"}])
	assert ec.main(["--repo", "bad", "--comment-id", "5", "--replacements", reps]) == 1
	assert ec.main(["--repo", "o/r", "--comment-id", "0", "--replacements", reps]) == 1


def test_oversized_body_is_rejected(github, tmp_path):
	reps = _write(tmp_path, [{"old": "run 1", "new": "x" * (ec.MAX_BODY_CHARS + 1)}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 1


def test_patch_sends_body_as_json(monkeypatch):
	captured = {}

	class Proc:
		returncode = 0
		stdout = '{"updated_at": "t"}'
		stderr = ""

	def fake_run(argv, **kwargs):
		captured["argv"] = argv
		captured["body"] = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
		return Proc()

	monkeypatch.setattr(ec.subprocess, "run", fake_run)
	assert ec._patch_comment("o/r", 5, "hello `x` $y")["updated_at"] == "t"
	assert captured["argv"][:5] == ["gh", "api", "-X", "PATCH", "repos/o/r/issues/comments/5"]
	assert captured["body"] == {"body": "hello `x` $y"}


def test_payload_file_is_removed_after_a_failed_patch(monkeypatch):
	seen = []

	class Proc:
		returncode = 1
		stdout = ""
		stderr = "gh: Not Found (HTTP 404)"

	def fake_run(argv, **kwargs):
		seen.append(argv[argv.index("--input") + 1])
		return Proc()

	monkeypatch.setattr(ec.subprocess, "run", fake_run)
	with pytest.raises(ec.check_in_status.ReadError):
		ec._patch_comment("o/r", 5, "hello")
	assert len(seen) == 1 and not Path(seen[0]).exists()


def test_payload_file_is_removed_when_serialisation_fails(monkeypatch):
	created = []
	real = ec.tempfile.NamedTemporaryFile

	def tracking(*args, **kwargs):
		handle = real(*args, **kwargs)
		created.append(handle.name)
		return handle

	monkeypatch.setattr(ec.tempfile, "NamedTemporaryFile", tracking)
	monkeypatch.setattr(ec.subprocess, "run", lambda *a, **k: pytest.fail("gh must not run"))
	with pytest.raises(ec.check_in_status.ReadError):
		ec._patch_comment("o/r", 5, object())
	assert len(created) == 1 and not Path(created[0]).exists()


@pytest.mark.parametrize("path", SETTINGS_PATHS)
def test_settings_allow_the_helper(path):
	allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/edit_comment.py *)" in allow


def test_template_parity():
	assert TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")
