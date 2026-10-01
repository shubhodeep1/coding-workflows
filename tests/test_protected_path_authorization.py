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


def _auto_merge_pr(head=HEAD, auto_merge=True):
	pr = _pr(head=head)
	pr["node_id"] = "PR_node7"
	pr["auto_merge"] = {"merge_method": "squash"} if auto_merge else None
	return pr


def test_check_pr_turns_off_a_pending_auto_merge_on_a_blocked_protected_pr():
	# GitHub keeps auto-merge across pushes by anyone with write access, so an
	# auto-merge enabled on an earlier, unprotected head must not land an
	# unauthorized protected one (conformance run 1, issue #4919).
	fake = _FakeGitHub(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [])
	disabled = []
	decision = ppa.check_pr("o/r", 7, get=fake.get, post=fake.post, disable_auto_merge=True, disable=disabled.append)
	assert decision["decision"] == "block" and decision["auto_merge"] == "disabled"
	assert disabled == ["PR_node7"]
	assert len(fake.reads) == 3, "the PR read already carries auto_merge; no extra read"


@pytest.mark.parametrize("pr, files, comments, flag, expected", [
	(_auto_merge_pr(auto_merge=False), [{"filename": ".claude/settings.json"}], [], True, "none pending"),
	(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [_comment()], True, None),
	(_auto_merge_pr(), [{"filename": "README.md"}], [], True, None),
	(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [], False, None),
])
def test_check_pr_leaves_auto_merge_alone_unless_a_protected_pr_is_refused(pr, files, comments, flag, expected):
	fake = _FakeGitHub(pr, files, comments)
	disabled = []
	decision = ppa.check_pr("o/r", 7, get=fake.get, post=fake.post, disable_auto_merge=flag, disable=disabled.append)
	assert decision.get("auto_merge") == expected
	assert disabled == []


def test_check_pr_head_mismatch_leaves_auto_merge_alone():
	fake = _FakeGitHub(_auto_merge_pr(head=OTHER), [{"filename": ".claude/settings.json"}], [])
	disabled = []
	decision = ppa.check_pr("o/r", 7, HEAD, get=fake.get, post=fake.post, disable_auto_merge=True, disable=disabled.append)
	assert decision["decision"] == "block" and "auto_merge" not in decision and disabled == []


def test_check_pr_still_blocks_when_turning_auto_merge_off_fails():
	fake = _FakeGitHub(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [])
	attempts, sleeps = [], []

	def failing(node_id):
		attempts.append(node_id)
		raise ppa.ReadError(f"gh api graphql failed: HTTP 502 (attempt {len(attempts)})")

	decision = ppa.check_pr(
		"o/r", 7, get=fake.get, post=fake.post, disable_auto_merge=True, disable=failing, sleep=sleeps.append,
	)
	assert decision["decision"] == "block"
	assert decision["auto_merge"] == "disable failed: gh api graphql failed: HTTP 502 (attempt 3)"
	# Review round 1 on PR #5157: retried like a read, with a PR re-read
	# before each retry.
	assert attempts == ["PR_node7"] * ppa.READ_ATTEMPTS and sleeps == [2, 4]
	assert fake.reads.count("repos/o/r/pulls/7") == 3
	no_node = _FakeGitHub(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [])
	del no_node.pr["node_id"]
	decision = ppa.check_pr("o/r", 7, get=no_node.get, post=no_node.post, disable_auto_merge=True, disable=failing)
	assert decision["decision"] == "block" and decision["auto_merge"].startswith("disable failed: PR node_id")


def test_check_pr_retries_a_failed_auto_merge_disable_until_it_succeeds():
	fake = _FakeGitHub(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [])
	attempts, sleeps = [], []

	def flaky(node_id):
		attempts.append(node_id)
		if len(attempts) == 1:
			raise ppa.ReadError("gh api graphql failed: HTTP 503")

	decision = ppa.check_pr(
		"o/r", 7, get=fake.get, post=fake.post, disable_auto_merge=True, disable=flaky, sleep=sleeps.append,
	)
	assert decision["decision"] == "block" and decision["auto_merge"] == "disabled"
	assert len(attempts) == 2 and sleeps == [2]


def test_check_pr_stops_retrying_when_a_re_read_shows_auto_merge_off():
	# A failed attempt may still have landed; a second mutation on a PR with
	# no auto-merge fails, so the re-read decides.
	fake = _FakeGitHub(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [])
	attempts = []

	def lands_then_errors(node_id):
		attempts.append(node_id)
		fake.pr = dict(fake.pr, auto_merge=None)
		raise ppa.ReadError("gh api graphql failed: connection reset")

	decision = ppa.check_pr(
		"o/r", 7, get=fake.get, post=fake.post, disable_auto_merge=True, disable=lands_then_errors, sleep=lambda _s: None,
	)
	assert decision["auto_merge"] == "disabled" and attempts == ["PR_node7"]


def test_disable_auto_merge_uses_the_graphql_mutation(monkeypatch):
	calls = []
	confirmed = {"data": {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": None}}}}
	monkeypatch.setattr(ppa, "_gh_json", lambda args: calls.append(args) or confirmed)
	ppa._disable_auto_merge("PR_node7")
	assert calls == [["graphql", "-f", f"query={ppa.DISABLE_AUTO_MERGE_MUTATION}", "-f", "id=PR_node7"]]
	assert "disablePullRequestAutoMerge" in ppa.DISABLE_AUTO_MERGE_MUTATION
	assert "autoMergeRequest" in ppa.DISABLE_AUTO_MERGE_MUTATION


@pytest.mark.parametrize("response, problem", [
	({"errors": [{"message": "Resource not accessible by integration"}], "data": {"disablePullRequestAutoMerge": None}},
		"GraphQL errors: Resource not accessible by integration"),
	({"errors": "boom"}, "GraphQL errors: unreadable"),
	({"data": {"disablePullRequestAutoMerge": None}}, "response has no disablePullRequestAutoMerge.pullRequest"),
	({"data": None}, "response has no disablePullRequestAutoMerge.pullRequest"),
	({}, "response has no disablePullRequestAutoMerge.pullRequest"),
	([], "response is not a JSON object"),
	({"data": {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"enabledAt": "2026-09-29T00:00:00Z"}}}}},
		"auto-merge is still enabled after the mutation"),
])
def test_disable_auto_merge_fails_unless_the_response_confirms_it(monkeypatch, response, problem):
	# Review round 1 on PR #5157: GraphQL can answer HTTP 200 with `errors`,
	# so a discarded response could report `disabled` while auto-merge stays on.
	monkeypatch.setattr(ppa, "_gh_json", lambda args: response)
	with pytest.raises(ppa.ReadError) as info:
		ppa._disable_auto_merge("PR_node7")
	assert str(info.value) == f"gh api graphql disablePullRequestAutoMerge: {problem}"
	assert ppa.auto_merge_disable_error(response) == problem


def test_cli_disable_auto_merge_flag(monkeypatch, capsys):
	fake = _FakeGitHub(_auto_merge_pr(), [{"filename": ".claude/settings.json"}], [])
	disabled = []
	monkeypatch.setattr(ppa, "gh_get_json", fake.get)
	monkeypatch.setattr(ppa, "_post_comment", fake.post)
	monkeypatch.setattr(ppa, "_disable_auto_merge", disabled.append)
	assert ppa.main(["pr", "--repo", "o/r", "--pr", "7"]) == ppa.EXIT_BLOCKED
	assert "auto_merge" not in json.loads(capsys.readouterr().out) and disabled == []
	assert ppa.main(["pr", "--repo", "o/r", "--pr", "7", "--disable-auto-merge"]) == ppa.EXIT_BLOCKED
	assert json.loads(capsys.readouterr().out)["auto_merge"] == "disabled" and disabled == ["PR_node7"]


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


def _merge_with_edit(repo, branches, path, text, message):
	"""Merge `branches` into the current branch, edit `path` inside the merge
	commit itself (a conflict is resolved by that edit), and return the merge
	commit."""
	subprocess.run(
		["git", "-C", str(repo), "-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "--no-commit", *branches],
		capture_output=True, text=True,
		env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
	)
	return _commit(repo, path, text, message)


def _release_repo(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	arrival = _commit(repo, "scripts/protected_path_authorization.py", "# gate\n", "gate arrives")
	_commit(repo, ".claude/commands/a.md", "one\n", "claude before the release")
	_git(repo, "tag", "stable")
	return repo, arrival


def _release_get(prs, comments):
	def get(path):
		match = re.fullmatch(r"repos/o/r/commits/([0-9a-f]{40})/pulls", path)
		if match:
			return prs.get(match.group(1), [])
		match = re.fullmatch(r"repos/o/r/issues/(\d+)/comments\?per_page=100&page=1", path)
		if match:
			return comments.get(int(match.group(1)), [])
		raise AssertionError(path)
	return get


def test_check_release_checks_a_merge_that_carries_its_own_protected_change(tmp_path):
	# Wrapping a protected edit in a merge commit (a hand-edited merge pushed
	# straight to a branch) must not hide it: `--no-merges` alone skips it.
	repo, _arrival = _release_repo(tmp_path)
	_git(repo, "checkout", "-q", "-b", "side")
	_commit(repo, "docs/side.md", "side\n", "unprotected side change")
	_git(repo, "checkout", "-q", "main")
	_commit(repo, "README.md", "readme\n", "unprotected main change")
	evil = _merge_with_edit(repo, ["side"], ".claude/commands/a.md", "evil\n", "merge side")
	assert len(_git(repo, "rev-list", "--parents", "-n", "1", evil).split()) == 3, "the edit must live in the merge commit"

	prs, comments = {}, {}
	get = _release_get(prs, comments)
	blocked = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=get)
	assert blocked["decision"] == "block" and blocked["checked"] == 1
	assert [entry["commit"] for entry in blocked["blocked"]] == [evil]

	# GitHub links a local merge pushed straight to a branch to the PR it
	# merged, but the owner's approval of that PR's head never covered the
	# edit made inside the merge, which the head does not contain.
	prs[evil] = [_merged(21)]
	comments[21] = [_comment()]
	head_only = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=get)
	assert head_only["decision"] == "block"
	assert f"/authorize-protected-paths {evil}" in head_only["blocked"][0]["reason"]

	# An owner comment naming the merge commit itself authorizes it.
	comments[21] = [_comment(body=f"/authorize-protected-paths {evil}")]
	assert ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=get)["decision"] == "pass"


def test_check_release_covers_a_merge_inside_the_authorized_pr_head(tmp_path):
	# A conflict resolution made on the PR's own branch is part of the head
	# the owner approved, so the head comment covers it.
	repo, _arrival = _release_repo(tmp_path)
	_git(repo, "checkout", "-q", "-b", "side")
	_commit(repo, "docs/side.md", "side\n", "unprotected side change")
	_git(repo, "checkout", "-q", "-b", "pr", "stable")
	_commit(repo, "docs/pr.md", "pr\n", "unprotected pr change")
	branch_merge = _merge_with_edit(repo, ["side"], ".claude/commands/a.md", "resolved\n", "merge side into the PR")
	pr_head = _commit(repo, "docs/pr.md", "pr two\n", "later PR commit")
	_git(repo, "checkout", "-q", "main")
	_git(repo, "merge", "-q", "--no-ff", "-m", "merge pr", "pr")

	prs = {branch_merge: [_merged(30, head=pr_head)]}
	comments = {30: [_comment(body=f"/authorize-protected-paths {pr_head}")]}
	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get(prs, comments))
	assert result["checked"] == 1 and result["decision"] == "pass" and result["authorized"] == 1

	# The same merge linked to a PR whose head does not contain it is blocked.
	prs[branch_merge] = [_merged(31, head=OTHER)]
	comments[31] = [_comment(body=f"/authorize-protected-paths {OTHER}")]
	assert ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get(prs, comments))["decision"] == "block"


def test_evaluate_release_merge_rules():
	merge_sha = "c" * 40
	assert merge_sha not in (HEAD, OTHER)
	pulls = lambda sha: [_merged(1)]
	head_comment = lambda number: [_comment()]
	merge_comment = lambda number: [_comment(body=f"/authorize-protected-paths {merge_sha}")]
	contains = lambda head, sha: True
	not_contains = lambda head, sha: False

	def decide(comments_for, **kwargs):
		return ppa.evaluate_release([merge_sha], set(), pulls, comments_for, **kwargs)["decision"]

	assert decide(head_comment, merges={merge_sha}, head_contains=contains) == "pass"
	assert decide(head_comment, merges={merge_sha}, head_contains=not_contains) == "block"
	assert decide(head_comment, merges={merge_sha}) == "block", "no containment check fails closed"
	assert decide(merge_comment, merges={merge_sha}) == "pass"
	# A non-merge commit is unaffected: the head comment covers it, and a
	# comment naming the commit itself does not.
	assert decide(head_comment) == "pass"
	assert decide(merge_comment) == "block"


def test_read_errors_are_retried_until_one_succeeds(monkeypatch):
	results = [ppa.ReadError("one"), ppa.ReadError("two"), {"ok": True}]

	def flaky(args):
		result = results.pop(0)
		if isinstance(result, Exception):
			raise result
		return result

	monkeypatch.setattr(ppa, "_gh_json", flaky)
	sleeps = []
	assert ppa.gh_get_json("repos/o/r/pulls/7", sleep=sleeps.append) == {"ok": True}
	assert sleeps == [2, 4] and not results


def test_check_release_checks_a_protected_conflict_resolution_but_not_a_clean_merge(tmp_path):
	repo, _arrival = _release_repo(tmp_path)
	_git(repo, "checkout", "-q", "-b", "side")
	side = _commit(repo, ".claude/commands/a.md", "side\n", "side changes the command")
	_git(repo, "checkout", "-q", "main")
	ours = _commit(repo, ".claude/commands/a.md", "main\n", "main changes the command")
	resolved = _merge_with_edit(repo, ["side"], ".claude/commands/a.md", "resolved\n", "resolve merge conflicts")
	_git(repo, "checkout", "-q", "-b", "clean", "stable")
	_commit(repo, "docs/clean.md", "clean\n", "unprotected")
	_git(repo, "checkout", "-q", "main")
	_git(repo, "merge", "-q", "--no-ff", "-m", "merge clean", "clean")
	clean_merge = _git(repo, "rev-parse", "HEAD")

	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get({}, {}))
	listed = [entry["commit"] for entry in result["blocked"]]
	assert set(listed) == {side, ours, resolved}
	assert clean_merge not in listed and result["checked"] == 3


def test_check_release_checks_an_octopus_merge_that_differs_from_every_parent(tmp_path):
	repo, _arrival = _release_repo(tmp_path)
	for branch in ("one", "two"):
		_git(repo, "checkout", "-q", "-b", branch, "stable")
		_commit(repo, f"docs/{branch}.md", f"{branch}\n", f"unprotected {branch}")
	_git(repo, "checkout", "-q", "main")
	_commit(repo, "README.md", "readme\n", "unprotected main change")
	octopus = _merge_with_edit(repo, ["one", "two"], ".claude/commands/a.md", "evil\n", "octopus")
	assert len(_git(repo, "rev-list", "--parents", "-n", "1", octopus).split()) == 4
	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get({}, {}))
	assert [entry["commit"] for entry in result["blocked"]] == [octopus]


def _octopus_repo(tmp_path):
	"""A release repo with three branches off `stable`: `one` changes
	`.claude/commands/a.md`, `two` adds `.claude/commands/b.md`, and `three`
	changes an unprotected file; `main` has moved too, so no merge fast-forwards."""
	repo, _arrival = _release_repo(tmp_path)
	branch_commits = {}
	for branch, path in (("one", ".claude/commands/a.md"), ("two", ".claude/commands/b.md"), ("three", "docs/three.md")):
		_git(repo, "checkout", "-q", "-b", branch, "stable")
		branch_commits[branch] = _commit(repo, path, f"{branch} branch\n", f"{branch} change")
	_git(repo, "checkout", "-q", "main")
	_commit(repo, "README.md", "readme\n", "unprotected main change")
	return repo, branch_commits


def _octopus(repo, branches, message, edit=None):
	subprocess.run(
		["git", "-C", str(repo), "-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "--no-commit", *branches],
		check=True, capture_output=True, text=True,
		env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
	)
	if edit:
		edit(repo)
	_git(repo, "commit", "-q", "--allow-empty", "-m", message)
	sha = _git(repo, "rev-parse", "HEAD")
	assert len(_git(repo, "rev-list", "--parents", "-n", "1", sha).split()) == len(branches) + 2, "must be an octopus merge"
	return sha


def test_check_release_passes_a_clean_octopus_that_combines_protected_changes(tmp_path):
	# Each protected file comes from the one parent that changed it: the merge
	# adds nothing of its own, so only the parents' commits are checked.
	repo, branch_commits = _octopus_repo(tmp_path)
	octopus = _octopus(repo, ["one", "two", "three"], "clean octopus")
	assert not ppa.octopus_makes_protected_change(octopus, str(repo))
	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get({}, {}))
	assert octopus not in [entry["commit"] for entry in result["blocked"]]
	assert {entry["commit"] for entry in result["blocked"]} == {branch_commits["one"], branch_commits["two"]}


def test_check_release_checks_an_octopus_that_drops_a_parents_protected_change(tmp_path):
	# Keeping the base version of a file one parent changed reverts that
	# change inside the merge; tree-level path limiting skips such a merge
	# when its protected tree equals another parent's.
	repo, _branch_commits = _octopus_repo(tmp_path)

	def keep_base(repo_dir):
		_git(repo_dir, "checkout", "stable", "--", ".claude/commands/a.md")

	octopus = _octopus(repo, ["one", "two", "three"], "octopus that drops one", edit=keep_base)
	assert ppa.octopus_makes_protected_change(octopus, str(repo))
	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get({}, {}))
	assert octopus in [entry["commit"] for entry in result["blocked"]]


def test_check_release_checks_an_octopus_that_adds_a_protected_file(tmp_path):
	repo, _branch_commits = _octopus_repo(tmp_path)

	def add_file(repo_dir):
		(repo_dir / ".claude" / "commands" / "new.md").write_text("new\n", encoding="utf-8")
		_git(repo_dir, "add", ".claude/commands/new.md")

	octopus = _octopus(repo, ["three", "two"], "octopus with a new protected file", edit=add_file)
	assert ppa.octopus_makes_protected_change(octopus, str(repo))


def test_octopus_with_a_protected_file_two_parents_changed_fails_closed(tmp_path):
	# Two parents changing the same protected file need a content merge the
	# check cannot verify, so the merge is checked even when git merged it
	# cleanly.
	repo, _arrival = _release_repo(tmp_path)
	_commit(repo, ".claude/commands/c.md", "1\n2\n3\n4\n5\n6\n7\n", "seed")
	_git(repo, "tag", "-f", "stable")
	for branch, old, new in (("top", "1", "one"), ("bottom", "7", "seven")):
		_git(repo, "checkout", "-q", "-b", branch, "stable")
		text = (repo / ".claude" / "commands" / "c.md").read_text(encoding="utf-8").replace(f"{old}\n", f"{new}\n")
		_commit(repo, ".claude/commands/c.md", text, branch)
	_git(repo, "checkout", "-q", "-b", "other", "stable")
	_commit(repo, "docs/other.md", "other\n", "other")
	_git(repo, "checkout", "-q", "main")
	_commit(repo, "README.md", "readme\n", "unprotected main change")
	octopus = _octopus(repo, ["top", "bottom", "other"], "same-file octopus")
	assert ppa.octopus_makes_protected_change(octopus, str(repo))


def test_protected_merge_commits_names_the_git_version_when_remerge_diff_is_unknown(monkeypatch):
	def old_git(args, git_dir):
		raise ppa.ReadError("git log --merges failed: fatal: unrecognized argument: --remerge-diff")

	monkeypatch.setattr(ppa, "_git", old_git)
	with pytest.raises(ppa.ReadError, match="git 2.36 or later"):
		ppa.protected_merge_commits("base", "head", None)


def test_check_release_grandfathers_a_protected_merge_before_the_gate(tmp_path):
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_commit(repo, "README.md", "readme\n", "root")
	_git(repo, "checkout", "-q", "-b", "side")
	_commit(repo, "docs/side.md", "side\n", "side")
	_git(repo, "checkout", "-q", "main")
	_commit(repo, "docs/main.md", "main\n", "main")
	_merge_with_edit(repo, ["side"], ".claude/commands/a.md", "old\n", "pre-gate merge with a protected edit")
	_commit(repo, "scripts/protected_path_authorization.py", "# gate\n", "gate arrives")
	result = ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(repo), get=_release_get({}, {}))
	assert result["decision"] == "pass" and result["grandfathered"] == 2 and result["checked"] == 2


def test_check_release_refuses_a_shallow_checkout(tmp_path):
	# In a shallow clone the boundary commit stands in for the gate's arrival
	# and would be grandfathered, and `--remerge-diff` has no merge base.
	repo, _arrival = _release_repo(tmp_path)
	_commit(repo, ".claude/commands/a.md", "unauthorized\n", "unauthorized change")
	shallow = tmp_path / "shallow"
	subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True, capture_output=True)
	with pytest.raises(ppa.ReadError, match="shallow"):
		ppa.check_release("o/r", "refs/tags/stable", "HEAD", git_dir=str(shallow), get=_release_get({}, {}))


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
	# No well-formed head in the decision (placeholder SHAs in orchestrator
	# fakes): nothing to bind, so the call runs exactly as written.
	proc, merges, checks = _run_gate(
		tmp_path,
		{"decision": "allow", "protected": False, "head": "sha910"},
		0,
		'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --auto',
	)
	assert "rc=0" in proc.stdout, proc.stderr
	assert merges == [["gh", "pr", "merge", "42", "--repo", "o/r", "--squash", "--auto"]] * 2
	assert len(checks) == 1, "the second identical call must reuse the cached decision"
	assert checks[0][:5] == ["pr", "--repo", "o/r", "--pr", "42"]
	assert "--post-instructions" in checks[0] and "--disable-auto-merge" in checks[0]


def test_gate_binds_an_unprotected_merge_to_the_checked_head(tmp_path):
	# A push that adds a protected path between the check and the merge must
	# fail the merge, not ride on the unprotected decision (conformance run 1).
	proc, merges, _checks = _run_gate(
		tmp_path,
		{"decision": "allow", "protected": False, "head": HEAD},
		0,
		'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --auto',
	)
	assert "rc=0" in proc.stdout, proc.stderr
	assert merges == [["gh", "pr", "merge", "42", "--repo", "o/r", "--squash", "--auto", "--match-head-commit", HEAD]] * 2
	assert "decision=allow" not in proc.stderr, "an unprotected merge logs nothing new"
	(tmp_path / "bound").mkdir()
	proc, merges, _checks = _run_gate(
		tmp_path / "bound",
		{"decision": "allow", "protected": False, "head": HEAD},
		0,
		f'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --match-head-commit={OTHER}',
	)
	assert merges[0] == ["gh", "pr", "merge", "42", "--repo", "o/r", "--squash", f"--match-head-commit={OTHER}"]


def test_gate_logs_what_happened_to_a_pending_auto_merge(tmp_path):
	proc, merges, _checks = _run_gate(
		tmp_path,
		{"decision": "block", "protected": True, "head": HEAD, "reason": "no owner comment", "auto_merge": "disabled"},
		3,
		'protected_path_guarded_merge fake_merge gh pr merge 42 --repo o/r --squash --auto',
	)
	assert "rc=3" in proc.stdout and merges == []
	assert f"PROTECTED_PATH_GATE pr=42 head={HEAD} decision=block rc=3 auto_merge=disabled" in proc.stderr


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


def _mark_stable_setup(tmp_path, checker_rc):
	"""A bare `o/r.git` remote with a `stable` branch and tag, an operator
	clone, and a copy of mark-stable.sh next to a stub release checker."""
	env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_REPOSITORY", "GH_TOKEN", "GITHUB_TOKEN")}
	env.update({
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
		"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
	})
	(tmp_path / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.com\n[tag]\n\tgpgsign = false\n[commit]\n\tgpgsign = false\n", encoding="utf-8")
	remote = tmp_path / "o" / "r.git"
	remote.parent.mkdir()
	subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=env)
	seed = tmp_path / "seed"
	subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, env=env, capture_output=True)
	for text, tag in (("one\n", "stable"), ("two\n", None)):
		(seed / "README.md").write_text(text, encoding="utf-8")
		subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, env=env)
		subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", text.strip()], check=True, env=env)
		if tag:
			subprocess.run(["git", "-C", str(seed), "tag", "-a", tag, "-m", tag], check=True, env=env)
	subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "HEAD:refs/heads/stable", "refs/tags/stable"], check=True, env=env, capture_output=True)
	operator = tmp_path / "operator"
	subprocess.run(["git", "clone", "-q", str(remote), str(operator)], check=True, env=env, capture_output=True)
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	(scripts / "mark-stable.sh").write_text((SCRIPTS_DIR / "mark-stable.sh").read_text(encoding="utf-8"), encoding="utf-8")
	calls = tmp_path / "checker_calls.jsonl"
	(scripts / "protected_path_authorization.py").write_text(textwrap.dedent(f"""\
		import json, subprocess, sys
		base = sys.argv[sys.argv.index("--base") + 1]
		resolved = subprocess.run(["git", "rev-parse", "--verify", "--quiet", base + "^{{commit}}"], capture_output=True, text=True).stdout.strip()
		with open({str(calls)!r}, "a", encoding="utf-8") as fh:
		    fh.write(json.dumps({{"argv": sys.argv[1:], "base_commit": resolved}}) + "\\n")
		print(json.dumps({{"decision": "pass" if {checker_rc} == 0 else "block"}}))
		sys.exit({checker_rc})
		"""), encoding="utf-8")
	stable_head = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
	previous = subprocess.run(["git", "-C", str(seed), "rev-parse", "stable^{commit}"], check=True, capture_output=True, text=True).stdout.strip()
	return env, remote, operator, scripts, calls, stable_head, previous


def _remote_tags(remote, env):
	# Full ref names: `stable` is both a branch and a tag, so a short name is ambiguous.
	out = subprocess.run(["git", "-C", str(remote), "for-each-ref", "--format=%(refname) %(*objectname)%(objectname)", "refs/tags"], check=True, capture_output=True, text=True, env=env).stdout
	return {line.split()[0].removeprefix("refs/tags/"): line.split()[1][:40] for line in out.splitlines() if line.strip()}


def test_mark_stable_script_refuses_to_move_tags_when_the_release_check_blocks(tmp_path):
	env, remote, operator, scripts, calls, stable_head, previous = _mark_stable_setup(tmp_path, 3)
	before = _remote_tags(remote, env)
	proc = subprocess.run(["bash", str(scripts / "mark-stable.sh"), "v9.9.9"], cwd=operator, capture_output=True, text=True, env=env)
	assert proc.returncode == 6, proc.stderr
	assert "No tag was moved" in proc.stderr
	assert _remote_tags(remote, env) == before, "a refused release must not move or add any tag"
	[call] = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
	argv = call["argv"]
	assert argv[0] == "release"
	assert argv[argv.index("--repo") + 1] == "o/r", "the repository comes from the origin URL when GITHUB_REPOSITORY is unset"
	assert argv[argv.index("--head") + 1] == stable_head
	assert call["base_commit"] == previous, "the base is the remote's previous stable tag"


def test_mark_stable_script_releases_the_checked_commit_when_the_check_passes(tmp_path):
	env, remote, operator, scripts, calls, stable_head, _previous = _mark_stable_setup(tmp_path, 0)
	proc = subprocess.run(["bash", str(scripts / "mark-stable.sh"), "v9.9.9"], cwd=operator, capture_output=True, text=True, env={**env, "GITHUB_REPOSITORY": "owner/name"})
	assert proc.returncode == 0, proc.stderr
	tags = _remote_tags(remote, env)
	assert tags["v9.9.9"] == tags["stable"] == tags["v9"] == stable_head
	# A rerun takes the partial-publish recovery path: the version tag already
	# exists, and the check runs against the commit that tag names.
	rerun = subprocess.run(["bash", str(scripts / "mark-stable.sh"), "v9.9.9"], cwd=operator, capture_output=True, text=True, env={**env, "GITHUB_REPOSITORY": "owner/name"})
	assert rerun.returncode == 0, rerun.stderr
	assert "partial-publish recovery" in rerun.stdout
	first, second = [json.loads(line)["argv"] for line in calls.read_text(encoding="utf-8").splitlines()]
	assert first[first.index("--repo") + 1] == "owner/name"
	assert first[first.index("--head") + 1] == second[second.index("--head") + 1] == stable_head


def _race_checker(scripts, race_code):
	"""Replace the stub checker with one that passes after running
	`race_code` (a concurrent release acting while this run is checking)."""
	(scripts / "protected_path_authorization.py").write_text(
		"import json, subprocess, sys\n" + textwrap.dedent(race_code) + 'print(json.dumps({"decision": "pass"}))\n',
		encoding="utf-8",
	)


def test_mark_stable_script_does_not_roll_back_a_stable_tag_moved_after_it_was_read(tmp_path):
	env, remote, operator, scripts, _calls, stable_head, _previous = _mark_stable_setup(tmp_path, 0)
	# Another release points origin's stable tag somewhere new mid-run.
	_race_checker(scripts, f"""\
		subprocess.run(["git", "-C", {str(remote)!r}, "tag", "-f", "stable", {stable_head!r}], check=True, capture_output=True)
		""")
	proc = subprocess.run(["bash", str(scripts / "mark-stable.sh"), "v9.9.9"], cwd=operator, capture_output=True, text=True, env=env)
	assert proc.returncode == 8, proc.stderr
	assert "refs/tags/stable is no longer" in proc.stderr
	raw_stable = subprocess.run(["git", "-C", str(remote), "rev-parse", "refs/tags/stable"], check=True, capture_output=True, text=True, env=env).stdout.strip()
	assert raw_stable == stable_head, "the concurrent release's lightweight stable tag must survive"
	assert "v9" not in _remote_tags(remote, env), "the major-version pointer must not move either"


def test_mark_stable_script_drops_its_unpushed_version_tag_when_origin_stable_moves(tmp_path):
	env, remote, operator, scripts, _calls, _stable_head, _previous = _mark_stable_setup(tmp_path, 0)
	# A first release (no stable tag yet), and the stable branch advances
	# after the check read it.
	subprocess.run(["git", "-C", str(remote), "tag", "-d", "stable"], check=True, capture_output=True, env=env)
	_race_checker(scripts, f"""\
		remote = {str(remote)!r}
		tree = subprocess.run(["git", "-C", remote, "rev-parse", "refs/heads/stable^{{tree}}"], check=True, capture_output=True, text=True).stdout.strip()
		new = subprocess.run(["git", "-C", remote, "commit-tree", tree, "-p", "refs/heads/stable", "-m", "race"], check=True, capture_output=True, text=True).stdout.strip()
		subprocess.run(["git", "-C", remote, "update-ref", "refs/heads/stable", new], check=True)
		""")
	proc = subprocess.run(["bash", str(scripts / "mark-stable.sh"), "v9.9.9"], cwd=operator, capture_output=True, text=True, env=env)
	assert proc.returncode == 7, proc.stderr
	assert _remote_tags(remote, env) == {}, "no tag may be pushed"
	local = subprocess.run(["git", "-C", str(operator), "tag", "-l", "v9.9.9"], check=True, capture_output=True, text=True, env=env).stdout.strip()
	assert local == "", "the unpushed local version tag must be removed so a rerun can create it"


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
