#!/usr/bin/env python3
"""Contract for permission prompt reports (CLAUDE.md §23.I).

Covers `.claude/hooks/permission_prompt_logger.py` (never decides, never
fails the permission flow, logs outside the repo) and
`.claude/scripts/permission_prompts.py` (pattern grouping, redaction, filing
only in coding-workflows, dedupe by marker, state), plus the settings.json
wiring, template parity, and the CLAUDE.md / ci.yml / label contract hooks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "permission_prompt_logger.py"
SCRIPT_PATH = REPO_ROOT / ".claude" / "scripts" / "permission_prompts.py"
TEMPLATE_HOOK_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "permission_prompt_logger.py"
TEMPLATE_SCRIPT_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "permission_prompts.py"
SETTINGS_PATHS = [REPO_ROOT / ".claude" / "settings.json", REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LABEL_CONTRACT = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
PLAN_COMMAND = REPO_ROOT / ".claude" / "commands" / "implement-plan-claude.md"


def _load(name, path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


logger = _load("permission_prompt_logger", HOOK_PATH)
# The workflow-templates twin, which a `.claude/` change lands in first (twin-first, CLAUDE.md §28.C);
# test_template_parity keeps it identical to `.claude/scripts/permission_prompts.py`.
pp = _load("permission_prompts", TEMPLATE_SCRIPT_PATH)

NOW = datetime(2026, 9, 27, 3, 0, 0, tzinfo=timezone.utc)


def _payload(command=None, event="PermissionRequest", tool="Bash", **extra):
	payload = {
		"hook_event_name": event,
		"session_id": "sess-1",
		"tool_name": tool,
		"tool_input": {"command": command} if command is not None else extra.pop("tool_input", {}),
		"cwd": "/home/user/coding-workflows",
		"permission_mode": "auto",
	}
	payload.update(extra)
	return payload


def _hook_env(tmp_path):
	"""Environment for running the real hook: a temporary HOME and no cloud session, so its reporter child never posts."""
	env = dict(os.environ, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
	env.pop(pp.REMOTE_SESSION_ENV, None)
	return env


def _log(tmp_path, payloads):
	directory = tmp_path / "log"
	for payload in payloads:
		logger.append_record(logger.build_record(payload, NOW), directory)
	return directory


# ──────────────────────────────────────────────────────────────────
# Logger hook
# ──────────────────────────────────────────────────────────────────


def test_hook_logs_a_prompt_and_prints_nothing(tmp_path):
	env = _hook_env(tmp_path)
	payload = _payload("gh api -X GET search/issues -f q=x | sort -n", reason="Bash command")
	result = subprocess.run([sys.executable, str(HOOK_PATH)], input=json.dumps(payload), capture_output=True, text=True, env=env, check=False)
	assert result.returncode == 0
	assert result.stdout == "" and result.stderr == ""
	lines = (tmp_path / ".claude" / "permission-prompts" / "sess-1.jsonl").read_text().splitlines()
	record = json.loads(lines[0])
	assert record["event"] == "PermissionRequest"
	assert record["tool_input"]["command"].startswith("gh api -X GET")
	assert record["reason"] == "Bash command"


def test_hook_reads_decision_reason_and_logs_denials():
	record = logger.build_record(_payload("rm x", event="PermissionDenied", decision_reason="[Self-Modification]"), NOW)
	assert record["event"] == "PermissionDenied"
	assert record["reason"] == "[Self-Modification]"


def test_hook_ignores_other_events():
	assert logger.build_record({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, NOW) is None


def test_hook_truncates_long_values():
	record = logger.build_record(_payload("x" * (logger.MAX_VALUE_CHARS + 50)), NOW)
	assert len(record["tool_input"]["command"]) < logger.MAX_VALUE_CHARS + 60
	assert "truncated 50 chars" in record["tool_input"]["command"]


@pytest.mark.parametrize("stdin_text", ["", "   ", "not json", "[1]", '{"hook_event_name": 5}'])
def test_hook_never_fails_or_decides(tmp_path, stdin_text):
	env = _hook_env(tmp_path)
	result = subprocess.run([sys.executable, str(HOOK_PATH)], input=stdin_text, capture_output=True, text=True, env=env, check=False)
	assert result.returncode == 0 and result.stdout == ""


def test_hook_swallows_write_errors(tmp_path):
	blocker = tmp_path / ".claude"
	blocker.write_text("not a directory")
	env = _hook_env(tmp_path)
	result = subprocess.run([sys.executable, str(HOOK_PATH)], input=json.dumps(_payload("ls")), capture_output=True, text=True, env=env, check=False)
	assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def test_hook_session_id_cannot_escape_the_log_dir(tmp_path):
	path = logger.append_record(logger.build_record(_payload("ls", session_id="../../etc/passwd"), NOW), tmp_path / "log")
	assert path.parent == tmp_path / "log"


def test_hook_has_no_api_calls_or_env_reads():
	source = HOOK_PATH.read_text(encoding="utf-8")
	for forbidden in ("os.environ", "getenv", "urllib", "api.github.com", "subprocess.run", "check_output", ".wait(", ".communicate("):
		assert forbidden not in source
	# The only child process is the detached reporter (issue #4755).
	assert source.count("subprocess.Popen(") == 1
	assert "def spawn_reporter(" in source


class FakePopen:
	calls: list = []

	def __init__(self, args, **kwargs):
		FakePopen.calls.append((args, kwargs))


@pytest.fixture
def fake_popen(monkeypatch):
	FakePopen.calls = []
	monkeypatch.setattr(logger.subprocess, "Popen", FakePopen)
	return FakePopen


def _run_hook_main(monkeypatch, tmp_path, payload):
	monkeypatch.setattr(logger, "log_dir", lambda: tmp_path / "log")
	monkeypatch.setattr(logger.sys, "stdin", __import__("io").StringIO(json.dumps(payload)))
	return logger.main()


def test_hook_spawns_one_detached_reporter_for_a_prompt(monkeypatch, tmp_path, fake_popen, capsys):
	assert _run_hook_main(monkeypatch, tmp_path, _payload("ls")) == 0
	assert capsys.readouterr().out == ""
	assert len(fake_popen.calls) == 1
	args, kwargs = fake_popen.calls[0]
	log_file = tmp_path / "log" / "sess-1.jsonl"
	digest = __import__("hashlib").sha256(log_file.read_text(encoding="utf-8").splitlines()[-1].encode("utf-8")).hexdigest()
	assert args[1:] == ["-B", str(logger.REPORTER_PATH), "report-now", "--log-file", str(log_file), "--cwd", "/home/user/coding-workflows", "--record-sha256", digest]
	assert logger.REPORTER_PATH == SCRIPT_PATH
	assert kwargs["start_new_session"] is True and kwargs["close_fds"] is True
	for stream in ("stdin", "stdout", "stderr"):
		assert kwargs[stream] is logger.subprocess.DEVNULL


def test_hook_spawns_nothing_for_a_denial(monkeypatch, tmp_path, fake_popen):
	assert _run_hook_main(monkeypatch, tmp_path, _payload("ls", event="PermissionDenied")) == 0
	assert fake_popen.calls == []
	assert (tmp_path / "log" / "sess-1.jsonl").exists()


def test_hook_swallows_a_spawn_failure(monkeypatch, tmp_path, capsys):
	def boom(*args, **kwargs):
		raise OSError("fork failed")

	monkeypatch.setattr(logger.subprocess, "Popen", boom)
	assert _run_hook_main(monkeypatch, tmp_path, _payload("ls")) == 0
	assert capsys.readouterr().out == ""
	assert (tmp_path / "log" / "sess-1.jsonl").exists()


# ──────────────────────────────────────────────────────────────────
# Patterns, shapes, redaction
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	("command", "shape"),
	[
		("gh api -X GET search/issues -f q='repo:a/b' -f per_page=50 --jq '.items[]' | sort -n", "gh api -X GET search/issues -f * --jq * | sort -n"),
		("cd /tmp/x && gh api -X PATCH repos/o/r/issues/comments/5 -F body=@pc.md", "cd * && gh api -X PATCH repos/*/*/issues/comments/N -F *"),
		(
			"S=/tmp; gh api repos/o/r/issues/comments/5 --jq .body > $S/b.md && python3 - \"$S/b.md\" <<'EOF'\nprint(1)\nEOF\necho done",
			"S=* ; gh api repos/*/*/issues/comments/N --jq * > * && python3 * << * ; echo *",
		),
		("gh api 'repos/o/r/actions/workflows/x.yml/runs?per_page=1'", "gh api repos/*/*/actions/workflows/x.yml/runs?*"),
		("git commit -m 'hello world'", "git commit -m *"),
		("echo 'unbalanced", "unparseable: echo 'unbalanced"),
	],
)
def test_command_shape(command, shape):
	assert pp.command_shape(command) == shape


def test_same_shape_different_values_share_a_signature(tmp_path):
	directory = _log(tmp_path, [_payload("gh api -X GET search/issues -f q=a | sort -n"), _payload("gh api -X GET search/issues -f q=b | sort -n")])
	patterns = pp.group_patterns(pp.load_records(directory))
	assert len(patterns) == 1 and patterns[0]["count"] == 2


def test_event_and_tool_are_part_of_the_signature(tmp_path):
	directory = _log(
		tmp_path,
		[
			_payload("ls"),
			_payload("ls", event="PermissionDenied"),
			_payload(tool="Edit", tool_input={"file_path": "/home/user/coding-workflows/.claude/hooks/x.py", "old_string": "a", "new_string": "b"}),
		],
	)
	patterns = pp.group_patterns(pp.load_records(directory))
	assert len({pattern["signature"] for pattern in patterns}) == 3
	assert patterns[2]["shape"] == ".claude/hooks/*"
	assert "chars omitted" in patterns[2]["example"]


@pytest.mark.parametrize(
	"secret",
	[
		"ghp_abcdefghijklmnopqrstuvwxyz0123",
		"github_pat_11ABCDEFG0123456789_abcdefghijkl",
		"sk-abcdefghijklmnop1234",
		"xoxb-1234567890-abcdefgh",
		"AKIAABCDEFGHIJKLMNOP",
		"Bearer abc.def.ghi-1234",
		"0123456789abcdef0123456789abcdef01234567",
	],
)
def test_redaction_masks_token_like_strings(secret):
	assert secret not in pp.redact(f"curl -H 'Authorization: {secret}' x")


def test_redaction_keeps_paths_and_masks_assignments():
	text = pp.redact("gh api repos/shubhodeep1/coding-workflows/issues/comments/5846305455 token=abc123")
	assert "repos/shubhodeep1/coding-workflows/issues/comments/5846305455" in text
	assert "token=***" in text


def test_example_drops_heredoc_bodies_and_truncates(tmp_path):
	body = "\n".join(["line"] * 200)
	directory = _log(tmp_path, [_payload(f"python3 - <<'EOF'\n{body}\nEOF\ngh api x " + "y" * 3000)])
	example = pp.group_patterns(pp.load_records(directory))[0]["example"]
	assert example.startswith("python3 - <<'EOF'\n<heredoc body omitted>\nEOF\ngh api x ")
	assert "\nline\n" not in example
	assert example.endswith("… [truncated]")


def test_unterminated_heredoc_still_hides_the_body():
	# The logger truncates long values, which can cut off the closing delimiter.
	assert pp.strip_heredocs("cat <<EOF\nsecret body", placeholder="<heredoc body omitted>") == "cat <<EOF\n<heredoc body omitted>"


# ──────────────────────────────────────────────────────────────────
# Filing
# ──────────────────────────────────────────────────────────────────


class FakeIssues:
	def __init__(self, existing=None):
		self.existing = existing or []
		self.posts: list[tuple[str, dict]] = []
		self.reads = 0

	def gh_api_list(self, path):
		self.reads += 1
		assert "labels=ai%3Apermission-prompt" in path and "state=all" in path
		return self.existing

	def post(self, path, body):
		self.posts.append((path, body))
		return {"number": 900 + len(self.posts)} if path.endswith("/issues") else {"id": 1}


@pytest.fixture
def issues(monkeypatch):
	def install(existing=None):
		fake = FakeIssues(existing)
		monkeypatch.setattr(pp.check_in_status, "gh_api_list", fake.gh_api_list)
		monkeypatch.setattr(pp, "_post", fake.post)
		return fake

	return install


def test_files_nothing_outside_coding_workflows(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("ls")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="someone/consumer")
	assert code == 0 and "limited to shubhodeep1/coding-workflows" in summary["skipped"]
	assert summary["total"] == 1
	assert fake.reads == 0 and fake.posts == []


def test_new_pattern_opens_a_routed_issue_with_marker(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("gh api -X GET search/issues -f q=a | sort -n", reason="needs approval")])
	code, summary = pp.file_patterns(directory, "session_abc", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["errors"] == []
	path, body = fake.posts[0]
	assert path == "repos/shubhodeep1/coding-workflows/issues"
	assert body["labels"] == ["ai:permission-prompt", "ai:claude"]
	assert body["title"].startswith("[permission-prompt] Bash: gh api -X GET search/issues")
	sig = summary["filed"][0]["signature"]
	assert f"<!-- ai:permission-prompt:v1 sig={sig} -->" in body["body"]
	assert "session `session_abc`" in body["body"] and "needs approval" in body["body"]


def test_issue_guidance_works_without_the_gh_api_guard_and_warns_about_protected_paths():
	body = pp.issue_body({"signature": "0" * 12, "event": "PermissionRequest", "tool_name": "Bash", "shape": "ls", "count": 1, "reasons": [], "first_ts": "t", "last_ts": "t", "example": "ls"}, 1, "s1")
	assert "where the repository has one" in body and "when it exists" in body
	assert "CLAUDE.md §28.C" in body and "Status: BLOCKED" in body


def test_existing_issue_gets_a_comment_even_when_closed(tmp_path, issues):
	directory = _log(tmp_path, [_payload("ls -la")])
	sig = pp.group_patterns(pp.load_records(directory))[0]["signature"]
	fake = issues([{"number": 42, "state": "closed", "body": f"x\n<!-- ai:permission-prompt:v1 sig={sig} -->"}])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0
	assert fake.posts[0][0] == "repos/shubhodeep1/coding-workflows/issues/42/comments"
	assert fake.posts[0][1]["body"].startswith("Seen again.")
	assert summary["commented"] == [{"signature": sig, "issue": 42, "occurrences": 1}]


def test_state_files_only_new_occurrences(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("ls")])
	pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert len(fake.posts) == 1
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and len(fake.posts) == 1 and fake.reads == 1
	logger.append_record(logger.build_record(_payload("ls"), NOW), directory)
	fake.existing = [{"number": 901, "state": "open", "body": fake.posts[0][1]["body"]}]
	pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert fake.posts[-1][0].endswith("/issues/901/comments")
	assert "**Occurrences:** 1 " in fake.posts[-1][1]["body"]


def test_dry_run_posts_nothing_and_keeps_state(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("ls")])
	code, summary = pp.file_patterns(directory, "s1", True, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["dry_run"] is True and len(summary["filed"]) == 1
	assert fake.posts == [] and not (directory / pp.STATE_FILE).exists()


def test_list_read_failure_exits_2(tmp_path, monkeypatch):
	def boom(path):
		raise pp.check_in_status.ReadError("proxy 403")

	monkeypatch.setattr(pp.check_in_status, "gh_api_list", boom)
	directory = _log(tmp_path, [_payload("ls")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 2 and summary["errors"] == ["proxy 403"]


def test_post_payload_file_is_removed_when_serialisation_fails(monkeypatch):
	created = []
	real = pp.tempfile.NamedTemporaryFile

	def tracking(*args, **kwargs):
		handle = real(*args, **kwargs)
		created.append(handle.name)
		return handle

	monkeypatch.setattr(pp.tempfile, "NamedTemporaryFile", tracking)
	monkeypatch.setattr(pp.subprocess, "run", lambda *a, **k: pytest.fail("gh must not run"))
	with pytest.raises(pp.check_in_status.ReadError):
		pp._post("repos/o/r/issues", {"body": object()})
	assert len(created) == 1 and not Path(created[0]).exists()


def test_post_failure_is_listed_and_not_recorded(tmp_path, issues, monkeypatch):
	issues()

	def fail(path, body):
		raise pp.check_in_status.ReadError("POST failed")

	monkeypatch.setattr(pp, "_post", fail)
	directory = _log(tmp_path, [_payload("ls")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["errors"] == ["POST failed"]
	assert not (directory / pp.STATE_FILE).exists()


def test_no_new_occurrences_means_no_api_calls(tmp_path, issues):
	fake = issues()
	code, summary = pp.file_patterns(tmp_path / "empty", "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["total"] == 0 and fake.reads == 0


def test_main_rejects_bad_session_label(capsys):
	assert pp.main(["file", "--session-label", "bad label"]) == 1


def test_extract_repo_slug():
	assert pp.extract_repo_slug("http://local_proxy@127.0.0.1:1/git/shubhodeep1/coding-workflows") == "shubhodeep1/coding-workflows"
	assert pp.extract_repo_slug("https://evilgithub.com/a/b") == ""


# ──────────────────────────────────────────────────────────────────
# Immediate report (report-now, issue #4755)
# ──────────────────────────────────────────────────────────────────

SESSION = "session_01Pqd1mbhdV8mCxriki9onge"
FILING = "shubhodeep1/coding-workflows"


class FakeGitHub:
	"""Fake `gh api` reads and POSTs for report-now and lookup."""

	def __init__(self, issues=None, pulls=None, search=None, comments=None, fail_read=False, fail_post=False, fail_search=False):
		self.issues = issues or []
		self.pulls = pulls or []
		self.search = search or {"items": []}
		self.comments = comments or {}
		self.fail_read = fail_read
		self.fail_post = fail_post
		self.fail_search = fail_search
		self.reads: list[str] = []
		self.posts: list[tuple[str, dict]] = []

	def gh_api_list(self, path):
		self.reads.append(path)
		if self.fail_read:
			raise pp.check_in_status.ReadError("proxy 403")
		if "/pulls?" in path:
			return self.pulls
		if "/comments" in path:
			return self.comments.get(int(path.split("/issues/", 1)[1].split("/", 1)[0]), [])
		return self.issues

	def gh_api(self, path):
		self.reads.append(path)
		if self.fail_read:
			raise pp.check_in_status.ReadError("proxy 403")
		if self.fail_search and path.startswith("search/"):
			raise pp.check_in_status.ReadError("gh: This GitHub API path is not available (HTTP 403)")
		return self.search

	def post(self, path, body):
		if self.fail_post:
			raise pp.check_in_status.ReadError("POST failed")
		self.posts.append((path, body))
		return {"number": 900 + len(self.posts)} if path.endswith("/issues") else {"id": 1}


@pytest.fixture
def github(monkeypatch):
	monkeypatch.delenv(pp.REPORT_NOW_SWITCH_ENV, raising=False)

	def install(slug=FILING, branch="claude/some-branch", **kwargs):
		fake = FakeGitHub(**kwargs)
		monkeypatch.setattr(pp.check_in_status, "gh_api_list", fake.gh_api_list)
		monkeypatch.setattr(pp.check_in_status, "gh_api", fake.gh_api)
		monkeypatch.setattr(pp, "_post", fake.post)
		monkeypatch.setattr(pp, "local_repo_slug", lambda cwd=None: slug)
		monkeypatch.setattr(pp, "_git_output", lambda args, cwd: branch if args[:1] == ["branch"] else "")
		return fake

	return install


def _session_log(tmp_path, payloads):
	directory = _log(tmp_path, payloads)
	return directory / "sess-1.jsonl"


def _report(log_file, session=SESSION):
	return pp.report_now(log_file, "/home/user/coding-workflows", session_label=session, now=NOW)


@pytest.mark.parametrize(
	("session", "payload"),
	[
		("", _payload("ls")),
		(SESSION, _payload("ls", permission_mode="default")),
		(SESSION, _payload("ls", permission_mode="plan")),
		(SESSION, _payload("ls", permission_mode="acceptEdits")),
		(SESSION, _payload("ls", event="PermissionDenied")),
	],
)
def test_report_now_skips_attended_sessions_and_denials(tmp_path, github, session, payload):
	fake = github()
	log_file = _session_log(tmp_path, [payload])
	assert _report(log_file, session).startswith("skipped: not an unattended")
	assert fake.reads == [] and fake.posts == []
	assert not (log_file.parent / pp.IMMEDIATE_STATE_FILE).exists()


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
def test_report_now_opens_a_routed_issue_in_coding_workflows(tmp_path, github, mode):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("gh api -X GET search/issues -f q=a | sort -n", permission_mode=mode, reason="needs approval")])
	pp.write_session_meta(log_file.parent, "implement-issue-claude — #4707", SESSION)
	assert _report(log_file) == "reported: issue #901"
	path, body = fake.posts[0]
	assert path == "repos/shubhodeep1/coding-workflows/issues"
	assert body["labels"] == ["ai:permission-prompt", "ai:claude"]
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	assert f"<!-- ai:permission-prompt:v1 sig={sig} -->" in body["body"]
	assert f"<!-- ai:permission-prompt-session:v1 session={SESSION} sig={sig} -->" in body["body"]
	assert f"https://claude.ai/code/{SESSION}" in body["body"]
	assert "**Session title:** `implement-issue-claude — #4707`" in body["body"]
	assert "- **Event:** PermissionRequest (permission prompt)" in body["body"]
	assert "- **Tool:** `Bash`" in body["body"]
	state = json.loads((log_file.parent / pp.IMMEDIATE_STATE_FILE).read_text())
	bucket = state["sessions"][SESSION]
	assert bucket["count"] == 1 and bucket["reports"][sig]["target"] == "issue #901"
	assert json.loads((log_file.parent / pp.STATE_FILE).read_text()) == {sig: 1}


def test_report_now_comments_on_a_closed_matching_issue(tmp_path, github):
	log_file = _session_log(tmp_path, [_payload("ls -la")])
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	fake = github(issues=[{"number": 42, "state": "closed", "body": f"x\n<!-- ai:permission-prompt:v1 sig={sig} -->"}])
	assert _report(log_file) == "reported: issue #42"
	path, body = fake.posts[0]
	assert path == "repos/shubhodeep1/coding-workflows/issues/42/comments"
	assert body["body"].startswith("Seen again.") and pp.IMMEDIATE_HEADING in body["body"]
	assert "**Session title:** not recorded" in body["body"]


def test_report_now_sanitizes_the_command_and_title(tmp_path, github):
	fake = github()
	secret = "ghp_abcdefghijklmnopqrstuvwxyz0123"
	log_file = _session_log(tmp_path, [_payload(f"curl -H 'Authorization: Bearer {secret}' x && python3 - <<'EOF'\nprint('{secret}')\nEOF")])
	pp.write_session_meta(log_file.parent, f"stage `x` token={secret}\nsecond line", SESSION)
	_report(log_file)
	body = fake.posts[0][1]["body"]
	assert secret not in body
	assert "<heredoc body omitted>" in body and "print(" not in body
	assert "**Session title:** `stage 'x' token=*** second line`" in body


def test_report_now_once_per_signature_per_session(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file).startswith("reported")
	logger.append_record(logger.build_record(_payload("ls"), NOW), log_file.parent)
	assert _report(log_file) == "skipped: already reported"
	assert len(fake.posts) == 1 and len(fake.reads) == 1


def test_report_now_caps_reports_per_session(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [])
	for index in range(pp.MAX_IMMEDIATE_REPORTS + 1):
		logger.append_record(logger.build_record(_payload(f"tool{index} x"), NOW), log_file.parent)
		outcome = _report(log_file)
	assert outcome == "skipped: cap reached"
	assert len(fake.posts) == pp.MAX_IMMEDIATE_REPORTS


def test_file_skips_signatures_already_reported(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	_report(log_file)
	logger.append_record(logger.build_record(_payload("ls"), NOW), log_file.parent)
	reads = len(fake.reads)
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["filed"] == [] and summary["commented"] == []
	assert summary["already_reported"] == [{"signature": pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"], "target": "issue #901"}]
	assert len(fake.reads) == reads and len(fake.posts) == 1


def test_file_still_files_other_patterns_after_a_report(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	_report(log_file)
	logger.append_record(logger.build_record(_payload("rm x", event="PermissionDenied"), NOW), log_file.parent)
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and len(summary["filed"]) == 1 and len(summary["already_reported"]) == 1
	assert len(fake.posts) == 2


def test_report_now_records_the_repository_and_logged_session(tmp_path, github):
	github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	_report(log_file)
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	entry = pp._load_immediate_state(log_file.parent)["sessions"][SESSION]["reports"][sig]
	assert entry["repo"] == FILING and entry["log_session"] == "sess-1"


def test_file_is_not_suppressed_by_a_report_to_another_repository(tmp_path, github):
	# Issue #5125: a report on a consumer PR must not stop coding-workflows from filing the pattern.
	github(slug="someone/consumer", branch="claude/implement-plan-issue-12-fix", pulls=[{"number": 77}])
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "reported: PR #77"
	fake = github()
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["already_reported"] == [] and len(summary["filed"]) == 1
	assert fake.posts[0][0] == "repos/shubhodeep1/coding-workflows/issues"


def test_file_is_not_suppressed_by_another_sessions_report(tmp_path, github):
	# Issue #5125: one session's report covers its own occurrences, never another session's.
	log_file = _session_log(tmp_path, [_payload("ls")])
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	fake = github(issues=[{"number": 42, "state": "open", "body": f"x\n<!-- ai:permission-prompt:v1 sig={sig} -->"}])
	assert _report(log_file) == "reported: issue #42"
	logger.append_record(logger.build_record(_payload("ls"), NOW), log_file.parent)
	logger.append_record(logger.build_record(_payload("ls", session_id="sess-2"), NOW), log_file.parent)
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["already_reported"] == []
	# Only sess-2's occurrence is new: sess-1's later one is covered by its own report.
	assert summary["commented"] == [{"signature": sig, "issue": 42, "occurrences": 1}]
	assert len(fake.posts) == 2
	# Nothing is new afterwards, so a second run posts nothing.
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["commented"] == [] and summary["filed"] == [] and len(fake.posts) == 2


def test_file_is_not_suppressed_by_another_sessions_earlier_occurrence(tmp_path, github):
	# Issue #5125: sess-2's occurrence is already in the shared log when sess-1 reports; the
	# report marks only sess-1's occurrences as filed, so `file` still files sess-2's.
	log_file = _session_log(tmp_path, [_payload("ls")])
	logger.append_record(logger.build_record(_payload("ls", session_id="sess-2"), NOW), log_file.parent)
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	fake = github(issues=[{"number": 42, "state": "open", "body": f"x\n<!-- ai:permission-prompt:v1 sig={sig} -->"}])
	assert _report(log_file) == "reported: issue #42"
	assert pp._load_state(log_file.parent)[sig] == 1
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["already_reported"] == []
	assert summary["commented"] == [{"signature": sig, "issue": 42, "occurrences": 1}]
	assert len(fake.posts) == 2
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["commented"] == [] and summary["filed"] == [] and len(fake.posts) == 2


def test_file_ignores_reports_without_a_repository_or_logged_session(tmp_path, github):
	# Issue #5125: entries written before `repo` / `log_session` existed never suppress filing.
	github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	for legacy in ({"target": "PR #77"}, {"target": "issue #5", "repo": FILING}, {"target": "issue #5", "log_session": "sess-1"}):
		pp._save_immediate_state(log_file.parent, {"sessions": {SESSION: {"reports": {sig: legacy}, "count": 1}}})
		assert pp._delivered_reports(log_file.parent, FILING) == {}
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["already_reported"] == [] and len(summary["filed"]) == 1


def test_delivered_reports_match_the_repository_case_insensitively(tmp_path):
	directory = tmp_path / "log"
	entry = {"target": "issue #9", "repo": "Shubhodeep1/Coding-Workflows", "log_session": "sess-1"}
	pp._save_immediate_state(directory, {"sessions": {SESSION: {"reports": {"abc123abc123": entry}, "count": 1}}})
	assert pp._delivered_reports(directory, FILING) == {"abc123abc123": {"target": "issue #9", "log_sessions": {"sess-1"}}}
	assert pp._delivered_reports(directory, "someone/consumer") == {}


@pytest.mark.parametrize(
	("failure", "kwargs"),
	[("read", {"fail_read": True}), ("post", {"fail_post": True})],
)
def test_report_now_fails_open_and_records_nothing(tmp_path, github, failure, kwargs):
	github(**kwargs)
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file).startswith("error:")
	# The reservation made before the read and POST is released again.
	assert pp._load_immediate_state(log_file.parent)["sessions"][SESSION] == {"reports": {}, "count": 0}
	assert not (log_file.parent / pp.STATE_FILE).exists()
	fake = github()
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and len(summary["filed"]) == 1 and len(fake.posts) == 1


def test_report_now_unknown_repository_posts_nothing(tmp_path, github):
	fake = github(slug="")
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "skipped: unknown repository"
	assert fake.reads == [] and fake.posts == []


def test_report_now_elsewhere_prefers_the_open_pr(tmp_path, github):
	fake = github(slug="someone/consumer", branch="claude/implement-plan-issue-12-fix", pulls=[{"number": 77}])
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "reported: PR #77"
	assert fake.reads == ["repos/someone/consumer/pulls?state=open&head=someone%3Aclaude%2Fimplement-plan-issue-12-fix"]
	path, body = fake.posts[0]
	assert path == "repos/someone/consumer/issues/77/comments"
	assert "pull request" in body["body"] and "ai:permission-prompt-session:v1" in body["body"]
	assert not (log_file.parent / pp.STATE_FILE).exists()


def test_report_now_elsewhere_falls_back_to_the_issue_branch(tmp_path, github):
	fake = github(slug="someone/consumer", branch="claude/implement-plan-issue-12-fix-phase-1")
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "reported: issue #12"
	assert fake.posts[0][0] == "repos/someone/consumer/issues/12/comments"


def test_report_now_elsewhere_without_a_target_posts_nothing(tmp_path, github):
	fake = github(slug="someone/consumer", branch="feature/x")
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "skipped: no target"
	assert fake.posts == [] and pp._load_immediate_state(log_file.parent)["sessions"][SESSION] == {"reports": {}, "count": 0}


def test_report_now_main_prints_nothing_and_exits_0(tmp_path, capsys, monkeypatch):
	monkeypatch.delenv(pp.REMOTE_SESSION_ENV, raising=False)
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert pp.main(["report-now", "--log-file", str(log_file), "--cwd", str(tmp_path)]) == 0
	assert pp.main(["report-now", "--log-file", str(tmp_path / "missing.jsonl")]) == 0
	assert capsys.readouterr().out == ""


def _digest(record):
	return logger.record_digest(record)


def test_hook_digest_matches_the_line_it_wrote(tmp_path):
	record = logger.build_record(_payload("ls é"), NOW)
	path = logger.append_record(record, tmp_path / "log")
	line = path.read_text(encoding="utf-8").splitlines()[-1]
	assert _digest(record) == __import__("hashlib").sha256(line.encode("utf-8")).hexdigest()


def test_report_now_reports_the_record_that_spawned_it(tmp_path, github):
	# Review round 1: a later prompt logged before the child starts must not replace the one that spawned it.
	fake = github()
	log_file = _session_log(tmp_path, [])
	first = logger.build_record(_payload("ls first"), NOW)
	logger.append_record(first, log_file.parent)
	logger.append_record(logger.build_record(_payload("cat later", tool="Bash"), NOW), log_file.parent)
	assert pp.report_now(log_file, "/x", session_label=SESSION, now=NOW, record_digest=_digest(first)).startswith("reported")
	assert "ls first" in fake.posts[0][1]["body"] and "cat later" not in fake.posts[0][1]["body"]


def test_unicode_line_separators_stay_in_one_record(tmp_path, github):
	# U+2028 / U+0085 are not escaped by json.dumps(ensure_ascii=False); splitlines() would cut the record.
	fake = github()
	log_file = _session_log(tmp_path, [])
	record = logger.build_record(_payload("echo 'a\u2028b\x85c'"), NOW)
	logger.append_record(record, log_file.parent)
	assert len(pp.load_records(log_file.parent)) == 1
	assert pp._last_record(log_file) == record
	assert pp.report_now(log_file, "/x", session_label=SESSION, now=NOW, record_digest=_digest(record)).startswith("reported")
	assert len(fake.posts) == 1


def test_report_now_skips_a_bad_or_unknown_digest(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert pp.report_now(log_file, "/x", session_label=SESSION, now=NOW, record_digest="zz") == "skipped: invalid record digest"
	assert pp.report_now(log_file, "/x", session_label=SESSION, now=NOW, record_digest="0" * 64).startswith("skipped: not an unattended")
	assert fake.reads == [] and fake.posts == []


def test_report_now_main_passes_the_digest(tmp_path, monkeypatch):
	seen = {}
	monkeypatch.setattr(pp, "report_now", lambda log_file, cwd, **kwargs: seen.update(kwargs) or "x")
	assert pp.main(["report-now", "--log-file", str(tmp_path / "a.jsonl"), "--record-sha256", "a" * 64]) == 0
	assert seen == {"record_digest": "a" * 64}


def test_report_now_state_is_per_session(tmp_path, github):
	# Review round 1: one session's report or cap never suppresses another session's report.
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	other = "session_01OTHER"
	assert _report(log_file).startswith("reported")
	assert _report(log_file, other).startswith("reported")
	assert len(fake.posts) == 2
	state = json.loads((log_file.parent / pp.IMMEDIATE_STATE_FILE).read_text())
	assert set(state["sessions"]) == {SESSION, other}
	for index in range(pp.MAX_IMMEDIATE_REPORTS):
		logger.append_record(logger.build_record(_payload(f"tool{index} x"), NOW), log_file.parent)
		outcome = _report(log_file)
	# SESSION used its 5 reports (ls, tool0-tool3); `other` has used 1 and still reports.
	assert outcome == "skipped: cap reached"
	assert _report(log_file, other).startswith("reported")
	assert len(fake.posts) == pp.MAX_IMMEDIATE_REPORTS + 2


def test_report_now_reserves_before_posting(tmp_path, github, monkeypatch):
	# Review round 1: a state write failing after a successful POST must not lead to a second report.
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	real_save = pp._save_immediate_state
	calls = []

	def save(log_dir, state):
		calls.append(json.loads(json.dumps(state)))
		if len(calls) == 2:
			raise OSError("disk full")
		real_save(log_dir, state)

	monkeypatch.setattr(pp, "_save_immediate_state", save)
	assert _report(log_file).startswith("error:")
	sig = pp.group_patterns(pp.load_records(log_file.parent))[0]["signature"]
	assert calls[0]["sessions"][SESSION]["reports"][sig]["target"] == pp.PENDING_TARGET
	assert len(fake.posts) == 1
	monkeypatch.setattr(pp, "_save_immediate_state", real_save)
	assert _report(log_file) == "skipped: already reported"
	assert len(fake.posts) == 1
	# A reservation left `pending` is not "reported" for `file`, which still files the pattern.
	code, summary = pp.file_patterns(log_file.parent, "s1", False, slug=FILING)
	assert code == 0 and summary["already_reported"] == [] and len(summary["filed"]) == 1


def test_report_now_posts_nothing_when_the_reservation_cannot_be_saved(tmp_path, github, monkeypatch):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])

	def save(log_dir, state):
		raise OSError("read-only")

	monkeypatch.setattr(pp, "_save_immediate_state", save)
	assert _report(log_file).startswith("error:")
	assert fake.posts == [] and fake.reads == []


def test_report_now_releases_the_reservation_after_a_failed_post(tmp_path, github):
	github(fail_post=True)
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file).startswith("error:")
	state = json.loads((log_file.parent / pp.IMMEDIATE_STATE_FILE).read_text())
	assert state["sessions"][SESSION] == {"reports": {}, "count": 0}
	fake = github()
	assert _report(log_file).startswith("reported")
	assert len(fake.posts) == 1


@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_report_now_kill_switch(tmp_path, github, monkeypatch, value):
	fake = github()
	monkeypatch.setenv(pp.REPORT_NOW_SWITCH_ENV, value)
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "skipped: switched off"
	assert fake.reads == [] and fake.posts == []
	monkeypatch.setenv(pp.REPORT_NOW_SWITCH_ENV, "on")
	assert _report(log_file).startswith("reported")


def test_report_now_elsewhere_matches_a_bare_issue_branch(tmp_path, github):
	fake = github(slug="someone/consumer", branch="claude/implement-plan-issue-12")
	log_file = _session_log(tmp_path, [_payload("ls")])
	assert _report(log_file) == "reported: issue #12"
	assert fake.posts[0][0] == "repos/someone/consumer/issues/12/comments"
	assert pp._ISSUE_BRANCH_RE.match("claude/implement-plan-issue-12x") is None


def test_report_now_ignores_another_sessions_title(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("ls")])
	pp.write_session_meta(log_file.parent, "someone else's stage", "session_01OTHER")
	_report(log_file)
	assert "**Session title:** not recorded" in fake.posts[0][1]["body"]


def test_filing_lock_excludes_another_process(tmp_path):
	# Review round 1: flock on separately opened descriptions of one file does exclude another process.
	script = (
		"import fcntl, sys\n"
		"with open(sys.argv[1], 'a') as h:\n"
		"\ttry:\n"
		"\t\tfcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
		"\t\tprint('acquired')\n"
		"\texcept BlockingIOError:\n"
		"\t\tprint('blocked')\n"
	)
	with pp._filing_lock(tmp_path):
		out = subprocess.run([sys.executable, "-c", script, str(tmp_path / pp.LOCK_FILE)], capture_output=True, text=True, check=True).stdout
	assert out.strip() == "blocked"


@pytest.mark.parametrize(
	("raw", "label"),
	[("cse_01ABC", "session_01ABC"), ("session_01ABC", "session_01ABC"), ("01ABC", "session_01ABC"), ("", ""), ("bad id", ""), ("../x", "")],
)
def test_session_label_normalization(monkeypatch, raw, label):
	monkeypatch.setenv(pp.REMOTE_SESSION_ENV, raw)
	assert pp.session_label_from_env() == label


def test_session_meta_writes_the_title(tmp_path, capsys, monkeypatch):
	monkeypatch.setenv(pp.REMOTE_SESSION_ENV, "cse_01ABC")
	assert pp.main(["session-meta", "--title", "implement-plan x — phase 1/1", "--log-dir", str(tmp_path / "log")]) == 0
	assert json.loads(capsys.readouterr().out) == {"ok": True}
	assert pp.read_session_title(tmp_path / "log", "session_01ABC") == "implement-plan x — phase 1/1"
	# Another session sharing the log directory never shows this title (review round 1).
	assert pp.read_session_title(tmp_path / "log", "session_01OTHER") == ""


def test_session_meta_keeps_each_sessions_title(tmp_path):
	# Review round 2: a second session's session-meta never overwrites the first session's title.
	log_dir = tmp_path / "log"
	pp.write_session_meta(log_dir, "stage A", "session_01A")
	pp.write_session_meta(log_dir, "stage B", "session_01B")
	pp.write_session_meta(log_dir, "stage A — renamed", "session_01A")
	assert pp.read_session_title(log_dir, "session_01A") == "stage A — renamed"
	assert pp.read_session_title(log_dir, "session_01B") == "stage B"
	assert json.loads((log_dir / pp.SESSION_META_FILE).read_text(encoding="utf-8")) == {
		"sessions": {"session_01A": "stage A — renamed", "session_01B": "stage B"}
	}


def test_session_meta_ignores_an_invalid_file(tmp_path):
	log_dir = tmp_path / "log"
	log_dir.mkdir()
	(log_dir / pp.SESSION_META_FILE).write_text("[1, 2]", encoding="utf-8")
	assert pp.read_session_title(log_dir, "session_01A") == ""
	pp.write_session_meta(log_dir, "stage A", "session_01A")
	assert pp.read_session_title(log_dir, "session_01A") == "stage A"


def test_session_meta_never_fails(tmp_path, capsys):
	blocker = tmp_path / "file"
	blocker.write_text("x")
	assert pp.main(["session-meta", "--title", "t", "--log-dir", str(blocker / "log")]) == 0
	assert json.loads(capsys.readouterr().out)["ok"] is False


def _reported_body(tmp_path, github):
	fake = github()
	log_file = _session_log(tmp_path, [_payload("gh api repos/o/r/issues --jq '.[]'")])
	pp.write_session_meta(log_file.parent, "implement-issue-claude — #4707", SESSION)
	_report(log_file)
	return fake.posts[0][1]["body"]


def test_lookup_finds_the_report_in_an_issue_body(tmp_path, github):
	body = _reported_body(tmp_path, github)
	fake = github(search={"items": [{"number": 901, "html_url": "https://github.com/x/y/issues/901", "author_association": "OWNER", "body": body}]})
	result = pp.lookup(SESSION, FILING)
	assert result["found"] is True and result["issue_url"] == "https://github.com/x/y/issues/901"
	assert result["command"] == "gh api repos/o/r/issues --jq '.[]'"
	assert result["title"] == "implement-issue-claude — #4707"
	assert result["event"] == "PermissionRequest" and result["tool_name"] == "Bash"
	assert len(fake.reads) == 1 and "search/issues?q=repo%3Ashubhodeep1%2Fcoding-workflows%20%22" in fake.reads[0]


def test_lookup_finds_the_newest_trusted_comment(tmp_path, github):
	body = _reported_body(tmp_path, github)
	comments = {
		42: [
			{"html_url": "c1", "author_association": "OWNER", "body": body},
			{"html_url": "c2", "author_association": "NONE", "body": body.replace("--jq", "--forged")},
		]
	}
	fake = github(search={"items": [{"number": 42, "html_url": "i42", "author_association": "NONE", "body": body}]}, comments=comments)
	result = pp.lookup(SESSION, FILING)
	assert result["found"] is True and result["comment_url"] == "c1" and "--forged" not in result["command"]
	assert len(fake.reads) == 2


def test_lookup_falls_back_to_the_pattern_issues_when_search_is_refused(tmp_path, github):
	# Claude Code Web's agent proxy answers search/issues with HTTP 403, and the poller runs there.
	body = _reported_body(tmp_path, github)
	issues = [
		{"number": 7, "html_url": "i7", "author_association": "OWNER", "body": "another pattern"},
		{"number": 901, "html_url": "i901", "author_association": "OWNER", "body": body},
	]
	fake = github(fail_search=True, issues=issues)
	result = pp.lookup(SESSION, FILING)
	assert result["found"] is True and result["issue_url"] == "i901" and result["comment_url"] == ""
	assert result["command"] == "gh api repos/o/r/issues --jq '.[]'"
	assert fake.reads[0].startswith("search/issues?")
	assert fake.reads[1] == f"repos/{FILING}/issues?labels=ai%3Apermission-prompt&state=all&sort=updated&direction=desc"
	# Issue 7's comments are read before 901's body matches; nothing past the match.
	assert fake.reads[2:] == [f"repos/{FILING}/issues/7/comments"]


def test_lookup_fallback_checks_comments_and_caps_the_hits(tmp_path, github):
	body = _reported_body(tmp_path, github)
	issues = [{"number": n, "html_url": f"i{n}", "author_association": "OWNER", "body": "other"} for n in (1, 2, 3, 4)]
	fake = github(fail_search=True, issues=issues, comments={4: [{"html_url": "c4", "author_association": "OWNER", "body": body}]})
	# Issue 4 is past LOOKUP_MAX_HITS, so its report is not read.
	assert pp.lookup(SESSION, FILING) == {"found": False, "session": SESSION}
	assert len(fake.reads) == 2 + pp.LOOKUP_MAX_HITS
	fake = github(fail_search=True, issues=issues[:1], comments={1: [{"html_url": "c1", "author_association": "OWNER", "body": body}]})
	result = pp.lookup(SESSION, FILING)
	assert result["found"] is True and result["issue_url"] == "i1" and result["comment_url"] == "c1"


def test_lookup_not_found_and_read_failure(tmp_path, github, capsys):
	github()
	assert pp.lookup(SESSION, FILING) == {"found": False, "session": SESSION}
	github(fail_read=True)
	assert pp.main(["lookup", "--session", "cse_01Pqd1mbhdV8mCxriki9onge", "--repo", FILING]) == 2
	assert json.loads(capsys.readouterr().out)["error"] == "proxy 403"
	assert pp.main(["lookup", "--session", "bad id", "--repo", FILING]) == 1


# ──────────────────────────────────────────────────────────────────
# Wiring and docs
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", SETTINGS_PATHS)
def test_settings_wire_the_logger_on_both_events(path):
	settings = json.loads(path.read_text(encoding="utf-8"))
	for event in logger.SETTINGS_EVENTS:
		entries = settings["hooks"][event]
		commands = [hook["command"] for entry in entries for hook in entry["hooks"]]
		assert commands == ['python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/permission_prompt_logger.py']
	assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py *)" in settings["permissions"]["allow"]


def test_template_parity():
	assert TEMPLATE_HOOK_PATH.read_text(encoding="utf-8") == HOOK_PATH.read_text(encoding="utf-8")
	assert TEMPLATE_SCRIPT_PATH.read_text(encoding="utf-8") == SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["implement-plan-claude.md", "implement-issue-claude.md"])
def test_commands_record_the_session_title(name):
	for root in (REPO_ROOT / ".claude", REPO_ROOT / "workflow-templates" / ".claude"):
		text = (root / "commands" / name).read_text(encoding="utf-8")
		assert 'permission_prompts.py session-meta --title "<title from get_session>"' in text


def test_label_is_in_the_contract():
	labels = json.loads(LABEL_CONTRACT.read_text(encoding="utf-8"))["labels"]
	assert pp.LABEL in labels and pp.ROUTE_LABEL in labels


def test_claude_md_documents_the_reports():
	text = CLAUDE_MD.read_text(encoding="utf-8")
	assert "### I) Permission Prompt Reports" in text
	assert ".claude/hooks/permission_prompt_logger.py" in text
	assert "tests/test_permission_prompts.py" in text


def test_plan_command_runs_the_report_every_stage():
	text = PLAN_COMMAND.read_text(encoding="utf-8")
	assert "14. **Report.** First run the [permission prompt report](#permission-prompt-report)." in text
	assert "permission_prompts.py file --session-label <your session id>" in text


def test_ci_runs_the_helper_tests():
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	for name in ("tests/test_permission_prompts.py", "tests/test_dispatch_workflow.py", "tests/test_edit_comment.py"):
		assert name in text
