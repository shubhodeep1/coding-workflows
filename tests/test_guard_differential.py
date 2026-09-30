#!/usr/bin/env python3
"""Behaviour and wiring contract for the guard differential check (issue #5174).

Covers:
  1. Decision parsing, the strictness order, the `Intended loosening:`
     PR-body section, and the base-ref loosening policy (issue #5326).
  2. The comparison on fake hooks: a loosening fails with or without a
     warning (issue #5325), a base-policy approval passes, a PR-body listing
     alone does not, a deleted guard is a loosening, an added one is not.
  3. The git side: changed-hook detection, a missing corpus, and the CLI exit
     codes, in a scratch repository.
  4. The shipped corpora and scenarios against the real hooks, the
     no-GitHub-API environment, and the ci.yml / agents.md wiring.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "guard_differential.py"
CORPUS_DIR = REPO_ROOT / "tests" / "guard_corpus"
HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
TEMPLATE_HOOKS_DIR = REPO_ROOT / "workflow-templates" / ".claude" / "hooks"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGENTS_MD = REPO_ROOT / "agents.md"


def _load_script():
	spec = importlib.util.spec_from_file_location("guard_differential", SCRIPT_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules["guard_differential"] = module
	spec.loader.exec_module(module)
	return module


gd = _load_script()


# ──────────────────────────────────────────────────────────────────
# Decision parsing
# ──────────────────────────────────────────────────────────────────


def _pd(decision: str, message: str = "") -> str:
	body: dict = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}
	if message:
		body["systemMessage"] = message
	return json.dumps(body)


@pytest.mark.parametrize(
	("returncode", "stdout", "decision", "warned"),
	[
		(2, "", "block", False),
		(0, "", "none", False),
		(0, _pd("deny"), "deny", False),
		(0, _pd("ask", "needs confirmation"), "ask", True),
		(0, _pd("allow"), "allow", False),
		(0, json.dumps({"systemMessage": "guard skipped"}), "none", True),
		(0, "not json\n" + _pd("ask"), "ask", False),
		(1, "", "error", False),
		(0, json.dumps([1, 2]), "none", False),
	],
)
def test_classify_output(returncode: int, stdout: str, decision: str, warned: bool) -> None:
	outcome = gd.classify_output(returncode, stdout)
	assert (outcome.decision, outcome.warned) == (decision, warned)


def test_strictness_order() -> None:
	s = gd.STRICTNESS
	assert s["block"] == s["deny"] > s["ask"] > s["none"] > s["allow"] == s["error"]


# ──────────────────────────────────────────────────────────────────
# PR body parsing
# ──────────────────────────────────────────────────────────────────


def test_intended_loosening_section_is_parsed() -> None:
	body = textwrap.dedent(
		"""\
		## Summary
		- `git push` is not in this section

		## Intended loosening:
		- pr_merge_status_guard: `git -C @@WORKTREE@@ push origin HEAD:feature/open`
		- `gh api repos/o/r/issues/1 > /tmp/out.json`
		* pr_watch_guard.py: `json: {"tool_name": "x"}`
		- plain item without backticks

		## Test plan
		- `git push origin HEAD`
		"""
	)
	assert gd.intended_loosening(body) == [
		("pr_merge_status_guard", "git -C @@WORKTREE@@ push origin HEAD:feature/open"),
		("", "gh api repos/o/r/issues/1 > /tmp/out.json"),
		("pr_watch_guard", 'json: {"tool_name": "x"}'),
		("", "plain item without backticks"),
	]


@pytest.mark.parametrize(
	"heading",
	["Intended loosening:", "**Intended loosening:**", "### Intended loosening", "intended loosening:"],
)
def test_intended_loosening_heading_forms(heading: str) -> None:
	assert gd.intended_loosening(f"{heading}\n- `git push`\n") == [("", "git push")]


def test_no_section_means_nothing_listed() -> None:
	assert gd.intended_loosening("- `git push`\nIntended loosening is discussed here.\n") == []


def test_is_intended_matches_hook_and_exact_text() -> None:
	shape = gd.Shape(hook="pr_merge_status_guard", text="git push", line=1)
	assert gd.is_intended(shape, [("", "git push")])
	assert gd.is_intended(shape, [("pr_merge_status_guard", "git push")])
	assert not gd.is_intended(shape, [("gh_api_write_guard", "git push")])
	assert not gd.is_intended(shape, [("", "git push origin HEAD")])


# ──────────────────────────────────────────────────────────────────
# Corpus loading
# ──────────────────────────────────────────────────────────────────


def test_load_corpus_skips_comments_and_keeps_line_numbers(tmp_path: Path) -> None:
	corpus = tmp_path / "my_guard.txt"
	corpus.write_text('# comment\n\ngit push\n  json: {"tool_name": "x"}\nstdin: {bad\n', encoding="utf-8")
	shapes = gd.load_corpus(corpus)
	assert [(s.hook, s.text, s.line) for s in shapes] == [
		("my_guard", "git push", 3),
		("my_guard", 'json: {"tool_name": "x"}', 4),
		("my_guard", "stdin: {bad", 5),
	]


@pytest.mark.parametrize("line", ["json: {not json", "json: [1]"])
def test_load_corpus_rejects_bad_json_shapes(tmp_path: Path, line: str) -> None:
	corpus = tmp_path / "my_guard.txt"
	corpus.write_text(line + "\n", encoding="utf-8")
	with pytest.raises(gd.SetupError):
		gd.load_corpus(corpus)


def test_build_stdin_forms(tmp_path: Path) -> None:
	subs = {"@@REPO@@": "/r"}
	bash = json.loads(gd.build_stdin(gd.Shape("h", "cd @@REPO@@ && git push", 1), tmp_path, subs))
	assert bash == {"tool_name": "Bash", "tool_input": {"command": "cd /r && git push"}, "cwd": str(tmp_path)}
	mcp = json.loads(gd.build_stdin(gd.Shape("h", 'json: {"tool_name": "t", "cwd": "/keep"}', 1), tmp_path, subs))
	assert mcp == {"tool_name": "t", "cwd": "/keep"}
	assert gd.build_stdin(gd.Shape("h", "stdin: {bad", 1), tmp_path, subs) == "{bad"


# ──────────────────────────────────────────────────────────────────
# Comparison on fake hooks
# ──────────────────────────────────────────────────────────────────


FAKE_BLOCKING_HOOK = "import sys\nsys.stdin.read()\nsys.exit(2)\n"
FAKE_ALLOWING_HOOK = (
	"import json, sys\nsys.stdin.read()\n"
	'print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}}))\n'
)
FAKE_SILENT_HOOK = "import sys\nsys.stdin.read()\n"
FAKE_WARNING_HOOK = 'import json, sys\nsys.stdin.read()\nprint(json.dumps({"systemMessage": "fell back"}))\n'
FAKE_ASKING_HOOK = (
	"import json, sys\nsys.stdin.read()\n"
	'print(json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}))\n'
)
FAKE_CRASHING_HOOK = "raise SystemExit(1)\n"


def _hook_dirs(tmp_path: Path, base_source: str | None, head_source: str | None) -> tuple[Path, Path]:
	base_dir = tmp_path / "base"
	head_dir = tmp_path / "head"
	base_dir.mkdir()
	head_dir.mkdir()
	if base_source is not None:
		(base_dir / "fake_guard.py").write_text(base_source, encoding="utf-8")
	if head_source is not None:
		(head_dir / "fake_guard.py").write_text(head_source, encoding="utf-8")
	return base_dir, head_dir


def _compare(tmp_path: Path, base_source, head_source, listed=None, shapes=("git push",), approvals=None):
	base_dir, head_dir = _hook_dirs(tmp_path, base_source, head_source)
	corpora = {"fake_guard": [gd.Shape("fake_guard", text, index + 1) for index, text in enumerate(shapes)]}
	return gd.compare_hook_dirs(base_dir, head_dir, corpora, tmp_path / "runs", listed or [], "", approvals)


HEAD_SHA = "a" * 40


def _approval(hook: str = "fake_guard", shape: str = "git push", head_sha: str = HEAD_SHA):
	return gd.LooseningApproval(hook=hook, shape=shape, head_sha=head_sha, approved_by="operator", reason="test")


@pytest.mark.parametrize(
	("base_source", "head_source", "regression"),
	[
		(FAKE_BLOCKING_HOOK, FAKE_SILENT_HOOK, True),
		(FAKE_BLOCKING_HOOK, FAKE_ALLOWING_HOOK, True),
		(FAKE_BLOCKING_HOOK, FAKE_CRASHING_HOOK, True),
		(FAKE_ASKING_HOOK, FAKE_SILENT_HOOK, True),
		(FAKE_SILENT_HOOK, FAKE_ALLOWING_HOOK, True),
		# A warning never excuses a loosening (issue #5325).
		(FAKE_BLOCKING_HOOK, FAKE_WARNING_HOOK, True),
		(FAKE_BLOCKING_HOOK, FAKE_ASKING_HOOK + FAKE_WARNING_HOOK, True),
		(FAKE_SILENT_HOOK, FAKE_ALLOWING_HOOK + FAKE_WARNING_HOOK, True),
		(FAKE_SILENT_HOOK, FAKE_WARNING_HOOK, False),
		(FAKE_BLOCKING_HOOK, FAKE_BLOCKING_HOOK + FAKE_WARNING_HOOK, False),
		(FAKE_BLOCKING_HOOK, FAKE_BLOCKING_HOOK, False),
		(FAKE_SILENT_HOOK, FAKE_BLOCKING_HOOK, False),
		(FAKE_ALLOWING_HOOK, FAKE_ASKING_HOOK, False),
		(FAKE_BLOCKING_HOOK, None, True),
		(None, FAKE_BLOCKING_HOOK, False),
		(None, FAKE_ALLOWING_HOOK, True),
	],
)
def test_loosening_rule(tmp_path: Path, base_source, head_source, regression: bool) -> None:
	[result] = _compare(tmp_path, base_source, head_source)
	assert result.regression is regression


def test_warned_loosening_keeps_the_warning_as_a_diagnostic(tmp_path: Path) -> None:
	[result] = _compare(tmp_path, FAKE_BLOCKING_HOOK, FAKE_WARNING_HOOK)
	assert (result.head.decision, result.head.warned, result.loosened, result.regression) == (
		"none",
		True,
		True,
		True,
	)


def test_policy_approval_excuses_a_warned_loosening(tmp_path: Path) -> None:
	"""Issues #5325 and #5326 together: the warning excuses nothing, and the
	base-ref policy is still the one way to approve the loosening."""
	[result] = _compare(tmp_path, FAKE_BLOCKING_HOOK, FAKE_WARNING_HOOK, approvals=[_approval()])
	assert (result.head.warned, result.loosened, result.intended, result.regression) == (
		True,
		True,
		True,
		False,
	)


def test_pr_body_listing_alone_excuses_nothing(tmp_path: Path) -> None:
	"""Issue #5326: the PR author writes the body, so a listing is data only."""
	results = _compare(
		tmp_path,
		FAKE_BLOCKING_HOOK,
		FAKE_SILENT_HOOK,
		listed=[("fake_guard", "git push"), ("", "git commit -m x")],
		shapes=("git push", "git commit -m x", "git status"),
	)
	assert [(r.shape.text, r.intended, r.pr_body_listed, r.regression) for r in results] == [
		("git push", False, True, True),
		("git commit -m x", False, True, True),
		("git status", False, False, True),
	]


def test_policy_approval_excuses_only_the_approved_hook_and_shape(tmp_path: Path) -> None:
	results = _compare(
		tmp_path,
		FAKE_BLOCKING_HOOK,
		FAKE_SILENT_HOOK,
		shapes=("git push", "git commit -m x", "git status"),
		approvals=[_approval(), _approval(hook="other_guard", shape="git commit -m x")],
	)
	assert [(r.shape.text, r.intended, r.approved_by, r.regression) for r in results] == [
		("git push", True, "operator", False),
		("git commit -m x", False, "", True),
		("git status", False, "", True),
	]


def test_policy_approval_does_not_mark_a_shape_that_did_not_loosen(tmp_path: Path) -> None:
	[result] = _compare(tmp_path, FAKE_BLOCKING_HOOK, FAKE_BLOCKING_HOOK, approvals=[_approval()])
	assert (result.loosened, result.intended, result.regression) == (False, False, False)


def _policy_text(*entries: dict) -> str:
	return json.dumps({"version": 1, "exceptions": list(entries)})


def _entry(**overrides) -> dict:
	entry = {"hook": "fake_guard", "shape": "git push", "head_sha": HEAD_SHA, "approved_by": "operator", "reason": "r"}
	entry.update(overrides)
	return entry


def test_parse_loosening_policy_accepts_valid_entries() -> None:
	approvals = gd.parse_loosening_policy(_policy_text(_entry(), _entry(head_sha="b" * 64)), "p")
	assert approvals == [
		gd.LooseningApproval("fake_guard", "git push", HEAD_SHA, "operator", "r"),
		gd.LooseningApproval("fake_guard", "git push", "b" * 64, "operator", "r"),
	]
	assert gd.parse_loosening_policy(_policy_text(), "p") == []


@pytest.mark.parametrize(
	"text",
	[
		"not json",
		"[]",
		json.dumps({"version": 1}),
		json.dumps({"exceptions": {}}),
		json.dumps({"exceptions": []}),
		json.dumps({"version": 2, "exceptions": []}),
		json.dumps({"version": "1", "exceptions": []}),
		json.dumps({"version": None, "exceptions": []}),
		json.dumps({"version": True, "exceptions": []}),
		json.dumps({"version": 1.0, "exceptions": []}),
		_policy_text("x"),
		_policy_text({k: v for k, v in _entry().items() if k != "approved_by"}),
		_policy_text(_entry(reason="  ")),
		_policy_text(_entry(shape=1)),
		_policy_text(_entry(head_sha="abc123")),
		_policy_text(_entry(head_sha="A" * 40)),
		_policy_text(_entry(hook="fake_guard.py")),
		_policy_text(_entry(hook="fake_guard\n")),
		_policy_text(_entry(head_sha=HEAD_SHA + "\n")),
	],
)
def test_parse_loosening_policy_rejects_malformed_policies(text: str) -> None:
	with pytest.raises(gd.SetupError):
		gd.parse_loosening_policy(text, "p")


def test_shipped_loosening_policy_parses() -> None:
	path = REPO_ROOT / gd.LOOSENING_POLICY_PATH
	gd.parse_loosening_policy(path.read_text(encoding="utf-8"), str(path))


def test_corpus_without_a_hook_on_either_side_is_skipped(tmp_path: Path) -> None:
	assert _compare(tmp_path, None, None) == []


def test_each_side_runs_with_a_fresh_scenario(tmp_path: Path) -> None:
	"""A hook that leaves state behind cannot change the other side's verdict."""
	stateful = (
		"import os, sys\nsys.stdin.read()\n"
		"marker = os.path.join(os.getcwd(), 'seen')\n"
		"if os.path.exists(marker):\n    sys.exit(0)\n"
		"open(marker, 'w').close()\nsys.exit(2)\n"
	)
	[result] = _compare(tmp_path, stateful, stateful)
	assert (result.base.decision, result.head.decision) == ("block", "block")


# ──────────────────────────────────────────────────────────────────
# Git side and CLI
# ──────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env["HOME"] = str(repo)
	proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True)
	return proc.stdout.strip()


@pytest.fixture()
def hook_repo(tmp_path: Path):
	"""A repository with one blocking guard (+ corpus) committed on `main`."""
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.email", "t@example.com")
	_git(repo, "config", "user.name", "T")
	_git(repo, "config", "commit.gpgsign", "false")
	hooks = repo / ".claude" / "hooks"
	hooks.mkdir(parents=True)
	(hooks / "fake_guard.py").write_text(FAKE_BLOCKING_HOOK, encoding="utf-8")
	(hooks / "notes_hook.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	corpus = repo / "tests" / "guard_corpus"
	corpus.mkdir(parents=True)
	(corpus / "fake_guard.txt").write_text("git push\n", encoding="utf-8")
	(repo / "README.md").write_text("x\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	return repo


def _cli(repo: Path, *extra: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(SCRIPT_PATH), "--repo-root", str(repo), "--base-ref", "main", *extra],
		capture_output=True,
		text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		timeout=300,
	)


def test_cli_skips_when_no_hook_changed(hook_repo: Path) -> None:
	(hook_repo / "README.md").write_text("changed\n", encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "status=skipped reason=no-hook-change" in proc.stdout


def test_cli_fails_on_a_silent_loosening_in_the_working_tree(hook_repo: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "::error::GUARD_DIFFERENTIAL regression tree=.claude/hooks hook=fake_guard line=1" in proc.stdout
	assert "base=block head=none" in proc.stdout


def test_cli_fails_on_a_warned_loosening_and_reports_the_warning(hook_repo: Path) -> None:
	"""Issue #5325: a `systemMessage` from the new hook does not turn a
	loosening into a pass; the warning is only reported."""
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_WARNING_HOOK, encoding="utf-8")
	proc = _cli(hook_repo, "--json")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "base=block head=none+warning" in proc.stdout
	assert "status=fail" in proc.stdout and "regressions=1" in proc.stdout
	assert "fall back with a warning" not in proc.stdout
	[row] = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
	assert row["head"] == {"decision": "none", "warned": True}
	assert row["loosened"] is True


def test_cli_deleting_the_shape_does_not_hide_the_loosening(hook_repo: Path) -> None:
	"""The base branch's corpus runs too, so a PR cannot pass by removing the
	shape its hook change loosens."""
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	(hook_repo / "tests" / "guard_corpus" / "fake_guard.txt").write_text("git status\n", encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert 'shape="git push"' in proc.stdout


def test_merge_corpora_keeps_the_first_occurrence() -> None:
	head = {"g": [gd.Shape("g", "a", 1), gd.Shape("g", "b", 2)]}
	base = {"g": [gd.Shape("g", "b", 5), gd.Shape("g", "c", 6)], "h": [gd.Shape("h", "x", 1)]}
	merged = gd.merge_corpora(head, base)
	assert [(s.text, s.line) for s in merged["g"]] == [("a", 1), ("b", 2), ("c", 6)]
	assert [s.text for s in merged["h"]] == ["x"]


def test_cli_pr_body_listing_does_not_authorize_a_loosening(hook_repo: Path, tmp_path: Path) -> None:
	"""Issue #5326's exploit: the author weakens a guard and lists the shape
	under `Intended loosening:` in their own PR body. The check still fails."""
	_git(hook_repo, "checkout", "-q", "-b", "pr")
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	_git(hook_repo, "commit", "-q", "-am", "loosen")
	body = tmp_path / "body.md"
	body.write_text("## Intended loosening:\n- fake_guard: `git push`\n", encoding="utf-8")
	proc = _cli(hook_repo, "--head-ref", "pr", "--pr-body-file", str(body))
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert 'hook=fake_guard line=1 base=block head=none shape="git push" pr_body_listed=true' in proc.stdout
	assert "intended_loosening tree=" not in proc.stdout
	assert "status=fail" in proc.stdout and "intended_loosening=0" in proc.stdout


def _commit_policy(repo: Path, *entries: dict, message: str = "policy") -> None:
	path = repo / gd.LOOSENING_POLICY_PATH
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(_policy_text(*entries), encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", message)


def _loosening_pr(repo: Path) -> str:
	"""Commit a silent loosening of `fake_guard` on branch `pr`; return its sha."""
	_git(repo, "checkout", "-q", "-b", "pr")
	(repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	_git(repo, "commit", "-q", "-am", "loosen")
	sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "-q", "main")
	return sha


def test_cli_passes_with_a_base_policy_entry_for_the_head_commit(hook_repo: Path) -> None:
	head_sha = _loosening_pr(hook_repo)
	_commit_policy(hook_repo, _entry(head_sha=head_sha))
	proc = _cli(hook_repo, "--head-ref", "pr")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert 'intended_loosening tree=.claude/hooks hook=fake_guard base=block head=none shape="git push" approved_by="operator"' in proc.stdout
	assert "status=pass" in proc.stdout and "intended_loosening=1" in proc.stdout


def test_cli_json_rows_carry_the_approval_audit_fields(hook_repo: Path) -> None:
	"""`--json` rows name the approver and reason of a policy-approved shape,
	as the text `intended_loosening` line does, and leave both empty otherwise."""
	head_sha = _loosening_pr(hook_repo)
	unapproved = _cli(hook_repo, "--head-ref", "pr", "--json")
	assert unapproved.returncode == 1, unapproved.stdout + unapproved.stderr
	[row] = [json.loads(line) for line in unapproved.stdout.splitlines() if line.startswith("{")]
	assert (row["loosened"], row["intended"], row["approved_by"], row["approval_reason"]) == (True, False, "", "")
	_commit_policy(hook_repo, _entry(head_sha=head_sha, reason="retire the old refspec rule"))
	proc = _cli(hook_repo, "--head-ref", "pr", "--json")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	[row] = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
	assert (row["intended"], row["approved_by"], row["approval_reason"]) == (
		True,
		"operator",
		"retire the old refspec rule",
	)


@pytest.mark.parametrize(
	"overrides",
	[{"head_sha": "c" * 40}, {"hook": "other_guard"}, {"shape": "git push origin HEAD"}],
)
def test_cli_a_policy_entry_for_another_commit_hook_or_shape_does_not_match(hook_repo: Path, overrides: dict) -> None:
	head_sha = _loosening_pr(hook_repo)
	_commit_policy(hook_repo, _entry(**{"head_sha": head_sha, **overrides}))
	proc = _cli(hook_repo, "--head-ref", "pr")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "::error::GUARD_DIFFERENTIAL regression" in proc.stdout


def test_cli_a_policy_entry_only_on_the_head_side_is_never_read(hook_repo: Path) -> None:
	"""The PR's own copy of the policy cannot approve the PR's loosening."""
	_git(hook_repo, "checkout", "-q", "-b", "pr")
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	_git(hook_repo, "commit", "-q", "-am", "loosen")
	loosen_sha = _git(hook_repo, "rev-parse", "HEAD")
	_commit_policy(hook_repo, _entry(head_sha=loosen_sha))
	head_sha = _git(hook_repo, "rev-parse", "HEAD")
	path = hook_repo / gd.LOOSENING_POLICY_PATH
	path.write_text(_policy_text(_entry(head_sha=loosen_sha), _entry(head_sha=head_sha)), encoding="utf-8")
	_git(hook_repo, "commit", "-q", "-am", "approve my own head")
	_git(hook_repo, "checkout", "-q", "main")
	for extra in (("--head-ref", "pr"), ("--head-ref", "pr~1")):
		proc = _cli(hook_repo, *extra)
		assert proc.returncode == 1, proc.stdout + proc.stderr
		assert "::error::GUARD_DIFFERENTIAL regression" in proc.stdout


def test_cli_a_working_tree_run_fails_closed_without_a_head_sha(hook_repo: Path) -> None:
	"""With no head commit no policy entry can match; `--head-sha` (the CI
	path, where the checkout is a merge commit) supplies it."""
	head_sha = "d" * 40
	_commit_policy(hook_repo, _entry(head_sha=head_sha))
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert '"head_sha": "<head commit sha>"' in proc.stdout
	proc = _cli(hook_repo, "--head-sha", head_sha)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "intended_loosening=1" in proc.stdout


def test_cli_failure_hint_names_the_policy_and_the_head_commit(hook_repo: Path) -> None:
	head_sha = _loosening_pr(hook_repo)
	proc = _cli(hook_repo, "--head-ref", "pr")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert gd.LOOSENING_POLICY_PATH in proc.stdout
	assert f'"head_sha": "{head_sha}"' in proc.stdout
	assert "Intended loosening:` section of the PR body" not in proc.stdout


@pytest.mark.parametrize("head_sha", ["abc", "E" * 40, "g" * 40, "d" * 40 + "\n"])
def test_cli_a_malformed_head_sha_exits_2(hook_repo: Path, head_sha: str) -> None:
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	proc = _cli(hook_repo, "--head-sha", head_sha)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "status=error" in proc.stdout


def test_cli_a_malformed_head_sha_exits_2_even_when_no_hook_changed(hook_repo: Path) -> None:
	"""`--head-sha` is validated before the no-hook-change skip (AD-7)."""
	(hook_repo / "README.md").write_text("changed\n", encoding="utf-8")
	assert _cli(hook_repo).returncode == 0
	proc = _cli(hook_repo, "--head-sha", "abc")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "not a full lowercase commit SHA" in proc.stdout
	assert "status=skipped" not in proc.stdout


def test_cli_a_head_sha_that_contradicts_the_head_ref_exits_2(hook_repo: Path) -> None:
	_loosening_pr(hook_repo)
	proc = _cli(hook_repo, "--head-ref", "pr", "--head-sha", "e" * 40)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "does not match --head-ref" in proc.stdout


def test_cli_a_malformed_base_policy_exits_2_only_when_a_hook_changed(hook_repo: Path) -> None:
	path = hook_repo / gd.LOOSENING_POLICY_PATH
	path.parent.mkdir(parents=True)
	path.write_text("{not json", encoding="utf-8")
	_git(hook_repo, "add", "-A")
	_git(hook_repo, "commit", "-q", "-m", "bad policy")
	(hook_repo / "README.md").write_text("changed\n", encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "status=skipped" in proc.stdout
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_BLOCKING_HOOK + "# edit\n", encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "invalid JSON" in proc.stdout


def test_cli_compares_committed_refs(hook_repo: Path) -> None:
	_git(hook_repo, "checkout", "-q", "-b", "pr")
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_ALLOWING_HOOK, encoding="utf-8")
	_git(hook_repo, "commit", "-q", "-am", "loosen")
	_git(hook_repo, "checkout", "-q", "main")
	proc = _cli(hook_repo, "--head-ref", "pr")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "base=block head=allow" in proc.stdout


def test_cli_a_sibling_change_reruns_every_corpus_in_that_tree(hook_repo: Path) -> None:
	"""A hook may load a sibling (the inline-edit guard uses the gh api guard's
	tokenizer), so any `.py` change re-runs every corpus of that tree."""
	(hook_repo / ".claude" / "hooks" / "notes_hook.py").write_text("# changed\n" + FAKE_SILENT_HOOK, encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "shapes=1 regressions=0" in proc.stdout


def test_cli_fails_when_a_changed_guard_has_no_corpus(hook_repo: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "other_guard.py").write_text(FAKE_BLOCKING_HOOK, encoding="utf-8")
	_git(hook_repo, "add", "-A")
	_git(hook_repo, "commit", "-q", "-m", "add other guard")
	_git(hook_repo, "branch", "-f", "base-with-other")
	(hook_repo / ".claude" / "hooks" / "other_guard.py").write_text(FAKE_ASKING_HOOK, encoding="utf-8")
	proc = subprocess.run(
		[sys.executable, str(SCRIPT_PATH), "--repo-root", str(hook_repo), "--base-ref", "base-with-other"],
		capture_output=True,
		text=True,
		timeout=300,
	)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "missing_corpus path=.claude/hooks/other_guard.py" in proc.stdout


def test_cli_fails_when_a_changed_guard_has_an_empty_corpus(hook_repo: Path) -> None:
	(hook_repo / "tests" / "guard_corpus" / "fake_guard.txt").write_text("# no shapes yet\n\n", encoding="utf-8")
	_git(hook_repo, "add", "-A")
	_git(hook_repo, "commit", "-q", "-m", "empty corpus")
	_git(hook_repo, "branch", "-f", "base-empty-corpus")
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	proc = subprocess.run(
		[sys.executable, str(SCRIPT_PATH), "--repo-root", str(hook_repo), "--base-ref", "base-empty-corpus"],
		capture_output=True,
		text=True,
		timeout=300,
	)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "missing_corpus path=.claude/hooks/fake_guard.py" in proc.stdout
	assert "shapes=0" in proc.stdout


@pytest.mark.parametrize("staged", [False, True], ids=["untracked", "staged"])
def test_cli_fails_when_a_new_guard_has_no_corpus(hook_repo: Path, staged: bool) -> None:
	"""AD-9: without a shape, a new guard that answers `allow` is never run."""
	(hook_repo / ".claude" / "hooks" / "brand_new_guard.py").write_text(FAKE_ALLOWING_HOOK, encoding="utf-8")
	if staged:
		_git(hook_repo, "add", "-A")
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "missing_corpus path=.claude/hooks/brand_new_guard.py" in proc.stdout


def test_cli_fails_when_a_committed_new_guard_has_no_corpus(hook_repo: Path) -> None:
	_git(hook_repo, "checkout", "-q", "-b", "pr")
	(hook_repo / ".claude" / "hooks" / "brand_new_guard.py").write_text(FAKE_BLOCKING_HOOK, encoding="utf-8")
	_git(hook_repo, "add", "-A")
	_git(hook_repo, "commit", "-q", "-m", "add guard")
	_git(hook_repo, "checkout", "-q", "main")
	proc = _cli(hook_repo, "--head-ref", "pr")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "missing_corpus path=.claude/hooks/brand_new_guard.py" in proc.stdout


def test_cli_a_new_blocking_guard_with_a_corpus_passes(hook_repo: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "brand_new_guard.py").write_text(FAKE_BLOCKING_HOOK, encoding="utf-8")
	(hook_repo / "tests" / "guard_corpus" / "brand_new_guard.txt").write_text("git push\n", encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "missing_corpus=0" in proc.stdout
	assert "hook=brand_new_guard" not in proc.stdout


def test_cli_a_new_allowing_guard_with_a_corpus_is_a_loosening(hook_repo: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "brand_new_guard.py").write_text(FAKE_ALLOWING_HOOK, encoding="utf-8")
	(hook_repo / "tests" / "guard_corpus" / "brand_new_guard.txt").write_text("git push\n", encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "hook=brand_new_guard" in proc.stdout
	assert "base=none (no hook) head=allow" in proc.stdout


def test_cli_deleting_a_guard_is_a_loosening(hook_repo: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").unlink()
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "base=block head=none (no hook)" in proc.stdout


@pytest.mark.parametrize("extra", [("--base-ref", "no-such-ref"), ("--head-ref", "no-such-ref")])
def test_cli_bad_refs_exit_2(hook_repo: Path, extra: tuple[str, str]) -> None:
	proc = subprocess.run(
		[sys.executable, str(SCRIPT_PATH), "--repo-root", str(hook_repo), "--base-ref", "main", *extra],
		capture_output=True,
		text=True,
		timeout=120,
	)
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "status=error" in proc.stdout


def test_cli_unreadable_pr_body_exits_2(hook_repo: Path, tmp_path: Path) -> None:
	proc = _cli(hook_repo, "--pr-body-file", str(tmp_path / "missing.md"))
	assert proc.returncode == 2


# ──────────────────────────────────────────────────────────────────
# Shipped corpora, scenarios, and environment
# ──────────────────────────────────────────────────────────────────


def test_every_shipped_corpus_parses() -> None:
	corpora = gd.load_corpora(CORPUS_DIR)
	assert set(corpora) >= {"pr_merge_status_guard", "gh_api_write_guard", "pr_watch_guard", "inline_edit_guard"}
	for hook, shapes in corpora.items():
		assert shapes, hook
		assert len({s.text for s in shapes}) == len(shapes), f"duplicate shape in {hook}"


def test_every_guard_hook_has_a_corpus() -> None:
	corpora = set(gd.load_corpora(CORPUS_DIR))
	for hooks_dir in (HOOKS_DIR, TEMPLATE_HOOKS_DIR):
		for hook in hooks_dir.glob("*_guard.py"):
			assert hook.stem in corpora, f"{hook} has no tests/guard_corpus/{hook.stem}.txt"


def test_seed_shapes_from_the_issue_are_in_the_corpora() -> None:
	merge = {s.text for s in gd.load_corpus(CORPUS_DIR / "pr_merge_status_guard.txt")}
	for shape in (
		"git push origin HEAD:heads/feature/x",
		"git push origin HEAD:$B",
		'git push origin "$B"',
		"git push origin 'refs/heads/*:refs/heads/*'",
		"cd @@WORKTREE@@ || git push origin HEAD:feature/open",
		"cd @@WORKTREE@@ & git push origin HEAD:feature/open",
		"(cd missing-dir && git push)",
		"git -C missing-dir push",
		"GIT_DIR=missing-dir/.git git push",
		"git push origin -- -x:feature/x",
	):
		assert shape in merge, shape
	gh_api = {s.text for s in gd.load_corpus(CORPUS_DIR / "gh_api_write_guard.txt")}
	assert any(" -X " in s for s in gh_api)
	assert any("-F body=@" in s for s in gh_api)
	assert any("--input" in s for s in gh_api)
	assert any("$(gh api" in s for s in gh_api)
	inline = [s.text for s in gd.load_corpus(CORPUS_DIR / "inline_edit_guard.txt")]
	assert any(s.startswith("env -i ") for s in inline)
	assert any("python3 -" in s for s in inline)
	assert any("<<" in s for s in inline)


def test_hook_env_never_reaches_github(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setenv("GH_TOKEN", "secret")
	monkeypatch.setenv("GITHUB_TOKEN", "secret")
	monkeypatch.setenv("CLAUDE_PR_MERGE_GUARD", "off")
	monkeypatch.setenv("GIT_DIR", "/elsewhere")
	env = gd.hook_env({}, tmp_path / "bin", tmp_path / "home", tmp_path / "cache")
	assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
	assert not any(key.startswith("CLAUDE_") for key in env)
	assert "GIT_DIR" not in env
	assert env["GIT_ALLOW_PROTOCOL"] == "file"
	assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")


def test_scenario_env_cannot_override_isolation(tmp_path: Path) -> None:
	scenario_env = {
		"GIT_ALLOW_PROTOCOL": "https",
		"GIT_CONFIG_NOSYSTEM": "0",
		"GIT_TERMINAL_PROMPT": "1",
		"GIT_SSH_COMMAND": "ssh -o ProxyCommand=evil",
		"GIT_DIR": "/elsewhere/.git",
		"GH_TOKEN": "secret",
		"CLAUDE_PR_MERGE_GUARD": "off",
		"SCENARIO_ONLY": "kept",
	}
	env = gd.hook_env(scenario_env, tmp_path / "bin", tmp_path / "home", tmp_path / "cache")
	assert env["GIT_ALLOW_PROTOCOL"] == "file"
	assert env["GIT_CONFIG_NOSYSTEM"] == "1"
	assert env["GIT_TERMINAL_PROMPT"] == "0"
	assert "GIT_SSH_COMMAND" not in env and "GIT_DIR" not in env
	assert "GH_TOKEN" not in env and "CLAUDE_PR_MERGE_GUARD" not in env
	assert env["SCENARIO_ONLY"] == "kept"


def test_stub_gh_prints_payloads_verbatim(tmp_path: Path) -> None:
	payload = '[{"title": "a"}]\nEOF\necho pwned > ' + str(tmp_path / "pwned")
	stub_bin = tmp_path / "bin"
	gd._write_stub_gh(stub_bin, {"o:feat $(x)": payload})
	stub = str(stub_bin / "gh")
	hit = subprocess.run([stub, "api", "head=o:feat $(x)"], capture_output=True, text=True, check=True)
	assert hit.stdout == payload
	assert not (tmp_path / "pwned").exists()
	miss = subprocess.run([stub, "api", "head=o:other"], capture_output=True, text=True, check=True)
	assert miss.stdout.strip() == "[]"


def test_repo_git_ignores_an_inherited_git_dir(hook_repo: Path, tmp_path: Path, monkeypatch) -> None:
	other = tmp_path / "other"
	other.mkdir()
	_git(other, "init", "-q", "-b", "main")
	monkeypatch.setenv("GIT_DIR", str(other / ".git"))
	monkeypatch.setenv("GIT_WORK_TREE", str(other))
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	assert gd.changed_paths(hook_repo, "main", None) == [".claude/hooks/fake_guard.py"]


def test_merged_checkout_scenario_blocks_on_the_real_merge_guard(tmp_path: Path) -> None:
	scenario = gd.merged_checkout_scenario(tmp_path / "s")
	env = gd.hook_env(scenario.env, scenario.stub_bin, tmp_path / "home", tmp_path / "cache")
	(tmp_path / "home").mkdir()
	(tmp_path / "cache").mkdir()
	shape = gd.Shape("pr_merge_status_guard", "git push origin HEAD", 1)
	outcome = gd.run_hook(HOOKS_DIR / "pr_merge_status_guard.py", gd.build_stdin(shape, scenario.cwd, {}), scenario.cwd, env)
	assert outcome.decision == "block"
	calls = (scenario.stub_bin / "gh-calls.log").read_text(encoding="utf-8")
	assert "head=o:feature/x" in calls


def test_current_hooks_are_deterministic_against_themselves() -> None:
	"""Base and head both the current tree: any regression would be a flaky
	scenario, which would make the check fail on unrelated PRs."""
	proc = subprocess.run(
		[sys.executable, str(SCRIPT_PATH), "--repo-root", str(REPO_ROOT), "--base-ref", "HEAD", "--all", "--json"],
		capture_output=True,
		text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
		timeout=600,
	)
	rows = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
	assert rows, proc.stdout + proc.stderr
	changed = [row for row in rows if row["base"] != row["head"]]
	assert not changed, changed


# ──────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────


def _ci_step(name_fragment: str) -> str:
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	start = text.index(f'- name: "{name_fragment}"')
	end = text.find("\n      - name:", start + 1)
	return text[start : end if end != -1 else len(text)]


def test_ci_runs_the_check_on_pull_requests() -> None:
	step = _ci_step("Guard differential check (issue #5174)")
	assert "if: github.event_name == 'pull_request'" in step
	assert "scripts/guard_differential.py" in step
	assert "--base-ref FETCH_HEAD" in step
	assert "--pr-body-file" in step
	assert '--head-sha "${GUARD_DIFFERENTIAL_HEAD_SHA}"' in step
	assert "GUARD_DIFFERENTIAL_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in step
	assert 'git fetch --no-tags --depth=1 origin "${GUARD_DIFFERENTIAL_BASE_REF}"' in step
	assert "${{ github.base_ref }}" in step
	assert not re.search(r"\bgh\s+(?:api|pr|issue|run)\b", step) and "api.github.com" not in step


def test_ci_runs_the_unit_tests() -> None:
	step = _ci_step("Guard differential tests (issue #5174)")
	assert "tests/test_guard_differential.py" in step


def test_agents_md_documents_the_corpus() -> None:
	text = AGENTS_MD.read_text(encoding="utf-8")
	assert "tests/guard_corpus/<hook>.txt" in text
	assert "scripts/guard_differential.py" in text
	assert "Intended loosening:" in text
	assert ".github/guard_differential/intended_loosening.json" in text
	assert "pr_body_listed=true" in text
	flat = " ".join(text.split())
	assert "malformed `--head-sha` (or one that contradicts `--head-ref`) always exits 2, even when no hook changed" in flat
