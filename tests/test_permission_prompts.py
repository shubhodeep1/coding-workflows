#!/usr/bin/env python3
"""Contract for permission prompt reports (CLAUDE.md §23.I).

Covers `.claude/hooks/permission_prompt_logger.py` (never decides, never
fails the permission flow, logs outside the repo) and
`.claude/scripts/permission_prompts.py` (pattern grouping, families, redaction,
filing only in coding-workflows, dedupe by marker and by family, state), plus
the settings.json wiring, template parity, and the CLAUDE.md / ci.yml / label
contract hooks.

The script is loaded from its `workflow-templates/.claude/` twin: `.claude/**`
changes land in the twin first and reach the root copy through the twin sync
(#4785), and `test_template_parity` pins the two copies to each other.
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


def test_main_rejects_bad_session_label(capsys):
	assert pp.main(["file", "--session-label", "bad label"]) == 1


def test_extract_repo_slug():
	assert pp.extract_repo_slug("http://local_proxy@127.0.0.1:1/git/shubhodeep1/coding-workflows") == "shubhodeep1/coding-workflows"
	assert pp.extract_repo_slug("https://evilgithub.com/a/b") == ""


# ──────────────────────────────────────────────────────────────────
# Families (issue #5668)
# ──────────────────────────────────────────────────────────────────


# Harmless variants of each family in #5668's audit table: every command in a
# list must land in one family, and the lists must land in six families.
TABLE_FAMILIES = {
	"python3 * <<": [
		"python3 - <<'EOF'\nprint(1)\nEOF",
		"python3 - <<'EOF'\nopen('a').write('b')\nEOF\ngit diff --stat -- a",
		"F=/tmp/x && python3 - \"$F\" <<'EOF'\nprint(2)\nEOF\ngrep -n x a",
		"cd /home/user/coding-workflows && PYTHONDONTWRITEBYTECODE=1 python3 - <<EOF\nx = 1\nEOF\nsed -n 1,5p a",
		"export PYTHONDONTWRITEBYTECODE=1 && python3 <<'PY'\nprint()\nPY",
	],
	"PYTHONDONTWRITEBYTECODE=* python3": [
		"PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/check_in_status.py --repo a/b --pr 5",
		"cd /home/user/coding-workflows && PYTHONDONTWRITEBYTECODE=1 timeout 60 python3 .claude/scripts/check_in_status.py --run 9 ; echo done",
		"python3 /home/user/coding-workflows/.claude/scripts/check_in_status.py --issues 1,2 | tail -3",
	],
	"git status": [
		"git status",
		"git status -sb | head -3 && git fetch origin main 2>&1 | tail -2",
		"cd /home/user/coding-workflows; git status --short | head",
	],
	"git fetch": [
		"git fetch origin main",
		"git fetch -q origin claude/x && git show origin/claude/x:README.md",
		"timeout -k 5 30 git fetch origin main 2>&1 | tail -1 ; git show HEAD",
	],
	"for *": [
		"for f in a b; do echo $f; done",
		"for i in 1 2 3; do wc -l $i; done | sort -n",
		"X=1; for f in *.md; do head -1 $f; done",
	],
	"echo *": [
		"echo hi",
		"echo '== status' && git status | head -3",
		"echo done ; ls -la",
	],
}


def _bash_family(command, event="PermissionRequest"):
	return pp.pattern_family(event, "Bash", pp.command_shape(command), command)[0]


@pytest.mark.parametrize("family", sorted(TABLE_FAMILIES))
def test_table_family_variants_share_one_family(family):
	commands = TABLE_FAMILIES[family]
	assert len({pp.command_shape(command) for command in commands}) == len(commands)  # distinct signatures today
	assert len({_bash_family(command) for command in commands}) == 1


def test_table_families_stay_apart():
	assert len({_bash_family(commands[0]) for commands in TABLE_FAMILIES.values()}) == len(TABLE_FAMILIES)


@pytest.mark.parametrize(
	("command", "family"),
	[
		("python3 - <<'EOF'\nprint(1)\nEOF", ("python3", ("heredoc",))),
		("PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/stale_routines.py --triggers x", ("python3 stale_routines.py", ())),
		("gh api -X GET 'search/issues?q=a' --jq '.items[]'", ("gh api", ())),
		("timeout --signal KILL 10 git push origin x", ("git push", ())),
		("timeout 600 python3 -m pytest tests -q", ("python3", ())),
		("export A=1 B && cd /tmp && npm run build", ("npm run", ())),
		("S=/tmp/s; cd /tmp; ls \"$(pwd)\"", ("ls", ("subst",))),
		("while read line; do echo $line; done < f", ("while", ("loop",))),
		("grep -n x a | head ; python3 - <<'EOF'\nprint(1)\nEOF", ("grep", ("heredoc",))),
		("cd /tmp", ("cd", ())),
		("X=1", ("", ())),
		("gh -R o/r api repos/o/r/pulls/5", ("gh api", ())),
		("gh --repo o/r issue view 5", ("gh issue", ())),
		("git -C /tmp/x status -sb", ("git status", ())),
		("gh --hostname ghe.example.com api repos/o/r", ("gh api", ())),
		("gh --hostname ghe.example.com issue view 5", ("gh issue", ())),
		("git -c a=b --no-pager commit -m x", ("git commit", ())),
		("make -C dir test", ("make test", ())),
		("timeout -k 5 git fetch origin main", ("git fetch", ())),
		("timeout $T git fetch origin main", ("git fetch", ())),
		("timeout 1.5m git fetch origin main", ("git fetch", ())),
		# A `$(…)` or `$((…))` in a prefix is one word, not simple commands of its own (PR #5697 review round 2).
		("ROOT=$(pwd) && git status", ("git status", ("subst",))),
		("ROOT=$(pwd)&&git status", ("git status", ("subst",))),
		("cd $(pwd) && git status", ("git status", ("subst",))),
		("export ROOT=$(pwd); git status", ("git status", ("subst",))),
		("X=$((1 + 2)) && git status", ("git status", ())),
		("X=$(echo \"a)b\" | tr a b) && git fetch", ("git fetch", ("subst",))),
		("X=$(echo $(pwd)) && git fetch", ("git fetch", ("subst",))),
		("git -C $(pwd) status", ("git status", ("subst",))),
		("echo $(mysql -p x) && git fetch", ("echo", ("subst",))),
		("(cd x && git status)", ("git status", ())),
		# A `#` comment that starts a word inside `$(…)` runs to the end of its line, as in Bash; a `#` inside a
		# word, `$#`, or a quoted `#)` is not one (PR #5697 review round 3).
		("X=$(printf a # note :)\n) && git status", ("git status", ("subst",))),
		("X=$(#c)\necho a) && git fetch", ("git fetch", ("subst",))),
		("X=$(echo a#b) && git status", ("git status", ("subst",))),
		("X=$(echo $#) && git fetch", ("git fetch", ("subst",))),
		("X=$(echo \"#)\" ) && git fetch", ("git fetch", ("subst",))),
		# Bash removes a backslash-newline, and an escaped character continues the word, so neither starts a
		# comment (PR #5697 review round 4).
		("X=$(echo a\\\n#b) && git status", ("git status", ("subst",))),
		("X=$(echo a\\ #b) && git status", ("git status", ("subst",))),
	],
)
def test_command_family(command, family):
	assert pp.command_family(command) == family


@pytest.mark.parametrize(
	("command", "has_substitution"),
	[
		('echo "$(date)"', True),
		("echo $(date)", True),
		("echo '$(date)'", False),
		("echo $((1 + 2))", False),
		("echo \\$(date)", False),
		("python3 - <<'EOF'\nprint('$(not a substitution)')\nEOF", False),
	],
)
def test_substitution_construct(command, has_substitution):
	assert ("subst" in pp.command_family(command)[1]) is has_substitution


def test_family_separates_events_subcommands_scripts_and_constructs():
	families = {
		_bash_family("git fetch origin main"),
		_bash_family("git fetch origin main", event="PermissionDenied"),
		_bash_family("git push origin main"),
		_bash_family("python3 .claude/scripts/check_in_status.py --pr 1"),
		_bash_family("python3 .claude/scripts/dispatch_workflow.py --workflow x"),
		_bash_family("python3 -c 'print(1)'"),
		_bash_family("python3 - <<'EOF'\nprint(1)\nEOF"),
		_bash_family("for f in a; do python3 x; done"),
		_bash_family("for f in $(ls); do python3 x; done"),
	}
	assert len(families) == 9


def test_global_value_options_do_not_merge_subcommand_families():
	assert _bash_family("gh -R o/r api repos/o/r/pulls/5") == _bash_family("gh api repos/o/r/pulls/5")
	assert _bash_family("gh -R o/r api repos/o/r/pulls/5") != _bash_family("gh -R o/r issue view 5")
	assert _bash_family("git -C /tmp/x status") != _bash_family("git -C /tmp/x fetch origin main")
	# The family skips the option; the shape, and so the signature, keeps it.
	assert pp.command_shape("gh -R o/r api repos/o/r/pulls/5") == "gh -R *"
	assert pp.command_shape("git -C /tmp/x status") == "git -C *"


def test_non_bash_tools_and_unparseable_commands_keep_signature_granularity():
	edit_claude = pp.pattern_family("PermissionRequest", "Edit", ".claude/hooks/*")
	edit_tests = pp.pattern_family("PermissionRequest", "Edit", "tests/*")
	mcp = pp.pattern_family("PermissionDenied", "mcp__x__get_session", "")
	assert edit_claude[0] != edit_tests[0] and edit_claude[1] == ".claude/hooks/*" and mcp[1] == "mcp__x__get_session"
	broken = pp.pattern_family("PermissionRequest", "Bash", pp.command_shape("echo 'x"), "echo 'x")
	assert broken == pp.pattern_family("PermissionRequest", "Bash", "unparseable: echo 'x")


def test_patterns_carry_the_latest_records_family(tmp_path):
	directory = _log(tmp_path, [_payload("git fetch origin main"), _payload("git fetch -q origin x && git show y")])
	patterns = pp.group_patterns(pp.load_records(directory))
	assert len(patterns) == 2 and patterns[0]["family"] == patterns[1]["family"]
	assert patterns[0]["family_label"] == "git fetch"


def _pattern(command, event="PermissionRequest"):
	record = logger.build_record(_payload(command, event=event), NOW)
	return pp.group_patterns([record])[0]


def _issue(number, body, state="open", state_reason=None):
	return {"number": number, "state": state, "state_reason": state_reason, "body": body}


def _family_body(command, **kwargs):
	return pp.issue_body(_pattern(command, **kwargs), 1, "s0")


def _legacy_body(command, **kwargs):
	pattern = _pattern(command, **kwargs)
	pattern.pop("family")
	return pp.issue_body(pattern, 1, "s0")


def test_new_issue_carries_the_unchanged_signature_marker_and_a_family_line(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("git fetch origin main")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	body = fake.posts[0][1]["body"]
	sig, family = summary["filed"][0]["signature"], summary["filed"][0]["family"]
	assert f"\n<!-- ai:permission-prompt:v1 sig={sig} -->\n" in body
	assert pp.MARKER_RE.search(body).group(1) == sig
	assert body.rstrip().endswith(f"<!-- ai:permission-prompt-family:v1 family={family} -->")
	assert "**Family:** `git fetch`" in body


def test_new_shape_in_a_family_comments_on_the_family_issue(tmp_path, issues):
	fake = issues([_issue(50, _family_body("git fetch origin main"))])
	directory = _log(tmp_path, [_payload("git fetch -q origin x 2>&1 | tail -1 ; git show y")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and summary["filed"] == []
	(path, body), = fake.posts
	assert path == "repos/shubhodeep1/coding-workflows/issues/50/comments"
	assert body["body"].startswith("Seen again with a new command shape in this family (`git fetch`)")
	assert "**New pattern:** `git fetch -q * 2>& * | tail -1 ; git show *`" in body["body"]
	assert "**Occurrences:** 1 " in body["body"]
	assert summary["commented"][0]["issue"] == 50
	assert summary["commented"][0]["family"] == _bash_family("git fetch origin main")


def test_family_variants_in_one_run_file_one_issue(tmp_path, issues):
	fake = issues()
	commands = TABLE_FAMILIES["python3 * <<"]
	directory = _log(tmp_path, [_payload(command) for command in commands])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert code == 0 and len(summary["filed"]) == 1 and len(summary["commented"]) == len(commands) - 1
	assert [path for path, _ in fake.posts] == ["repos/shubhodeep1/coding-workflows/issues"] + ["repos/shubhodeep1/coding-workflows/issues/901/comments"] * (len(commands) - 1)


def test_distinct_families_file_separately(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload(commands[0]) for commands in TABLE_FAMILIES.values()])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert len(summary["filed"]) == len(TABLE_FAMILIES) and summary["commented"] == []
	assert len({pp.FAMILY_MARKER_RE.search(body["body"]).group(1) for _, body in fake.posts}) == len(TABLE_FAMILIES)


def test_signature_match_wins_over_the_family(tmp_path, issues):
	directory = _log(tmp_path, [_payload("git fetch origin main")])
	sig = pp.group_patterns(pp.load_records(directory))[0]["signature"]
	fake = issues([_issue(42, f"x\n<!-- ai:permission-prompt:v1 sig={sig} -->", state="closed", state_reason="duplicate"), _issue(50, _family_body("git fetch -q origin x"))])
	pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert fake.posts[0][0].endswith("/issues/42/comments") and fake.posts[0][1]["body"].startswith("Seen again.\n")


@pytest.mark.parametrize("state_reason", ["completed", "not_planned", None])
def test_closed_family_issue_gets_a_comment_not_a_reopen(tmp_path, issues, state_reason):
	fake = issues([_issue(60, _family_body("git fetch origin main"), state="closed", state_reason=state_reason)])
	directory = _log(tmp_path, [_payload("git fetch -q origin x")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert [path for path, _ in fake.posts] == ["repos/shubhodeep1/coding-workflows/issues/60/comments"]
	assert summary["filed"] == []


def test_family_issue_closed_as_duplicate_is_not_the_familys_issue(tmp_path, issues):
	fake = issues([_issue(60, _family_body("git fetch origin main"), state="closed", state_reason="duplicate")])
	directory = _log(tmp_path, [_payload("git fetch -q origin x")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert [path for path, _ in fake.posts] == ["repos/shubhodeep1/coding-workflows/issues"]
	assert len(summary["filed"]) == 1


def test_open_family_issue_is_preferred_then_the_lowest_number():
	candidates = [
		{"number": 10, "state": "closed", "state_reason": "completed"},
		{"number": 70, "state": "open", "state_reason": None},
		{"number": 60, "state": "open", "state_reason": "reopened"},
		{"number": 5, "state": "closed", "state_reason": "duplicate"},
	]
	assert pp.choose_family_issue(candidates)["number"] == 60
	assert pp.choose_family_issue(candidates[:1] + candidates[3:])["number"] == 10
	assert pp.choose_family_issue(candidates[3:]) is None
	assert pp.choose_family_issue([]) is None


@pytest.mark.parametrize("event", ["PermissionRequest", "PermissionDenied"])
def test_legacy_issue_family_is_derived_from_the_recorded_example(event):
	command = "python3 - <<'EOF'\nprint(1)\nEOF\ngit diff --stat -- a"
	body = _legacy_body(command, event=event)
	assert pp.FAMILY_MARKER_RE.search(body) is None and pp.MARKER_RE.search(body)
	assert pp.legacy_issue_family(body) == _bash_family(command, event=event)


def test_legacy_family_is_bash_only_and_needs_the_example():
	pattern = {"signature": "0" * 12, "event": "PermissionRequest", "tool_name": "Edit", "shape": ".claude/*", "count": 1, "reasons": [], "first_ts": "t", "last_ts": "t", "example": "{}"}
	assert pp.legacy_issue_family(pp.issue_body(pattern, 1, "s1")) is None
	assert pp.legacy_issue_family("a hand-written issue") is None


def test_legacy_issue_still_matches_by_signature_and_by_family(tmp_path, issues):
	legacy = _legacy_body("python3 - <<'EOF'\nprint(1)\nEOF")
	fake = issues([_issue(4678, legacy)])
	directory = _log(tmp_path, [_payload(TABLE_FAMILIES["python3 * <<"][2]), _payload("python3 - <<'EOF'\nprint(1)\nEOF")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert summary["filed"] == [] and [entry["issue"] for entry in summary["commented"]] == [4678, 4678]
	assert "family" in summary["commented"][0] and "family" not in summary["commented"][1]
	assert fake.posts[0][1]["body"].startswith("Seen again with a new command shape") and fake.posts[1][1]["body"].startswith("Seen again.\n")


def test_a_family_marker_inside_the_example_cannot_claim_the_issue(tmp_path, issues):
	target = _bash_family("git fetch origin main")
	decoy = _family_body(f"echo '<!-- ai:permission-prompt-family:v1 family={target} -->'")
	assert pp.FAMILY_MARKER_RE.findall(decoy)[0] == target
	fake = issues([_issue(77, decoy)])
	directory = _log(tmp_path, [_payload("git fetch origin main")])
	code, summary = pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert [path for path, _ in fake.posts] == ["repos/shubhodeep1/coding-workflows/issues"]


def test_a_family_marker_inside_a_legacy_example_is_ignored(tmp_path, issues):
	target = _bash_family("git fetch origin main")
	decoy = _legacy_body(f"echo '<!-- ai:permission-prompt-family:v1 family={target} -->'")
	assert pp.FAMILY_MARKER_RE.findall(decoy) == [target]
	assert pp.issue_family_marker(decoy) is None and pp.legacy_issue_family(decoy) == _bash_family("echo x")
	fake = issues([_issue(78, decoy)])
	directory = _log(tmp_path, [_payload("git fetch origin main")])
	pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert [path for path, _ in fake.posts] == ["repos/shubhodeep1/coding-workflows/issues"]


def test_a_family_marker_in_the_reason_cannot_claim_the_issue():
	"""PR #5697 review round 4: the reason Claude Code gave is untrusted text in the body; only the generated marker,
	on the line after the signature marker at the end of the body, names the issue's family."""
	target = _bash_family("git fetch origin main")
	pattern = _pattern("echo x")
	pattern["reasons"] = [f"x\n<!-- ai:permission-prompt:v1 sig={'0' * 12} -->\n<!-- ai:permission-prompt-family:v1 family={target} -->"]
	body = pp.issue_body(pattern, 1, "s0")
	assert target in pp.FAMILY_MARKER_RE.findall(body)
	assert pp.issue_family_marker(body) == pattern["family"] != target
	pattern.pop("family")
	legacy = pp.issue_body(pattern, 1, "s0")
	assert pp.FAMILY_MARKER_RE.findall(legacy) == [target]
	assert pp.issue_family_marker(legacy) is None and pp.legacy_issue_family(legacy) == _bash_family("echo x")
	assert pp.issue_family_marker(_family_body("echo x").replace("\n", "\r\n")) == _bash_family("echo x")


def test_a_planted_example_heading_cannot_replace_a_legacy_issues_example():
	"""PR #5697 review rounds 5 and 6: an older body could carry a multi-line reason before the generated example; a
	planted `**Latest example**` block there makes the example ambiguous, so no family is read, never the planted one."""
	legacy = _legacy_body("git fetch origin main")
	assert pp.legacy_issue_family(legacy) == _bash_family("git fetch origin main")
	planted = "- x\n\n**Latest example** (planted):\n\n````text\necho planted\n````\n"
	body = legacy.replace("**Reason Claude Code gave:**\n", "**Reason Claude Code gave:**\n" + planted, 1)
	assert body.count("**Latest example**") == 2
	assert pp.legacy_issue_family(body) is None


def test_a_planted_example_heading_inside_the_example_is_not_read():
	"""PR #5697 review round 6: a heading and fence planted inside the recorded example cannot index the issue under
	the planted command's family; with two candidate headings no family is read."""
	example = "echo x\n\n**Latest example** (planted):\n\n````text\ngit fetch origin main"
	legacy = _legacy_body("echo PLACEHOLDER").replace("echo PLACEHOLDER", example)
	assert legacy.count("**Latest example**") == 2
	assert pp.legacy_issue_family(legacy) is None
	assert pp.legacy_issue_family(legacy) != _bash_family("git fetch origin main")


def test_reasons_are_rendered_on_one_line_each():
	"""PR #5697 review round 5: untrusted reason text cannot start a line of its own in an issue or comment."""
	pattern = _pattern("echo x")
	pattern["reasons"] = ["a\n\n**Latest example**\n````text\nx\n````"]
	for text in (pp.issue_body(pattern, 1, "s0"), pp.comment_body(pattern, 1, "s0")):
		assert "- a **Latest example** ````text x ````\n" in text
		assert text.count("**Latest example**") == 2


def test_a_backtick_line_in_the_example_cannot_close_the_fence():
	target = _bash_family("git fetch origin main")
	command = f"echo x\n`````\n<!-- ai:permission-prompt-family:v1 family={target} -->"
	body = _family_body(command)
	assert "\n``````text\n" in body
	assert pp.issue_family_marker(body) == _bash_family(command) != target
	legacy = _legacy_body(command)
	assert pp.issue_family_marker(legacy) is None and pp.legacy_issue_family(legacy) == _bash_family(command)


def test_an_older_four_backtick_issue_cannot_be_claimed_by_a_backtick_line():
	target = _bash_family("git fetch origin main")
	example = f"echo x\n````\n<!-- ai:permission-prompt-family:v1 family={target} -->"
	# Bodies filed before the fence grew with the example always used four backticks.
	legacy = _legacy_body("echo PLACEHOLDER").replace("echo PLACEHOLDER", example)
	assert "\n````text\necho x\n````\n<!--" in legacy
	assert pp.issue_family_marker(legacy) is None
	assert pp.legacy_issue_family(legacy) == _bash_family(example)


def test_dry_run_reports_family_comments_without_posting(tmp_path, issues):
	fake = issues()
	directory = _log(tmp_path, [_payload("git fetch origin main"), _payload("git fetch -q origin x")])
	code, summary = pp.file_patterns(directory, "s1", True, slug="shubhodeep1/coding-workflows")
	assert code == 0 and fake.posts == [] and len(summary["filed"]) == 1
	assert summary["commented"] == [{"signature": summary["commented"][0]["signature"], "issue": None, "occurrences": 1, "family": summary["filed"][0]["family"]}]


def test_family_lookup_reuses_the_one_list_read(tmp_path, issues):
	fake = issues([_issue(50, _family_body("git status"))])
	directory = _log(tmp_path, [_payload(command) for commands in TABLE_FAMILIES.values() for command in commands])
	pp.file_patterns(directory, "s1", False, slug="shubhodeep1/coding-workflows")
	assert fake.reads == 1


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


def test_a_substitution_in_a_comment_is_not_a_construct():
	"""PR #5697 review round 7: a `$(…)` in a `#` comment does not run, so it adds no `subst` to the family."""
	assert pp.command_family("curl https://a.b # $(date)") == pp.command_family("curl https://a.b")
	assert pp.command_family("curl https://a.b\n# $(date)\ngit status")[1] == ()
	assert pp.command_family('echo "#$(date)"')[1] == ("subst",)
	assert pp.command_family("echo a#$(date)")[1] == ("subst",)
	# Bash removes a backslash-newline, so a `#` after one still starts a comment when a space came before it, and
	# does not when the word runs on (PR #5697 review round 8).
	assert pp.command_family("curl https://a.b \\\n# $(date)")[1] == ()
	assert pp.command_family("echo a\\\n#$(date)")[1] == ("subst",)


def test_an_unclosed_substitution_has_no_command_family():
	"""PR #5697 review round 7: Bash rejects an unclosed `$(`, so the command falls back to its shape's family."""
	assert pp.command_family("echo $(date") is None
	assert pp.command_family('git log "$(date"') is None
	family, label = pp.pattern_family("PermissionRequest", "Bash", "echo *", "echo $(date")
	assert label == "echo *" and family == pp.pattern_family("PermissionRequest", "Bash", "echo *")[0]


def test_timeout_end_of_options_before_the_duration_keeps_the_family():
	"""PR #5697 review round 7: `timeout -- 5 git fetch` is the valid form (GNU timeout stops reading options at the
	duration, so `timeout 5 -- git fetch` runs a command named `--`)."""
	assert pp.command_family("timeout -- 5 git fetch") == pp.command_family("git fetch")


def test_shape_and_family_label_cannot_break_their_code_spans():
	"""PR #5697 review round 7: a backtick or a line break in a command-derived shape or family label stays inside its
	code span in the issue body and in the family comment."""
	pattern = _pattern("echo x")
	pattern["shape"] = "./a`b.sh\n# planted heading"
	pattern["family_label"] = "./a`b.sh\n**planted**"
	for text in (pp.issue_body(pattern, 1, "s0"), pp.family_comment_body(pattern, 1, "s0")):
		assert "``./a`b.sh # planted heading``" in text
		assert "``./a`b.sh **planted**``" in text
		assert "\n# planted heading" not in text and "\n**planted**" not in text
	assert pp._family_code_span("`x") == "`` `x ``"


def test_comments_never_become_the_family():
	"""PR #5697 review round 9: a leading comment line is not the command word, and an unclosed `$(` in a comment does
	not stop the real substitutions from being collapsed."""
	assert pp.command_family("# note\ngit status") == pp.command_family("git status")
	assert pp.command_family("# x $(\ngit status") == pp.command_family("git status")
	assert pp.command_family("x=$(pwd) && git status # $(") == pp.command_family("x=$(pwd) && git status")
	assert pp.command_family('echo "#x" $#') == pp.command_family("echo x")


def test_a_legacy_body_with_crlf_line_ends_keeps_its_family():
	"""PR #5697 review round 9: an issue body edited in the web UI can carry CRLF line ends."""
	legacy = _legacy_body("git fetch origin main")
	assert pp.legacy_issue_family(legacy.replace("\n", "\r\n")) == pp.legacy_issue_family(legacy) == _bash_family("git fetch origin main")


def test_a_hash_after_a_closing_substitution_is_not_a_comment():
	"""PR #5697 review round 10: after the `)` that closes a `$(…)` the word goes on, so `#suffix` is literal and the
	command after `;` keeps its family."""
	assert pp.command_family("X=$(pwd)#suffix; git status") == ("git status", ("subst",))
	assert pp.command_family("echo $(date)#$(id)")[1] == ("subst",)


def test_invisible_format_characters_are_escaped_in_code_spans():
	"""PR #5697 review round 10: zero-width and bidirectional control characters are shown as escapes."""
	assert pp._family_code_span("a\u202eb\u200bc") == "`a\\u202eb\\u200bc`"

