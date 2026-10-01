#!/usr/bin/env python3
"""Behaviour and wiring contract for the guard differential check (issue #5174).

Covers:
  1. Decision parsing, the strictness order, and the `Intended loosening:`
     PR-body section.
  2. The comparison on fake hooks: a loosening fails with or without a
     warning (issue #5325), an intended listing passes, a deleted guard is a
     loosening, an added one is not.
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
	assert "status=skipped reason=no-hook-change checked=hooks,settings" in proc.stdout


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
# Settings guard wiring (issue #5328)
# ──────────────────────────────────────────────────────────────────


SETTINGS_PATHS = (".claude/settings.json", "workflow-templates/.claude/settings.json")
CANONICAL_FAKE_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/fake_guard.py'
EXPLOIT_FAKE_COMMAND = "python3 -c 'pass' \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/fake_guard.py"


def _settings(command: str = CANONICAL_FAKE_COMMAND, matcher: object = "Bash", **entry_extra) -> dict:
	entry = {"type": "command", "command": command, "timeout": 30, **entry_extra}
	group: dict = {"hooks": [entry]}
	if matcher is not None:
		group["matcher"] = matcher
	return {
		"permissions": {"allow": ["Bash(git status)"]},
		"hooks": {
			"PreToolUse": [group],
			"PostToolUse": [
				{"matcher": "Bash", "hooks": [{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/notes_hook.py'}]}
			],
		},
	}


def _write_settings(repo: Path, settings: object, paths=SETTINGS_PATHS) -> None:
	for path in paths:
		target = repo / path
		target.parent.mkdir(parents=True, exist_ok=True)
		text = settings if isinstance(settings, str) else json.dumps(settings, indent=2)
		target.write_text(text, encoding="utf-8")


@pytest.fixture()
def settings_repo(hook_repo: Path) -> Path:
	"""`hook_repo` plus the twin hooks tree and both settings files wiring
	`fake_guard` canonically, committed on `main`."""
	twin = hook_repo / "workflow-templates" / ".claude" / "hooks"
	twin.mkdir(parents=True)
	(twin / "fake_guard.py").write_text(FAKE_BLOCKING_HOOK, encoding="utf-8")
	_write_settings(hook_repo, _settings())
	_git(hook_repo, "add", "-A")
	_git(hook_repo, "commit", "-q", "-m", "wire the guard")
	return hook_repo


def _wiring_lines(proc: subprocess.CompletedProcess) -> list[str]:
	return [line for line in proc.stdout.splitlines() if "wiring_regression settings=" in line]


def test_extract_guard_wiring_reads_only_guard_commands() -> None:
	settings = _settings()
	settings["hooks"]["PreToolUse"].append("not a group")
	settings["hooks"]["Stop"] = "not a list"
	[wiring] = gd.extract_guard_wiring(settings)
	assert (wiring.event, wiring.matcher, wiring.hook, wiring.command, wiring.timeout) == (
		"PreToolUse",
		"Bash",
		"fake_guard",
		CANONICAL_FAKE_COMMAND,
		30,
	)
	assert gd.extract_guard_wiring({"hooks": []}) == []
	assert gd.extract_guard_wiring({}) == []


@pytest.mark.parametrize(
	("head", "base", "covers"),
	[
		("Bash", "Bash", True),
		(None, "Bash", True),
		("", "Bash", True),
		("*", "Bash", True),
		("Bash|Edit", "Bash", True),
		("mcp__a|mcp__b|Bash", "mcp__b|mcp__a", True),
		("Bashx", "Bash", False),
		("Edit", "Bash", False),
		("mcp__a", "mcp__a|mcp__b", False),
		("mcp__.*__x|Bash", "mcp__.*__x", False),
		("Bash", None, False),
		(".*", "Bash", False),
	],
)
def test_matcher_covers(head, base, covers: bool) -> None:
	assert gd.matcher_covers(head, base) is covers


def test_wiring_failures_require_the_hook_at_the_head() -> None:
	base = gd.GuardWiring("PreToolUse", "Bash", "fake_guard", "python3 old-form fake_guard", 30, "{}")
	head = gd.GuardWiring("PreToolUse", "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, 30, "{}")
	assert gd.wiring_failures(head, base, {"fake_guard"}) == []
	assert gd.wiring_failures(head, base, set()) == ["command"]


def test_malformed_timeouts_fail_closed_without_crashing() -> None:
	base = gd.GuardWiring("PreToolUse", "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, 30, "{}")
	for timeout in ([30], "30", True):
		head = gd.GuardWiring("PreToolUse", "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, timeout, "{}")
		assert gd.wiring_failures(head, base, {"fake_guard"}) == ["timeout"]


def test_non_finite_timeouts_fail_closed() -> None:
	# A NaN compares false with everything, so `head < base` alone let it pass.
	base = gd.GuardWiring("PreToolUse", "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, 30, "{}")
	for timeout in (float("nan"), float("inf"), float("-inf"), 10**400):
		head = gd.GuardWiring("PreToolUse", "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, timeout, "{}")
		assert gd.wiring_failures(head, base, {"fake_guard"}) == ["timeout"]


@pytest.mark.parametrize(
	("event", "head_timeout", "base_timeout", "failures"),
	[
		("PreToolUse", 90, None, ["timeout"]),
		("PreToolUse", None, 30, []),
		("PreToolUse", 600, None, []),
		("UserPromptSubmit", None, 100, ["timeout"]),
		("UserPromptSubmit", 30, None, []),
		("MessageDisplay", 5, None, ["timeout"]),
	],
)
def test_an_omitted_timeout_is_claude_codes_default(event: str, head_timeout, base_timeout, failures: list[str]) -> None:
	"""600 s for a command hook, lowered on some events (code.claude.com/docs/en/hooks)."""
	base = gd.GuardWiring(event, "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, base_timeout, "{}")
	head = gd.GuardWiring(event, "Bash", "fake_guard", CANONICAL_FAKE_COMMAND, head_timeout, "{}")
	assert gd.wiring_failures(head, base, {"fake_guard"}) == failures


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'])
def test_parse_settings_rejects_non_standard_constants(text: str) -> None:
	assert json.loads(text)  # Python accepts them by default; Claude Code does not.
	assert gd.parse_settings(text) is None


def test_a_settings_read_failure_is_a_setup_error(settings_repo: Path, monkeypatch) -> None:
	def unreadable(*args, **kwargs):
		raise PermissionError("denied")

	monkeypatch.setattr(Path, "read_text", unreadable)
	with pytest.raises(gd.SetupError, match="cannot read settings .claude/settings.json"):
		gd.read_settings_text(settings_repo, None, ".claude/settings.json")


def test_cli_non_utf8_settings_are_unparseable_in_the_tree_and_at_a_ref(settings_repo: Path) -> None:
	target = settings_repo / ".claude" / "settings.json"
	target.write_bytes(json.dumps(_settings()).encode("utf-8") + b"\xff")
	proc = _cli(settings_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert "reason=unparseable " in line, line
	_git(settings_repo, "commit", "-q", "-am", "non-utf8 settings")
	proc = _cli(settings_repo, "--base-ref", "HEAD~1", "--head-ref", "HEAD")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert "reason=unparseable " in line, line


def test_cli_the_finding_exploit_fails_in_both_settings_files(settings_repo: Path) -> None:
	"""The #5328 finding: rewiring the guard to `python3 -c 'pass' <hook path>`
	used to skip the check because no hook `*.py` file changed."""
	_write_settings(settings_repo, _settings(EXPLOIT_FAKE_COMMAND))
	proc = _cli(settings_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	lines = _wiring_lines(proc)
	assert len(lines) == 2, proc.stdout
	for path in SETTINGS_PATHS:
		assert any(
			f"settings={path} event=PreToolUse matcher=\"Bash\" hook=fake_guard reason=command "
			f'shape="settings:{path}:PreToolUse:Bash:fake_guard"' in line
			for line in lines
		), proc.stdout
	assert "status=fail" in proc.stdout
	assert "wiring_regressions=2" in proc.stdout


@pytest.mark.parametrize("path", SETTINGS_PATHS)
def test_cli_the_exploit_fails_between_committed_refs(settings_repo: Path, path: str) -> None:
	_git(settings_repo, "checkout", "-q", "-b", "pr")
	_write_settings(settings_repo, _settings(EXPLOIT_FAKE_COMMAND), paths=(path,))
	_git(settings_repo, "commit", "-q", "-am", "rewire")
	_git(settings_repo, "checkout", "-q", "main")
	proc = _cli(settings_repo, "--head-ref", "pr")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert f"settings={path} " in line


def _without_guard(settings: dict) -> dict:
	settings["hooks"]["PreToolUse"] = []
	return settings


def _moved_to_post_tool_use(settings: dict) -> dict:
	settings["hooks"]["PostToolUse"] += settings["hooks"].pop("PreToolUse")
	return settings


def _all_hooks_disabled(settings: dict) -> dict:
	settings["disableAllHooks"] = True
	return settings


def _with_env(settings: dict, **env: str) -> dict:
	settings["env"] = env
	return settings


@pytest.mark.parametrize(
	("head", "reason"),
	[
		(_without_guard(_settings()), "removed"),
		(_moved_to_post_tool_use(_settings()), "removed"),
		(_settings(matcher="Bashx"), "matcher"),
		(_settings(matcher="Edit|Write"), "matcher"),
		(_settings(command="python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/fake_guard.py || true"), "command"),
		(_settings(command="python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/fake_guard.py; exit 0"), "command"),
		(_settings(timeout=1), "timeout"),
		(_settings(**{"async": True}), "keys"),
		(_settings(type="prompt"), "keys"),
		(_all_hooks_disabled(_settings()), "disableAllHooks"),
		(_with_env(_settings(), CLAUDE_PR_MERGE_GUARD="off"), "env"),
		(_with_env(_settings(), PATH="./bin:/usr/bin"), "env"),
		("{not json", "unparseable"),
		("[]", "unparseable"),
		(json.dumps(_settings(timeout=float("nan"))), "unparseable"),
		(json.dumps(_settings(timeout=float("inf"))), "unparseable"),
		(json.dumps(_settings(timeout=12345)).replace("12345", "1e400"), "timeout"),
		(json.dumps(_settings(timeout=12345)).replace("12345", "1" + "0" * 400), "timeout"),
	],
	ids=[
		"removed",
		"moved-event",
		"matcher-typo",
		"matcher-other-tools",
		"command-or-true",
		"command-exit-0",
		"timeout-lowered",
		"async-added",
		"type-changed",
		"disable-all-hooks",
		"env-kill-switch",
		"env-path-shadow",
		"invalid-json",
		"not-an-object",
		"timeout-nan",
		"timeout-infinity",
		"timeout-overflows-to-inf",
		"timeout-int-overflows-float",
	],
)
def test_cli_unverifiable_wiring_changes_fail_closed(settings_repo: Path, head, reason: str) -> None:
	_write_settings(settings_repo, head, paths=(".claude/settings.json",))
	proc = _cli(settings_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert "settings=.claude/settings.json " in line
	assert f"reason={reason} " in line, line


def test_cli_a_deleted_settings_file_fails(settings_repo: Path) -> None:
	(settings_repo / ".claude" / "settings.json").unlink()
	proc = _cli(settings_repo)
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert "reason=removed" in line


def test_cli_a_deleted_settings_file_with_only_env_fails(settings_repo: Path) -> None:
	# The `env` regression is the only line when the deleted file wires no
	# guard, so it is not a duplicate of the per-entry `removed` lines.
	_write_settings(settings_repo, _with_env(_without_guard(_settings()), CLAUDE_PR_MERGE_GUARD="on"), paths=(".claude/settings.json",))
	_git(settings_repo, "commit", "-q", "-am", "env only")
	(settings_repo / ".claude" / "settings.json").unlink()
	proc = _cli(settings_repo, "--base-ref", "HEAD")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert 'reason=env shape="settings:.claude/settings.json:env"' in line, line


def _commit_unparseable_base(repo: Path, text: str) -> None:
	_write_settings(repo, text, paths=(".claude/settings.json",))
	_git(repo, "commit", "-q", "-am", "unparseable base")


@pytest.mark.parametrize(
	"base_text",
	["{not json", "[]", json.dumps(_settings(timeout=float("nan")))],
	ids=["not-json", "non-object", "nan"],
)
@pytest.mark.parametrize(
	"head",
	[_without_guard(_settings()), _settings()],
	ids=["guard-dropped", "guard-kept"],
)
def test_cli_an_unparseable_base_fails_closed(settings_repo: Path, base_text: str, head) -> None:
	"""A committed base that does not parse has no wiring to compare, so a
	head that repairs it cannot be verified to keep any guard running."""
	_commit_unparseable_base(settings_repo, base_text)
	_write_settings(settings_repo, head, paths=(".claude/settings.json",))
	proc = _cli(settings_repo, "--base-ref", "HEAD")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert (
		"settings=.claude/settings.json event=- matcher=null hook=- reason=base-unparseable "
		'shape="settings:.claude/settings.json:base-unparseable"'
	) in line, line


def test_cli_an_unparseable_base_fails_between_committed_refs(settings_repo: Path) -> None:
	_commit_unparseable_base(settings_repo, "{not json")
	_write_settings(settings_repo, _without_guard(_settings()), paths=(".claude/settings.json",))
	_git(settings_repo, "commit", "-q", "-am", "repair without the guard")
	proc = _cli(settings_repo, "--base-ref", "HEAD~1", "--head-ref", "HEAD")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert "reason=base-unparseable " in line, line


def test_cli_an_unparseable_head_is_reported_before_an_unparseable_base(settings_repo: Path) -> None:
	_commit_unparseable_base(settings_repo, "{not json")
	_write_settings(settings_repo, "[]", paths=(".claude/settings.json",))
	proc = _cli(settings_repo, "--base-ref", "HEAD")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	[line] = _wiring_lines(proc)
	assert 'reason=unparseable shape="settings:.claude/settings.json:unparseable"' in line, line


def test_cli_the_pr_body_can_list_an_unparseable_base_repair(settings_repo: Path, tmp_path: Path) -> None:
	_commit_unparseable_base(settings_repo, "{not json")
	_write_settings(settings_repo, _settings(), paths=(".claude/settings.json",))
	body = tmp_path / "body.md"
	body.write_text("## Intended loosening:\n- `settings:.claude/settings.json:base-unparseable`\n", encoding="utf-8")
	proc = _cli(settings_repo, "--base-ref", "HEAD", "--pr-body-file", str(body))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "GUARD_DIFFERENTIAL intended_wiring_change settings=.claude/settings.json" in proc.stdout
	assert "reason=base-unparseable" in proc.stdout
	assert not _wiring_lines(proc)


def _delete_object(repo: Path, rev: str) -> None:
	"""Drop the loose object `rev` names, as a missing or corrupt blob would be.

	`unlink` fails loudly when the object is not loose, and the `cat-file -e`
	check fails the test when git can still read the object (for example from
	a pack), so a caller never runs against a blob that is still readable."""
	oid = _git(repo, "rev-parse", rev)
	(repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
	with pytest.raises(subprocess.CalledProcessError):
		_git(repo, "cat-file", "-e", oid)


def test_read_settings_text_at_a_ref_tells_absent_from_present(settings_repo: Path) -> None:
	assert gd.read_settings_text(settings_repo, "main", ".claude/settings.json") == json.dumps(_settings(), indent=2)
	assert gd.read_settings_text(settings_repo, "main", ".claude/missing.json") is None
	assert gd.read_settings_text(settings_repo, "main", "no-such-dir/settings.json") is None


def test_cli_an_unreadable_base_settings_blob_exits_2(settings_repo: Path) -> None:
	"""A base settings file that exists but cannot be read is not an absent
	one: read as absent, it left no base wiring, so dropping the guard passed.
	(Against the working tree, `git diff` already fails on the missing blob.)"""
	_git(settings_repo, "checkout", "-q", "-b", "pr")
	_write_settings(settings_repo, _without_guard(_settings()))
	_git(settings_repo, "commit", "-q", "-am", "drop the guard")
	_delete_object(settings_repo, "main:.claude/settings.json")
	proc = _cli(settings_repo, "--head-ref", "pr")
	assert proc.returncode == 2, proc.stdout + proc.stderr
	assert "status=error" in proc.stdout
	assert "cat-file -p main:.claude/settings.json failed" in proc.stdout, proc.stdout
	assert not _wiring_lines(proc)


def _permissions_only(settings: dict) -> dict:
	settings["permissions"]["allow"].append("Bash(git log)")
	return settings


def _canonical_twice(settings: dict) -> dict:
	settings["hooks"]["PreToolUse"].append({"matcher": "Edit", "hooks": [{"type": "command", "command": CANONICAL_FAKE_COMMAND}]})
	return settings


@pytest.mark.parametrize(
	"head",
	[
		_permissions_only(_settings()),
		_settings(matcher="Bash|Edit"),
		_settings(matcher=None),
		_settings(timeout=90),
		_canonical_twice(_settings()),
	],
	ids=["permissions-only", "matcher-widened", "matcher-match-all", "timeout-raised", "second-entry-added"],
)
def test_cli_verifiable_wiring_changes_pass(settings_repo: Path, head) -> None:
	_write_settings(settings_repo, head)
	proc = _cli(settings_repo)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "status=pass" in proc.stdout
	assert "settings=.claude/settings.json,workflow-templates/.claude/settings.json wiring_regressions=0" in proc.stdout


def test_cli_a_changed_command_in_the_canonical_form_passes(settings_repo: Path) -> None:
	_write_settings(settings_repo, _settings(command='python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/fake_guard.py 2>&1'))
	_git(settings_repo, "commit", "-q", "-am", "non-canonical base")
	_write_settings(settings_repo, _settings())
	proc = _cli(settings_repo, "--base-ref", "HEAD")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "wiring_regressions=0" in proc.stdout


def test_cli_the_pr_body_can_list_an_intended_wiring_change(settings_repo: Path, tmp_path: Path) -> None:
	_write_settings(settings_repo, _settings(matcher="Edit"), paths=(".claude/settings.json",))
	body = tmp_path / "body.md"
	body.write_text(
		"## Intended loosening:\n- fake_guard: `settings:.claude/settings.json:PreToolUse:Bash:fake_guard`\n",
		encoding="utf-8",
	)
	proc = _cli(settings_repo, "--pr-body-file", str(body))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "GUARD_DIFFERENTIAL intended_wiring_change settings=.claude/settings.json" in proc.stdout
	assert not _wiring_lines(proc)


def test_cli_a_listing_for_another_hook_does_not_excuse_the_change(settings_repo: Path, tmp_path: Path) -> None:
	_write_settings(settings_repo, _settings(matcher="Edit"), paths=(".claude/settings.json",))
	body = tmp_path / "body.md"
	body.write_text(
		"## Intended loosening:\n- other_guard: `settings:.claude/settings.json:PreToolUse:Bash:fake_guard`\n",
		encoding="utf-8",
	)
	proc = _cli(settings_repo, "--pr-body-file", str(body))
	assert proc.returncode == 1, proc.stdout + proc.stderr


def test_cli_settings_json_output_rows(settings_repo: Path) -> None:
	_write_settings(settings_repo, _settings(EXPLOIT_FAKE_COMMAND), paths=(".claude/settings.json",))
	proc = _cli(settings_repo, "--json")
	rows = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
	assert rows == [
		{
			"settings": ".claude/settings.json",
			"event": "PreToolUse",
			"matcher": "Bash",
			"hook": "fake_guard",
			"reason": "command",
			"identity": "settings:.claude/settings.json:PreToolUse:Bash:fake_guard",
			"intended": False,
		}
	]


def test_shipped_settings_wire_every_guard_canonically() -> None:
	"""Both shipped settings files wire each guard in the canonical form, so a
	change to any of them is caught and a revert to that form passes."""
	for path in SETTINGS_PATHS:
		settings = json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
		wirings = gd.extract_guard_wiring(settings)
		assert {wiring.hook for wiring in wirings} >= {"pr_merge_status_guard", "gh_api_write_guard", "pr_watch_guard"}
		for wiring in wirings:
			assert gd.CANONICAL_GUARD_COMMAND_RE.fullmatch(wiring.command), (path, wiring.command)


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


def test_ci_runs_the_unit_tests() -> None:
	step = _ci_step("Guard differential tests (issue #5174)")
	assert "tests/test_guard_differential.py" in step


def test_agents_md_documents_the_corpus() -> None:
	text = AGENTS_MD.read_text(encoding="utf-8")
	assert "tests/guard_corpus/<hook>.txt" in text
	assert "scripts/guard_differential.py" in text
	assert "Intended loosening:" in text
