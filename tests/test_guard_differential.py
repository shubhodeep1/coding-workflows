#!/usr/bin/env python3
"""Behaviour and wiring contract for the guard differential check (issue #5174).

Covers:
  1. Decision parsing, the strictness order, and the `Intended loosening:`
     PR-body section.
  2. The comparison on fake hooks: a silent loosening fails, a warning or an
     intended listing passes, a deleted guard is a loosening, an added one
     is not.
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


def _compare(tmp_path: Path, base_source, head_source, listed=None, shapes=("git push",)):
	base_dir, head_dir = _hook_dirs(tmp_path, base_source, head_source)
	corpora = {"fake_guard": [gd.Shape("fake_guard", text, index + 1) for index, text in enumerate(shapes)]}
	return gd.compare_hook_dirs(base_dir, head_dir, corpora, tmp_path / "runs", listed or [])


@pytest.mark.parametrize(
	("base_source", "head_source", "regression"),
	[
		(FAKE_BLOCKING_HOOK, FAKE_SILENT_HOOK, True),
		(FAKE_BLOCKING_HOOK, FAKE_ALLOWING_HOOK, True),
		(FAKE_BLOCKING_HOOK, FAKE_CRASHING_HOOK, True),
		(FAKE_ASKING_HOOK, FAKE_SILENT_HOOK, True),
		(FAKE_SILENT_HOOK, FAKE_ALLOWING_HOOK, True),
		(FAKE_BLOCKING_HOOK, FAKE_WARNING_HOOK, False),
		(FAKE_BLOCKING_HOOK, FAKE_ASKING_HOOK + FAKE_WARNING_HOOK, False),
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


def test_intended_listing_excuses_only_the_listed_shape(tmp_path: Path) -> None:
	results = _compare(
		tmp_path,
		FAKE_BLOCKING_HOOK,
		FAKE_SILENT_HOOK,
		listed=[("fake_guard", "git push")],
		shapes=("git push", "git commit -m x"),
	)
	assert [(r.shape.text, r.intended, r.regression) for r in results] == [
		("git push", True, False),
		("git commit -m x", False, True),
	]


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
	assert "status=skipped reason=no-hook-change verifier_changes=0" in proc.stdout


def test_cli_fails_on_a_silent_loosening_in_the_working_tree(hook_repo: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	proc = _cli(hook_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "::error::GUARD_DIFFERENTIAL regression tree=.claude/hooks hook=fake_guard line=1" in proc.stdout
	assert "base=block head=none" in proc.stdout


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


def test_cli_passes_when_the_pr_body_lists_the_loosening(hook_repo: Path, tmp_path: Path) -> None:
	(hook_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	body = tmp_path / "body.md"
	body.write_text("## Intended loosening:\n- fake_guard: `git push`\n", encoding="utf-8")
	proc = _cli(hook_repo, "--pr-body-file", str(body))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "intended_loosening tree=.claude/hooks hook=fake_guard" in proc.stdout
	assert "status=pass" in proc.stdout


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
# Verifier-change report (issue #5327)
# ──────────────────────────────────────────────────────────────────


SAMPLE_CI_WORKFLOW = textwrap.dedent(
	"""\
	jobs:
	  tests:
	    steps:
	      - name: Checkout repository
	        uses: actions/checkout@v5

	      - name: "Guard differential check (issue #5174)"
	        if: github.event_name == 'pull_request'
	        run: |
	          python3 verifier.py --base-ref FETCH_HEAD

	      - name: Other step
	        run: echo other
	  later:
	    steps:
	      - name: Guard differential tests
	        run: echo tests
	"""
)


@pytest.fixture()
def verifier_repo(hook_repo: Path) -> Path:
	"""`hook_repo` with the verifier script and a CI workflow committed on `main`."""
	(hook_repo / "scripts").mkdir()
	(hook_repo / "scripts" / "guard_differential.py").write_text("print('v1')\n", encoding="utf-8")
	workflows = hook_repo / ".github" / "workflows"
	workflows.mkdir(parents=True)
	(workflows / "ci.yml").write_text(SAMPLE_CI_WORKFLOW, encoding="utf-8")
	_git(hook_repo, "add", "-A")
	_git(hook_repo, "commit", "-q", "-m", "verifier")
	return hook_repo


def test_guard_differential_steps_cut_each_named_step() -> None:
	steps = gd.guard_differential_steps(SAMPLE_CI_WORKFLOW)
	assert len(steps) == 2
	assert steps[0].startswith('      - name: "Guard differential check (issue #5174)"')
	assert steps[0].endswith("python3 verifier.py --base-ref FETCH_HEAD")
	assert "Other step" not in steps[0]
	assert steps[1] == "      - name: Guard differential tests\n        run: echo tests"
	assert gd.guard_differential_steps(None) == []
	assert gd.guard_differential_steps("jobs: {}\n") == []


def test_guard_differential_steps_read_past_outer_comment_lines() -> None:
	# PR #5368 review round 2: a comment indented less than the step used to
	# end it, so an edit to the keys after the comment went unreported.
	workflow = textwrap.dedent(
		"""\
		jobs:
		  tests:
		    steps:
		      - name: Guard differential check
		# pinned verifier below
		        run: |
		          # shell comment inside the run block
		          python3 verifier.py
		      # comment before the next step
		      - name: Other step
		        run: echo other
		      - name: Guard differential tests
		        run: echo tests
		  # comment before the next job
		  later:
		    steps: []
		"""
	)
	# dedent strips the source file's tab margin, so the YAML is space-indented.
	assert "\t" not in workflow
	steps = gd.guard_differential_steps(workflow)
	assert len(steps) == 2
	assert steps[0].endswith("python3 verifier.py")
	assert "# shell comment inside the run block" in steps[0]
	assert "comment before the next step" not in steps[0]
	assert steps[1] == "      - name: Guard differential tests\n        run: echo tests"
	edited = workflow.replace("python3 verifier.py", "true")
	assert gd.guard_differential_steps(edited) != steps
	recommented = workflow.replace("# comment before the next step", "# reworded").replace(
		"# comment before the next job", "# reworded"
	)
	assert gd.guard_differential_steps(recommented) == steps


def test_guard_differential_steps_find_the_shipped_ci_steps() -> None:
	steps = gd.guard_differential_steps(CI_WORKFLOW.read_text(encoding="utf-8"))
	names = [step.splitlines()[0].strip() for step in steps]
	assert names == [
		'- name: "Guard differential tests (issue #5174)"',
		'- name: "Guard differential check (issue #5174)"',
	]
	assert "FETCH_HEAD:scripts/guard_differential.py" in steps[1]
	assert "check-in reminder" not in steps[1]


def test_cli_reports_a_verifier_script_change_without_failing(verifier_repo: Path) -> None:
	(verifier_repo / "scripts" / "guard_differential.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
	proc = _cli(verifier_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "::warning::GUARD_DIFFERENTIAL verifier_change path=scripts/guard_differential.py:" in proc.stdout
	assert "path=.github/workflows/ci.yml" not in proc.stdout
	assert "status=skipped reason=no-hook-change verifier_changes=1" in proc.stdout


@pytest.mark.parametrize(
	("old", "new"),
	[
		("python3 verifier.py --base-ref FETCH_HEAD", "true"),
		('      - name: "Guard differential check (issue #5174)"\n', '      - name: "Renamed check"\n'),
		("echo tests", "echo skipped"),
	],
)
def test_cli_reports_a_guard_step_change(verifier_repo: Path, old: str, new: str) -> None:
	workflow = verifier_repo / ".github" / "workflows" / "ci.yml"
	text = workflow.read_text(encoding="utf-8")
	assert text.count(old) == 1
	workflow.write_text(text.replace(old, new), encoding="utf-8")
	proc = _cli(verifier_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "::warning::GUARD_DIFFERENTIAL verifier_change path=.github/workflows/ci.yml:" in proc.stdout


def test_cli_ignores_other_workflow_edits(verifier_repo: Path) -> None:
	workflow = verifier_repo / ".github" / "workflows" / "ci.yml"
	workflow.write_text(
		workflow.read_text(encoding="utf-8").replace("echo other", "echo changed") + "  extra:\n    steps: []\n",
		encoding="utf-8",
	)
	proc = _cli(verifier_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "verifier_change path=" not in proc.stdout
	assert "status=skipped reason=no-hook-change verifier_changes=0" in proc.stdout


def test_cli_verifier_change_keeps_the_regression_exit_code(verifier_repo: Path) -> None:
	(verifier_repo / ".claude" / "hooks" / "fake_guard.py").write_text(FAKE_SILENT_HOOK, encoding="utf-8")
	(verifier_repo / "scripts" / "guard_differential.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
	proc = _cli(verifier_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "verifier_change path=scripts/guard_differential.py" in proc.stdout
	assert "regressions=1" in proc.stdout and "verifier_changes=1" in proc.stdout


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
	assert 'git fetch --no-tags --depth=1 origin "${GUARD_DIFFERENTIAL_BASE_REF}"' in step
	assert "${{ github.base_ref }}" in step
	assert not re.search(r"\bgh\s+(?:api|pr|issue|run)\b", step) and "api.github.com" not in step
	# Issue #5327: the verifier that runs is the base commit's copy.
	assert 'git show "FETCH_HEAD:scripts/guard_differential.py"' in step
	assert 'python3 "${guard_differential_verifier_dir}/guard_differential.py"' in step
	assert "python3 scripts/guard_differential.py" not in step


def _ci_step_run(name: str) -> str:
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
	for job in workflow["jobs"].values():
		for step in job.get("steps", []):
			if step.get("name") == name:
				return step["run"]
	raise AssertionError(f"step {name!r} not found")


def _fake_verifier(label: str, code: int) -> str:
	return textwrap.dedent(
		f"""\
		import sys
		body = open(sys.argv[sys.argv.index("--pr-body-file") + 1], encoding="utf-8").read()
		print("VERIFIER={label} path0=" + sys.path[0] + " argv=" + " ".join(sys.argv[1:]) + " body=" + repr(body))
		sys.exit({code})
		"""
	)


def _run_check_step(tmp_path: Path, base_has_verifier: bool) -> subprocess.CompletedProcess:
	"""Run the real `Guard differential check` step body in a checkout of a
	PR branch whose own verifier exits 3, against a local `origin`."""
	origin = tmp_path / "origin"
	origin.mkdir()
	_git(origin, "init", "-q", "-b", "main")
	_git(origin, "config", "user.email", "t@example.com")
	_git(origin, "config", "user.name", "T")
	_git(origin, "config", "commit.gpgsign", "false")
	(origin / "README.md").write_text("base\n", encoding="utf-8")
	if base_has_verifier:
		(origin / "scripts").mkdir()
		(origin / "scripts" / "guard_differential.py").write_text(_fake_verifier("base", 0), encoding="utf-8")
	_git(origin, "add", "-A")
	_git(origin, "commit", "-q", "-m", "base")
	workspace = tmp_path / "workspace"
	_git(tmp_path, "clone", "-q", origin.as_uri(), str(workspace))
	_git(workspace, "config", "user.email", "t@example.com")
	_git(workspace, "config", "user.name", "T")
	_git(workspace, "config", "commit.gpgsign", "false")
	_git(workspace, "checkout", "-q", "-b", "pr")
	(workspace / "scripts").mkdir(exist_ok=True)
	(workspace / "scripts" / "guard_differential.py").write_text(_fake_verifier("head", 3), encoding="utf-8")
	_git(workspace, "add", "-A")
	_git(workspace, "commit", "-q", "-m", "pr edits the verifier")
	runner_temp = tmp_path / "runner-temp"
	runner_temp.mkdir()
	event = tmp_path / "event.json"
	event.write_text(json.dumps({"pull_request": {"body": "the pr body"}}), encoding="utf-8")
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update(
		{
			"HOME": str(tmp_path),
			"GIT_CONFIG_NOSYSTEM": "1",
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_EVENT_PATH": str(event),
			"GUARD_DIFFERENTIAL_BASE_REF": "main",
		}
	)
	return subprocess.run(
		["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _ci_step_run("Guard differential check (issue #5174)")],
		cwd=workspace,
		capture_output=True,
		text=True,
		env=env,
		timeout=120,
	)


def test_ci_step_runs_the_base_verifier_not_the_prs(tmp_path: Path) -> None:
	proc = _run_check_step(tmp_path, base_has_verifier=True)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "VERIFIER=base" in proc.stdout
	assert "VERIFIER=head" not in proc.stdout
	assert "GUARD_DIFFERENTIAL verifier=base source=" in proc.stdout
	assert f"path0={tmp_path / 'runner-temp' / 'guard-differential-verifier'} " in proc.stdout
	assert "argv=--base-ref FETCH_HEAD --pr-body-file " in proc.stdout
	assert "body='the pr body'" in proc.stdout


def test_ci_step_falls_back_to_the_prs_verifier_with_a_warning(tmp_path: Path) -> None:
	proc = _run_check_step(tmp_path, base_has_verifier=False)
	assert proc.returncode == 3, proc.stdout + proc.stderr
	assert "VERIFIER=head" in proc.stdout
	assert "::warning::GUARD_DIFFERENTIAL verifier=head reason=base-has-no-verifier base=main" in proc.stdout


def test_ci_runs_the_unit_tests() -> None:
	step = _ci_step("Guard differential tests (issue #5174)")
	assert "tests/test_guard_differential.py" in step


def test_agents_md_documents_the_corpus() -> None:
	text = AGENTS_MD.read_text(encoding="utf-8")
	assert "tests/guard_corpus/<hook>.txt" in text
	assert "scripts/guard_differential.py" in text
	assert "Intended loosening:" in text
