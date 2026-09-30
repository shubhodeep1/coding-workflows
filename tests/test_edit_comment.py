#!/usr/bin/env python3
"""Contract for `.claude/scripts/edit_comment.py` (CLAUDE.md §23.I helpers)."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / ".claude" / "scripts" / "edit_comment.py"
TEMPLATE_SCRIPT_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "edit_comment.py"
SETTINGS_PATHS = [REPO_ROOT / ".claude" / "settings.json", REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"]

BODY = "<!-- ai:claude-issue-progress:v1 -->\n**Stage:** security-pass 1/5\n**Waiting on:** run 1\n"


def _load():
	# The twin is loaded so the tests pass before the `.claude/` twin sync
	# (CLAUDE.md §28.C); test_template_parity keeps both copies identical.
	spec = importlib.util.spec_from_file_location("edit_comment", TEMPLATE_SCRIPT_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


ec = _load()


SESSION_ID = "session-uuid"


@pytest.fixture
def scratchpad(monkeypatch, tmp_path):
	"""This session's scratchpad under a fake temp root: <root>/claude-<uid>/<project>/<session>/scratchpad/."""
	root = tmp_path / "tmp"
	pad = root / f"claude-{os.getuid()}" / "-home-user-repo" / SESSION_ID / "scratchpad"
	pad.mkdir(parents=True)
	monkeypatch.setattr(ec, "_temp_roots", lambda: [root.resolve()])
	monkeypatch.setenv(ec.SESSION_ID_ENV_VAR, SESSION_ID)
	return pad


@pytest.fixture
def github(monkeypatch, scratchpad):
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


def _write(directory, data):
	path = directory / "reps.json"
	path.write_text(json.dumps(data), encoding="utf-8")
	return str(path)


def test_replaces_each_old_exactly_once_and_patches(github, scratchpad):
	reps = _write(scratchpad, [{"old": "security-pass 1/5", "new": "validation 1/3"}, {"old": "run 1", "new": "run 2"}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 0
	assert github["patches"] == [("o/r", 5, BODY.replace("security-pass 1/5", "validation 1/3").replace("run 1", "run 2"))]


def test_missing_or_ambiguous_old_writes_nothing(github, scratchpad, capsys):
	for old in ("not there", "**"):
		reps = _write(scratchpad, [{"old": old, "new": "x"}])
		assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 1
		assert "expected exactly 1" in json.loads(capsys.readouterr().out)["error"]
	assert github["patches"] == []


def test_dry_run_prints_and_does_not_write(github, scratchpad, capsys):
	reps = _write(scratchpad, [{"old": "run 1", "new": "run 9"}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps, "--dry-run"]) == 0
	out = json.loads(capsys.readouterr().out)
	assert out["updated"] is False and "run 9" in out["body"]
	assert github["patches"] == []


def test_unchanged_body_is_not_patched(github, scratchpad, capsys):
	reps = _write(scratchpad, [{"old": "run 1", "new": "run 1"}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 0
	assert json.loads(capsys.readouterr().out)["reason"] == "body unchanged"
	assert github["patches"] == []


def test_body_file_replaces_the_whole_body(github, scratchpad):
	body_file = scratchpad / "body.md"
	body_file.write_text("new body\n", encoding="utf-8")
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)]) == 0
	assert github["patches"][-1][2] == "new body\n"


@pytest.mark.parametrize(
	"data",
	[[], {"old": "a", "new": "b"}, [{"old": "", "new": "b"}], [{"old": "a"}], [{"old": "a", "new": "b", "x": 1}], [{"old": 1, "new": "b"}]],
)
def test_malformed_replacements_are_rejected(github, scratchpad, data):
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", _write(scratchpad, data)]) == 1
	assert github["patches"] == []


def test_bad_repo_and_id_are_rejected(github, scratchpad):
	reps = _write(scratchpad, [{"old": "run 1", "new": "run 2"}])
	assert ec.main(["--repo", "bad", "--comment-id", "5", "--replacements", reps]) == 1
	assert ec.main(["--repo", "o/r", "--comment-id", "0", "--replacements", reps]) == 1


def test_oversized_body_is_rejected(github, scratchpad):
	reps = _write(scratchpad, [{"old": "run 1", "new": "x" * (ec.MAX_BODY_CHARS + 1)}])
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--replacements", reps]) == 1


SECRET = "oauth_token: gho_secretvalue\n"


def _rejected(capsys, argv, github, reads):
	assert ec.main(argv) == 1
	out = capsys.readouterr().out
	assert "gho_secretvalue" not in out
	assert github["patches"] == [] and reads == []
	return json.loads(out)["error"]


@pytest.fixture
def reads(monkeypatch, github):
	"""Records every comment read; a rejected input file must not reach the API."""
	seen = []
	real = ec.check_in_status.gh_api

	def gh_api(path):
		seen.append(path)
		return real(path)

	monkeypatch.setattr(ec.check_in_status, "gh_api", gh_api)
	return seen


@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_file_outside_the_scratchpad_is_rejected_before_any_call(github, reads, scratchpad, tmp_path, capsys, flag, dry_run):
	secret = tmp_path / "hosts.yml"
	secret.write_text(SECRET, encoding="utf-8")
	argv = ["--repo", "o/r", "--comment-id", "5", flag, str(secret)] + (["--dry-run"] if dry_run else [])
	assert "session scratchpad" in _rejected(capsys, argv, github, reads)


@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
def test_symlink_and_dotdot_escapes_are_rejected(github, reads, scratchpad, tmp_path, capsys, flag):
	secret = tmp_path / "hosts.yml"
	secret.write_text(SECRET, encoding="utf-8")
	link = scratchpad / "body.md"
	link.symlink_to(secret)
	for path in (link, scratchpad / ".." / ".." / ".." / ".." / ".." / "hosts.yml"):
		assert "session scratchpad" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", flag, str(path)], github, reads)


def test_hard_link_into_the_scratchpad_is_rejected(github, reads, scratchpad, tmp_path, capsys):
	secret = tmp_path / "hosts.yml"
	secret.write_text(SECRET, encoding="utf-8")
	link = scratchpad / "body.md"
	os.link(secret, link)
	assert "hard links" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(link)], github, reads)


def test_directory_fifo_and_missing_file_are_rejected(github, reads, scratchpad, capsys):
	directory = scratchpad / "dir"
	directory.mkdir()
	fifo = scratchpad / "fifo"
	os.mkfifo(fifo)
	assert "not a regular file" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(directory)], github, reads)
	assert "not a regular file" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(fifo)], github, reads)
	_rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(scratchpad / "missing.md")], github, reads)


@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
def test_shallow_scratchpad_lookalike_is_rejected(github, reads, scratchpad, capsys, flag):
	root = scratchpad.parents[3]
	fake = root / "claude-EVIL" / "scratchpad"
	fake.mkdir(parents=True)
	body_file = fake / "body.md"
	body_file.write_text(SECRET, encoding="utf-8")
	assert "session scratchpad" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", flag, str(body_file)], github, reads)


@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
def test_file_over_the_size_limit_is_rejected_before_reading(monkeypatch, github, reads, scratchpad, capsys, flag):
	monkeypatch.setattr(ec, "MAX_INPUT_FILE_BYTES", 64)
	big = scratchpad / "big.md"
	big.write_text(SECRET + "x" * 64, encoding="utf-8")
	assert "not read" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", flag, str(big)], github, reads)


def test_file_that_grew_after_fstat_is_not_read_in_full(monkeypatch, github, reads, scratchpad, capsys):
	monkeypatch.setattr(ec, "MAX_INPUT_FILE_BYTES", 64)
	big = scratchpad / "big.md"
	big.write_text(SECRET + "x" * 64, encoding="utf-8")
	real_fstat = os.fstat

	def small_fstat(fd):
		info = real_fstat(fd)
		fields = list(info)
		fields[6] = 1  # st_size, as if the file were written to after the check
		return os.stat_result(fields)

	monkeypatch.setattr(ec.os, "fstat", small_fstat)
	assert "larger than 64 bytes" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(big)], github, reads)


def test_multibyte_file_that_grew_after_fstat_is_capped_in_bytes(monkeypatch, github, reads, scratchpad, capsys):
	monkeypatch.setattr(ec, "MAX_INPUT_FILE_BYTES", 64)
	big = scratchpad / "big.md"
	# 40 characters but 80 bytes: a character count would let it through.
	big.write_text("é" * 40, encoding="utf-8")
	real_fstat = os.fstat

	def small_fstat(fd):
		info = real_fstat(fd)
		fields = list(info)
		fields[6] = 1  # st_size, as if the file were written to after the check
		return os.stat_result(fields)

	monkeypatch.setattr(ec.os, "fstat", small_fstat)
	assert "larger than 64 bytes" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(big)], github, reads)


def test_multibyte_file_within_the_byte_limit_is_read(monkeypatch, github, reads, scratchpad):
	monkeypatch.setattr(ec, "MAX_INPUT_FILE_BYTES", 64)
	body_file = scratchpad / "body.md"
	body_file.write_text("é" * 32, encoding="utf-8")
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)]) == 0
	assert github["patches"][-1][2] == "é" * 32


def test_body_file_newlines_are_decoded_as_before(github, reads, scratchpad):
	body_file = scratchpad / "body.md"
	body_file.write_bytes(b"a\r\nb\rc\n")
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)]) == 0
	assert github["patches"][-1][2] == Path(body_file).read_text(encoding="utf-8") == "a\nb\nc\n"


def test_invalid_utf8_is_rejected(github, reads, scratchpad, capsys):
	body_file = scratchpad / "body.md"
	body_file.write_bytes(b"\xff\xfe bad")
	_rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)], github, reads)


def test_file_at_the_size_limit_is_read(monkeypatch, github, reads, scratchpad):
	monkeypatch.setattr(ec, "MAX_INPUT_FILE_BYTES", 64)
	body_file = scratchpad / "body.md"
	body_file.write_text("y" * 64, encoding="utf-8")
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)]) == 0
	assert github["patches"][-1][2] == "y" * 64


def test_scratchpad_body_file_reads_the_comment_once(github, reads, scratchpad):
	body_file = scratchpad / "nested" / "body.md"
	body_file.parent.mkdir()
	body_file.write_text("new body\n", encoding="utf-8")
	assert ec.main(["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)]) == 0
	assert reads == ["repos/o/r/issues/comments/5"]
	assert github["patches"][-1][2] == "new body\n"


@pytest.mark.parametrize(
	("relative", "expected"),
	[
		("claude-0/-home-user-repo/session/scratchpad/body.md", True),
		("claude-0/-home-user-repo/session/scratchpad/sub/reps.json", True),
		("claude-0/-home-user-repo/session/scratchpad", False),
		("claude-code-1.diag.log", False),
		("mcp-config-cse_x.json", False),
		("other/claude-0/p/s/scratchpad/body.md", False),
		("claude-0/-home-user-repo/session/notes/body.md", False),
		# Shallower or deeper than <root>/claude-*/<project>/<session>/scratchpad/ (PR #5465 review round 1).
		("claude-EVIL/scratchpad/body.md", False),
		("claude-0/scratchpad/sub/body.md", False),
		("claude-0/-home-user-repo/scratchpad/body.md", False),
		("claude-0/-home-user-repo/session/extra/scratchpad/body.md", False),
	],
)
def test_is_scratchpad_path(relative, expected):
	root = Path("/tmp")
	assert ec.is_scratchpad_path(root / relative, [root]) is expected
	assert ec.is_scratchpad_path(Path("/home/user/.config/gh/hosts.yml"), [root]) is False


@pytest.mark.parametrize(
	("relative", "expected"),
	[
		("claude-0/-home-user-repo/me/scratchpad/body.md", True),
		("claude-0/other-project/me/scratchpad/sub/reps.json", True),
		# Another session's or another user's scratchpad (issue #5700).
		("claude-0/-home-user-repo/other-session/scratchpad/body.md", False),
		("claude-1000/-home-user-repo/me/scratchpad/body.md", False),
		("claude-00/-home-user-repo/me/scratchpad/body.md", False),
		# The #5452 layout rules still apply.
		("claude-0/-home-user-repo/me/scratchpad", False),
		("claude-0/-home-user-repo/me/extra/scratchpad/body.md", False),
		("claude-0/me/scratchpad/body.md", False),
	],
)
def test_is_own_scratchpad_path(relative, expected):
	root = Path("/tmp")
	assert ec.is_own_scratchpad_path(root / relative, [root], ("claude-0", "me")) is expected
	assert ec.is_own_scratchpad_path(Path("/home/user/.config/gh/hosts.yml"), [root], ("claude-0", "me")) is False


@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
def test_another_sessions_scratchpad_is_rejected(github, reads, scratchpad, capsys, flag):
	other = scratchpad.parents[1] / "other-session" / "scratchpad"
	other.mkdir(parents=True)
	body_file = other / "body.md"
	body_file.write_text(SECRET, encoding="utf-8")
	assert "session scratchpad" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", flag, str(body_file)], github, reads)


@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
def test_another_uids_scratchpad_is_rejected(github, reads, scratchpad, capsys, flag):
	other = scratchpad.parents[3] / f"claude-{os.getuid() + 1}" / "-home-user-repo" / SESSION_ID / "scratchpad"
	other.mkdir(parents=True)
	body_file = other / "body.md"
	body_file.write_text(SECRET, encoding="utf-8")
	assert "session scratchpad" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", flag, str(body_file)], github, reads)


@pytest.mark.parametrize("session_id", [None, "", "..", ".", "a/b", "../session-uuid", "-x", "x" * 129])
@pytest.mark.parametrize("flag", ["--body-file", "--replacements"])
def test_unverifiable_session_id_rejects_every_file(monkeypatch, github, reads, scratchpad, capsys, flag, session_id):
	body_file = scratchpad / "body.md"
	body_file.write_text(SECRET, encoding="utf-8")
	if session_id is None:
		monkeypatch.delenv(ec.SESSION_ID_ENV_VAR, raising=False)
	else:
		monkeypatch.setenv(ec.SESSION_ID_ENV_VAR, session_id)
	assert ec.SESSION_ID_ENV_VAR in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", flag, str(body_file)], github, reads)


def test_missing_getuid_rejects_every_file(monkeypatch, github, reads, scratchpad, capsys):
	body_file = scratchpad / "body.md"
	body_file.write_text(SECRET, encoding="utf-8")
	monkeypatch.delattr(ec.os, "getuid")
	assert "cannot be verified" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)], github, reads)


def test_file_owned_by_another_uid_is_rejected(monkeypatch, github, reads, scratchpad, capsys):
	body_file = scratchpad / "body.md"
	body_file.write_text(SECRET, encoding="utf-8")
	real_fstat = os.fstat

	def foreign_fstat(fd):
		fields = list(real_fstat(fd))
		fields[4] = os.getuid() + 1  # st_uid, as if another account created the file
		return os.stat_result(fields)

	monkeypatch.setattr(ec.os, "fstat", foreign_fstat)
	assert "owned by uid" in _rejected(capsys, ["--repo", "o/r", "--comment-id", "5", "--body-file", str(body_file)], github, reads)


def test_caller_scratchpad_identity(monkeypatch):
	monkeypatch.setenv(ec.SESSION_ID_ENV_VAR, "884f673e-3156-5ccb-a763-8feb64e7b7e4")
	assert ec._caller_scratchpad_identity() == (f"claude-{os.getuid()}", "884f673e-3156-5ccb-a763-8feb64e7b7e4")
	monkeypatch.delenv(ec.SESSION_ID_ENV_VAR)
	assert ec._caller_scratchpad_identity() is None


def test_temp_roots_include_the_resolved_temp_dir():
	roots = ec._temp_roots()
	assert Path(tempfile.gettempdir()).resolve() in roots
	assert len(roots) == len(set(roots))


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
