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
	env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
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
	home = tmp_path / "home"
	settings = project / ".claude" / "settings.json"
	_run_recorder(claude_dir, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-1"}, home, project)
	settings.write_text('{"hooks": {"PreToolUse": []}}\n', encoding="utf-8")
	result = _run_recorder(claude_dir, {
		"hook_event_name": "ConfigChange", "config_source": "project_settings",
		"config_file_path": str(settings), "session_id": "s-1",
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
		"hook_event_name": "ConfigChange", "config_source": config_source,
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
