#!/usr/bin/env python3
"""Owner-authorization gate for protected-equivalent paths (issue #4919).

Covers `scripts/protected_path_authorization.py` (path matching, the
authorization comment rules, the PR and release decisions, the instruction
comment), `scripts/protected_path_gate.sh` (the merge wrapper, with a stubbed
checker and command), and the wiring: every `gh pr merge` in `scripts/*.sh`
is wrapped, the gate is sourced and staged wherever those scripts run, the
release workflows run the release check before the tag moves, CI runs this
file, and CLAUDE.md documents the rule.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "protected_path_authorization.py"
GATE_PATH = REPO_ROOT / "scripts" / "protected_path_gate.sh"
SCRIPTS_DIR = REPO_ROOT / "scripts"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

HEAD = "a" * 40
OTHER = "b" * 40


def _load():
	spec = importlib.util.spec_from_file_location("protected_path_authorization", SCRIPT_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


ppa = _load()


def _comment(body=f"/authorize-protected-paths {HEAD}", **overrides):
	comment = {
		"id": 1,
		"body": body,
		"user": {"login": "owner", "type": "User"},
		"author_association": "OWNER",
		"performed_via_github_app": None,
		"created_at": "2026-09-29T10:00:00Z",
		"updated_at": "2026-09-29T10:00:00Z",
	}
	comment.update(overrides)
	return comment


def _pr(head=HEAD, changed_files=1, number=7):
	return {"number": number, "head": {"sha": head}, "changed_files": changed_files}


# ──────────────────────────────────────────────────────────────────
# Protected-equivalent paths
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
	".claude/commands/verify-activation.md",
	".claude/settings.json",
	"workflow-templates/.claude/commands/deploy-activate.md",
	"workflow-templates/.claude/hooks/pr_watch_guard.py",
	"tests/test_claude_template_parity.py",
	"scripts/protected_path_authorization.py",
	"scripts/protected_path_gate.sh",
	".Claude/settings.json",
	"Workflow-Templates/.CLAUDE/x",
	"./.claude/x",
])
def test_protected_paths(path):
	assert ppa.is_protected_path(path)


@pytest.mark.parametrize("path", [
	".claudex/settings.json",
	"docs/.claude.md",
	"workflow-templates/claude/x",
	"workflow-templates/CLAUDE.md",
	"CLAUDE.md",
	"sub/.claude/settings.json",
	"tests/test_claude_template_parity.py.bak",
	"scripts/protected_path_gate.sh.orig",
	"",
	None,
])
def test_unprotected_paths(path):
	assert not ppa.is_protected_path(path)


def test_rename_source_counts():
	files = [
		{"filename": "docs/moved.md", "previous_filename": ".claude/commands/x.md"},
		{"filename": "README.md"},
	]
	assert ppa.protected_files(files) == [".claude/commands/x.md"]


# ──────────────────────────────────────────────────────────────────
# Authorization comment rules
# ──────────────────────────────────────────────────────────────────


def test_owner_command_authorizes():
	assert ppa.comment_authorizes(_comment(), HEAD) == ""
	assert ppa.comment_authorizes(_comment(body=f"  /authorize-protected-paths {HEAD}\n"), HEAD) == ""
	for association in ("MEMBER", "COLLABORATOR"):
		assert ppa.comment_authorizes(_comment(author_association=association), HEAD) == ""


@pytest.mark.parametrize("overrides, reason", [
	({"body": f"/authorize-protected-paths {OTHER}"}, "authorizes"),
	({"body": f"/authorize-protected-paths {HEAD[:12]}"}, "not exactly"),
	({"body": f"/authorize-protected-paths {HEAD} please"}, "not exactly"),
	({"body": f"> /authorize-protected-paths {HEAD}"}, "not exactly"),
	({"body": f"LGTM\n/authorize-protected-paths {HEAD}"}, "not exactly"),
	({"body": f"/authorize-protected-paths {HEAD.upper()}"}, "not exactly"),
	({"body": None}, "not exactly"),
	({"user": {"login": "github-actions[bot]", "type": "Bot"}}, "not User"),
	({"author_association": "CONTRIBUTOR"}, "not trusted"),
	({"author_association": "NONE"}, "not trusted"),
	({"performed_via_github_app": {"slug": "claude"}}, "GitHub App claude"),
	({"performed_via_github_app": {"slug": "some-other-app"}}, "GitHub App some-other-app"),
	({"updated_at": "2026-09-29T10:05:00Z"}, "edited"),
	({"created_at": None, "updated_at": None}, "edited"),
])
def test_comment_rejections(overrides, reason):
	assert reason in ppa.comment_authorizes(_comment(**overrides), HEAD)


def test_instruction_comment_never_authorizes():
	body = ppa.instruction_body(HEAD, [".claude/settings.json"])
	assert f"<!-- ai:protected-path-authorization:v1 head={HEAD} -->" in body
	assert f"/authorize-protected-paths {HEAD}" in body
	assert ppa.comment_authorizes(_comment(body=body), HEAD) != ""


def test_instruction_comment_caps_the_path_list():
	paths = [f".claude/commands/cmd-{n:04d}.md" for n in range(3000)]
	body = ppa.instruction_body(HEAD, paths)
	listed = [line for line in body.splitlines() if line.startswith("- `")]
	assert listed == [f"- `{path}`" for path in paths[:ppa.PATHS_IN_OUTPUT]]
	assert f"- … and {3000 - ppa.PATHS_IN_OUTPUT} more" in body
	assert len(body) < 5000
	assert "- … and" not in ppa.instruction_body(HEAD, paths[:ppa.PATHS_IN_OUTPUT])


def test_authorizing_comment_requires_full_sha():
	assert ppa.authorizing_comment([_comment()], "") is None
	assert ppa.authorizing_comment([_comment(body="/authorize-protected-paths short")], "short") is None


# ──────────────────────────────────────────────────────────────────
# PR decision
# ──────────────────────────────────────────────────────────────────


def test_unprotected_pr_is_allowed_without_reading_comments():
	decision = ppa.evaluate_pr(_pr(), [{"filename": "README.md"}], True, None)
	assert decision["decision"] == "allow" and decision["protected"] is False


def test_protected_pr_needs_the_owner_comment():
	files = [{"filename": ".claude/commands/x.md"}]
	blocked = ppa.evaluate_pr(_pr(), files, True, [_comment(performed_via_github_app={"slug": "claude"})])
	assert blocked["decision"] == "block" and blocked["protected"] is True
	assert blocked["paths"] == [".claude/commands/x.md"]
	allowed = ppa.evaluate_pr(_pr(), files, True, [_comment()])
	assert allowed["decision"] == "allow" and allowed["authorized"] is True and allowed["head"] == HEAD


def test_pin_only_change_is_protected():
	decision = ppa.evaluate_pr(_pr(), [{"filename": "tests/test_claude_template_parity.py"}], True, [])
	assert decision["decision"] == "block"


def test_truncated_file_list_counts_as_protected():
	assert ppa.evaluate_pr(_pr(changed_files=5000), [{"filename": "README.md"}], True, [])["decision"] == "block"
	assert ppa.evaluate_pr(_pr(), [{"filename": "README.md"}], False, [])["decision"] == "block"
	allowed = ppa.evaluate_pr(_pr(changed_files=5000), [{"filename": "README.md"}], True, [_comment()])
	assert allowed["decision"] == "allow"


def test_head_mismatch_and_missing_head_block():
	files = [{"filename": "README.md"}]
	protected = [{"filename": ".claude/settings.json"}]
	assert ppa.evaluate_pr(_pr(), files, True, None, expected_head=OTHER)["decision"] == "block"
	assert ppa.evaluate_pr(_pr(head=""), files, True, None, expected_head=HEAD)["decision"] == "block"
	# A protected PR needs a real head to bind and authorize.
	assert ppa.evaluate_pr(_pr(head=""), protected, True, [_comment()])["decision"] == "block"
	assert ppa.evaluate_pr({"number": 7}, protected, True, [_comment()])["decision"] == "block"
	# An unprotected PR merges as before, whatever its head looks like.
	assert ppa.evaluate_pr(_pr(head="sha910"), files, True, None)["decision"] == "allow"


def test_approval_of_an_older_head_does_not_carry_over():
	files = [{"filename": "workflow-templates/.claude/commands/verify-activation.md"}]
	decision = ppa.evaluate_pr(_pr(head=OTHER), files, True, [_comment()])
	assert decision["decision"] == "block"


class _FakeGitHub:
	def __init__(self, pr, files, comments):
		self.pr, self.files, self.comments = pr, files, comments
		self.reads: list[str] = []
		self.posts: list[tuple[str, str]] = []

	def get(self, path):
		self.reads.append(path)
		if path.endswith("/pulls/7"):
			return self.pr
		if "/pulls/7/files" in path:
			return self.files if path.endswith("page=1") else []
		if "/issues/7/comments" in path:
			return self.comments if path.endswith("page=1") else []
		raise AssertionError(path)

	def post(self, path, body):
		self.posts.append((path, body))


def test_check_pr_reads_comments_only_for_a_protected_pr():
	fake = _FakeGitHub(_pr(), [{"filename": "README.md"}], [])
	decision = ppa.check_pr("o/r", 7, post_instructions=True, get=fake.get, post=fake.post)
	assert decision["decision"] == "allow"
	assert len(fake.reads) == 2 and not any("comments" in read for read in fake.reads)
	assert fake.posts == []


def test_check_pr_posts_instructions_once_per_head():
	fake = _FakeGitHub(_pr(), [{"filename": ".claude/settings.json"}], [])
	decision = ppa.check_pr("o/r", 7, post_instructions=True, get=fake.get, post=fake.post)
	assert decision["decision"] == "block" and decision["instructions"] == "posted"
	assert len(fake.posts) == 1 and fake.posts[0][0] == "repos/o/r/issues/7/comments"
	assert f"head={HEAD}" in fake.posts[0][1]
	fake.comments = [{"body": fake.posts[0][1]}]
	again = ppa.check_pr("o/r", 7, post_instructions=True, get=fake.get, post=fake.post)
	assert again["instructions"] == "already posted" and len(fake.posts) == 1


def test_check_pr_without_post_flag_posts_nothing():
	fake = _FakeGitHub(_pr(), [{"filename": ".claude/settings.json"}], [])
	assert ppa.check_pr("o/r", 7, get=fake.get, post=fake.post)["decision"] == "block"
	assert fake.posts == []


def test_check_pr_rejects_bad_arguments():
	for kwargs in ({"repo": "bad", "number": 7}, {"repo": "o/r", "number": 0}, {"repo": "o/r", "number": 7, "expected_head": "abc"}):
		with pytest.raises(ValueError):
			ppa.check_pr(**kwargs, get=lambda path: {})


def test_read_errors_are_retried_then_raised(monkeypatch):
	calls = []

	def failing(args):
		calls.append(args)
		raise ppa.ReadError("boom")

	monkeypatch.setattr(ppa, "_gh_json", failing)
	sleeps = []
	with pytest.raises(ppa.ReadError):
		ppa.gh_get_json("repos/o/r/pulls/7", sleep=sleeps.append)
	assert len(calls) == ppa.READ_ATTEMPTS and sleeps == [2, 4]


def test_cli_exit_codes(monkeypatch, capsys):
	fake = _FakeGitHub(_pr(), [{"filename": ".claude/settings.json"}], [])
	monkeypatch.setattr(ppa, "gh_get_json", fake.get)
	monkeypatch.setattr(ppa, "_post_comment", fake.post)
	assert ppa.main(["pr", "--repo", "o/r", "--pr", "7"]) == ppa.EXIT_BLOCKED
	assert json.loads(capsys.readouterr().out)["decision"] == "block"
	fake.comments = [_comment()]
	assert ppa.main(["pr", "--repo", "o/r", "--pr", "7"]) == ppa.EXIT_OK
	assert ppa.main(["pr", "--repo", "bad", "--pr", "7"]) == ppa.EXIT_USAGE
	assert ppa.main(["nope"]) == ppa.EXIT_USAGE

	def broken(path):
		raise ppa.ReadError("down")

	monkeypatch.setattr(ppa, "gh_get_json", broken)
	capsys.readouterr()
	assert ppa.main(["pr", "--repo", "o/r", "--pr", "7"]) == ppa.EXIT_READ_ERROR
	assert json.loads(capsys.readouterr().out)["decision"] == "block"


# ──────────────────────────────────────────────────────────────────
# Release decision
# ──────────────────────────────────────────────────────────────────


def _merged(number, head=HEAD):
	return {"number": number, "merged_at": "2026-09-30T00:00:00Z", "head": {"sha": head}}


def test_evaluate_release_rules():
	pulls = {
		"c-auth": [_merged(1)],
		"c-unauth": [_merged(2)],
		"c-nopr": [{"number": 3, "merged_at": None, "head": {"sha": HEAD}}],
		"c-otherhead": [_merged(4, head=OTHER)],
		"c-two": [_merged(5, head=OTHER), _merged(1)],
	}
	comments = {1: [_comment()], 2: [_comment(performed_via_github_app={"slug": "claude"})], 4: [_comment()], 5: []}
	result = ppa.evaluate_release(
		["c-old", "c-auth", "c-unauth", "c-nopr", "c-otherhead", "c-two"],
		{"c-old"},
		lambda sha: pulls[sha],
		lambda number: comments[number],
	)
	assert result["decision"] == "block"
	assert result["checked"] == 6 and result["grandfathered"] == 1 and result["authorized"] == 2
	assert [entry["commit"] for entry in result["blocked"]] == ["c-unauth", "c-nopr", "c-otherhead"]


def test_evaluate_release_passes_when_everything_is_covered():
	result = ppa.evaluate_release(["c1", "c2"], {"c1"}, lambda sha: [_merged(1)], lambda number: [_comment()])
	assert result == {"decision": "pass", "checked": 2, "authorized": 1, "grandfathered": 1, "blocked": []}


def _git(repo, *args, env=None):
	full_env = dict(os.environ)
	full_env.update({
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
	})
	full_env.update(env or {})
	return subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args], check=True, capture_output=True, text=True, env=full_env).stdout.strip()


def _commit(repo, path, text, message):
	target = repo / path
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(text, encoding="utf-8")
	_git(repo, "add", path)
	_git(repo, "commit", "-q", "-m", message)
	return _git(repo, "rev-parse", "HEAD")


def test_check_release_on_a_real_history(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	old_claude = _commit(repo, ".claude/commands/a.md", "one\n", "old claude change")
	_git(repo, "tag", "stable")
	pre_gate = _commit(repo, "workflow-templates/.claude/commands/a.md", "one\n", "pre-gate template change")
	_commit(repo, "README.md", "readme\n", "unprotected")
	arrival = _commit(repo, "scripts/protected_path_authorization.py", "# gate\n", "gate arrives")
	after_auth = _commit(repo, ".claude/commands/a.md", "two\n", "authorized change")
	_git(repo, "checkout", "-q", "-b", "side", arrival)
	side = _commit(repo, "tests/test_claude_template_parity.py", "pins\n", "side-branch pin change")
	_git(repo, "checkout", "-q", "main")
	_git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")

	prs = {after_auth: [_merged(10)], side: [_merged(11)]}
	comments = {10: [_comment()], 11: []}

	def get(path):
		match = re.fullmatch(r"repos/o/r/commits/([0-9a-f]{40})/pulls", path)
		if match:
			return prs[match.group(1)]
		match = re.fullmatch(r"repos/o/r/issues/(\d+)/comments\?per_page=100&page=1", path)
		if match:
			return comments[int(match.group(1))]
		raise AssertionError(path)

	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=get)
	assert result["gate_arrival"] == arrival
	assert result["checked"] == 4
	assert result["grandfathered"] == 2
	assert result["authorized"] == 1
	assert [entry["commit"] for entry in result["blocked"]] == [side]
	assert result["base"] == old_claude, "the previous release is the range base, never checked itself"
	assert pre_gate not in json.dumps(result["blocked"])

	comments[11] = [_comment()]
	prs[side] = [_merged(11)]
	assert ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=get)["decision"] == "pass"

	# A first release (no stable tag) checks the whole history, still grandfathered.
	_git(repo, "tag", "-d", "stable")
	whole = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=get)
	assert whole["base"] is None and whole["grandfathered"] == 3 and whole["decision"] == "pass"


def test_check_release_without_the_gate_script_fails_closed(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_commit(repo, ".claude/x", "x\n", "claude")
	with pytest.raises(ppa.ReadError):
		ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=lambda path: [])
	with pytest.raises(ppa.ReadError):
		ppa.check_release("o/r", "refs/tags/stable", "no-such-ref", git_dir=str(repo), get=lambda path: [])


# ──────────────────────────────────────────────────────────────────
# Merge wrapper (scripts/protected_path_gate.sh)
# ──────────────────────────────────────────────────────────────────


def _run_gate(tmp_path, decision_json, checker_rc, merge_call):
	checker = tmp_path / "checker.py"
	checker_log = tmp_path / "checker_args.jsonl"
	checker.write_text(textwrap.dedent(f"""\
		import json, sys
		with open({str(checker_log)!r}, "a", encoding="utf-8") as fh:
		    fh.write(json.dumps(sys.argv[1:]) + "\\n")
		sys.stdout.write({json.dumps(decision_json)!r})
		sys.exit({checker_rc})
		"""), encoding="utf-8")
	merge_log = tmp_path / "merge_args.jsonl"
	script = textwrap.dedent(f"""\
		set -euo pipefail
		export PROTECTED_PATH_GATE_CHECKER={str(checker)!r}
		source {str(GATE_PATH)!r}
		fake_merge() {{ python3 -c 'import json,sys; open(sys.argv[1],"a").write(json.dumps(sys.argv[2:])+"\\n")' {str(merge_log)!r} "$@"; }}
		rc=0
		{merge_call} || rc=$?
		{merge_call} || rc2=$?
		echo "rc=${{rc}}"
		""")
	proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "GITHUB_REPOSITORY": "o/r"})
	merges = [json.loads(line) for line in merge_log.read_text(encoding="utf-8").splitlines()] if merge_log.exists() else []
	checks = [json.loads(line) for line in checker_log.read_text(encoding="utf-8").splitlines()] if checker_log.exists() else []
	return proc, merges, checks


def test_gate_passes_unprotected_merges_through_unchanged(tmp_path):
	proc, merges, checks = _run_gate(
		tmp_path,
		{"decision": "allow", "protected": False, "head": HEAD},
		0,
		'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --auto',
	)
	assert "rc=0" in proc.stdout, proc.stderr
	assert merges == [["gh", "pr", "merge", "42", "--repo", "o/r", "--squash", "--auto"]] * 2
	assert len(checks) == 1, "the second identical call must reuse the cached decision"
	assert checks[0][:5] == ["pr", "--repo", "o/r", "--pr", "42"] and "--post-instructions" in checks[0]


def test_gate_binds_an_authorized_protected_merge_to_its_head(tmp_path):
	proc, merges, _checks = _run_gate(
		tmp_path,
		{"decision": "allow", "protected": True, "head": HEAD, "reason": "authorized by comment 1"},
		0,
		'protected_path_guarded_merge fake_merge gh pr merge 42 --squash',
	)
	assert "rc=0" in proc.stdout, proc.stderr
	assert merges[0] == ["gh", "pr", "merge", "42", "--squash", "--match-head-commit", HEAD]
	assert f"PROTECTED_PATH_GATE pr=42 head={HEAD} decision=allow protected=true" in proc.stderr


def test_gate_keeps_an_existing_head_binding(tmp_path):
	proc, merges, checks = _run_gate(
		tmp_path,
		{"decision": "allow", "protected": True, "head": HEAD},
		0,
		f'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --match-head-commit {HEAD}',
	)
	assert "rc=0" in proc.stdout, proc.stderr
	assert merges[0].count("--match-head-commit") == 1
	assert checks[0][-2:] == ["--head", HEAD]


@pytest.mark.parametrize("decision_json, checker_rc", [
	({"decision": "block", "protected": True, "head": HEAD, "reason": "no owner comment"}, 3),
	({"decision": "block", "error": "gh api failed"}, 2),
	({"decision": "allow", "protected": True, "head": ""}, 0),
	("not json", 0),
])
def test_gate_refuses_and_does_not_run_the_command(tmp_path, decision_json, checker_rc):
	proc, merges, _checks = _run_gate(
		tmp_path, decision_json, checker_rc,
		'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --auto',
	)
	assert "rc=3" in proc.stdout, proc.stderr
	assert merges == []
	assert "decision=block" in proc.stderr


def test_gate_refuses_unparseable_calls(tmp_path):
	for call in (
		'protected_path_guarded_merge fake_merge gh pr merge --repo o/r --squash',
		'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --match-head-commit nothex',
	):
		proc, merges, checks = _run_gate(tmp_path, {"decision": "allow", "protected": False}, 0, call)
		assert "rc=3" in proc.stdout, proc.stderr
		assert merges == [] and checks == []


def test_gate_fails_closed_when_the_checker_is_missing(tmp_path):
	script = textwrap.dedent(f"""\
		export PROTECTED_PATH_GATE_CHECKER={str(tmp_path / "missing.py")!r}
		source {str(GATE_PATH)!r}
		ran=no
		fake_merge() {{ ran=yes; }}
		protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash; echo "rc=$? ran=${{ran}}"
		""")
	proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
	assert "rc=3 ran=no" in proc.stdout, proc.stderr


# ──────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────

# A `gh pr merge` call is either shaped like one (the PR expansion follows
# `pr merge`) or sits where a command starts, whatever follows it; the second
# form catches a call that puts a flag before the PR number. Mentions inside
# messages (`echo "… 'gh pr merge --auto' failed"`) match neither.
MERGE_CALL_RE = re.compile(
	r'gh pr merge "\$\{'
	r'|(?:^|[;&|({!]|\$\(|\b(?:if|elif|then|else|do|while|until|gh_retry|protected_path_guarded_merge))\s*gh pr merge\b'
)
PR_FIRST_RE = re.compile(r'gh pr merge "\$\{')


def _code_lines(path):
	for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
		if line.lstrip().startswith("#"):
			continue
		yield number, line


def _merge_call_problems(path):
	"""Return (calls found, problems) for every `gh pr merge` call in `path`.

	A call must be wrapped in `protected_path_guarded_merge`, and must put the
	PR number right after `pr merge`: the gate reads it from there and refuses
	(fails closed) any other shape, so a reordered call would never merge.
	"""
	found = 0
	problems = []
	for number, line in _code_lines(path):
		for match in MERGE_CALL_RE.finditer(line):
			found += 1
			start = line.index("gh pr merge", match.start())
			where = f"{path.name}:{number}: {line.strip()}"
			if not re.search(r"protected_path_guarded_merge (gh_retry )?$", line[:start]):
				problems.append(f"unwrapped: {where}")
			if not PR_FIRST_RE.match(line, start):
				problems.append(f"PR number not first after `pr merge`: {where}")
	return found, problems


def test_every_scripted_gh_pr_merge_is_wrapped():
	found = 0
	problems = []
	for path in sorted(SCRIPTS_DIR.glob("*.sh")):
		path_found, path_problems = _merge_call_problems(path)
		found += path_found
		problems += path_problems
	assert found >= 20, found
	assert not problems, "gh pr merge call(s) the gate cannot guard:\n" + "\n".join(problems)


@pytest.mark.parametrize("line, calls, expected", [
	('protected_path_guarded_merge gh_retry gh pr merge "${PR}" --repo "${R}" --squash', 1, []),
	('  if protected_path_guarded_merge gh pr merge "${PR}" --squash; then', 1, []),
	('gh pr merge "${PR}" --squash', 1, ["unwrapped"]),
	('x="$(gh pr merge "${PR}" --squash)"', 1, ["unwrapped"]),
	('protected_path_guarded_merge gh_retry gh pr merge --repo "${R}" "${PR}" --squash', 1, ["PR number not first after `pr merge`"]),
	('if gh_retry gh pr merge --squash "${PR}"; then', 1, ["unwrapped", "PR number not first after `pr merge`"]),
	('echo "  enabling auto-merge via \'gh pr merge --auto\'..."', 0, []),
	('echo "::warning::PR #${PR}: gh pr merge --auto failed; will retry."', 0, []),
])
def test_merge_call_scan_flags_unwrapped_and_reordered_calls(tmp_path, line, calls, expected):
	script = tmp_path / "sample.sh"
	script.write_text(f"# gh pr merge --repo o/r 1 (a comment)\n{line}\n", encoding="utf-8")
	found, problems = _merge_call_problems(script)
	assert found == calls
	assert [problem.split(": sample.sh")[0] for problem in problems] == expected


@pytest.mark.parametrize("script, needle", [
	("review_enable_auto_merge.sh", 'source "${SCRIPT_DIR}/protected_path_gate.sh"'),
	("review_rb_judge.sh", 'source "${SUPPORT_SCRIPTS_DIR}/protected_path_gate.sh"'),
	("orchestrate_poll_process.sh", 'source "${_OPP_LIB_DIR}/protected_path_gate.sh"'),
])
def test_merge_scripts_source_the_gate(script, needle):
	text = (SCRIPTS_DIR / script).read_text(encoding="utf-8")
	assert needle in text
	assert text.index(needle) < text.index("protected_path_guarded_merge gh")


def test_gate_files_are_staged_wherever_the_merge_scripts_run():
	stage = (SCRIPTS_DIR / "stage_workflow_support.sh").read_text(encoding="utf-8")
	required = re.search(r'^REQUIRED_BOOTSTRAP_SCRIPTS="([^"]+)"', stage, re.M).group(1).split()
	assert {"protected_path_gate.sh", "protected_path_authorization.py", "review_enable_auto_merge.sh", "review_rb_judge.sh"} <= set(required)
	poll = (WORKFLOWS_DIR / "orchestrate_poll.yml").read_text(encoding="utf-8")
	staged = [line for line in poll.splitlines() if "orchestrate_poll_process.sh" in line and line.strip().startswith("for f in")]
	assert staged and all("protected_path_gate.sh" in line and "protected_path_authorization.py" in line for line in staged)


def test_review_enable_auto_merge_refuses_without_ready_labels():
	text = (SCRIPTS_DIR / "review_enable_auto_merge.sh").read_text(encoding="utf-8")
	block = text[text.index('_auto_merge_rc=0'):]
	assert 'elif [ "${_auto_merge_rc}" -eq 3 ]; then' in block
	refuse = block[block.index('-eq 3 ]; then'):block.index("else")]
	assert "AUTOFIX_AUTO_MERGE_PROTECTED_PATH" in refuse
	assert 'record_auto_merge_ready_labels_allowed "true"' not in refuse


def _run_auto_merge_script_on_protected_pr(tmp_path, head_ref):
	"""Run the real review_enable_auto_merge.sh and gate with a fake `gh`.

	The PR changes `.claude/settings.json` and has no owner comment, so the
	real checker blocks it; the fake records every `gh` call.
	"""
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	calls_path = tmp_path / "gh_calls.jsonl"
	env_path = tmp_path / "github_env.txt"
	fake_gh = bin_dir / "gh"
	fake_gh.write_text(textwrap.dedent(f"""\
		#!/usr/bin/env python3
		import json, sys
		args = sys.argv[1:]
		with open({str(calls_path)!r}, "a", encoding="utf-8") as fh:
		    fh.write(json.dumps(args) + "\\n")
		if args[:1] == ["api"]:
		    path = next(a for a in args[1:] if not a.startswith("-"))
		    if "/labels" in path:
		        sys.exit(0)
		    if "/pulls/42/files" in path:
		        print(json.dumps([{{"filename": ".claude/settings.json", "status": "modified"}}]))
		        sys.exit(0)
		    if "/issues/42/comments" in path:
		        print("{{}}" if "POST" in args else "[]")
		        sys.exit(0)
		    if path.endswith("/pulls/42"):
		        print(json.dumps({{"head": {{"ref": {head_ref!r}, "sha": {HEAD!r}}}, "body": ""}}))
		        sys.exit(0)
		    sys.exit(1)
		if args[:2] == ["pr", "merge"]:
		    sys.exit(0)
		sys.exit(1)
		"""), encoding="utf-8")
	fake_gh.chmod(0o755)
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
		"GITHUB_REPOSITORY": "o/r",
		"PR_NUMBER": "42",
		"ENABLE_AUTO_MERGE": "true",
		"FORWARD_MERGE_FALLBACK_AUTO_MERGE": "true",
		"ORCH_INTEGRATION_BRANCH_PATTERN": "^orchestrator/project-",
		"INITIAL_HEAD_SHA": HEAD,
		"GH_TOKEN": "fake-token",
		"GITHUB_ENV": str(env_path),
		"GH_RETRY_MAX_ATTEMPTS": "1",
	}
	proc = subprocess.run(
		["bash", str(SCRIPTS_DIR / "review_enable_auto_merge.sh")],
		cwd=str(tmp_path), env=env, capture_output=True, text=True, check=False,
	)
	calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()] if calls_path.exists() else []
	labels_allowed = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
	return proc, calls, labels_allowed


@pytest.mark.parametrize("head_ref, action", [
	("ai/issue-42", "refuse"),
	("auto/forward-merge-stable-20260929", "refuse_merge_commit"),
])
def test_review_enable_auto_merge_refuses_an_unauthorized_protected_pr_end_to_end(tmp_path, head_ref, action):
	proc, calls, labels_allowed = _run_auto_merge_script_on_protected_pr(tmp_path, head_ref)
	assert proc.returncode == 0, proc.stderr
	assert not [call for call in calls if call[:2] == ["pr", "merge"]], calls
	assert f"AUTOFIX_AUTO_MERGE_PROTECTED_PATH pr=42 head_sha={HEAD} action={action}" in proc.stdout, proc.stdout
	assert "decision=block" in proc.stderr
	assert labels_allowed == ["AUTO_MERGE_READY_LABELS_ALLOWED=false"]
	posts = [call for call in calls if call[:1] == ["api"] and "POST" in call]
	assert len(posts) == 1 and f"head={HEAD}" in " ".join(posts[0])


def test_every_protected_path_is_refused_the_deterministic_skip():
	# review_autofix.yml's deterministic-skip merge is not wrapped by the gate
	# (AD-3): its PROTECTED_SKIP_SUPPRESSED guard must cover the gate's whole
	# protected set, including paths added to it later.
	from test_workflow_failure_heal import SHA_A, _run_gate as _run_review_gate

	paths = sorted(ppa.PROTECTED_FILES) + [f"{prefix}settings.json" for prefix in ppa.PROTECTED_PREFIXES]
	paths.append("Tests/Test_Claude_Template_Parity.py")
	for path in paths:
		for previous in (None, path):
			entry = {"filename": path, "status": "modified"}
			if previous:
				entry = {"filename": "docs/renamed.md", "previous_filename": previous, "status": "renamed"}
			with tempfile.TemporaryDirectory(prefix="gate-protected-path-") as tmp_name:
				result, outputs, _state = _run_review_gate(Path(tmp_name), comments=[], state_overrides={
					"pr": {
						"state": "open", "merged": False, "head": {"ref": "ai/issue-4919", "sha": SHA_A},
						"labels": [], "additions": 1, "deletions": 1, "changed_files": 1,
						"mergeable": True, "mergeable_state": "clean", "title": "test", "body": "",
					},
					"file_pages": [[entry]],
				}, extra_env={"AGENTS_MD_MATERIALITY_ENABLED": "false"})
			assert result.returncode == 0, (path, result.stderr)
			assert outputs["deterministic_skip"] == "false", (path, previous, result.stdout)
			assert "protected_suppressed=true" in result.stdout, (path, previous, result.stdout)


@pytest.mark.parametrize("workflow, before", [
	("test-and-mark-stable.yml", "- name: Verify CI passed on source branch"),
	("mark-stable.yml", "- name: Verify CI passed on stable"),
])
def test_release_workflows_gate_protected_changes_before_the_tag_moves(workflow, before):
	text = (WORKFLOWS_DIR / workflow).read_text(encoding="utf-8")
	# Quoted: an unquoted ` #4919` would start a YAML comment and cut the name.
	step = '- name: "Verify protected-path changes are authorized (issue #4919)"'
	assert text.count(step) == 1
	validate = text.index("\n  validate:\n")
	release = text.index("\n  release:\n")
	assert validate < text.index(before) < text.index(step) < release
	block = text[text.index(step):release]
	assert "scripts/protected_path_authorization.py release" in block
	assert "--base refs/tags/stable --head HEAD" in block
	assert "exit 1" in block
	release_job = text[release:release + 400]
	assert "validate" in release_job.split("needs:", 1)[1].splitlines()[0]


@pytest.mark.parametrize("prefix", ["PROTECTED_PATH_GATE", "AUTOFIX_AUTO_MERGE_PROTECTED_PATH"])
def test_log_prefixes_are_registered(prefix):
	agents_text = (REPO_ROOT / "agents.md").read_text(encoding="utf-8")
	assert f"- `{prefix}`" in agents_text
	assert f"LOG_PREFIX.name={prefix}\n" in agents_text


def test_ci_runs_this_file():
	assert "tests/test_protected_path_authorization.py" in (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")


def test_claude_md_documents_the_gate():
	text = " ".join(CLAUDE_MD.read_text(encoding="utf-8").split())
	start = text.index("### I) Permission Prompt Reports")
	section = text[start:text.index("## §24.", start)]
	assert "`/authorize-protected-paths <head sha>`" in section
	assert "`scripts/protected_path_gate.sh`" in section
	assert "`performed_via_github_app`" in section
	assert "A Claude session never posts it" in section
	assert "issue #4919" in section
