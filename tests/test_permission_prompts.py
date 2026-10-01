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
pp = _load("permission_prompts", SCRIPT_PATH)

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


def _log(tmp_path, payloads):
	directory = tmp_path / "log"
	for payload in payloads:
		logger.append_record(logger.build_record(payload, NOW), directory)
	return directory


# ──────────────────────────────────────────────────────────────────
# Logger hook
# ──────────────────────────────────────────────────────────────────


def test_hook_logs_a_prompt_and_prints_nothing(tmp_path):
	env = dict(os.environ, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
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
	env = dict(os.environ, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run([sys.executable, str(HOOK_PATH)], input=stdin_text, capture_output=True, text=True, env=env, check=False)
	assert result.returncode == 0 and result.stdout == ""


def test_hook_swallows_write_errors(tmp_path):
	blocker = tmp_path / ".claude"
	blocker.write_text("not a directory")
	env = dict(os.environ, HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run([sys.executable, str(HOOK_PATH)], input=json.dumps(_payload("ls")), capture_output=True, text=True, env=env, check=False)
	assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


def test_hook_session_id_cannot_escape_the_log_dir(tmp_path):
	path = logger.append_record(logger.build_record(_payload("ls", session_id="../../etc/passwd"), NOW), tmp_path / "log")
	assert path.parent == tmp_path / "log"


def test_hook_has_no_api_calls_or_env_reads():
	source = HOOK_PATH.read_text(encoding="utf-8")
	for forbidden in ("os.environ", "getenv", "subprocess", "urllib", "api.github.com"):
		assert forbidden not in source


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


def test_expected_guard_denies_are_counted_not_filed(tmp_path, issues):
	# `.claude/hooks/inline_edit_guard.py` denies inline-interpreter edits on
	# purpose (issue #4858). Its records, and a PermissionDenied carrying its
	# redirect reason without the `source` field, are expected: counted, never filed.
	fake = issues()
	directory = tmp_path / "log"
	guard_payload = _payload("sed -i s/a/b/ f", event="PermissionDenied", reason="Edit files with the Edit tool (exact old_string/new_string) or the Write tool, not an inline interpreter.")
	guard_record = logger.build_record(guard_payload, NOW)
	guard_record["source"] = "inline_edit_guard"
	logger.append_record(guard_record, directory)
	logger.append_record(logger.build_record(_payload("perl -pi -e x f", event="PermissionDenied", reason=guard_payload["reason"]), NOW), directory)
	logger.append_record(logger.build_record(_payload("gh api -X GET search/issues -f q=a | sort -n", reason="needs approval"), NOW), directory)
	summary = pp.report(directory)
	assert summary["expected_denies"] == 2
	assert summary["total"] == 1 and [pattern["event"] for pattern in summary["patterns"]] == ["PermissionRequest"]
	code, filed = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and filed["expected_denies"] == 2 and len(filed["filed"]) == 1
	assert len(fake.posts) == 1 and "sed -i" not in fake.posts[0][1]["body"]


def test_only_guard_denies_means_no_api_calls(tmp_path, issues):
	fake = issues()
	directory = tmp_path / "log"
	record = logger.build_record(_payload("sed -i s/a/b/ f", event="PermissionDenied", reason="guard"), NOW)
	record["source"] = "inline_edit_guard"
	logger.append_record(record, directory)
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["total"] == 0 and summary["expected_denies"] == 1
	assert fake.reads == 0 and fake.posts == []


def test_other_denials_with_a_source_are_still_filed():
	assert not pp.is_expected_deny({"event": "PermissionDenied", "source": "something_else", "reason": "blocked"})
	assert not pp.is_expected_deny({"event": "PermissionRequest", "reason": "Edit files with the Edit tool (exact old_string/new_string) or the Write tool"})
	# A prompt is always filed, even one carrying the guard's source (final PR #4877 review round 4).
	assert not pp.is_expected_deny({"event": "PermissionRequest", "source": "inline_edit_guard", "reason": "needs approval"})
	assert not pp.is_expected_deny({"source": "inline_edit_guard"})


def test_main_rejects_bad_session_label(capsys):
	assert pp.main(["file", "--session-label", "bad label"]) == 1


def test_extract_repo_slug():
	assert pp.extract_repo_slug("http://local_proxy@127.0.0.1:1/git/shubhodeep1/coding-workflows") == "shubhodeep1/coding-workflows"
	assert pp.extract_repo_slug("https://evilgithub.com/a/b") == ""


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
