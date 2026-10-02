"""Contract for the loaded-settings record and check (issue #5259).

`.claude/hooks/settings_load_recorder.py` records the sha256 of the
`.claude/settings.json` a session has loaded (SessionStart `startup` /
`resume` / `fork`, and every `ConfigChange` for `project_settings`), and
`.claude/scripts/loaded_settings_check.py` compares it with the working tree
after the Claude-asset sync merged a new `settings.json`. A missing record is
"not current", so the check fails closed.

Both the in-repo copies and their `workflow-templates/.claude/` twins are
exercised; the twins must stay byte-identical.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIRS = (ROOT / ".claude", ROOT / "workflow-templates" / ".claude")
RECORDER = Path("hooks") / "settings_load_recorder.py"
CHECK = Path("scripts") / "loaded_settings_check.py"
RECORDER_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/settings_load_recorder.py'
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
DIR_IDS = lambda path: str(path.relative_to(ROOT))  # noqa: E731


def _load(path: Path):
	spec = importlib.util.spec_from_file_location(f"mod_{abs(hash(str(path)))}", path)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


@pytest.fixture(params=CLAUDE_DIRS, ids=DIR_IDS)
def claude_dir(request) -> Path:
	return request.param


@pytest.fixture()
def project(tmp_path: Path) -> Path:
	root = tmp_path / "project"
	(root / ".claude").mkdir(parents=True)
	(root / ".claude" / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
	return root


def _sha(path: Path) -> str:
	return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_recorder(claude_dir: Path, payload, home: Path, project_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
	env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
	if project_dir is not None:
		env["CLAUDE_PROJECT_DIR"] = str(project_dir)
	stdin = payload if isinstance(payload, str) else json.dumps(payload)
	return subprocess.run(
		[sys.executable, str(claude_dir / RECORDER)], input=stdin, env=env,
		capture_output=True, text=True, timeout=30, check=False,
	)


def _run_check(claude_dir: Path, cwd: Path, home: Path, *args: str, session_env: str | None = None) -> subprocess.CompletedProcess[str]:
	# 0 keeps the not-current cases fast; the wait has its own tests below.
	env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LOADED_SETTINGS_CHECK_WAIT_SECONDS": "0"}
	if session_env is not None:
		env["CLAUDE_CODE_SESSION_ID"] = session_env
	return subprocess.run(
		[sys.executable, str(claude_dir / CHECK), *args], cwd=cwd, env=env,
		capture_output=True, text=True, timeout=30, check=False,
	)


def _record_file(home: Path, session_id: str) -> Path:
	return home / ".claude" / "loaded-settings" / f"{session_id}.json"


@pytest.mark.parametrize("relative", (RECORDER, CHECK, Path("settings.json")), ids=str)
def test_twins_are_byte_identical(relative: Path):
	assert (CLAUDE_DIRS[0] / relative).read_bytes() == (CLAUDE_DIRS[1] / relative).read_bytes()


@pytest.mark.parametrize("source", ("startup", "resume", "fork"))
def test_session_start_records_the_loaded_file(claude_dir: Path, project: Path, tmp_path: Path, source: str):
	home = tmp_path / "home"
	result = _run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": source, "session_id": "s-1", "cwd": "/elsewhere"}, home, project)
	assert result.returncode == 0
	assert result.stdout == "" and result.stderr == ""
	record = json.loads(_record_file(home, "s-1").read_text(encoding="utf-8"))
	assert record["sha256"] == _sha(project / ".claude" / "settings.json")
	assert record["event"] == "SessionStart" and record["source"] == source
	assert record["path"] == str(project / ".claude" / "settings.json")


def test_session_start_falls_back_to_the_payload_cwd(claude_dir: Path, project: Path, tmp_path: Path):
	home = tmp_path / "home"
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1", "cwd": str(project)}, home)
	record = json.loads(_record_file(home, "s-1").read_text(encoding="utf-8"))
	assert record["sha256"] == _sha(project / ".claude" / "settings.json")


@pytest.mark.parametrize("source", ("clear", "compact", "", None))
def test_clear_and_compact_keep_the_earlier_record(claude_dir: Path, project: Path, tmp_path: Path, source):
	home = tmp_path / "home"
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, project)
	before = _record_file(home, "s-1").read_text(encoding="utf-8")
	(project / ".claude" / "settings.json").write_text('{"hooks": {"X": []}}\n', encoding="utf-8")
	payload = {"hook_event_name": "SessionStart", "session_id": "s-1"}
	if source is not None:
		payload["source"] = source
	assert _run_recorder(claude_dir, payload, home, project).returncode == 0
	assert _record_file(home, "s-1").read_text(encoding="utf-8") == before


def test_config_change_replaces_the_record(claude_dir: Path, project: Path, tmp_path: Path):
	"""The payload names the source in `source` and the file in `file_path`
	(Claude Code hooks reference, "ConfigChange input"; PR #5283 review round 1)."""
	home = tmp_path / "home"
	settings = project / ".claude" / "settings.json"
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, project)
	settings.write_text('{"hooks": {"PreToolUse": []}}\n', encoding="utf-8")
	result = _run_recorder(claude_dir, {
		"hook_event_name": "ConfigChange", "source": "project_settings",
		"file_path": str(settings), "session_id": "s-1",
	}, home, project)
	assert result.returncode == 0 and result.stdout == ""
	record = json.loads(_record_file(home, "s-1").read_text(encoding="utf-8"))
	assert record["sha256"] == _sha(settings)
	assert record["event"] == "ConfigChange" and record["source"] == "project_settings"
	assert list((home / ".claude" / "loaded-settings").iterdir()) == [_record_file(home, "s-1")]


@pytest.mark.parametrize("config_source", ("user_settings", "local_settings", "policy_settings", "skills"))
def test_other_config_sources_are_ignored(claude_dir: Path, project: Path, tmp_path: Path, config_source: str):
	home = tmp_path / "home"
	_run_recorder(claude_dir, {
		"hook_event_name": "ConfigChange", "source": config_source,
		"file_path": str(project / ".claude" / "settings.json"), "session_id": "s-1",
	}, home, project)
	assert not _record_file(home, "s-1").exists()


def test_config_change_reads_only_the_documented_payload_fields(claude_dir: Path, project: Path, tmp_path: Path):
	"""`config_source` / `config_file_path` are not Claude Code's field names; a
	recorder that read them never recorded a real reload (PR #5283 review round 1)."""
	home = tmp_path / "home"
	_run_recorder(claude_dir, {
		"hook_event_name": "ConfigChange", "config_source": "project_settings",
		"config_file_path": str(project / ".claude" / "settings.json"), "session_id": "s-1",
	}, home, project)
	assert not _record_file(home, "s-1").exists()


def test_missing_settings_file_is_recorded_as_absent(claude_dir: Path, tmp_path: Path):
	home = tmp_path / "home"
	empty = tmp_path / "empty"
	empty.mkdir()
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, empty)
	assert json.loads(_record_file(home, "s-1").read_text(encoding="utf-8"))["sha256"] == "absent"


@pytest.mark.parametrize("stdin_text", ("", "   ", "not json", "[1]", '{"hook_event_name": 5}', '{"hook_event_name": "SessionStart", "source": "startup"}'))
def test_bad_payloads_never_fail_and_write_nothing(claude_dir: Path, project: Path, tmp_path: Path, stdin_text: str):
	home = tmp_path / "home"
	result = _run_recorder(claude_dir, stdin_text, home, project)
	assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
	assert not (home / ".claude" / "loaded-settings").exists()


def test_unwritable_record_directory_never_fails(claude_dir: Path, project: Path, tmp_path: Path):
	home = tmp_path / "home"
	(home / ".claude").mkdir(parents=True)
	(home / ".claude" / "loaded-settings").write_text("a file, not a directory", encoding="utf-8")
	result = _run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, project)
	assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def test_session_id_is_sanitised_into_one_file_name(claude_dir: Path, project: Path, tmp_path: Path):
	module = _load(claude_dir / RECORDER)
	record = module.build_record({"hook_event_name": "SessionStart", "source": "startup", "session_id": "../x/y"}, NOW, str(project))
	path = module.write_record(record, tmp_path / "records")
	assert path.parent == tmp_path / "records" and path.name == ".._x_y.json"


def test_check_reports_current_after_the_recorded_load(claude_dir: Path, project: Path, tmp_path: Path):
	home = tmp_path / "home"
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, project)
	result = _run_check(claude_dir, project, home, session_env="s-1")
	assert result.returncode == 0, result.stdout
	verdict = json.loads(result.stdout)
	assert verdict["current"] is True
	assert verdict["loaded_sha256"] == verdict["file_sha256"] == _sha(project / ".claude" / "settings.json")
	assert verdict["recorded_event"] == "SessionStart"


def test_check_reports_not_current_after_an_unrecorded_merge(claude_dir: Path, project: Path, tmp_path: Path):
	home = tmp_path / "home"
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, project)
	(project / ".claude" / "settings.json").write_text('{"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}\n', encoding="utf-8")
	result = _run_check(claude_dir, project, home, "--session-id", "s-1")
	assert result.returncode == 1
	verdict = json.loads(result.stdout)
	assert verdict["current"] is False
	assert verdict["reason"] == "the session has not loaded this settings.json"


def test_check_fails_closed_without_a_record_or_session_id(claude_dir: Path, project: Path, tmp_path: Path):
	home = tmp_path / "home"
	missing = _run_check(claude_dir, project, home, session_env="s-1")
	assert missing.returncode == 1
	assert json.loads(missing.stdout)["reason"].startswith("no record for this session")
	no_id = _run_check(claude_dir, project, home)
	assert no_id.returncode == 1
	assert json.loads(no_id.stdout)["reason"].startswith("no session id")


def test_check_fails_closed_on_a_malformed_record(claude_dir: Path, project: Path, tmp_path: Path):
	home = tmp_path / "home"
	record = _record_file(home, "s-1")
	record.parent.mkdir(parents=True)
	record.write_text("{not json", encoding="utf-8")
	assert json.loads(_run_check(claude_dir, project, home, session_env="s-1").stdout)["reason"] == "record unreadable"
	record.write_text('{"sha256": 5}', encoding="utf-8")
	assert json.loads(_run_check(claude_dir, project, home, session_env="s-1").stdout)["reason"] == "record malformed"


def test_check_fails_closed_without_a_settings_file_even_when_the_record_says_absent(claude_dir: Path, tmp_path: Path):
	home = tmp_path / "home"
	empty = tmp_path / "empty"
	empty.mkdir()
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, empty)
	assert json.loads(_record_file(home, "s-1").read_text(encoding="utf-8"))["sha256"] == "absent"
	result = _run_check(claude_dir, empty, home, session_env="s-1")
	assert result.returncode == 1, result.stdout
	verdict = json.loads(result.stdout)
	assert verdict["current"] is False
	assert verdict["loaded_sha256"] == verdict["file_sha256"] == "absent"
	assert verdict["reason"] == "no readable settings.json to verify"


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(
		["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
		cwd=cwd, check=True, capture_output=True, text=True, timeout=30,
	)


def _merged_repo(root: Path, before_settings: dict) -> Path:
	"""A repo whose HEAD merged a new settings.json over `before_settings` (HEAD^1)."""
	(root / ".claude").mkdir(parents=True)
	_git(root, "init", "-q", "-b", "work")
	(root / ".claude" / "settings.json").write_text(json.dumps(before_settings) + "\n", encoding="utf-8")
	_git(root, "add", ".")
	_git(root, "commit", "-q", "-m", "branch")
	_git(root, "checkout", "-q", "-b", "source")
	merged = json.loads(json.dumps(before_settings))
	merged.setdefault("hooks", {})["PreToolUse"] = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}]
	(root / ".claude" / "settings.json").write_text(json.dumps(merged) + "\n", encoding="utf-8")
	_git(root, "commit", "-q", "-am", "guard")
	_git(root, "checkout", "-q", "work")
	(root / "other.txt").write_text("x\n", encoding="utf-8")
	_git(root, "add", ".")
	_git(root, "commit", "-q", "-m", "work")
	_git(root, "merge", "-q", "--no-ff", "-m", "[claude-asset-sync] merge source", "source")
	return root


RECORDER_WIRING = {"hooks": {"ConfigChange": [{"matcher": "project_settings", "hooks": [{"type": "command", "command": RECORDER_COMMAND}]}]}}


def test_check_names_a_pre_recorder_branch_without_changing_the_verdict(claude_dir: Path, tmp_path: Path):
	"""ConfigChange runs the hooks loaded before a change, so a branch without
	the recorder cannot record the merged file's reload (issue #5259)."""
	home = tmp_path / "home"
	repo = _merged_repo(tmp_path / "repo", {"hooks": {}})
	record = _record_file(home, "s-1")
	record.parent.mkdir(parents=True)
	# The session recorded the branch's file (the watcher's checkout reload), not the merge.
	before_sha = hashlib.sha256(subprocess.run(["git", "show", "HEAD^1:.claude/settings.json"], cwd=repo, capture_output=True, check=True).stdout).hexdigest()
	record.write_text(json.dumps({"sha256": before_sha, "event": "ConfigChange"}), encoding="utf-8")
	result = _run_check(claude_dir, repo, home, "--before", "HEAD^1", session_env="s-1")
	assert result.returncode == 1, result.stdout
	verdict = json.loads(result.stdout)
	assert verdict["current"] is False
	assert verdict["before_recorder_wired"] is False
	assert "wired no ConfigChange recorder" in verdict["reason"] and "(HEAD^1)" in verdict["reason"]
	# Without --before the field is null and the reason is unchanged.
	plain = json.loads(_run_check(claude_dir, repo, home, session_env="s-1").stdout)
	assert plain["before_recorder_wired"] is None
	assert plain["reason"] == "the session has not loaded this settings.json"


def test_check_names_a_pre_recorder_branch_without_a_record(claude_dir: Path, tmp_path: Path):
	"""The canonical pre-recorder case: the recorder never ran, so there is no
	record at all; the reason still names the cause (issue #5259, PR #5511)."""
	home = tmp_path / "home"
	repo = _merged_repo(tmp_path / "repo", {"hooks": {}})
	assert not _record_file(home, "s-1").exists()
	result = _run_check(claude_dir, repo, home, "--before", "HEAD^1", session_env="s-1")
	assert result.returncode == 1, result.stdout
	verdict = json.loads(result.stdout)
	assert verdict["current"] is False
	assert verdict["before_recorder_wired"] is False
	assert verdict["loaded_sha256"] is None
	assert "wired no ConfigChange recorder" in verdict["reason"] and "(HEAD^1)" in verdict["reason"]
	# A branch that wired the recorder before keeps the plain no-record reason.
	wired = _merged_repo(tmp_path / "wired", RECORDER_WIRING)
	plain = json.loads(_run_check(claude_dir, wired, home, "--before", "HEAD^1", session_env="s-1").stdout)
	assert plain["before_recorder_wired"] is True
	assert plain["reason"].startswith("no record for this session")


def test_check_before_an_undecodable_revision_is_null(claude_dir: Path, tmp_path: Path):
	"""Undecodable `git show` output is an unreadable revision, never a crash."""
	root = tmp_path / "repo"
	(root / ".claude").mkdir(parents=True)
	_git(root, "init", "-q", "-b", "work")
	(root / ".claude" / "settings.json").write_bytes(b'{"hooks": {"x": "\xff\xfe"}}\n')
	_git(root, "add", ".")
	_git(root, "commit", "-q", "-m", "undecodable")
	(root / ".claude" / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
	_git(root, "commit", "-q", "-am", "readable")
	result = _run_check(claude_dir, root, tmp_path / "home", "--before", "HEAD^1", session_env="s-1")
	assert result.returncode == 1, result.stderr
	verdict = json.loads(result.stdout)
	assert verdict["before_recorder_wired"] is None
	assert verdict["reason"].startswith("no record for this session")


def test_check_before_decodes_a_revision_as_utf8_whatever_the_locale(claude_dir: Path, tmp_path: Path):
	"""A UTF-8 settings.json is read the same under an ASCII locale (PR #5511)."""
	repo = tmp_path / "repo"
	(repo / ".claude").mkdir(parents=True)
	_git(repo, "init", "-q", "-b", "work")
	wiring = dict(RECORDER_WIRING, _note="café — non-ASCII")
	(repo / ".claude" / "settings.json").write_text(json.dumps(wiring, ensure_ascii=False) + "\n", encoding="utf-8")
	_git(repo, "add", ".")
	_git(repo, "commit", "-q", "-m", "utf-8 wiring")
	(repo / ".claude" / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
	_git(repo, "commit", "-q", "-am", "later")
	assert b"\xc3\xa9" in subprocess.run(["git", "show", "HEAD^1:.claude/settings.json"], cwd=repo, capture_output=True, check=True).stdout
	env = {
		"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "CLAUDE_CODE_SESSION_ID": "s-1",
		"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
	}
	result = subprocess.run(
		[sys.executable, str(claude_dir / CHECK), "--before", "HEAD^1"], cwd=repo, env=env,
		capture_output=True, text=True, timeout=30, check=False,
	)
	assert result.returncode == 1, result.stderr
	verdict = json.loads(result.stdout)
	assert verdict["before_recorder_wired"] is True
	assert verdict["reason"].startswith("no record for this session")


def test_check_with_a_recorder_wired_before_keeps_the_plain_reason(claude_dir: Path, tmp_path: Path):
	home = tmp_path / "home"
	repo = _merged_repo(tmp_path / "repo", RECORDER_WIRING)
	record = _record_file(home, "s-1")
	record.parent.mkdir(parents=True)
	record.write_text(json.dumps({"sha256": "0" * 64, "event": "SessionStart"}), encoding="utf-8")
	verdict = json.loads(_run_check(claude_dir, repo, home, "--before", "HEAD^1", session_env="s-1").stdout)
	assert verdict["current"] is False
	assert verdict["before_recorder_wired"] is True
	assert verdict["reason"] == "the session has not loaded this settings.json"
	# A current record stays current whatever the branch had before.
	record.write_text(json.dumps({"sha256": _sha(repo / ".claude" / "settings.json"), "event": "ConfigChange"}), encoding="utf-8")
	current = _run_check(claude_dir, repo, home, "--before", "HEAD^1", session_env="s-1")
	assert current.returncode == 0 and json.loads(current.stdout)["current"] is True


def test_check_before_an_unreadable_revision_is_null(claude_dir: Path, project: Path, tmp_path: Path):
	verdict = json.loads(_run_check(claude_dir, project, tmp_path / "home", "--before", "HEAD^1", session_env="s-1").stdout)
	assert verdict["before_recorder_wired"] is None
	assert verdict["reason"].startswith("no record for this session")


def test_check_refuses_a_before_value_git_would_read_as_an_option(claude_dir: Path, tmp_path: Path):
	"""PR #5283 operator review N1: `--before=--output=<path>` made the
	allowlisted check run `git show --output=<path>:./settings.json`, which
	wrote a file. A `-`-prefixed value is now a bad argument and nothing runs."""
	home = tmp_path / "home"
	repo = _merged_repo(tmp_path / "repo", RECORDER_WIRING)
	target = tmp_path / "out"
	(tmp_path / "out:.").mkdir()
	before_files = sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts)
	for value in (f"--output={target}", "-p", "--stat"):
		result = _run_check(claude_dir, repo, home, f"--before={value}", session_env="s-1")
		assert result.returncode == 2, (value, result.stdout, result.stderr)
		assert result.stdout == ""
		assert "--before must be a revision name" in result.stderr
	assert not (tmp_path / "out:." / "settings.json").exists()
	assert sorted(p for p in tmp_path.rglob("*") if ".git" not in p.parts) == before_files


@pytest.mark.parametrize("value", ("", "HEAD:other", "HEAD 1", "HEAD\n", "x;y"), ids=repr)
def test_check_refuses_a_before_value_that_is_not_a_plain_revision(claude_dir: Path, project: Path, tmp_path: Path, value: str):
	result = _run_check(claude_dir, project, tmp_path / "home", "--before", value, session_env="s-1")
	assert result.returncode == 2, result.stdout
	assert result.stdout == ""


@pytest.mark.parametrize("value", ("HEAD", "HEAD^1", "HEAD~2", "HEAD@{1}", "origin/main", "claude/a-b", "34cce53"))
def test_safe_before_revisions_are_accepted(claude_dir: Path, value: str):
	assert _load(claude_dir / CHECK).is_safe_before_rev(value) is True


def test_recorder_wired_at_never_passes_an_option_to_git(claude_dir: Path, tmp_path: Path, monkeypatch):
	"""Direct callers get the same guard, and `git show` always gets
	`--end-of-options` before the revision."""
	module = _load(claude_dir / CHECK)
	calls = []

	def fake_run(argv, **kwargs):
		calls.append(argv)
		return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(RECORDER_WIRING), stderr="")

	monkeypatch.setattr(module.subprocess, "run", fake_run)
	settings = tmp_path / ".claude" / "settings.json"
	assert module.recorder_wired_at(f"--output={tmp_path / 'x'}", settings) is None
	assert calls == []
	assert module.recorder_wired_at("HEAD^1", settings) is True
	assert calls == [["git", "-C", str(settings.parent), "show", "--end-of-options", "HEAD^1:./settings.json"]]


@pytest.mark.parametrize(
	("settings_data", "wired"),
	(
		(RECORDER_WIRING, True),
		({"hooks": {"ConfigChange": [{"hooks": [{"command": RECORDER_COMMAND}]}]}}, True),
		({"hooks": {"ConfigChange": [{"matcher": "project_settings|local_settings", "hooks": [{"command": RECORDER_COMMAND}]}]}}, True),
		({"hooks": {"ConfigChange": [{"matcher": "user_settings", "hooks": [{"command": RECORDER_COMMAND}]}]}}, False),
		({"hooks": {"SessionStart": [{"hooks": [{"command": RECORDER_COMMAND}]}]}}, False),
		({"hooks": {"ConfigChange": "bad"}}, False),
		({"hooks": {"ConfigChange": [{"matcher": "project_settings", "hooks": True}]}}, False),
		({"hooks": {"ConfigChange": [{"matcher": "project_settings", "hooks": 42}, {"hooks": [{"command": RECORDER_COMMAND}]}]}}, True),
		([], False),
	),
)
def test_recorder_wired_reads_the_config_change_matcher(claude_dir: Path, settings_data, wired: bool):
	assert _load(claude_dir / CHECK).recorder_wired(settings_data) is wired


def test_recorder_and_check_share_the_record_contract(claude_dir: Path):
	recorder = _load(claude_dir / RECORDER)
	check = _load(claude_dir / CHECK)
	assert recorder.RECORD_DIR_PARTS == check.RECORD_DIR_PARTS
	assert recorder.ABSENT == check.ABSENT
	assert recorder._SESSION_FILE_RE.pattern == check._SESSION_FILE_RE.pattern
	assert recorder.SETTINGS_RELATIVE_PATH == check.DEFAULT_SETTINGS


def test_check_usage_error_exits_2(claude_dir: Path, project: Path, tmp_path: Path):
	assert _run_check(claude_dir, project, tmp_path, "--unknown").returncode == 2


def test_settings_wire_the_recorder_and_allow_the_check(claude_dir: Path):
	settings = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
	module = _load(claude_dir / RECORDER)
	hooks = settings["hooks"]
	for event in module.SETTINGS_EVENTS:
		commands = [hook["command"] for entry in hooks[event] for hook in entry["hooks"]]
		assert commands.count(RECORDER_COMMAND) == 1, event
	# The existing SessionStart hook stays first and unchanged (CLAUDE.md §6).
	assert hooks["SessionStart"][0]["hooks"][0]["command"] == '"$CLAUDE_PROJECT_DIR"/.claude/hooks/session-start.sh'
	# A blocking ConfigChange hook would leave a record for a change never applied.
	assert [entry.get("matcher") for entry in hooks["ConfigChange"]] == [module.CONFIG_CHANGE_SOURCE]
	assert [hook["command"] for entry in hooks["ConfigChange"] for hook in entry["hooks"]] == [RECORDER_COMMAND]
	allow = settings["permissions"]["allow"]
	assert "Bash(python3 .claude/scripts/loaded_settings_check.py *)" in allow
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/loaded_settings_check.py *)" in allow


class _FakeClock:
	"""A monotonic clock that only `pause` advances, so wait tests never sleep."""

	def __init__(self):
		self.now = 0.0
		self.pauses: list[float] = []

	def clock(self) -> float:
		return self.now

	def pause(self, seconds: float) -> None:
		self.pauses.append(seconds)
		self.now += seconds


def test_check_waits_for_a_late_config_change(claude_dir: Path, project: Path, tmp_path: Path):
	"""The watcher runs `ConfigChange` a few seconds after the file changes, so
	the check re-reads a stale record before it reports not current (PR #5283
	review round 2)."""
	module = _load(claude_dir / CHECK)
	settings = project / ".claude" / "settings.json"
	directory = tmp_path / "records"
	directory.mkdir()
	record = directory / "s-1.json"
	record.write_text(json.dumps({"sha256": "0" * 64, "event": "SessionStart"}), encoding="utf-8")
	fake = _FakeClock()

	def pause(seconds: float) -> None:
		fake.pause(seconds)
		if fake.now >= 2.0:
			record.write_text(json.dumps({"sha256": _sha(settings), "event": "ConfigChange"}), encoding="utf-8")

	result = module.check_with_wait("s-1", settings, directory, wait_seconds=10, clock=fake.clock, pause=pause)
	assert result["current"] is True and result["recorded_event"] == "ConfigChange"
	assert fake.now == 2.0 and all(step == module.POLL_INTERVAL_SECONDS for step in fake.pauses)


@pytest.mark.parametrize("missing_record", (False, True), ids=("stale-record", "no-record"))
def test_check_gives_up_after_the_wait(claude_dir: Path, project: Path, tmp_path: Path, missing_record: bool):
	module = _load(claude_dir / CHECK)
	directory = tmp_path / "records"
	directory.mkdir()
	if not missing_record:
		(directory / "s-1.json").write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
	fake = _FakeClock()
	result = module.check_with_wait("s-1", project / ".claude" / "settings.json", directory, wait_seconds=3, clock=fake.clock, pause=fake.pause)
	assert result["current"] is False
	assert result["reason"] == (module.NO_RECORD_REASON if missing_record else module.NOT_LOADED_REASON)
	assert fake.now == 3.0


@pytest.mark.parametrize("case", ("no-session", "no-settings", "malformed-record", "current"))
def test_check_never_waits_when_waiting_cannot_help(claude_dir: Path, project: Path, tmp_path: Path, case: str):
	module = _load(claude_dir / CHECK)
	settings = project / ".claude" / "settings.json"
	directory = tmp_path / "records"
	directory.mkdir()
	session = None if case == "no-session" else "s-1"
	if case == "no-settings":
		settings.unlink()
		(directory / "s-1.json").write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")
	elif case == "malformed-record":
		(directory / "s-1.json").write_text("[]", encoding="utf-8")
	elif case == "current":
		(directory / "s-1.json").write_text(json.dumps({"sha256": _sha(settings)}), encoding="utf-8")
	fake = _FakeClock()
	result = module.check_with_wait(session, settings, directory, wait_seconds=10, clock=fake.clock, pause=fake.pause)
	assert result["current"] is (case == "current")
	assert fake.pauses == []


@pytest.mark.parametrize(("raw", "expected"), ((None, 10.0), ("", 10.0), ("0", 0.0), ("2.5", 2.5), ("-1", 10.0), ("x", 10.0), ("inf", 10.0), ("nan", 10.0)))
def test_wait_default_comes_from_the_environment(claude_dir: Path, monkeypatch, raw, expected):
	module = _load(claude_dir / CHECK)
	if raw is None:
		monkeypatch.delenv(module.WAIT_SECONDS_ENV, raising=False)
	else:
		monkeypatch.setenv(module.WAIT_SECONDS_ENV, raw)
	assert module.default_wait_seconds() == expected


@pytest.mark.parametrize("value", ("-1", "inf", "nan", "soon"))
def test_bad_wait_seconds_is_a_usage_error(claude_dir: Path, project: Path, tmp_path: Path, value: str):
	assert _run_check(claude_dir, project, tmp_path, "--wait-seconds", value, session_env="s-1").returncode == 2
