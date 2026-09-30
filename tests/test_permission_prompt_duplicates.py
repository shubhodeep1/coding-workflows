#!/usr/bin/env python3
"""Contract for closing pipeline-filed `ai:permission-prompt` duplicates (issue #4867).

Covers, in `workflow-templates/.claude/scripts/permission_prompts.py` (the
twin leads under the interim twin-first rule; `tests/test_permission_prompts.py`
keeps `.claude/scripts/` byte-identical to it):
  - the `inline-interpreter-write` command class (issue #4858 item 1);
  - class routing in `file`: a new pattern of an open class issue's class is a
    "Seen again" comment there, never a new issue;
  - `duplicate-check`, which decides CLAUDE.md §23.I conditions 1 and 2;
and the carve-out text in CLAUDE.md §23.C / §23.I / §28.C and in
`/implement-issue-claude` step 5a.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_SCRIPT_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "scripts" / "permission_prompts.py"
TEMPLATE_ISSUE_COMMAND = REPO_ROOT / "workflow-templates" / ".claude" / "commands" / "implement-issue-claude.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SLUG = "shubhodeep1/coding-workflows"


def _load(name, path):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


pp = _load("permission_prompts_twin", TEMPLATE_SCRIPT_PATH)


def _flat(path: Path) -> str:
	return " ".join(path.read_text(encoding="utf-8").split())


def _record(command, event="PermissionRequest", reason="", ts="2026-09-28T12:52:05Z"):
	return {"event": event, "tool_name": "Bash", "tool_input": {"command": command}, "reason": reason, "ts": ts}


def _log(tmp_path, records):
	directory = tmp_path / "log"
	directory.mkdir(parents=True, exist_ok=True)
	with (directory / "session.jsonl").open("a", encoding="utf-8") as handle:
		for record in records:
			handle.write(json.dumps(record) + "\n")
	return directory


# ──────────────────────────────────────────────────────────────────
# Command class (issue #4858 item 1)
# ──────────────────────────────────────────────────────────────────

HEREDOC_WRITE = "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('tests/x.py')\np.write_text(p.read_text().replace('a', 'b'))\nEOF\ngit diff --stat | tail -3"
HEREDOC_OPEN_WRITE = "cd /repo && python3 - <<'PY' 2>&1\nwith open('CLAUDE.md', 'w') as f:\n    f.write('x')\nPY\nls changelog.d | head -5"


@pytest.mark.parametrize(
	"command",
	[
		HEREDOC_WRITE,
		HEREDOC_OPEN_WRITE,
		"python3 <<EOF\nimport os\nos.replace('a', 'b')\nEOF",
		"python3 -c \"open('a.txt', 'w').write('x')\"",
		"python -c 'import shutil; shutil.copy(\"a\", \"b\")'",
		"python3 - <<'EOF'\nimport shutil\nshutil.move('a', 'b')\nEOF",
		"python3 - <<'EOF'\nimport shutil\nshutil.rmtree('build')\nEOF",
		"python3 -c \"open('a.bin', 'rb+').write(b'x')\"",
		"python3 -c \"open('a.bin', 'r+b').write(b'x')\"",
		"python3 -c \"open('a.txt', mode='w').write('x')\"",
		"python3 -c \"from pathlib import Path; Path('x').open('a').write('y')\"",
		"python3 -Ic \"open('a.txt', 'w').write('x')\"",
		"python3 -IBc \"open('a.txt', 'w').write('x')\"",
		"sed -i 's/a/b/' README.md",
		"sed -i.bak -e 's/a/b/' README.md",
		"sed --in-place 's/a/b/' README.md",
		"perl -pi -e 's/a/b/' CLAUDE.md",
		"perl -i.bak -pe 's/a/b/' CLAUDE.md",
		"ruby -i -pe 'gsub(/a/, \"b\")' x.rb",
		"awk -i inplace '{print}' x.txt",
		"FOO=1 /usr/bin/python3 - <<'EOF'\nPath('x').unlink()\nEOF",
		"python3 -c \"import os; open(os.path.join(d, 'x.json'), 'w').write('1')\"",
		"python3 -c \"open(str(p), 'w').write('x')\"",
		"python3 - <<'EOF'\nimport os\nwith open(os.path.join(str(d), 'f'), 'w') as f:\n    f.write('x')\nEOF",
		"cat <<'A'\nnotes\nA\npython3 - <<'B'\nopen(p, 'w').write('x')\nB",
		"python3 -c \"open(os.path.join(str(p.replace('/', '_')), 'x'), 'w').write('1')\"",
		"python3 -c \"open('a(b', 'w').write('1')\"",
		"python3 -c \"open(mode='w', file=p).write('1')\"",
		"python3 -c \"open(p, encoding='utf-8', mode='a').write('1')\"",
		"grep -c x <<< \"$y\"; python3 - <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 -X utf8 -c \"open('a.txt', 'w').write('x')\"",
		"python3 -W ignore - <<'EOF'\nPath('x').write_text('y')\nEOF",
		"python3 -- - <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 -I -- <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 - <<-'EOF'\n\topen(p, 'w').write('x')\n\tEOF",
		"cat <<-'A'\n\tnotes\n\tA\npython3 - <<'B'\nopen(p, 'w').write('x')\nB",
	],
)
def test_inline_interpreter_writes_are_classed(command):
	assert pp.command_class(_record(command)) == pp.INLINE_INTERPRETER_WRITE_CLASS


@pytest.mark.parametrize(
	"command",
	[
		"python3 - <<'EOF'\nimport json\nprint(json.load(open('a.json')))\nEOF",
		"python3 -c \"print(open('a.txt').read())\"",
		"python3 -c \"print(open('a.bin', 'rb').read())\"",
		"python3 - <<'EOF'\nprint(open('a').read())\nEOF",
		"python3 -c \"print(open('w').read(), open('x', 'r').read())\"",
		"python3 -Ic \"print(open('a.txt').read())\"",
		"python3 - <<'EOF'\nimport shutil\nprint(shutil.which('gh'))\nEOF",
		"python3 -c \"import shutil; print(shutil.get_terminal_size().columns)\"",
		"python3 -m pytest -q tests/test_permission_prompts.py",
		"python3 scripts/x.py --write",
		"python3 .claude/scripts/permission_prompts.py file <<EOF\nwrite_text(\nEOF",
		"git commit -m \"python3 - <<EOF write_text\"",
		"echo \"python3 -c open('a','w')\"",
		"sed -n '1,20p' README.md",
		"sed -e 's/i/j/' README.md",
		"perl -Mstrict -e 'print 1'",
		"awk '{print $1}' x.txt",
		"ls -la",
		"python3 -c \"print(open(os.path.join(d, 'w')).read())\"",
		"python3 - <<'A'\nprint(open('a').read())\nA\ncat <<'B'\nopen(p, 'w')\nB",
		"python3 -c \"print(open(os.path.join(str(p.replace('/', '_')), 'w')).read())\"",
		"python3 -c \"print(open('a, w').read())\"",
		"python3 - <<'PY' && echo \"<<X\"\nprint(1)\nPY\ncat <<'B'\nopen(p, 'w')\nB",
		"python3 tool.py -c \"open('x', 'w')\"",
		"python3 -m mod -c \"open('x', 'w')\"",
		"python3 tool.py - <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 -Im mod - <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 -- -c \"open('x', 'w')\"",
		"python3 -I -- -c \"open('x', 'w')\"",
		"python3 -- -m mod - <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 -- tool.py - <<'EOF'\nopen(p, 'w').write('x')\nEOF",
		"python3 - <<-'A'\n\tprint(open('a').read())\n\tA\ncat <<'B'\nopen(p, 'w')\nB",
	],
)
def test_reads_scripts_and_data_are_not_classed(command):
	assert pp.command_class(_record(command)) == ""


def test_non_bash_records_are_not_classed():
	assert pp.command_class({"event": "PermissionRequest", "tool_name": "Edit", "tool_input": {"file_path": "/x/sed -i"}}) == ""


def test_pattern_class_is_reported():
	patterns = pp.group_patterns([_record(HEREDOC_WRITE), _record("ls")])
	assert [pattern["class"] for pattern in patterns] == [pp.INLINE_INTERPRETER_WRITE_CLASS, ""]


# ──────────────────────────────────────────────────────────────────
# Class routing in `file`
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


def _class_issue(number, state="open", command_class=None):
	marker = pp.CLASS_MARKER_TEMPLATE.format(command_class=command_class or pp.INLINE_INTERPRETER_WRITE_CLASS)
	return {"number": number, "state": state, "body": f"x\n{marker}\n<!-- ai:permission-prompt:v1 sig=aaaaaaaaaaaa -->"}


def test_classed_pattern_files_an_issue_with_the_class_marker(tmp_path, issues):
	fake = issues()
	code, summary = pp.file_patterns(_log(tmp_path, [_record(HEREDOC_WRITE)]), "s1", False, slug=SLUG)
	assert code == 0 and summary["errors"] == [] and len(summary["filed"]) == 1
	body = fake.posts[0][1]["body"]
	assert "<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->" in body
	assert body.index("ai:permission-prompt-class:v1") < body.index("<!-- ai:permission-prompt:v1 sig=")
	assert pp.FILED_BY_LINE in body


def test_unclassed_pattern_has_no_class_marker(tmp_path, issues):
	fake = issues()
	pp.file_patterns(_log(tmp_path, [_record("ls -la")]), "s1", False, slug=SLUG)
	assert "ai:permission-prompt-class" not in fake.posts[0][1]["body"]


def test_new_shape_of_an_open_class_issue_is_a_comment_not_an_issue(tmp_path, issues):
	fake = issues([_class_issue(4700)])
	code, summary = pp.file_patterns(_log(tmp_path, [_record(HEREDOC_OPEN_WRITE)]), "session_x", False, slug=SLUG)
	assert code == 0 and summary["filed"] == []
	assert fake.posts[0][0] == f"repos/{SLUG}/issues/4700/comments"
	comment = fake.posts[0][1]["body"]
	assert comment.startswith("Seen again: a new pattern of the same command class (`inline-interpreter-write`)")
	assert "session `session_x`" in comment
	sig = summary["commented"][0]["signature"]
	assert f"signature `{sig}`" in comment
	assert summary["commented"] == [{"signature": sig, "issue": 4700, "occurrences": 1, "class": "inline-interpreter-write"}]
	assert fake.reads == 1


def test_closed_class_issue_attracts_nothing(tmp_path, issues):
	fake = issues([_class_issue(4700, state="closed")])
	code, summary = pp.file_patterns(_log(tmp_path, [_record(HEREDOC_WRITE)]), "s1", False, slug=SLUG)
	assert code == 0 and len(summary["filed"]) == 1 and summary["commented"] == []
	assert fake.posts[0][0] == f"repos/{SLUG}/issues"


def test_lowest_open_class_issue_wins():
	listed = [_class_issue(4800), _class_issue(4700), _class_issue(4600, state="closed"), {"number": 4500, "state": "open", "body": "no marker"}]
	assert pp.open_issues_by_class(listed) == {pp.INLINE_INTERPRETER_WRITE_CLASS: 4700}


def test_signature_match_wins_over_class_match(tmp_path, issues):
	directory = _log(tmp_path, [_record(HEREDOC_WRITE)])
	sig = pp.group_patterns(pp.load_records(directory))[0]["signature"]
	own = {"number": 42, "state": "closed", "body": f"x\n<!-- ai:permission-prompt:v1 sig={sig} -->"}
	fake = issues([_class_issue(4700), own])
	pp.file_patterns(directory, "s1", False, slug=SLUG)
	assert fake.posts[0][0] == f"repos/{SLUG}/issues/42/comments"
	assert fake.posts[0][1]["body"].startswith("Seen again.")


def test_second_classed_shape_in_one_run_joins_the_issue_just_filed(tmp_path, issues):
	fake = issues()
	code, summary = pp.file_patterns(_log(tmp_path, [_record(HEREDOC_WRITE), _record("sed -i 's/a/b/' x")]), "s1", False, slug=SLUG)
	assert code == 0 and len(summary["filed"]) == 1
	assert fake.posts[1][0] == f"repos/{SLUG}/issues/901/comments"


def test_existing_issues_keeps_its_signature():
	listed = [{"number": 7, "state": "open", "body": "<!-- ai:permission-prompt:v1 sig=bbbbbbbbbbbb -->"}]
	assert pp.index_by_signature(listed) == {"bbbbbbbbbbbb": {"number": 7, "state": "open"}}


# A command that mentions a marker (a session grepping for one) lands in the
# issue's fenced example; markers are read only outside fenced ````text blocks.
MARKER_MENTIONING_COMMAND = (
	"grep -rn '<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->' .claude/ ; "
	"echo '<!-- ai:permission-prompt:v1 sig=aaaaaaaaaaaa -->'"
)


def _filed_body(directory_root, command):
	directory = _log(directory_root, [_record(command)])
	pattern = pp.group_patterns(pp.load_records(directory))[0]
	return pattern, pp.issue_body(pattern, 1, "s1")


def test_markers_in_the_command_example_neither_class_nor_index_the_issue(tmp_path):
	pattern, body = _filed_body(tmp_path, MARKER_MENTIONING_COMMAND)
	assert pattern["class"] == ""
	assert "class=inline-interpreter-write -->" in body and "sig=aaaaaaaaaaaa" in body
	issue = {"number": 10, "state": "open", "body": body}
	assert pp.open_issues_by_class([issue]) == {}
	assert pp.index_by_signature([issue]) == {pattern["signature"]: {"number": 10, "state": "open"}}


def test_an_open_issue_whose_example_mentions_the_class_attracts_nothing(tmp_path, issues):
	_, body = _filed_body(tmp_path / "earlier", MARKER_MENTIONING_COMMAND)
	fake = issues([{"number": 4700, "state": "open", "body": body}])
	code, summary = pp.file_patterns(_log(tmp_path / "now", [_record(HEREDOC_WRITE)]), "s1", False, slug=SLUG)
	assert code == 0 and len(summary["filed"]) == 1 and summary["commented"] == []
	assert fake.posts[0][0] == f"repos/{SLUG}/issues"


def test_a_backtick_line_in_the_command_does_not_end_the_fenced_example(tmp_path):
	# A quoted newline puts a four-backtick line, then a marker, in the command.
	command = "echo 'a\n````\n<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->\n````` b'"
	pattern, body = _filed_body(tmp_path, command)
	assert pattern["class"] == ""
	assert "\n``````text\n" in body and pp._example_fence(pattern["example"]) == "``````"
	issue = {"number": 12, "state": "open", "body": body}
	assert pp.open_issues_by_class([issue]) == {}
	assert pp.index_by_signature([issue]) == {pattern["signature"]: {"number": 12, "state": "open"}}


def test_an_example_without_backticks_keeps_the_four_backtick_fence():
	assert pp._example_fence("ls -la") == "````"
	assert pp._example_fence("echo `date`") == "````"


def test_a_reason_that_quotes_markers_neither_classes_nor_describes_the_issue(tmp_path):
	reason = (
		"Denied: the command\n**Occurrences:** 99 (forged)\n"
		"<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->\n"
		"<!-- ai:permission-prompt:v1 sig=eeeeeeeeeeee -->"
	)
	directory = _log(tmp_path, [_record("ls -la", event="PermissionDenied", reason=reason)])
	pattern = pp.group_patterns(pp.load_records(directory))[0]
	body = pp.issue_body(pattern, 1, "s1")
	assert pattern["class"] == ""
	assert "- Denied: the command **Occurrences:** 99 (forged) <\\!-- ai:permission-prompt-class:v1" in body
	issue = {"number": 13, "state": "open", "body": body}
	assert pp.open_issues_by_class([issue]) == {}
	assert pp.index_by_signature([issue]) == {pattern["signature"]: {"number": 13, "state": "open"}}
	assert pp._first_match(pp.OCCURRENCES_RE, pp._outside_fenced_examples(body)).startswith("**Occurrences:** 1 (")


def test_markers_between_two_fenced_blocks_are_still_read():
	# A report appended after the markers can carry its own fenced command.
	body = (
		"Filed by `.claude/scripts/permission_prompts.py`.\n\n````text\nls\n````\n\n"
		"<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->\n"
		"<!-- ai:permission-prompt:v1 sig=cccccccccccc -->\n\n"
		"**Command:**\n\n````text\necho '<!-- ai:permission-prompt:v1 sig=dddddddddddd -->'\n````\n"
	)
	issue = {"number": 11, "state": "open", "body": body}
	assert pp.index_by_signature([issue]) == {"cccccccccccc": {"number": 11, "state": "open"}}
	assert pp.open_issues_by_class([issue]) == {pp.INLINE_INTERPRETER_WRITE_CLASS: 11}


# ──────────────────────────────────────────────────────────────────
# duplicate-check (CLAUDE.md §23.I conditions 1 and 2)
# ──────────────────────────────────────────────────────────────────

LOGIN = "shubhodeep1"
CREATED = "2026-09-28T13:32:40Z"
FILED_BODY = (
	"A Claude Code session hit a permission prompt. Filed by `.claude/scripts/permission_prompts.py` (CLAUDE.md §23.I).\n\n"
	"**Occurrences:** 1 (2026-09-28T12:52:05Z – 2026-09-28T12:52:05Z), session `session_01B`\n\n"
	"<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->\n"
	"<!-- ai:permission-prompt:v1 sig=432f8f1876e6 -->\n"
)


def _issue(**overrides):
	issue = {
		"number": 4843,
		"state": "open",
		"user": {"login": LOGIN, "type": "User"},
		"author_association": "OWNER",
		"created_at": CREATED,
		"labels": [{"name": "ai:permission-prompt"}, {"name": "ai:claude"}],
		"body": FILED_BODY,
	}
	issue.update(overrides)
	return issue


def _events(actor=LOGIN, at=CREATED, label="ai:permission-prompt"):
	return [{"event": "labeled", "actor": {"login": actor}, "created_at": at, "label": {"name": label}}]


def _target(**overrides):
	target = {"number": 4678, "state": "open", "state_reason": None, "user": {"login": LOGIN}, "body": "**Occurrences:** 1 (2026-09-27T23:49:14Z – 2026-09-27T23:49:14Z), session `session_01N`"}
	target.update(overrides)
	return target


def _fix(**overrides):
	fix = {
		"number": 4684,
		"state": "open",
		"merged_at": None,
		"title": "Never edit files with an inline interpreter — project integration",
		"body": "Fixes #4678",
		"head": {"ref": "claude/implement-plan-issue-4678-inline-interpreter-rule"},
		"base": {"ref": "main", "repo": {"default_branch": "main"}},
	}
	fix.update(overrides)
	return fix


def _decide(issue=None, events="default", target=None, fix=None, login=LOGIN, issue_number=4843, target_number=4678):
	return pp.decide_duplicate_close(
		issue or _issue(),
		_events() if events == "default" else events,
		target or _target(),
		fix or _fix(),
		login,
		issue_number,
		target_number,
	)


def test_pipeline_filed_duplicate_of_an_open_fix_is_eligible():
	verdict = _decide()
	assert verdict["eligible"] is True and verdict["reasons"] == []
	assert verdict["signature"] == "432f8f1876e6"
	assert verdict["class"] == "inline-interpreter-write"
	assert verdict["occurrences"].startswith("**Occurrences:** 1 (2026-09-28T12:52:05Z") and "session_01B" in verdict["occurrences"]
	assert "session_01N" in verdict["target_occurrences"]
	assert verdict["fix_pr"] == 4684 and verdict["fix_pr_state"] == "open"


def test_issue_payloads_with_a_null_pull_request_key_are_issues():
	# The REST schema marks `pull_request` optional; a null value is still an issue.
	verdict = _decide(issue=_issue(pull_request=None), target=_target(pull_request=None))
	assert verdict["eligible"] is True and verdict["reasons"] == []


def test_duplicate_check_ignores_markers_inside_the_command_example():
	# Markers and the "Filed by" line quoted in a fenced command are not the filer's.
	verdict = _decide(
		issue=_issue(body=f"Written by hand.\n\n````text\n{FILED_BODY}````\n"),
		target=_target(body=f"````text\n{FILED_BODY}````\n"),
	)
	assert verdict["eligible"] is False
	assert "#4843 has no ai:permission-prompt signature marker" in verdict["reasons"]
	assert f"#4843 has no '{pp.FILED_BY_LINE}' line" in verdict["reasons"]
	assert verdict["signature"] is None and verdict["class"] is None and verdict["target_class"] is None
	# The fenced `**Occurrences:**` line is not the issue's evidence either.
	assert verdict["occurrences"] is None and verdict["target_occurrences"] is None


def test_duplicate_check_reads_the_occurrences_line_outside_the_command_example():
	fenced = "````text\n**Occurrences:** 99 (forged), session `x`\n````\n"
	verdict = _decide(issue=_issue(body=fenced + FILED_BODY), target=_target(body=FILED_BODY + fenced))
	assert verdict["eligible"] is True
	real = "**Occurrences:** 1 (2026-09-28T12:52:05Z – 2026-09-28T12:52:05Z), session `session_01B`"
	assert verdict["occurrences"] == real and verdict["target_occurrences"] == real


def test_closed_completed_target_with_a_fix_merged_into_the_default_branch_is_eligible():
	verdict = _decide(target=_target(state="closed", state_reason="completed"), fix=_fix(state="closed", merged_at="2026-09-28T20:00:00Z"))
	assert verdict["eligible"] is True and verdict["fix_pr_state"] == "merged"


@pytest.mark.parametrize(
	"kwargs, reason",
	[
		# Condition 1: human-filed or relabelled issues still ask.
		({"issue": _issue(body="A human wrote this.\n")}, "no ai:permission-prompt signature marker"),
		({"issue": _issue(body="<!-- ai:permission-prompt:v1 sig=432f8f1876e6 -->\n")}, "no 'Filed by"),
		({"issue": _issue(user={"login": "someone-else", "type": "User"})}, "is not the session account"),
		({"login": ""}, "the authenticated account is unknown"),
		({"issue": _issue(labels=[{"name": "ai:claude"}])}, "is not labelled ai:permission-prompt"),
		({"events": _events(at="2026-09-28T15:00:00Z")}, "after creation, not at creation"),
		({"events": _events(actor="someone-else")}, "not the issue author"),
		({"events": []}, "no labeled event"),
		({"events": None}, "events could not be read"),
		({"events": _events() * 100}, "not verifiable in one page"),
		({"issue": _issue(state="closed")}, "#4843 is not open"),
		({"issue": _issue(pull_request={"url": f"https://api.github.com/repos/{SLUG}/pulls/4843"})}, "is a pull request"),
		# Condition 2: the target and its fix.
		({"target": _target(state="closed", state_reason="not_planned")}, "closed as not_planned, not completed"),
		({"target": _target(pull_request={"url": f"https://api.github.com/repos/{SLUG}/pulls/4678"})}, "#4678 is a pull request"),
		({"target_number": 4843}, "the target is the issue itself"),
		({"fix": _fix(state="closed")}, "closed without merging"),
		({"fix": _fix(body="Unrelated", title="Other", head={"ref": "claude/other"})}, "does not reference #4678"),
		({"fix": _fix(body="Refs #46789", title="x", head={"ref": "claude/issue-46789-x"})}, "does not reference #4678"),
		({"target": _target(state="closed", state_reason="completed")}, "is closed but PR #4684 is not merged"),
		(
			{"target": _target(state="closed", state_reason="completed"), "fix": _fix(state="closed", merged_at="2026-09-28T20:00:00Z", base={"ref": "claude/implement-plan-x", "repo": {"default_branch": "main"}})},
			"not the default branch main",
		),
	],
)
def test_each_failed_condition_blocks_the_close(kwargs, reason):
	verdict = _decide(**kwargs)
	assert verdict["eligible"] is False
	assert any(reason in item for item in verdict["reasons"]), verdict["reasons"]


def test_fix_pr_linked_by_branch_name_only_is_accepted():
	verdict = _decide(fix=_fix(body="", title="Phase 1"))
	assert verdict["eligible"] is True


def test_duplicate_check_reads_at_most_five_times(monkeypatch):
	calls: list[str] = []
	responses = {
		"user": {"login": LOGIN},
		f"repos/{SLUG}/issues/4843": _issue(),
		f"repos/{SLUG}/issues/4843/events?per_page=100": _events(),
		f"repos/{SLUG}/issues/4678": _target(),
		f"repos/{SLUG}/pulls/4684": _fix(),
	}

	def fake(path):
		calls.append(path)
		return responses[path]

	monkeypatch.setattr(pp.check_in_status, "gh_api", fake)
	monkeypatch.setattr(pp.check_in_status, "_gh_api_json", fake)
	code, verdict = pp.duplicate_check(SLUG, 4843, 4678, 4684)
	assert code == 0 and verdict["eligible"] is True
	assert len(calls) == 5


def test_duplicate_check_read_failure_exits_2_and_is_not_eligible(monkeypatch):
	def boom(path):
		raise pp.check_in_status.ReadError("proxy 403")

	monkeypatch.setattr(pp.check_in_status, "gh_api", boom)
	code, verdict = pp.duplicate_check(SLUG, 4843, 4678, 4684)
	assert code == 2 and verdict["eligible"] is False and verdict["reasons"] == ["proxy 403"]


def test_duplicate_check_on_itself_makes_no_call(monkeypatch):
	monkeypatch.setattr(pp.check_in_status, "gh_api", lambda path: pytest.fail("no read expected"))
	code, verdict = pp.duplicate_check(SLUG, 4843, 4843, 4684)
	assert code == 0 and verdict["eligible"] is False


def test_main_prints_one_json_line(monkeypatch, capsys):
	monkeypatch.setattr(pp, "duplicate_check", lambda repo, issue, target, fix: (0, {"eligible": True, "reasons": []}))
	assert pp.main(["duplicate-check", "--repo", SLUG, "--issue", "4843", "--target", "4678", "--fix-pr", "4684"]) == 0
	assert json.loads(capsys.readouterr().out) == {"eligible": True, "reasons": []}


@pytest.mark.parametrize("argv", [["--repo", "bad", "--issue", "1", "--target", "2", "--fix-pr", "3"], ["--repo", SLUG, "--issue", "0", "--target", "2", "--fix-pr", "3"]])
def test_main_rejects_bad_arguments(argv, capsys):
	assert pp.main(["duplicate-check", *argv]) == 1
	assert json.loads(capsys.readouterr().out)["eligible"] is False


def test_main_usage_error_exits_2_without_json(capsys):
	with pytest.raises(SystemExit) as exc:
		pp.main(["duplicate-check", "--repo", SLUG, "--issue", "abc", "--target", "2", "--fix-pr", "3"])
	assert exc.value.code == 2
	assert capsys.readouterr().out == ""
	assert "usage error" in pp.__doc__ and "exits 2" in " ".join(pp.__doc__.split())


def test_file_still_validates_the_session_label():
	assert pp.main(["file", "--session-label", "bad label"]) == 1


def test_missing_security_pass_skip_only_disables_duplicate_check(tmp_path, monkeypatch, capsys):
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	(scripts / "permission_prompts.py").write_bytes(TEMPLATE_SCRIPT_PATH.read_bytes())
	(scripts / "check_in_status.py").write_bytes((TEMPLATE_SCRIPT_PATH.parent / "check_in_status.py").read_bytes())
	module = _load("permission_prompts_without_skip_check", scripts / "permission_prompts.py")
	assert module.security_pass_skip is None and "security_pass_skip.py" in module._SKIP_CHECK_ERROR
	assert module.main(["report", "--log-dir", str(_log(tmp_path, [_record("ls -la")]))]) == 0
	capsys.readouterr()
	monkeypatch.setattr(module.check_in_status, "gh_api", lambda path: pytest.fail("no read expected"))
	assert module.main(["duplicate-check", "--repo", SLUG, "--issue", "4843", "--target", "4678", "--fix-pr", "4684"]) == 2
	verdict = json.loads(capsys.readouterr().out)
	assert verdict["eligible"] is False and "security_pass_skip.py" in verdict["reasons"][0]


# ──────────────────────────────────────────────────────────────────
# Instruction text: the carve-out and its limits
# ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def claude_md() -> str:
	return _flat(CLAUDE_MD)


@pytest.fixture(scope="module")
def issue_cmd() -> str:
	return _flat(TEMPLATE_ISSUE_COMMAND)


def test_section_23c_names_the_carve_out(claude_md):
	section = claude_md[claude_md.index("### C) Destructive & Administrative Writes") : claude_md.index("### D) Transport & Tool Precedence")]
	assert "**Pipeline-filed permission-prompt duplicates may be closed.**" in section
	assert '§23.I "Closing pipeline-filed duplicates"' in section
	assert "It never extends to a human-filed or relabelled issue, to a PR the session did not open, or to any other operation in this list" in section
	assert "anything outside those conditions keeps the ask" in section


def test_section_23i_states_the_four_conditions(claude_md):
	section = claude_md[claude_md.index("### I) Permission Prompt Reports") : claude_md.index("## §24.")]
	assert "**Closing pipeline-filed duplicates**" in section
	assert "1. **Pipeline-filed.**" in section and "A human-filed or relabelled issue still asks." in section
	assert "its author is the workflow/session account, the author applied the label at creation" in section
	assert "2. **An open or merged fix exists.**" in section
	assert "a closed-as-completed issue whose fix is on the default branch" in section
	assert "3. **Matching evidence.**" in section
	assert "the same denial reason (for example `Classifier unavailable`) or the same command class" in section
	assert "the session ids and timestamps involved" in section
	assert "`state_reason: duplicate` and `duplicate_of: <target>`" in section
	assert "4. **No project was created.**" in section
	assert "This covers only PRs it opened itself in this project" in section
	assert "permission_prompts.py duplicate-check --repo <owner>/<repo> --issue <N> --target <M> --fix-pr <P>" in section
	assert 'the session acts only on `"eligible": true`' in section
	assert "never extends to human-filed issues, PRs this session did not open, or any other §23.C operation" in section
	assert "<!-- ai:permission-prompt-class:v1 class=inline-interpreter-write -->" in section
	assert "tests/test_permission_prompt_duplicates.py" in section


def test_section_28c_names_the_exception(claude_md):
	section = claude_md[claude_md.index("### C) Never auto-decided") : claude_md.index("### D) Recording")]
	assert "The one exception is §23.C's carve-out for pipeline-filed `ai:permission-prompt` duplicates" in section


def test_issue_command_step_5a_applies_the_carve_out(issue_cmd):
	assert "5a. **Duplicate check.**" in issue_cmd
	assert issue_cmd.index("5. **Read project context.**") < issue_cmd.index("5a. **Duplicate check.**") < issue_cmd.index("6. **Write the single-phase plan.**")
	assert "`PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py duplicate-check --repo <owner>/<repo> --issue <N> --target <M> --fix-pr <P>`" in issue_cmd
	assert "Never decide this from the labels yourself." in issue_cmd
	assert "`<!-- ai:claude-duplicate-close:v1 target=<M> -->`" in issue_cmd
	assert "the session ids and timestamps involved" in issue_cmd
	assert "`state_reason: duplicate`, `duplicate_of: <M>`" in issue_cmd
	assert "Close no other PR and delete no branch." in issue_cmd
	assert "closing stays a §23.C ask-first operation" in issue_cmd
	assert "`<!-- ai:claude-blocked:v1 -->`" in issue_cmd
	assert "Any other issue is never closed here." in issue_cmd
	assert "**Close only a pipeline-filed duplicate, and only on the script's verdict.**" in issue_cmd
	# The command is unattended (§28.A): it never contains a stop-and-ask instruction.
	assert "stop and ask" not in issue_cmd


def test_duplicate_check_runs_under_the_existing_allow_rule():
	for path in (REPO_ROOT / ".claude" / "settings.json", REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"):
		allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
		assert "Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py *)" in allow


def test_ci_runs_this_file():
	assert "tests/test_permission_prompt_duplicates.py" in CI_WORKFLOW.read_text(encoding="utf-8")
