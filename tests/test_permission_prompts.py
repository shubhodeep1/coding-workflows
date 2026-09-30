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
from datetime import datetime, timedelta, timezone
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


OUTAGE_REASON = "Classifier unavailable"


@pytest.mark.parametrize(
	"reason",
	[
		"Classifier unavailable",
		"classifier is unavailable, try again",
		"Auto-mode classifier error",
		"classifier timed out",
		"The classifier returned no verdict",
		"Classifier did not return a verdict",
		"No verdict from the Auto-mode classifier",
	],
)
def test_classifier_outage_reasons_are_recognised(reason):
	assert pp.is_classifier_outage({"event": "PermissionDenied", "reason": reason})


@pytest.mark.parametrize(
	"record",
	[
		{"event": "PermissionDenied", "reason": "[Self-Modification] edits .claude/settings.json"},
		{"event": "PermissionDenied", "reason": "The classifier blocked a force push to main"},
		{"event": "PermissionDenied", "reason": ""},
		{"event": "PermissionRequest", "reason": "Classifier unavailable"},
		# A no-verdict phrase without the word "classifier" is a real denial (PR #4821 review round 1).
		{"event": "PermissionDenied", "reason": "The policy did not return a verdict"},
		{"event": "PermissionDenied", "reason": "The reviewer could not reach a verdict due to policy restrictions"},
		{"event": "PermissionDenied", "reason": "no verdict"},
		# The no-verdict phrase and "classifier" must share a line (PR #4821 review round 1 on 198cd19).
		{"event": "PermissionDenied", "reason": "Rule 12 denies this push; no verdict needed.\nclassifier: skipped"},
		{"event": "PermissionDenied", "reason": "classifier debug: rule loaded\nThe policy did not return a verdict"},
	],
)
def test_real_denials_and_prompts_are_not_outages(record):
	assert not pp.is_classifier_outage(record)


def test_outage_only_log_files_nothing_and_calls_no_api(tmp_path, issues):
	fake = issues()
	payloads = [
		_payload(event="PermissionDenied", tool="mcp__srv__get_session", tool_input={}, reason=OUTAGE_REASON),
		_payload("git status", event="PermissionDenied", reason=OUTAGE_REASON),
	]
	directory = _log(tmp_path, payloads)
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["total"] == 0 and summary["patterns"] == []
	assert summary["filed"] == [] and summary["commented"] == []
	assert summary["outage_denials"] == {
		"label": "classifier outage",
		"count": 2,
		"tools": ["mcp__srv__get_session", "Bash"],
		"first_ts": "2026-09-27T03:00:00Z",
		"last_ts": "2026-09-27T03:00:00Z",
	}
	assert fake.reads == 0 and fake.posts == []
	assert not (directory / pp.STATE_FILE).exists()


def test_mixed_log_files_only_the_real_pattern(tmp_path, issues):
	fake = issues()
	payloads = [
		_payload("git fetch origin main", event="PermissionDenied", reason=OUTAGE_REASON),
		_payload("rm -rf build", event="PermissionDenied", reason="destructive command"),
	]
	directory = _log(tmp_path, payloads)
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["total"] == 1 and len(summary["filed"]) == 1
	assert len(fake.posts) == 1 and "rm" in fake.posts[0][1]["title"]
	assert summary["outage_denials"]["count"] == 1
	state = json.loads((directory / pp.STATE_FILE).read_text())
	assert state["version"] == 2 and list(state["filed"]) == [summary["filed"][0]["signature"]]
	# Only the real record is recorded as filed; the outage record never is.
	assert state["filed"][summary["filed"][0]["signature"]] == ["sess-1.jsonl:1"]


# ──────────────────────────────────────────────────────────────────
# Filed state: per-record keys, legacy count migration (issue #5012)
# ──────────────────────────────────────────────────────────────────


REAL_DENIAL_REASON = "blocked by the Auto-mode classifier: destructive"


def _git_status_denial(reason):
	return _payload("git status", event="PermissionDenied", reason=reason)


def _git_status_signature():
	return pp.signature("PermissionDenied", "Bash", pp.command_shape("git status"))


def _write_legacy_state(directory, state):
	(directory / pp.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")


def _existing_issue(sig, number=901):
	return [{"number": number, "state": "open", "body": pp.MARKER_TEMPLATE.format(sig=sig)}]


def test_legacy_count_that_included_outages_does_not_hide_a_new_real_denial(tmp_path, issues):
	# Issue #5012: an older filer grouped the outage record into the pattern and
	# saved count 2. The outage is now filtered, so the real count is 2 only once
	# a new real denial arrives, and `count - saved` hid it.
	sig = _git_status_signature()
	fake = issues(_existing_issue(sig))
	directory = _log(tmp_path, [_git_status_denial(OUTAGE_REASON), _git_status_denial(REAL_DENIAL_REASON)])
	_write_legacy_state(directory, {sig: 2})
	logger.append_record(logger.build_record(_git_status_denial(REAL_DENIAL_REASON), NOW), directory)
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["errors"] == []
	assert summary["commented"] == [{"signature": sig, "issue": 901, "occurrences": 1}]
	assert fake.posts[0][0].endswith("/issues/901/comments")
	assert "**Occurrences:** 1 " in fake.posts[0][1]["body"]
	state = json.loads((directory / pp.STATE_FILE).read_text())
	assert state == {"version": 2, "filed": {sig: ["sess-1.jsonl:1", "sess-1.jsonl:2"]}}


def test_legacy_count_with_outages_arriving_after_the_last_filing(tmp_path, issues):
	# Saved count 1 covered only the first (real) record; the outage came later
	# and was never counted. The new real denial after it is still filed once.
	sig = _git_status_signature()
	fake = issues(_existing_issue(sig))
	directory = _log(tmp_path, [_git_status_denial(REAL_DENIAL_REASON), _git_status_denial(OUTAGE_REASON)])
	_write_legacy_state(directory, {sig: 1})
	logger.append_record(logger.build_record(_git_status_denial(REAL_DENIAL_REASON), NOW), directory)
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["commented"] == [{"signature": sig, "issue": 901, "occurrences": 1}]
	assert len(fake.posts) == 1


def test_legacy_count_already_covering_every_real_record_files_nothing(tmp_path, issues):
	sig = _git_status_signature()
	fake = issues(_existing_issue(sig))
	directory = _log(tmp_path, [_git_status_denial(OUTAGE_REASON), _git_status_denial(REAL_DENIAL_REASON)])
	_write_legacy_state(directory, {sig: 2})
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["filed"] == [] and summary["commented"] == []
	assert fake.reads == 0 and fake.posts == []
	# Nothing was posted, so the legacy file is left as it was.
	assert json.loads((directory / pp.STATE_FILE).read_text()) == {sig: 2}


def test_legacy_migration_takes_its_prefix_in_logging_order_not_file_name_order(tmp_path):
	# PR #5028 review round 1: a session log created after the last legacy filing
	# can sort before the older log. Its records were never counted, so the legacy
	# prefix follows the record `ts`, not the file name. Reads the
	# workflow-templates/ twin, which carries the fix before the `.claude/` sync.
	twin = _load("permission_prompts_twin", TEMPLATE_SCRIPT_PATH)
	sig = _git_status_signature()
	directory = tmp_path / "log"
	for _ in range(2):
		logger.append_record(logger.build_record(dict(_git_status_denial(OUTAGE_REASON), session_id="sess-b"), NOW), directory)
	for _ in range(2):
		logger.append_record(logger.build_record(dict(_git_status_denial(REAL_DENIAL_REASON), session_id="sess-a"), NOW + timedelta(hours=1)), directory)
	keyed_records = twin.load_keyed_records(directory)
	assert [key for key, _ in keyed_records] == ["sess-a.jsonl:0", "sess-a.jsonl:1", "sess-b.jsonl:0", "sess-b.jsonl:1"]
	# The legacy count 2 covered sess-b's two outages; sess-a's real denials stay unfiled.
	assert twin._migrate_legacy_counts({sig: 2}, keyed_records) == {}
	assert twin._migrate_legacy_counts({sig: 3}, keyed_records) == {sig: {"sess-a.jsonl:0"}}


def test_legacy_migration_never_lets_a_same_second_real_denial_take_an_outage_count(tmp_path):
	# PR #5028 review round (head c501b60): `ts` has whole-second precision, so a
	# real denial in a log whose name sorts first can tie an older outage in
	# another log. The cross-file order of a tie is unknown, so the real record
	# counts as filed only when the count covers it whichever file came first.
	# Within one log file the line order is exact and still decides. Reads the
	# workflow-templates/ twin, which carries the fix before the `.claude/` sync.
	twin = _load("permission_prompts_twin", TEMPLATE_SCRIPT_PATH)
	sig = _git_status_signature()
	directory = tmp_path / "log"
	logger.append_record(logger.build_record(dict(_git_status_denial(OUTAGE_REASON), session_id="sess-b"), NOW), directory)
	logger.append_record(logger.build_record(dict(_git_status_denial(REAL_DENIAL_REASON), session_id="sess-a"), NOW), directory)
	keyed_records = twin.load_keyed_records(directory)
	assert [key for key, _ in keyed_records] == ["sess-a.jsonl:0", "sess-b.jsonl:0"]
	# Count 1 may have covered only the outage: the real denial stays unfiled.
	assert twin._migrate_legacy_counts({sig: 1}, keyed_records) == {}
	# Count 2 covered both records whatever their order.
	assert twin._migrate_legacy_counts({sig: 2}, keyed_records) == {sig: {"sess-a.jsonl:0"}}
	# Same second, one file: the line order is the logging order.
	one_file = tmp_path / "one-file"
	logger.append_record(logger.build_record(_git_status_denial(REAL_DENIAL_REASON), NOW), one_file)
	logger.append_record(logger.build_record(_git_status_denial(OUTAGE_REASON), NOW), one_file)
	one_file_records = twin.load_keyed_records(one_file)
	assert twin._migrate_legacy_counts({sig: 1}, one_file_records) == {sig: {"sess-1.jsonl:0"}}
	# A later-second group is reached only after the earlier count is used up.
	logger.append_record(logger.build_record(_git_status_denial(REAL_DENIAL_REASON), NOW + timedelta(seconds=1)), one_file)
	assert twin._migrate_legacy_counts({sig: 2}, twin.load_keyed_records(one_file)) == {sig: {"sess-1.jsonl:0"}}
	assert twin._migrate_legacy_counts({sig: 3}, twin.load_keyed_records(one_file)) == {sig: {"sess-1.jsonl:0", "sess-1.jsonl:2"}}


def test_v2_state_files_each_record_once(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("ls"), _payload("ls")])
	pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	sig = pp.signature("PermissionRequest", "Bash", pp.command_shape("ls"))
	state = json.loads((directory / pp.STATE_FILE).read_text())
	assert state == {"version": 2, "filed": {sig: ["sess-1.jsonl:0", "sess-1.jsonl:1"]}}
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and len(fake.posts) == 1 and summary["filed"] == [] and summary["commented"] == []
	logger.append_record(logger.build_record(_payload("ls"), NOW), directory)
	fake.existing = [{"number": 901, "state": "open", "body": fake.posts[0][1]["body"]}]
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert summary["commented"] == [{"signature": sig, "issue": 901, "occurrences": 1}]


def test_record_keys_skip_invalid_lines_without_shifting(tmp_path):
	directory = _log(tmp_path, [_payload("ls")])
	with (directory / "sess-1.jsonl").open("a", encoding="utf-8") as handle:
		handle.write("not json\n")
	logger.append_record(logger.build_record(_payload("pwd"), NOW), directory)
	keyed = pp.load_keyed_records(directory)
	assert [key for key, _ in keyed] == ["sess-1.jsonl:0", "sess-1.jsonl:2"]
	assert [record for _, record in keyed] == pp.load_records(directory)


@pytest.mark.parametrize(
	"raw",
	["not json", "[1, 2]", '{"version": 2, "filed": ["x"]}', '{"version": 2, "filed": {"abc": "x"}}', '{"version": 3, "filed": {}}'],
)
def test_unreadable_state_files_everything(tmp_path, issues, raw):
	fake = issues()
	directory = _log(tmp_path, [_payload("ls")])
	(directory / pp.STATE_FILE).write_text(raw, encoding="utf-8")
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and len(summary["filed"]) == 1 and len(fake.posts) == 1


def test_file_patterns_loads_the_log_once(tmp_path, issues, monkeypatch):
	issues()
	directory = _log(tmp_path, [_payload("ls"), _payload("ls", event="PermissionDenied", reason=OUTAGE_REASON)])
	calls = []
	real = pp.load_keyed_records

	def counting(log_dir):
		calls.append(log_dir)
		return real(log_dir)

	monkeypatch.setattr(pp, "load_keyed_records", counting)
	code, summary = pp.file_patterns(directory, "s1", True, slug="someone/consumer")
	assert code == 0 and len(calls) == 1
	assert summary["total"] == 1 and summary["outage_denials"]["count"] == 1
	assert {key: summary[key] for key in ("total", "patterns", "outage_denials")} == pp.report(directory)


def test_report_shows_outage_denials(tmp_path):
	directory = _log(tmp_path, [_payload("ls"), _payload("ls", event="PermissionDenied", reason=OUTAGE_REASON)])
	result = pp.report(directory)
	assert result["total"] == 1 and len(result["patterns"]) == 1
	assert result["outage_denials"]["count"] == 1 and result["outage_denials"]["label"] == "classifier outage"
	empty = pp.report(tmp_path / "none")
	assert empty["outage_denials"] == {"label": "classifier outage", "count": 0, "tools": [], "first_ts": None, "last_ts": None}


def test_outage_timestamps_are_earliest_and_latest_across_session_files(tmp_path):
	# One log file per session, loaded in file-name order: the later session's
	# file ("a-…") sorts first, so file order is not time order (conformance run 1).
	directory = tmp_path / "log"
	early = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
	late = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
	for session, when in (("a-later", late), ("b-earlier", early)):
		payload = _payload("git status", event="PermissionDenied", reason=OUTAGE_REASON, session_id=session)
		logger.append_record(logger.build_record(payload, when), directory)
	outages = pp.report(directory)["outage_denials"]
	assert outages["count"] == 2
	assert outages["first_ts"] == "2026-09-28T07:00:00Z"
	assert outages["last_ts"] == "2026-09-28T09:00:00Z"


def test_outage_outside_coding_workflows_is_reported(tmp_path, issues):
	issues()
	directory = _log(tmp_path, [_payload("ls", event="PermissionDenied", reason=OUTAGE_REASON)])
	code, summary = pp.file_patterns(directory, "s1", False, slug="someone/consumer")
	assert code == 0 and summary["total"] == 0 and summary["outage_denials"]["count"] == 1


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


def test_claude_md_documents_classifier_outages():
	text = " ".join(CLAUDE_MD.read_text(encoding="utf-8").split())
	assert "### J) Auto-mode Classifier Outages" in text
	assert "**Retry once.** Repeat the refused call once, unchanged." in text
	assert "Do not ask, do not file an issue, do not stop at `Status: BLOCKED`" in text
	assert "(`delay_minutes: 30`, `initiation: own_followup`)" in text
	assert "**No allow rules for it.**" in text
	assert "reports them once as `classifier outage`" in text


def test_plan_command_reports_classifier_outages():
	text = " ".join(PLAN_COMMAND.read_text(encoding="utf-8").split())
	assert "classifier outage: <n> (not filed)" in text
	assert "never files them" in text
	assert (
		"Permission prompts: <none | <n> in <p> patterns — filed #a, commented #b | reported only (not coding-workflows)"
		" | report failed: <error>>[; classifier outage: <n> (not filed)]"
	) in text
	assert "append them to that line as `; classifier outage: <n> (not filed)`, with `<n>` = `outage_denials.count`" in text
	assert "<error>><" not in text


def test_plan_command_runs_the_report_every_stage():
	text = PLAN_COMMAND.read_text(encoding="utf-8")
	assert "14. **Report.** First run the [permission prompt report](#permission-prompt-report)." in text
	assert "permission_prompts.py file --session-label <your session id>" in text


def test_ci_runs_the_helper_tests():
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	for name in ("tests/test_permission_prompts.py", "tests/test_dispatch_workflow.py", "tests/test_edit_comment.py"):
		assert name in text
