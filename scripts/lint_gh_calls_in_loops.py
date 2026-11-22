#!/usr/bin/env python3
"""Warn on GitHub API calls a pull request adds inside shell loops (§15).

CLAUDE.md §15 (GitHub API call hygiene): the `GH_PAT` runs out of its hourly
budget, and the worst call sites are `gh` calls inside loops and polls. This
lint is §15's enforcement, in warning-only form (issue #9929, owner decision
Q3: A). It runs as a step of the `pr-body-lint` job in
`.github/workflows/pr-checks.yml`:

    lint_gh_calls_in_loops.py --base <rev> [--head <rev>] [--repo-root .]

It diffs `<base>` against `<head>` with the local git (no GitHub API call) and
looks only at lines the change ADDS, in `*.sh` / `*.bash` files and in the
`run:` blocks of `.github/workflows/*.yml` and `workflow-templates/*.yml`. An
added line warns when a GitHub API call (`gh api`, `gh pr view|list|checks`,
`gh issue view|list`, `gh run list|view`, `_safe_gh_jq`, `gh_retry`,
`gh_api_retry`, `curl ... api.github.com`) sits inside a `for` / `while` /
`until` body of the post-change file. Loop depth comes from a lightweight
`do` / `done` scan at command position that skips comments, quoted text and
heredoc bodies; it is a heuristic, which is why the lint never fails.

Suppression: a call through an allow-listed batched or cached helper
(ALLOWED_HELPERS), or the annotation `# gh-loop-ok: <reason>` (a reason is
required) on the same line or the line above.

Output: one `::warning file=<f>,line=<n>::...` line per finding (at most
MAX_PRINTED_WARNINGS printed), then `GH_LOOP_CALL_LINT warnings=<n> files=<n>`
(`files` counts the changed files scanned). When the base or head cannot be
read it prints `GH_LOOP_CALL_LINT outcome=skipped reason=<token>` instead
(`no_base`, `no_head`, `git_error`, `internal_error`). It exits 0 on every
path except an argparse usage error (exit 2).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

LOG_PREFIX = "GH_LOOP_CALL_LINT"
SHELL_SUFFIXES = (".sh", ".bash")
WORKFLOW_DIRS = (".github/workflows/", "workflow-templates/")
WORKFLOW_SUFFIX = ".yml"
DIFF_PATHSPECS = ("*.sh", "*.bash", ".github/workflows/*.yml", "workflow-templates/*.yml")
GIT_TIMEOUT_SECONDS = 120
MAX_PRINTED_WARNINGS = 200
SNIPPET_MAX_CHARS = 80

# A GitHub API call. `gh` must not follow a word character or `-`; the helper
# names must not be a function definition (`gh_retry() {`). Global options
# `-R` / `--repo` / `--hostname` may come before the subcommand.
API_CALL_RE = re.compile(
	r"(?<![\w-])gh\s+(?:(?:-R|--repo|--hostname)(?:\s+|=)\S+\s+)*(?:api\b|pr\s+(?:view|list|checks)\b|issue\s+(?:view|list)\b|run\s+(?:list|view)\b)"
	r"|(?<![\w-])(?:_safe_gh_jq|gh_retry|gh_api_retry)\b(?!\s*\(\))"
)
CURL_RE = re.compile(r"(?<![\w-])curl\b")
GITHUB_API_HOST = "api.github.com"
# Batched or cached helpers (CLAUDE.md §15). `gh_read` comes from #9897; a
# name-only entry is harmless while the helper does not exist yet.
ALLOWED_HELPERS = (
	"_fetch_candidate_issue_details_graphql",
	"_fetch_linked_pr_status_graphql",
	"_fetch_issue_labels_batch_graphql",
	"gh_review_pr_state",
	"gh_read",
)
ALLOWED_HELPER_RE = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(name) for name in ALLOWED_HELPERS) + r")\b")
ANNOTATION_RE = re.compile(r"#\s*gh-loop-ok:\s*\S")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
BLOCK_RUN_RE = re.compile(r"^(\s*)(-\s+)?run:\s*[|>][-+0-9]*\s*(#.*)?$")
INLINE_RUN_RE = re.compile(r"^(\s*)(-\s+)?run:\s+(\S.*)$")
# Characters that end a word and put the next word at command position.
COMMAND_SEPARATORS = ";&|(){}"
WORD_BREAKS = set(" \t;&|()<>")
# After these words the next word is still at command position.
COMMAND_PREFIX_WORDS = {"then", "else", "elif", "do", "!", "{", "}", "if", "while", "until", "time"}


@dataclass
class LineScan:
	"""One scanned line: comment-stripped code and loop depth by offset."""

	code: str
	code_unmasked: str
	in_heredoc: bool
	depth_start: int
	changes: list[tuple[int, int]] = field(default_factory=list)

	def depth_at(self, offset: int) -> int:
		depth = self.depth_start
		for change_offset, new_depth in self.changes:
			if change_offset <= offset:
				depth = new_depth
		return depth


@dataclass
class LoopCallFinding:
	path: str
	line: int
	snippet: str


def _git(repo_root: Path, *args: str) -> str:
	result = subprocess.run(
		["git", *args], cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
		errors="replace", check=False, timeout=GIT_TIMEOUT_SECONDS,
	)
	if result.returncode != 0:
		raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
	return result.stdout


def _is_scanned_path(path: str) -> bool:
	if path.endswith(SHELL_SUFFIXES):
		return True
	if not path.endswith(WORKFLOW_SUFFIX):
		return False
	for directory in WORKFLOW_DIRS:
		if path.startswith(directory) and "/" not in path[len(directory):]:
			return True
	return False


def parse_added_lines(diff_text: str) -> dict[str, set[int]]:
	"""Added (post-change) line numbers per path from a `--unified=0` diff."""
	added: dict[str, set[int]] = {}
	current: str | None = None
	for line in diff_text.split("\n"):
		if line.startswith("+++ "):
			target = line[4:]
			# `/dev/null` is a deleted file; a quoted path holds unusual
			# characters and is skipped rather than decoded.
			current = target[2:] if target.startswith("b/") else None
			if current is not None and not _is_scanned_path(current):
				current = None
			continue
		if current is None:
			continue
		match = HUNK_RE.match(line)
		if not match:
			continue
		start = int(match.group(1))
		count = int(match.group(2)) if match.group(2) is not None else 1
		if count > 0:
			added.setdefault(current, set()).update(range(start, start + count))
	return added


def added_lines_by_file(repo_root: Path, base: str, head: str) -> dict[str, set[int]]:
	diff_text = _git(
		repo_root, "-c", "core.quotePath=off", "diff", "--unified=0", "--no-color", "--no-ext-diff",
		"--src-prefix=a/", "--dst-prefix=b/", base, head, "--", *DIFF_PATHSPECS,
	)
	return parse_added_lines(diff_text)


def loop_depths(lines: list[str]) -> list[LineScan]:
	"""Scan shell lines; track loop depth (`do` / `done`) by character offset."""
	scans: list[LineScan] = []
	depth = 0
	in_squote = False
	in_dquote = False
	pending_heredocs: list[str] = []
	active_heredocs: list[str] = []
	# One open-paren counter per `$(` opened inside double quotes; its `)`
	# returns to the double-quoted string.
	subst_stack: list[int] = []
	# `case` state per open `case`: "header" (before `in`), "pattern" (a case
	# arm's pattern list, where `done)` is not a keyword) or "body".
	case_stack: list[str] = []
	for raw in lines:
		if active_heredocs:
			if raw.strip() == active_heredocs[0]:
				active_heredocs.pop(0)
			scans.append(LineScan(code="", code_unmasked="", in_heredoc=True, depth_start=depth))
			continue
		scan = LineScan(code="", code_unmasked="", in_heredoc=False, depth_start=depth)
		code_chars = list(raw)
		comment_at = len(raw)
		command_position = not in_squote and not in_dquote
		index = 0
		length = len(raw)
		while index < length:
			char = raw[index]
			if in_squote:
				if char == "'":
					in_squote = False
				else:
					code_chars[index] = " "
				index += 1
				continue
			if char == "\\":
				index += 2
				continue
			if in_dquote:
				if char == '"':
					in_dquote = False
				elif raw.startswith("$(", index):
					# `"$( ... )"`: the substitution body is shell code again.
					subst_stack.append(0)
					in_dquote = False
					command_position = True
					index += 2
					continue
				index += 1
				continue
			word_start = index == 0 or raw[index - 1] in WORD_BREAKS
			if char == "#" and word_start:
				comment_at = index
				break
			if char == "'":
				in_squote = True
				command_position = False
				index += 1
				continue
			if char == '"':
				in_dquote = True
				command_position = False
				index += 1
				continue
			if char == "<" and raw.startswith("<<", index) and not raw.startswith("<<<", index) and (index == 0 or raw[index - 1] != "<"):
				heredoc = HEREDOC_RE.match(raw, index)
				# `$(( x << y ))` is an arithmetic shift, not a heredoc.
				in_arithmetic = raw.rfind("((", 0, index) > raw.rfind("))", 0, index)
				if heredoc and not in_arithmetic:
					pending_heredocs.append(heredoc.group(3))
					index = heredoc.end()
					command_position = False
					continue
			if char in " \t":
				index += 1
				continue
			if char in COMMAND_SEPARATORS:
				if case_stack and case_stack[-1] == "pattern" and char in "()|":
					# `a|done)`: `)` ends the case pattern and starts the arm body.
					if char == ")":
						case_stack[-1] = "body"
						command_position = True
					index += 1
					continue
				if char == ";" and case_stack and case_stack[-1] == "body" and raw.startswith((";;", ";&"), index):
					case_stack[-1] = "pattern"
					index += 2
					continue
				if subst_stack and char == "(":
					subst_stack[-1] += 1
				elif subst_stack and char == ")":
					if subst_stack[-1] == 0:
						subst_stack.pop()
						in_dquote = True
						command_position = False
						index += 1
						continue
					subst_stack[-1] -= 1
				command_position = True
				index += 1
				continue
			if char in "<>":
				command_position = False
				index += 1
				continue
			# A word: read up to the next break or quote.
			end = index
			while end < length and raw[end] not in WORD_BREAKS and raw[end] not in "'\"\\":
				end += 1
			word = raw[index:end]
			if case_stack and case_stack[-1] == "pattern":
				# A case pattern word is never a keyword; `esac` ends the case.
				if word == "esac":
					case_stack.pop()
					command_position = False
				index = end
				continue
			if case_stack and case_stack[-1] == "header":
				if word == "in":
					case_stack[-1] = "pattern"
				index = end
				continue
			if command_position and word == "case":
				case_stack.append("header")
				command_position = False
				index = end
				continue
			if command_position and word == "esac" and case_stack:
				case_stack.pop()
			if command_position and end < length and raw[end] in "'\"\\":
				# A quoted or escaped word is never a keyword.
				command_position = False
				index = end
				continue
			if command_position and word == "do":
				depth += 1
				scan.changes.append((index, depth))
			elif command_position and word == "done":
				depth = max(depth - 1, 0)
				scan.changes.append((index, depth))
			command_position = command_position and word in COMMAND_PREFIX_WORDS
			index = end
		code = "".join(code_chars[:comment_at])
		scan.code = code
		scan.code_unmasked = raw[:comment_at]
		scans.append(scan)
		if pending_heredocs and not in_squote and not in_dquote:
			active_heredocs.extend(pending_heredocs)
			pending_heredocs = []
	return scans


def _indent(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def workflow_run_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
	"""`run:` bodies of a workflow as (1-based file line of body line 1, lines)."""
	blocks: list[tuple[int, list[str]]] = []
	index = 0
	total = len(lines)
	while index < total:
		line = lines[index]
		block = BLOCK_RUN_RE.match(line)
		if block:
			key_indent = len(block.group(1)) + len(block.group(2) or "")
			end = index + 1
			while end < total and (not lines[end].strip() or _indent(lines[end]) > key_indent):
				end += 1
			body = lines[index + 1:end]
			while body and not body[-1].strip():
				body.pop()
			first = next((text for text in body if text.strip()), None)
			if first is not None:
				body_indent = _indent(first)
				blocks.append((index + 2, [text[body_indent:] if text.strip() else "" for text in body]))
			index = end
			continue
		inline = INLINE_RUN_RE.match(line)
		if inline and not inline.group(3).startswith(("|", ">")):
			blocks.append((index + 1, [inline.group(3)]))
		index += 1
	return blocks


def _helper_wraps(code: str, call_start: int) -> bool:
	"""An allow-listed helper earlier in the same command runs the call."""
	for helper in ALLOWED_HELPER_RE.finditer(code, 0, call_start):
		if not any(separator in code[helper.end():call_start] for separator in ";&|"):
			return True
	return False


def _api_call_in_loop(scan: LineScan, continuation: str = "") -> bool:
	"""`continuation` holds the backslash-continued lines after this one."""
	for match in API_CALL_RE.finditer(scan.code):
		if scan.depth_at(match.start()) >= 1 and not _helper_wraps(scan.code, match.start()):
			return True
	for match in CURL_RE.finditer(scan.code):
		target = scan.code_unmasked[match.end():] + continuation
		if GITHUB_API_HOST in target and scan.depth_at(match.start()) >= 1 and not _helper_wraps(scan.code, match.start()):
			return True
	return False


def _continuation(scans: list[LineScan], offset: int) -> str:
	"""The lines a trailing backslash joins to line `offset` (curl URLs)."""
	parts: list[str] = []
	index = offset
	while index + 1 < len(scans) and scans[index].code_unmasked.rstrip().endswith("\\"):
		index += 1
		parts.append(scans[index].code_unmasked)
	return " " + " ".join(parts) if parts else ""


def _comment_part(raw: str, scan: LineScan) -> str:
	"""The real shell comment of a line (never quoted text or heredoc text)."""
	return "" if scan.in_heredoc else raw[len(scan.code_unmasked):]


def scan_file(path: str, text: str, added: set[int]) -> list[LoopCallFinding]:
	lines = text.split("\n")
	if path.endswith(SHELL_SUFFIXES):
		blocks = [(1, lines)]
	else:
		blocks = workflow_run_blocks(lines)
	warnings: list[LoopCallFinding] = []
	for start, block_lines in blocks:
		scans = loop_depths(block_lines)
		for offset, scan in enumerate(scans):
			file_line = start + offset
			if file_line not in added or scan.in_heredoc:
				continue
			continuation = _continuation(scans, offset) if CURL_RE.search(scan.code) else ""
			if not _api_call_in_loop(scan, continuation):
				continue
			raw = block_lines[offset]
			if ANNOTATION_RE.search(_comment_part(raw, scan)) or (
				offset > 0 and ANNOTATION_RE.search(_comment_part(block_lines[offset - 1], scans[offset - 1]))
			):
				continue
			warnings.append(LoopCallFinding(path=path, line=file_line, snippet=raw.strip()[:SNIPPET_MAX_CHARS]))
	return warnings


def _escape_data(value: str) -> str:
	return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
	return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def format_warning(warning: LoopCallFinding) -> str:
	message = (
		f"§15: GitHub API call inside a loop ({warning.snippet}). Use a batched or cached helper "
		"(see CLAUDE.md §15) or annotate the line with '# gh-loop-ok: <reason>'."
	)
	return f"::warning file={_escape_property(warning.path)},line={warning.line}::{_escape_data(message)}"


def _commit_exists(repo_root: Path, rev: str) -> bool:
	result = subprocess.run(
		["git", "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], cwd=repo_root,
		capture_output=True, text=True, check=False, timeout=GIT_TIMEOUT_SECONDS,
	)
	return result.returncode == 0


def lint(repo_root: Path, base: str, head: str) -> tuple[list[LoopCallFinding], int, str | None]:
	"""(warnings, changed files scanned, skip reason or None)."""
	try:
		if not _commit_exists(repo_root, base):
			return [], 0, "no_base"
		if not _commit_exists(repo_root, head):
			return [], 0, "no_head"
		added = added_lines_by_file(repo_root, base, head)
		warnings: list[LoopCallFinding] = []
		for path in sorted(added):
			try:
				text = _git(repo_root, "show", f"{head}:{path}")
			except RuntimeError:
				# One unreadable path (e.g. a non-UTF-8 name) must not drop
				# the findings of every other changed file.
				print(f"::warning::{LOG_PREFIX} could not read {_escape_data(path)}; skipped", flush=True)
				continue
			warnings.extend(scan_file(path, text, added[path]))
	except (RuntimeError, OSError, subprocess.TimeoutExpired):
		return [], 0, "git_error"
	return warnings, len(added), None


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("--base", required=True, help="base revision (the merge commit's first parent in CI)")
	parser.add_argument("--head", default="HEAD", help="head revision (default HEAD)")
	parser.add_argument("--repo-root", default=".", help="repository root (default .)")
	args = parser.parse_args(argv)
	try:
		warnings, files_scanned, skip_reason = lint(Path(args.repo_root), args.base, args.head)
	except Exception as exc:  # noqa: BLE001 - warning-only lint: never fail the job
		print(f"::warning::{LOG_PREFIX} crashed ({_escape_data(type(exc).__name__)}); no findings reported")
		print(f"{LOG_PREFIX} outcome=skipped reason=internal_error", flush=True)
		return 0
	if skip_reason is not None:
		print(f"{LOG_PREFIX} outcome=skipped reason={skip_reason}", flush=True)
		return 0
	for warning in warnings[:MAX_PRINTED_WARNINGS]:
		print(format_warning(warning))
	if len(warnings) > MAX_PRINTED_WARNINGS:
		print(f"{len(warnings) - MAX_PRINTED_WARNINGS} more warning(s) not printed.")
	print(f"{LOG_PREFIX} warnings={len(warnings)} files={files_scanned}", flush=True)
	return 0


if __name__ == "__main__":
	try:
		sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
	except (AttributeError, ValueError):
		pass
	sys.exit(main())
