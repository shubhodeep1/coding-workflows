#!/usr/bin/env python3
"""Guard differential check: run every adversarial corpus shape through the
base-branch hook and the PR's hook, and fail on a silent loosening.

Issue #5174. The `.claude/hooks/*_guard.py` hooks are the security boundary of
unattended sessions (CLAUDE.md §21, §23.H, §25, and the inline-edit guard of
#4858). The reviewer panel reads a hook diff but never runs it: #5144's
rewrite of `pr_merge_status_guard.py` (PR #5173 at `b6dd693`) passed review
with three command shapes where the old hook blocked and the new one allowed
without a warning. This check executes the old and the new hook side by side,
so that class of regression cannot land unnoticed.

What it does, per hook tree (`.claude/hooks/` and its twin
`workflow-templates/.claude/hooks/`) in which any `*.py` file changed between
the base and the head:
  1. Copies the whole hooks directory of each side into a scratch directory
     (a hook may load its siblings, e.g. the inline-edit guard reuses
     `gh_api_write_guard.py`'s tokenizer).
  2. For every corpus file `tests/guard_corpus/<hook>.txt` whose hook exists
     on either side (the union of the corpus on disk, at the head ref when
     one is given, and at the base ref, so deleting a shape does not hide a
     loosening), builds the hook's scenario (a scratch git repository and
     a stub `gh` on PATH, see SCENARIOS) and runs each shape through the base
     hook and the head hook, each with a cold cache directory.
  3. Records each decision (`block`, `deny`, `ask`, `none`, `allow`, `error`)
     and whether the hook emitted a warning (`systemMessage`).
  4. Fails on every shape whose head decision is less strict than its base
     decision (block = deny > ask > none > allow = error) while the head
     emitted no warning, unless the PR body lists the shape under an
     `Intended loosening:` section. A listed shape counts as a loosening
     under the retire-master Q3: A rule, so a sync carrying it waits for the
     operator.

A hook present on only one side runs as "no hook" (decision `none`) on the
other, so deleting a guard is a loosening, and a new hook is one only where it
answers `allow`. A changed `*_guard.py` (new, edited, or deleted) with no
corpus file, or with one that holds no shape, fails the check: every guard
must be covered.

Corpus format (one shape per line; blank lines and `#` comments skipped):
  <command>             a Bash `tool_input.command`;
  json: <object>        a whole PreToolUse payload (for MCP tools and
                        multi-line commands; JSON strings carry the `\\n`);
  stdin: <text>         raw hook stdin (malformed-payload shapes).
The scenario replaces `@@REPO@@` and `@@WORKTREE@@` with its scratch paths.
The shape's identity (for `Intended loosening:`) is the line text exactly
as written in the corpus, before placeholder substitution.

Batching contract (CLAUDE.md §15): no GitHub API calls. The hooks run with a
stub `gh` first on PATH, `GH_TOKEN` / `GITHUB_TOKEN` removed, and
`GIT_ALLOW_PROTOCOL=file`, so a hook's `git fetch` / `ls-remote` against
the scenario's `https://github.com/o/r.git` origin fails locally instead of
reaching the network. Every `CLAUDE_*` variable is removed, so no kill switch
is inherited.

Output: one `GUARD_DIFFERENTIAL` line per regression, per missing corpus, and
a summary line. `--json` also prints every shape's result. A change to this
script or to a `Guard differential …` step of `.github/workflows/ci.yml`
prints a `verifier_change` warning, whether or not a hook changed. Exit 0 when
clean or when no hook changed, 1 on a regression or a missing corpus, 2 on a
usage or setup error (bad ref, unreadable corpus). A `verifier_change` never
changes the exit code.

Wired as the `Guard differential check (issue #5174)` step of the
`tests-hooks-and-orchestrator` job in `.github/workflows/ci.yml` (reported
through the `CI / lint` aggregate), which runs on every pull request into
`main` and `stable`, so a #4785 twin-sync PR is gated by it too. The step
runs the base branch's copy of this script, not the PR's (issue #5327), so
a PR cannot weaken a guard and edit the verifier to pass in the same change.
Only a base that does not carry the script yet runs the PR's copy, with a
warning. The step may pass only flags the base copy already accepts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


LOG_KEY = "GUARD_DIFFERENTIAL"
HOOK_TREES = (".claude/hooks", "workflow-templates/.claude/hooks")
DEFAULT_CORPUS_DIR = "tests/guard_corpus"
VERIFIER_SCRIPT_PATH = "scripts/guard_differential.py"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
GUARD_STEP_NAME_PREFIX = "Guard differential"
HOOK_TIMEOUT_SECONDS = 120
_STRIPPED_ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")

STRICTNESS = {
	"block": 3,
	"deny": 3,
	"ask": 2,
	"none": 1,
	"allow": 0,
	"error": 0,
}

INTENDED_LOOSENING_HEADING_RE = re.compile(
	r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*intended\s+loosening\s*:?\s*(?:\*\*|__)?\s*:?\s*$",
	re.IGNORECASE,
)
MARKDOWN_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
BACKTICK_RE = re.compile(r"`([^`]+)`")
STEP_START_RE = re.compile(r"^(?P<indent>\s*)- name:\s*(?P<name>.*?)\s*$")


@dataclass(frozen=True)
class Shape:
	hook: str
	text: str
	line: int


@dataclass(frozen=True)
class Outcome:
	decision: str
	warned: bool
	detail: str = ""


@dataclass
class ShapeResult:
	shape: Shape
	tree: str
	base: Outcome
	head: Outcome
	loosened: bool = False
	intended: bool = False

	@property
	def regression(self) -> bool:
		return self.loosened and not self.intended


@dataclass
class Report:
	results: list[ShapeResult] = field(default_factory=list)
	missing_corpus: list[str] = field(default_factory=list)
	trees: list[str] = field(default_factory=list)
	verifier_changes: list[str] = field(default_factory=list)

	@property
	def regressions(self) -> list[ShapeResult]:
		return [result for result in self.results if result.regression]

	@property
	def failed(self) -> bool:
		return bool(self.regressions or self.missing_corpus)


class SetupError(Exception):
	"""A ref, file, or corpus the check needs could not be read."""


# ──────────────────────────────────────────────────────────────────
# Corpus and PR body parsing
# ──────────────────────────────────────────────────────────────────


def parse_corpus(text: str, hook: str, source: str) -> list[Shape]:
	"""Parse one corpus file's text into shapes, validating `json:` lines."""
	shapes: list[Shape] = []
	for number, raw in enumerate(text.splitlines(), start=1):
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		if line.startswith("json:"):
			try:
				payload = json.loads(line[len("json:"):])
			except ValueError as exc:
				raise SetupError(f"{source}:{number}: invalid json shape ({exc})") from exc
			if not isinstance(payload, dict):
				raise SetupError(f"{source}:{number}: json shape must be an object")
		shapes.append(Shape(hook=hook, text=line, line=number))
	return shapes


def load_corpus(path: Path) -> list[Shape]:
	"""Read one corpus file into shapes."""
	try:
		text = path.read_text(encoding="utf-8")
	except OSError as exc:
		raise SetupError(f"cannot read corpus {path}: {exc}") from exc
	return parse_corpus(text, path.stem, str(path))


def merge_corpora(*sources: dict[str, list[Shape]]) -> dict[str, list[Shape]]:
	"""Union corpora by hook and shape text, keeping the first occurrence.

	The check runs the base branch's corpus as well as the PR's, so a PR
	cannot pass by deleting the shape its hook change loosens.
	"""
	merged: dict[str, list[Shape]] = {}
	for corpora in sources:
		for hook, shapes in corpora.items():
			bucket = merged.setdefault(hook, [])
			seen = {shape.text for shape in bucket}
			for shape in shapes:
				if shape.text not in seen:
					bucket.append(shape)
					seen.add(shape.text)
	return merged


def intended_loosening(pr_body: str) -> list[tuple[str, str]]:
	"""Return the `(hook, shape)` pairs listed under `Intended loosening:`.

	`hook` is empty when an item names no hook (it then matches every hook).
	An item is a list entry; the shape is its backticked text, or the whole
	item text when it has no backticks. A `<hook>:` prefix before the
	backticks names the hook. The section ends at the next markdown heading.
	"""
	pairs: list[tuple[str, str]] = []
	in_section = False
	for line in pr_body.splitlines():
		if INTENDED_LOOSENING_HEADING_RE.match(line):
			in_section = True
			continue
		if not in_section:
			continue
		if MARKDOWN_HEADING_RE.match(line):
			in_section = False
			continue
		item = LIST_ITEM_RE.match(line)
		if not item:
			continue
		content = item.group(1)
		ticked = BACKTICK_RE.search(content)
		if ticked:
			shape = ticked.group(1).strip()
			prefix = content[: ticked.start()].strip().rstrip(":").strip()
			hook = prefix.removesuffix(".py").strip("` ") if prefix else ""
		else:
			hook, shape = "", content.strip()
		if shape:
			pairs.append((hook, shape))
	return pairs


def is_intended(shape: Shape, listed: list[tuple[str, str]]) -> bool:
	return any(text == shape.text and hook in ("", shape.hook) for hook, text in listed)


# ──────────────────────────────────────────────────────────────────
# Running one hook
# ──────────────────────────────────────────────────────────────────


def classify_output(returncode: int, stdout: str) -> Outcome:
	"""Map a hook's exit code and stdout to a decision and a warning flag.

	Exit 2 blocks (CLAUDE.md hook protocol). Any other non-zero exit is a
	crashed hook, which Claude Code treats as a non-blocking error: `error`.
	Exit 0 reads `hookSpecificOutput.permissionDecision` from the JSON lines
	on stdout, and a `systemMessage` anywhere counts as a warning.
	"""
	decision = ""
	warned = False
	for line in stdout.splitlines():
		try:
			parsed = json.loads(line)
		except ValueError:
			continue
		if not isinstance(parsed, dict):
			continue
		if parsed.get("systemMessage"):
			warned = True
		specific = parsed.get("hookSpecificOutput")
		if isinstance(specific, dict):
			value = specific.get("permissionDecision")
			if value in ("deny", "ask", "allow"):
				decision = value
	if returncode == 2:
		return Outcome("block", warned)
	if returncode != 0:
		return Outcome("error", warned, f"exit {returncode}")
	return Outcome(decision or "none", warned)


def hook_env(scenario_env: dict[str, str], stub_bin: Path, home: Path, cache: Path) -> dict[str, str]:
	"""The hook's environment. The scenario's variables are applied before the
	isolation settings and are filtered like the caller's: never a token, a
	`CLAUDE_*`, or a `GIT_*` variable, so no scenario can re-enable network
	access, system git config, or a kill switch, or point git elsewhere.
	"""
	env = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith(("CLAUDE_", "GIT_")) and key not in _STRIPPED_ENV_KEYS
	}
	env.update(
		{
			key: value
			for key, value in scenario_env.items()
			if not key.startswith(("CLAUDE_", "GIT_")) and key not in _STRIPPED_ENV_KEYS
		}
	)
	env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', os.defpath)}"
	env["HOME"] = str(home)
	env["TMPDIR"] = str(cache)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["GIT_ALLOW_PROTOCOL"] = "file"
	env["GIT_TERMINAL_PROMPT"] = "0"
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	return env


def build_stdin(shape: Shape, cwd: Path, substitutions: dict[str, str]) -> str:
	text = shape.text
	for placeholder, value in substitutions.items():
		text = text.replace(placeholder, value)
	if text.startswith("stdin:"):
		return text[len("stdin:"):].lstrip()
	if text.startswith("json:"):
		payload = json.loads(text[len("json:"):])
		payload.setdefault("cwd", str(cwd))
		return json.dumps(payload)
	return json.dumps({"tool_name": "Bash", "tool_input": {"command": text}, "cwd": str(cwd)})


def run_hook(hook_file: Path | None, stdin: str, cwd: Path, env: dict[str, str]) -> Outcome:
	if hook_file is None:
		return Outcome("none", False, "no hook")
	try:
		proc = subprocess.run(
			[sys.executable, str(hook_file)],
			input=stdin,
			capture_output=True,
			text=True,
			cwd=cwd,
			env=env,
			timeout=HOOK_TIMEOUT_SECONDS,
		)
	except subprocess.TimeoutExpired:
		return Outcome("error", False, "timeout")
	return classify_output(proc.returncode, proc.stdout)


# ──────────────────────────────────────────────────────────────────
# Scenarios
# ──────────────────────────────────────────────────────────────────


@dataclass
class Scenario:
	cwd: Path
	stub_bin: Path
	substitutions: dict[str, str]
	env: dict[str, str] = field(default_factory=dict)


def _git(cwd: Path, *args: str) -> str:
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env["HOME"] = str(cwd)
	proc = subprocess.run(
		["git", *args],
		cwd=cwd,
		capture_output=True,
		text=True,
		env=env,
		timeout=60,
	)
	if proc.returncode != 0:
		raise SetupError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
	return proc.stdout.strip()


def _write_stub_gh(stub_bin: Path, cases: dict[str, str]) -> None:
	"""A `gh` stub that answers REST pulls lookups by `head=` and `[]` otherwise.

	It never reaches GitHub; every call is appended to `gh-calls.log`. Each
	payload is written to its own file and printed with `cat`, and every
	pattern and path is shell-quoted, so no payload or head text can end a
	heredoc early or be run as shell.
	"""
	stub_bin.mkdir(parents=True, exist_ok=True)
	lines = [
		"#!/bin/sh",
		f'echo "$*" >> {shlex.quote(str(stub_bin / "gh-calls.log"))}',
		'for arg in "$@"; do',
		'  case "$arg" in',
	]
	for index, (head, payload) in enumerate(cases.items()):
		payload_file = stub_bin / f"gh-payload-{index}.json"
		payload_file.write_text(payload, encoding="utf-8")
		lines.append(f"    {shlex.quote(f'head={head}')}) cat {shlex.quote(str(payload_file))}; exit 0;;")
	lines += ["  esac", "done", "echo '[]'", ""]
	stub = stub_bin / "gh"
	stub.write_text("\n".join(lines), encoding="utf-8")
	stub.chmod(0o755)


def _init_repo(repo: Path) -> str:
	repo.mkdir(parents=True)
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.email", "guard-differential@example.com")
	_git(repo, "config", "user.name", "Guard Differential")
	_git(repo, "config", "commit.gpgsign", "false")
	_git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
	(repo / "seed.txt").write_text("seed\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "seed")
	seed_sha = _git(repo, "rev-parse", "HEAD")
	_git(repo, "update-ref", "refs/remotes/origin/main", seed_sha)
	_git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
	return seed_sha


def default_scenario(root: Path) -> Scenario:
	"""A clean checkout of `o/r` on `feature/open`; `gh` finds no PR."""
	repo = root / "repo"
	_init_repo(repo)
	_git(repo, "checkout", "-q", "-b", "feature/open")
	worktree = root / "wt"
	worktree.mkdir()
	stub_bin = root / "bin"
	_write_stub_gh(stub_bin, {})
	return Scenario(repo, stub_bin, {"@@REPO@@": str(repo), "@@WORKTREE@@": str(worktree)})


def merged_checkout_scenario(root: Path) -> Scenario:
	"""The §21 stranded state the #5173 review used.

	The session checkout `@@REPO@@` sits on `feature/x`, whose PR #41 merged
	with the branch tip as its head and no open PR; `@@WORKTREE@@` is a
	detached worktree with fresh work on top of `main`. The `gh` stub answers
	`feature/x` with the merged PR, `feature/open` with an open PR, and any
	other branch with no PR.
	"""
	repo = root / "repo"
	seed_sha = _init_repo(repo)
	_git(repo, "checkout", "-q", "-b", "feature/x")
	(repo / "work.txt").write_text("work\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "merged work")
	merged_sha = _git(repo, "rev-parse", "HEAD")
	worktree = root / "wt"
	_git(repo, "worktree", "add", "-q", "--detach", str(worktree), seed_sha)
	(worktree / "fresh.txt").write_text("fresh\n", encoding="utf-8")
	_git(worktree, "add", "-A")
	_git(worktree, "commit", "-q", "-m", "fresh work in a scratch worktree")
	merged = [
		{
			"number": 41,
			"state": "closed",
			"html_url": "https://github.com/o/r/pull/41",
			"title": "the merged one",
			"merged_at": "2026-07-01T10:00:00Z",
			"head": {"sha": merged_sha},
		}
	]
	open_pr = [
		{
			"number": 42,
			"state": "open",
			"html_url": "https://github.com/o/r/pull/42",
			"title": "the open one",
			"merged_at": None,
			"head": {"sha": seed_sha},
		}
	]
	stub_bin = root / "bin"
	_write_stub_gh(stub_bin, {"o:feature/x": json.dumps(merged), "o:feature/open": json.dumps(open_pr)})
	return Scenario(repo, stub_bin, {"@@REPO@@": str(repo), "@@WORKTREE@@": str(worktree)})


SCENARIOS: dict[str, Callable[[Path], Scenario]] = {
	"pr_merge_status_guard": merged_checkout_scenario,
}


# ──────────────────────────────────────────────────────────────────
# Materializing the two sides
# ──────────────────────────────────────────────────────────────────


def _repo_git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
	"""Run git in `repo_root` without the caller's `GIT_*` variables, so an
	inherited `GIT_DIR`, `GIT_WORK_TREE`, or `GIT_INDEX_FILE` (a git hook, a CI
	step) cannot point it at another repository."""
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	proc = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, env=env, timeout=120)
	if check and proc.returncode != 0:
		raise SetupError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
	return proc


def changed_paths(repo_root: Path, base_ref: str, head_ref: str | None) -> list[str]:
	args = ["diff", "--name-only", base_ref]
	if head_ref:
		args.append(head_ref)
	args += ["--", *HOOK_TREES]
	output = _repo_git(repo_root, *args).stdout
	if not head_ref:
		# `git diff` does not list untracked files, but `materialize(None)`
		# copies them, so a new hook that is not yet added still counts.
		output += _repo_git(repo_root, "ls-files", "--others", "--exclude-standard", "--", *HOOK_TREES).stdout
	return sorted({line for line in output.splitlines() if line.strip()})


def materialize(repo_root: Path, ref: str | None, tree: str, target: Path) -> None:
	"""Copy `<tree>/*` at `ref` (or the working tree when `ref` is None) to `target`."""
	target.mkdir(parents=True, exist_ok=True)
	if ref is None:
		source = repo_root / tree
		if source.is_dir():
			for item in source.iterdir():
				if item.is_file():
					shutil.copy2(item, target / item.name)
		return
	for name in _ref_blob_names(repo_root, ref, tree):
		blob = _repo_git(repo_root, "cat-file", "-p", f"{ref}:{tree}/{name}", check=False)
		if blob.returncode == 0:
			(target / name).write_text(blob.stdout, encoding="utf-8")


def _ref_blob_names(repo_root: Path, ref: str, tree: str) -> list[str]:
	"""Names of the files (not subdirectories) directly under `<ref>:<tree>`."""
	listing = _repo_git(repo_root, "ls-tree", f"{ref}:{tree}", check=False)
	if listing.returncode != 0:
		return []
	names = []
	for line in listing.stdout.splitlines():
		meta, _, name = line.partition("\t")
		if meta.split()[1:2] == ["blob"] and name:
			names.append(name)
	return names


def ref_corpora(repo_root: Path, ref: str, corpus_dir: str) -> dict[str, list[Shape]]:
	"""The corpora committed at `ref` (empty when the directory is absent there)."""
	corpora: dict[str, list[Shape]] = {}
	for name in _ref_blob_names(repo_root, ref, corpus_dir):
		if not name.endswith(".txt"):
			continue
		blob = _repo_git(repo_root, "cat-file", "-p", f"{ref}:{corpus_dir}/{name}")
		corpora[name[: -len(".txt")]] = parse_corpus(blob.stdout, name[: -len(".txt")], f"{ref}:{corpus_dir}/{name}")
	return corpora


def _side_text(repo_root: Path, ref: str | None, path: str) -> str | None:
	"""The text of `path` at `ref` (the working tree when `ref` is None), or
	None when the file is absent there."""
	if ref is None:
		try:
			return (repo_root / path).read_text(encoding="utf-8")
		except OSError:
			return None
	blob = _repo_git(repo_root, "cat-file", "-p", f"{ref}:{path}", check=False)
	return blob.stdout if blob.returncode == 0 else None


def guard_differential_steps(workflow_text: str | None) -> list[str]:
	"""The text of every workflow step whose name starts with
	GUARD_STEP_NAME_PREFIX: from its `- name:` line up to the next step at
	the same indentation, or the first non-blank line indented less."""
	if not workflow_text:
		return []
	lines = workflow_text.splitlines()
	steps: list[str] = []
	index = 0
	while index < len(lines):
		start = STEP_START_RE.match(lines[index])
		if not start or not start.group("name").strip("\"'").startswith(GUARD_STEP_NAME_PREFIX):
			index += 1
			continue
		indent = len(start.group("indent"))
		end = index + 1
		while end < len(lines):
			line = lines[end]
			if line.strip():
				line_indent = len(line) - len(line.lstrip())
				if line_indent < indent or (line_indent == indent and line.lstrip().startswith("- ")):
					break
			end += 1
		steps.append("\n".join(lines[index:end]).rstrip())
		index = end
	return steps


def verifier_changes(repo_root: Path, base_ref: str, head_ref: str | None) -> list[str]:
	"""Paths of the check's own machinery that differ between base and head.

	Issue #5327: CI runs the base branch's copy of this script, so a PR's
	change to it takes effect only after it merges; the change is still
	reported, as is any change to a `Guard differential …` step of the CI
	workflow, so reviewers read them as security-boundary changes. Other
	edits to the workflow are not reported.
	"""
	changes: list[str] = []
	if _side_text(repo_root, base_ref, VERIFIER_SCRIPT_PATH) != _side_text(repo_root, head_ref, VERIFIER_SCRIPT_PATH):
		changes.append(VERIFIER_SCRIPT_PATH)
	base_steps = guard_differential_steps(_side_text(repo_root, base_ref, CI_WORKFLOW_PATH))
	head_steps = guard_differential_steps(_side_text(repo_root, head_ref, CI_WORKFLOW_PATH))
	if base_steps != head_steps:
		changes.append(CI_WORKFLOW_PATH)
	return changes


# ──────────────────────────────────────────────────────────────────
# The comparison
# ──────────────────────────────────────────────────────────────────


def compare_hook_dirs(
	base_dir: Path,
	head_dir: Path,
	corpora: dict[str, list[Shape]],
	scratch: Path,
	listed: list[tuple[str, str]],
	tree: str = "",
) -> list[ShapeResult]:
	"""Run every corpus whose hook exists on either side through both sides."""
	results: list[ShapeResult] = []
	for hook, shapes in sorted(corpora.items()):
		base_hook = base_dir / f"{hook}.py"
		head_hook = head_dir / f"{hook}.py"
		base_file = base_hook if base_hook.is_file() else None
		head_file = head_hook if head_hook.is_file() else None
		if base_file is None and head_file is None:
			continue
		for index, shape in enumerate(shapes):
			outcomes: list[Outcome] = []
			for side, hook_file in (("base", base_file), ("head", head_file)):
				root = scratch / f"{hook}-{index}-{side}"
				scenario = SCENARIOS.get(hook, default_scenario)(root)
				home = root / "home"
				cache = root / "cache"
				home.mkdir()
				cache.mkdir()
				env = hook_env(scenario.env, scenario.stub_bin, home, cache)
				stdin = build_stdin(shape, scenario.cwd, scenario.substitutions)
				outcomes.append(run_hook(hook_file, stdin, scenario.cwd, env))
			base, head = outcomes
			loosened = STRICTNESS[head.decision] < STRICTNESS[base.decision] and not head.warned
			results.append(
				ShapeResult(
					shape=shape,
					tree=tree,
					base=base,
					head=head,
					loosened=loosened,
					intended=loosened and is_intended(shape, listed),
				)
			)
	return results


def load_corpora(corpus_dir: Path) -> dict[str, list[Shape]]:
	if not corpus_dir.is_dir():
		raise SetupError(f"corpus directory {corpus_dir} not found")
	return {path.stem: load_corpus(path) for path in sorted(corpus_dir.glob("*.txt"))}


def run_check(
	repo_root: Path,
	base_ref: str,
	head_ref: str | None,
	corpus_dir: Path,
	pr_body: str,
	all_trees: bool = False,
) -> Report:
	_repo_git(repo_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
	if head_ref:
		_repo_git(repo_root, "rev-parse", "--verify", f"{head_ref}^{{commit}}")
	committed: list[dict[str, list[Shape]]] = []
	try:
		relative_corpus_dir = corpus_dir.resolve().relative_to(repo_root).as_posix()
	except ValueError:
		relative_corpus_dir = ""
	if relative_corpus_dir:
		committed = [ref_corpora(repo_root, ref, relative_corpus_dir) for ref in (head_ref, base_ref) if ref]
	corpora = merge_corpora(load_corpora(corpus_dir), *committed)
	listed = intended_loosening(pr_body)
	changed = changed_paths(repo_root, base_ref, head_ref)
	report = Report(verifier_changes=verifier_changes(repo_root, base_ref, head_ref))
	for tree in HOOK_TREES:
		tree_changes = [path for path in changed if path.startswith(f"{tree}/") and path.endswith(".py")]
		if not tree_changes and not all_trees:
			continue
		report.trees.append(tree)
		with tempfile.TemporaryDirectory(prefix="guard-differential-") as tmp:
			scratch = Path(tmp)
			base_dir = scratch / "base-hooks"
			head_dir = scratch / "head-hooks"
			materialize(repo_root, base_ref, tree, base_dir)
			materialize(repo_root, head_ref, tree, head_dir)
			for path in tree_changes:
				stem = Path(path).stem
				# An empty or comments-only corpus runs no comparison, so it
				# counts as missing. A new guard needs one too (AD-9): without
				# a shape, a new guard that answers `allow` is never run.
				if (
					stem.endswith("_guard")
					and not corpora.get(stem)
					and ((base_dir / f"{stem}.py").is_file() or (head_dir / f"{stem}.py").is_file())
				):
					report.missing_corpus.append(path)
			# Each tree compares its own hook files (the twin is a separate
			# file), so rows for the same shape in two trees are not duplicates.
			report.results += compare_hook_dirs(base_dir, head_dir, corpora, scratch / "runs", listed, tree)
	return report


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────


def _describe(outcome: Outcome) -> str:
	text = outcome.decision + ("+warning" if outcome.warned else "")
	return f"{text} ({outcome.detail})" if outcome.detail else text


def print_report(report: Report, as_json: bool) -> None:
	for path in report.verifier_changes:
		print(
			f"::warning::{LOG_KEY} verifier_change path={path}: this PR changes the guard differential "
			"check itself. CI runs the base branch's copy of the verifier, so a change to it applies "
			"only once it has merged. Review it as a change to a security boundary."
		)
	if not report.trees:
		print(f"{LOG_KEY} status=skipped reason=no-hook-change")
		return
	for path in report.missing_corpus:
		print(
			f"::error::{LOG_KEY} missing_corpus path={path} "
			f"expected={DEFAULT_CORPUS_DIR}/{Path(path).stem}.txt"
		)
	for result in report.regressions:
		print(
			f"::error::{LOG_KEY} regression tree={result.tree} hook={result.shape.hook} "
			f"line={result.shape.line} base={_describe(result.base)} head={_describe(result.head)} "
			f"shape={json.dumps(result.shape.text)}"
		)
	for result in report.results:
		if result.intended:
			print(
				f"{LOG_KEY} intended_loosening tree={result.tree} hook={result.shape.hook} "
				f"base={_describe(result.base)} head={_describe(result.head)} shape={json.dumps(result.shape.text)}"
			)
	if as_json:
		for result in report.results:
			print(
				json.dumps(
					{
						"tree": result.tree,
						"hook": result.shape.hook,
						"line": result.shape.line,
						"shape": result.shape.text,
						"base": {"decision": result.base.decision, "warned": result.base.warned},
						"head": {"decision": result.head.decision, "warned": result.head.warned},
						"loosened": result.loosened,
						"intended": result.intended,
					}
				)
			)
	status = "fail" if report.failed else "pass"
	intended = sum(1 for result in report.results if result.intended)
	print(
		f"{LOG_KEY} status={status} trees={','.join(report.trees)} shapes={len(report.results)} "
		f"regressions={len(report.regressions)} intended_loosening={intended} "
		f"missing_corpus={len(report.missing_corpus)} verifier_changes={len(report.verifier_changes)}"
	)
	if report.regressions:
		print(
			f"{LOG_KEY}: a shape the base hook blocked, denied, or asked (or left to the normal "
			"permission flow) is now allowed without a warning. Make the new hook fall back with "
			"a warning, or, if the loosening is intended, list each shape verbatim under an "
			"`Intended loosening:` section of the PR body (the sync then waits for the operator)."
		)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--base-ref", required=True, help="git ref of the base side (e.g. origin/main)")
	parser.add_argument("--head-ref", default=None, help="git ref of the head side (default: the working tree)")
	parser.add_argument("--pr-body-file", default=None, help="file holding the PR body (for `Intended loosening:`)")
	parser.add_argument("--corpus-dir", default=None, help=f"corpus directory (default: {DEFAULT_CORPUS_DIR})")
	parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
	parser.add_argument("--all", action="store_true", help="compare every hook tree even when no hook changed")
	parser.add_argument("--json", action="store_true", help="also print every shape's result as JSON lines")
	args = parser.parse_args(argv)

	repo_root = Path(args.repo_root).resolve()
	corpus_dir = Path(args.corpus_dir) if args.corpus_dir else repo_root / DEFAULT_CORPUS_DIR
	pr_body = ""
	if args.pr_body_file:
		try:
			pr_body = Path(args.pr_body_file).read_text(encoding="utf-8")
		except OSError as exc:
			print(f"::error::{LOG_KEY} status=error reason=unreadable-pr-body detail={json.dumps(str(exc))}")
			return 2
	try:
		report = run_check(repo_root, args.base_ref, args.head_ref, corpus_dir, pr_body, args.all)
	except SetupError as exc:
		print(f"::error::{LOG_KEY} status=error detail={json.dumps(str(exc))}")
		return 2
	print_report(report, args.json)
	return 1 if report.failed else 0


if __name__ == "__main__":
	sys.exit(main())
