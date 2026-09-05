#!/usr/bin/env python3
"""Inline likely-to-be-edited files into the Codex prompt as a reference
block so the editor doesn't waste budget reading them.

This helper supports three input modes (mix-and-match — paths from each
source are unioned, preserving first-seen order):

  --plan-file <path>     Parse a markdown plan and extract the
                         "Files likely to change" / "Files to modify" /
                         "Files expected to change" section.
  --paths-file <path>    Read newline-separated explicit paths
                         (one per line; blank lines and `#` comments
                         skipped). Used by the autofix editor (driven
                         by LAST_RUN_CHANGED_FILES_FILE) and the
                         conflict resolvers (driven by the conflicted
                         file list).
  --paths "a,b,c"        Comma-separated explicit paths (shell-friendly).

Output is a bounded, unnumbered context block:

  === TARGETED FILE CONTEXT ===
  <header text — overridable via --header-text>

  --- FILE: <rel> (<bytes> bytes) ---
  <line 1>
  <line 2>
  ...
  --- END FILE: <rel> ---

The 2026-05-07 12:41 / 12:42 E2E smoke runs and the follow-up ablation
suite identified two prompt-side amplifiers of the OpenRouter Responses
+ Codex tool-call-emission failure (resolved separately by flipping
`apply_patch_tool_type: freeform → function` for OpenAI slugs in
scripts/codex_model_catalog.json):

  1. The previous header text said "your first tool call should be a
     write to one of those files unless the request is already
     satisfied". The conditional ("unless already satisfied") combined
     with the imperative ("first tool call should be a write") biased
     the model toward apply_patch on small inlined files; through
     OpenRouter the call collapsed into reasoning without an emitted
     tool. The replacement wording presents the inlined files as
     reference material and does not prescribe a tool choice.
  2. Line-numbering small fully-inlined files added prompt tokens
     without adding signal — the model can read unnumbered content
     fine, and the numbered-prefix shape is the apply_patch context
     format that biased the failure path. Fully inlined files now
     emit verbatim; line numbers are dropped.

For fully specified plain-text overwrites (.txt / .csv / .md) the
header explicitly allows a shell `printf` / heredoc write as the
preferred path — those are the file shapes where apply_patch's
diff-context tax adds zero value. Code files keep the model's normal
edit-tool choice.

Bounds (defensible default; override per caller):

  --max-bytes (default 102400)    Hard UTF-8 limit for the complete rendered
	                                  block, including headers, wrappers,
	                                  markers, fallback content, and summaries.
	                                  At most 256 paths of at most 1024 UTF-8
	                                  bytes are accepted before filesystem
	                                  access. Only complete fragments are
	                                  appended. Zero emits an empty block.

Designed to be safe on missing inputs: if the plan has no recognised
section, --paths-file is empty, and --paths is unset, the output is just
the header + "(no existing target files could be inlined)" — the caller
prompt still works, the model just falls back to read-then-write.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(_SCRIPT_DIR))
try:
	from emit_event import emit_event as _emit_event_helper
except Exception:
	_emit_event_helper = None

PATH_IN_BACKTICKS_RE = re.compile(r"`([^`]+)`")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
# Section-label heuristic: short label-shaped phrase, optional leading
# numeric prefix, optional trailing colon or period. The label body uses
# a restricted alphabet (alphanum + space + `/` + `_` + `-`) which
# excludes commas, backticks, and internal punctuation — so a sentence-
# shaped numbered bullet like `3. Add validation, for edge cases.`
# (comma) or `3. Edit \`foo.py\`` (backtick) cannot match. The plan
# template's actual section labels (`Files likely to change.`,
# `Functions or modules to implement.`) terminate with a period, hence
# the `[.:]?` trailing-punct allowance — without it, the scan never
# stops at the next section and inline-backticked paths from
# implementation prose leak into the inlining set.
SECTION_HEADER_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?([A-Za-z][A-Za-z0-9 /_-]{2,})\s*[.:]?\s*$")
# Markdown ATX heading. Plans frequently use `## Functions or modules` after
# the "Files likely to change" section; without this, the bullet scan would
# leak inline-backticked paths from later sections (e.g. `docs/note.md`
# mentioned in implementation prose) into the inlining set.
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
LIKELY_HEADER_RE = re.compile(
	r"files?\s+(?:likely|expected|to)\s+(?:to\s+)?(?:change|modify|edit)"
	r"|files?\s+to\s+(?:change|modify|edit)",
	re.I,
)

SAFE_EXTENSIONS = {
	".c", ".cc", ".cfg", ".conf", ".contract", ".cpp", ".cs", ".css",
	".csv", ".go", ".h", ".hpp", ".html", ".java", ".js", ".json",
	".jsx", ".kt", ".md", ".py", ".rb", ".rs", ".sh", ".sol", ".sql",
	".svelte", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml",
}
ROOT_LEVEL_BARE_FILENAMES = {"COPYING", "LICENCE", "LICENSE", "NOTICE", "README"}
ROOT_LEVEL_DOTTED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT_LEVEL_DOTFILE_RE = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT_LEVEL_FILESTYLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*file(?:\.[A-Za-z0-9][A-Za-z0-9._-]*)*$", re.I)
# Preserve literal trailing . , ; : on root-level filenames; a trailing ) is
# handled separately because reviewer ledger anchors commonly pick it up from
# surrounding prose / markdown formatting.
ROOT_LEVEL_LITERAL_TRAILING_PUNCTUATION = ".,;:"

DEFAULT_HEADER_TEXT = (
	"The approved plan / autofix-finding / conflict-marker scan named these "
	"files as likely to be edited. Their current contents are provided below "
	"for reference so you do not need to re-read them. Files marked \"would "
	"overflow total budget\" must be read with the read tool — never assume "
	"their content is in this block. For fully specified plain-text "
	"overwrites (.txt / .csv / .md), a shell `printf` or heredoc write is "
	"acceptable and often simpler than a patch."
)

# Plain-text file extensions where a shell `printf` / heredoc write is
# typically simpler and lower-risk than a patch-based write tool. The
# per-file FILE marker hints at this for these extensions so the editor
# has the suggestion adjacent to the file content rather than only in
# the header (which can scroll off the model's effective attention on
# long prompts).
PLAIN_TEXT_HINT_EXTENSIONS = {".txt", ".csv", ".md"}
SEMBLE_QUERY_TIMEOUT_SECS = 30
SEMBLE_READ_FALLBACK_MAX_BYTES = 4096
SEMBLE_MAX_CHUNKS_CAP = 20
MAX_TARGET_PATHS = 256
MAX_TARGET_PATH_BYTES = 1024


def _mirror_event(prefix: str, **fields: object) -> None:
	if _emit_event_helper is None:
		return
	normalized_fields: dict[str, object] = {}
	for key, value in fields.items():
		if isinstance(value, str):
			normalized_fields[key] = " ".join(value.split())
		else:
			normalized_fields[key] = value
	try:
		_emit_event_helper(prefix, **normalized_fields)
	except Exception:
		return


def _normalized_semble_log_context() -> str | None:
	# Shared with scripts/semble_helpers.sh: tests can opt into additive
	# classification without changing the stable SEMBLE_* prefix contract.
	raw = os.getenv("SEMBLE_LOG_CONTEXT", "")
	context = re.sub(r"-+", "-", re.sub(r"[\r\n\t ]", "-", raw)).strip("-")
	return context or None


def _log_semble_event(prefix: str, **fields: object) -> None:
	rendered_fields = dict(fields)
	if "context" not in rendered_fields:
		context = _normalized_semble_log_context()
		if context:
			rendered_fields["context"] = context
	parts = [prefix]
	for key, value in rendered_fields.items():
		# Keep the Python-side overflow telemetry aligned with
		# scripts/semble_helpers.sh: single-line, unquoted key=value fields on
		# stderr. Normalizing whitespace prevents multiline exception payloads
		# from spilling extra log lines while preserving the shared log shape.
		rendered = " ".join(str(value).split())
		parts.append(f"{key}={rendered}")
	print(" ".join(parts), file=sys.stderr)
	_mirror_event(prefix, **rendered_fields)


def _is_probable_root_level_path_core(value: str) -> bool:
	return (
		value in ROOT_LEVEL_BARE_FILENAMES
		or ROOT_LEVEL_DOTFILE_RE.fullmatch(value) is not None
		or ROOT_LEVEL_DOTTED_NAME_RE.fullmatch(value) is not None
		or ROOT_LEVEL_FILESTYLE_RE.fullmatch(value) is not None
	)


def _trim_path_candidate(value: str) -> str:
	value = value.strip()
	if is_probable_root_level_path(value):
		return value
	if value.endswith(")") and is_probable_root_level_path(value[:-1]):
		return value[:-1]
	return value.rstrip(".,;:)")


def is_probable_root_level_path(value: str) -> bool:
	if "/" in value:
		return False
	if _is_probable_root_level_path_core(value):
		return True
	candidate = value
	while candidate and candidate[-1] in ROOT_LEVEL_LITERAL_TRAILING_PUNCTUATION:
		candidate = candidate[:-1]
		if _is_probable_root_level_path_core(candidate):
			return True
	return False


def is_probable_path(value: str) -> bool:
	value = _trim_path_candidate(value)
	if not value or value.startswith(("http://", "https://", "#")):
		return False
	if any(part in {"", ".", ".."} for part in value.split("/")):
		return False
	if value.startswith(("/", "~")) or "\x00" in value:
		return False
	suffix = Path(value).suffix.lower()
	return "/" in value or suffix in SAFE_EXTENSIONS or is_probable_root_level_path(value)


def normalize_path(value: str) -> str | None:
	value = _trim_path_candidate(value)
	if len(value.encode("utf-8")) > MAX_TARGET_PATH_BYTES:
		return None
	if not is_probable_path(value):
		return None
	return value


def extract_paths_from_plan(plan_text: str) -> list[str]:
	"""Return ordered unique likely-to-change paths from an approved plan."""
	in_section = False
	found: list[str] = []
	seen: set[str] = set()

	for raw_line in plan_text.splitlines():
		line = raw_line.rstrip()
		if not in_section:
			if LIKELY_HEADER_RE.search(line):
				in_section = True
			continue

		if not line.strip():
			# Blank lines are common inside markdown lists; keep scanning.
			continue

		# Markdown ATX headings (## Foo) terminate the section. Without this
		# check the scan would leak inline-backticked paths from later
		# sections like "## Functions or modules to implement" into the
		# inlining set.
		if MARKDOWN_HEADING_RE.match(line) and not LIKELY_HEADER_RE.search(line):
			break

		# Numbered markdown section headers such as "2. Functions or modules"
		# also look like ordered-list bullets. Treat them as headers before
		# applying bullet extraction so the likely-files scan stops at the next
		# section instead of leaking paths from implementation notes.
		header = SECTION_HEADER_RE.match(line)
		if header and not LIKELY_HEADER_RE.search(line):
			break

		bullet = BULLET_RE.match(line)
		if not bullet:
			continue

		body = bullet.group(1)
		candidates = PATH_IN_BACKTICKS_RE.findall(body)
		if not candidates:
			# Support simple bullets like: - src/foo.py
			candidates = [body.split()[0]]

		for candidate in candidates:
			path = normalize_path(candidate)
			if path and path not in seen:
				seen.add(path)
				found.append(path)

	return found


def parse_paths_file(path_str: str) -> list[str]:
	"""Read newline-separated paths. Blank lines and `#` comments skipped.

	Lines starting with `+++ b/`, `--- a/`, ` `, `M `, `A `, `D `, etc.
	(common diff/git porcelain prefixes) are tolerated by stripping the
	leading marker. Rename / copy porcelain entries of the form
	`R  old/path.py -> new/path.py` are split on ` -> ` and only the
	destination path is emitted (the new file is the one the editor
	would now operate on; the old path no longer exists in the
	working tree). This lets callers pipe `git diff --name-only` or
	`git status --porcelain` directly.
	"""
	found: list[str] = []
	seen: set[str] = set()
	try:
		raw = Path(path_str).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return []
	for line in raw.splitlines():
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			continue
		# Strip git-porcelain status prefix (`?? path`, `M  path`, `A path`).
		# The status portion is exactly two columns followed by a space.
		if len(line) >= 4 and line[2:3] == " " and line[0:2].strip() in {
			"M", "A", "D", "R", "C", "U", "??", "MM", "AM", "AD", "RM", "RD",
			"!!", "T", "MT", "AT", "MD",
		}:
			candidate = line[3:].strip()
		else:
			candidate = stripped
		# Rename / copy porcelain entries: take the destination path
		# (the post-rename name is what's on disk and what the editor
		# would touch). Applied before diff-prefix stripping so we
		# don't get confused by patterns like `R  a/old -> b/new`.
		if " -> " in candidate:
			candidate = candidate.split(" -> ", 1)[1].strip()
		# Strip diff prefixes if present. Order matters: longest first so
		# `+++ b/foo` strips `+++ b/` (not just `+++ ` leaving `b/foo`).
		# Includes plain `a/` / `b/` prefixes for `git diff` output that
		# was already split (no leading `+++` / `---` marker).
		for prefix in ("+++ b/", "--- a/", "+++ ", "--- ", "a/", "b/"):
			if candidate.startswith(prefix):
				candidate = candidate[len(prefix):]
				break
		path = normalize_path(candidate)
		if path and path not in seen:
			seen.add(path)
			found.append(path)
	return found


def parse_paths_arg(arg: str) -> list[str]:
	"""Comma-separated paths from CLI."""
	found: list[str] = []
	seen: set[str] = set()
	for chunk in arg.split(","):
		path = normalize_path(chunk)
		if path and path not in seen:
			seen.add(path)
			found.append(path)
	return found


def _render_inlined_file_header(rel: str, size: int, ext: str) -> str:
	if ext in PLAIN_TEXT_HINT_EXTENSIONS:
		return (
			f"--- FILE: {rel} ({size} bytes; plain-text — for a "
			f"fully specified overwrite, `printf ... > {rel}` or a "
			f"heredoc write is acceptable) ---"
		)
	return f"--- FILE: {rel} ({size} bytes) ---"


def _append_inlined_file_block(output: list[str], rel: str, raw: bytes) -> None:
	text = raw.decode("utf-8", errors="replace")
	output.append(_render_inlined_file_header(rel, len(raw), Path(rel).suffix.lower()))
	# Unnumbered: emit the file content verbatim. Line numbers were
	# the apply_patch context shape and biased the OpenRouter +
	# OpenAI-slug failure path; the model reads unnumbered content
	# fine.
	output.extend(text.splitlines())
	output.append(f"--- END FILE: {rel} ---")
	output.append("")


def _read_optional_query_text(path_str: str | None) -> str | None:
	if not path_str:
		return None
	try:
		text = Path(path_str).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return None
	stripped = text.strip()
	return stripped or None


def _normalize_semble_max_chunks(value: int) -> int:
	if value <= 0:
		return 1
	if value > SEMBLE_MAX_CHUNKS_CAP:
		return SEMBLE_MAX_CHUNKS_CAP
	return value


def _run_semble_query(
	query_text: str,
	semble_bin: str | None,
	semble_index: str | None,
	semble_max_chunks: int,
	repo_root: Path,
) -> tuple[bool, str | None]:
	resolved_bin = semble_bin or shutil.which("semble")
	if not resolved_bin:
		return False, "binary-unavailable"
	if not semble_index:
		return False, "index-unavailable"
	try:
		result = subprocess.run(
			[
				resolved_bin,
				"query",
				query_text,
				"--index",
				semble_index,
				"--top-k",
				str(semble_max_chunks),
				"--format",
				"text",
			],
			check=False,
			capture_output=True,
			cwd=str(repo_root),
			timeout=SEMBLE_QUERY_TIMEOUT_SECS,
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		return False, str(exc)
	stderr_text = result.stderr.decode("utf-8", errors="replace")
	if result.returncode != 0:
		stderr_tail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else ""
		reason = f"exit={result.returncode}"
		if stderr_tail:
			reason = f"{reason} {stderr_tail}"
		return False, reason
	chunk_text = result.stdout.decode("utf-8", errors="replace").strip("\n")
	if not chunk_text.strip():
		return False, "empty-result"
	return True, chunk_text


def _clamp_text_to_byte_budget(text: str, budget_bytes: int) -> tuple[str, bool]:
	"""Clamp `text` to at most `budget_bytes` UTF-8 bytes on a line boundary.

	Returns `(clamped_text, was_clamped)`. Whole lines are preferred so a
	chunk block never ends mid-token; when even the first line exceeds the
	budget the text is cut at the largest byte prefix that decodes cleanly.
	That prefix can itself be empty when the budget is smaller than the
	first UTF-8 code point (e.g. a 1-byte budget against a multi-byte
	character), so callers must tolerate an empty result. A non-positive
	budget yields `("", True)`.
	"""
	if budget_bytes <= 0:
		return "", True
	encoded = text.encode("utf-8")
	if len(encoded) <= budget_bytes:
		return text, False
	kept_lines: list[str] = []
	kept_bytes = 0
	for line in text.splitlines():
		# +1 for the newline that rejoining re-adds between lines.
		line_cost = len(line.encode("utf-8")) + (1 if kept_lines else 0)
		if kept_bytes + line_cost > budget_bytes:
			break
		kept_lines.append(line)
		kept_bytes += line_cost
	if kept_lines:
		return "\n".join(kept_lines), True
	return encoded[:budget_bytes].decode("utf-8", errors="ignore"), True


# `truncated_to_budget` exists because the overflow branch in `emit_context`
# renders semble chunks whose size is set by the index, not by the file that
# triggered the query — a 12KB source file can retrieve a 129KB chunk set.
# Without clamping, one overflowing path could emit more than the caller's
# entire `max_bytes` budget, and every subsequent overflowing path added
# another unclamped block on top. That is what pushed the implement prompt
# past codex-cli's 1,048,576-character `turn/start` stdin cap on run
# 33796624872 (issue #3990), failing every attempt before the editor ran.
# The header records the clamp so the model knows the block is partial and
# should use its read tool for the rest.
def _append_semble_block(
	output: list[str],
	rel: str,
	raw_size: int,
	chunk_text: str,
	*,
	truncated_to_budget: bool = False,
) -> int:
	rendered_bytes = len(chunk_text.encode("utf-8"))
	if truncated_to_budget:
		output.append(
			f"--- FILE: {rel} ({raw_size} bytes — chunk-retrieved via semble, "
			f"truncated to {rendered_bytes} byte(s) to fit the remaining total "
			f"budget — read with read tool for the rest) ---"
		)
	else:
		output.append(f"--- FILE: {rel} ({raw_size} bytes — chunk-retrieved via semble) ---")
	output.extend(chunk_text.splitlines())
	output.append(f"--- END FILE: {rel} ---")
	output.append("")
	return rendered_bytes


def _append_read_fallback_block(output: list[str], rel: str, truncated: bytes, raw_size: int) -> int:
	if b"\x00" in truncated:
		output.extend([f"--- FILE: {rel} (binary skipped) ---", ""])
		return 0
	text = truncated.decode("utf-8", errors="replace")
	output.append(
		f"--- FILE: {rel} ({raw_size} bytes; overflow fallback read head, "
		f"truncated to {len(truncated)} byte(s)) ---"
	)
	output.extend(text.splitlines())
	output.append(f"--- END FILE: {rel} ---")
	output.append("")
	return len(truncated)


def emit_context(
	paths: list[str],
	repo_root: Path,
	max_bytes: int,
	header_text: str = DEFAULT_HEADER_TEXT,
	*,
	semble_bin: str | None = None,
	semble_index: str | None = None,
	semble_query_text: str | None = None,
	semble_max_chunks: int = 6,
	semble_fallback: str = "marker",
) -> str:
	if max_bytes <= 0:
		return ""

	fragments: list[str] = []
	rendered_bytes = 0
	final_summary_reserve = min(256, max_bytes)
	content_byte_limit = max(0, max_bytes - final_summary_reserve)
	included = 0
	inlined = 0
	marker_only = 0
	semble_rendered = 0
	read_fallback_rendered = 0
	off_suppressed = 0
	omitted_entries = 0
	semble_overflow_budget_exhausted = False
	semble_max_chunks = _normalize_semble_max_chunks(semble_max_chunks)

	def append_complete(fragment: str) -> bool:
		nonlocal rendered_bytes
		fragment_size = len(fragment.encode("utf-8"))
		if rendered_bytes + fragment_size > max_bytes:
			return False
		fragments.append(fragment)
		rendered_bytes += fragment_size
		return True

	def render_lines(lines: list[str]) -> str:
		return "\n".join(lines) + "\n"

	def append_content(fragment: str) -> bool:
		nonlocal rendered_bytes
		fragment_size = len(fragment.encode("utf-8"))
		if rendered_bytes + fragment_size > content_byte_limit:
			return False
		fragments.append(fragment)
		rendered_bytes += fragment_size
		return True

	def append_marker(marker: str) -> bool:
		return append_content(render_lines([marker, ""]))

	if not append_complete("=== TARGETED FILE CONTEXT ===\n"):
		return ""
	if not append_content(f"{header_text}\n\n"):
		append_content("(targeted context header omitted: rendered byte budget exhausted)\n\n")

	repo_root_resolved = repo_root.resolve()
	bounded_paths: list[str] = []
	for raw_path in paths[:MAX_TARGET_PATHS]:
		if not isinstance(raw_path, str) or len(raw_path.encode("utf-8")) > MAX_TARGET_PATH_BYTES:
			omitted_entries += 1
			continue
		bounded_paths.append(raw_path)
	omitted_entries += max(0, len(paths) - MAX_TARGET_PATHS)

	for rel in bounded_paths:
		abs_path = (repo_root / rel).resolve()
		try:
			abs_path.relative_to(repo_root_resolved)
		except ValueError:
			omitted_entries += 1
			continue
		if not abs_path.is_file():
			if append_marker(f"--- FILE: {rel} (missing) ---"):
				included += 1
				marker_only += 1
			else:
				omitted_entries += 1
			continue

		try:
			raw_size = abs_path.stat().st_size
		except OSError:
			omitted_entries += 1
			continue
		remaining_bytes = content_byte_limit - rendered_bytes
		header = _render_inlined_file_header(rel, raw_size, Path(rel).suffix.lower())
		minimum_inline_size = len(render_lines([header, f"--- END FILE: {rel} ---", ""]).encode("utf-8"))
		if raw_size + minimum_inline_size <= remaining_bytes:
			try:
				raw = abs_path.read_bytes()
			except OSError:
				omitted_entries += 1
				continue
			if b"\x00" in raw:
				if append_marker(f"--- FILE: {rel} (binary skipped) ---"):
					included += 1
					marker_only += 1
				else:
					omitted_entries += 1
				continue
			inline_lines: list[str] = []
			_append_inlined_file_block(inline_lines, rel, raw)
			if append_content(render_lines(inline_lines)):
				included += 1
				inlined += 1
				continue

		remaining_bytes = content_byte_limit - rendered_bytes
		if semble_query_text and remaining_bytes > 0 and not semble_overflow_budget_exhausted:
			query_start = time.monotonic()
			success, payload = _run_semble_query(
				f"{rel}\n{semble_query_text}",
				semble_bin,
				semble_index,
				semble_max_chunks,
				repo_root,
			)
			elapsed_ms = int((time.monotonic() - query_start) * 1000)
			if success and payload is not None:
				payload_budget = remaining_bytes
				semble_fragment = ""
				content_size = 0
				for _attempt in range(3):
					clamped_payload, payload_was_clamped = _clamp_text_to_byte_budget(payload, payload_budget)
					if not clamped_payload:
						break
					semble_lines: list[str] = []
					content_size = _append_semble_block(
						semble_lines,
						rel,
						raw_size,
						clamped_payload,
						truncated_to_budget=payload_was_clamped,
					)
					semble_fragment = render_lines(semble_lines)
					overage = len(semble_fragment.encode("utf-8")) - remaining_bytes
					if overage <= 0:
						break
					payload_budget = max(0, payload_budget - overage)
				if semble_fragment and append_content(semble_fragment):
					_log_semble_event(
						"SEMBLE_QUERY",
						target="overflow",
						file=rel,
						chunks=semble_max_chunks,
						bytes=content_size,
						ms=elapsed_ms,
					)
					included += 1
					semble_rendered += 1
					continue
				payload = "budget-exhausted"
			semble_overflow_budget_exhausted = True
			_log_semble_event(
				"SEMBLE_FALLBACK",
				target="overflow",
				file=rel,
				reason=payload or "unknown",
				ms=elapsed_ms,
			)

		if semble_fallback == "read":
			remaining_bytes = content_byte_limit - rendered_bytes
			head_size = min(raw_size, SEMBLE_READ_FALLBACK_MAX_BYTES, remaining_bytes)
			read_fragment = ""
			content_size = 0
			for _attempt in range(3):
				if head_size <= 0:
					break
				try:
					with abs_path.open("rb") as handle:
						truncated = handle.read(head_size)
				except OSError:
					break
				read_lines: list[str] = []
				content_size = _append_read_fallback_block(read_lines, rel, truncated, raw_size)
				read_fragment = render_lines(read_lines)
				overage = len(read_fragment.encode("utf-8")) - remaining_bytes
				if overage <= 0:
					break
				head_size = max(0, head_size - overage)
			if read_fragment and append_content(read_fragment):
				included += 1
				read_fallback_rendered += 1
				continue
		elif semble_fallback == "off":
			off_suppressed += 1
			continue

		if append_marker(
			f"--- FILE: {rel} ({raw_size} bytes; would overflow total budget — "
			f"read with read tool, max_bytes={max_bytes}) ---"
		):
			included += 1
			marker_only += 1
		else:
			omitted_entries += 1

	if included == 0 and off_suppressed == 0:
		summary = "(no existing target files could be inlined)"
	else:
		summary = (
			f"Included {included} entr{'y' if included == 1 else 'ies'} "
			f"({inlined} inlined, {marker_only} marker-only, "
			f"{semble_rendered} semble, {read_fallback_rendered} read)."
		)
		if off_suppressed:
			summary += f" Suppressed overflow entries: {off_suppressed}."
	if omitted_entries:
		summary += f" Omitted {omitted_entries} path(s) due to path or rendered-byte limits."
	append_complete(f"{summary}\n")
	return "".join(fragments)


def positive_int(value: str) -> int:
	try:
		parsed = int(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError(f"expected integer, got {value!r}") from exc
	if parsed < 0:
		raise argparse.ArgumentTypeError("expected non-negative integer")
	return parsed


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--plan-file", default=None, help="markdown plan to scan for 'Files likely to change' section")
	parser.add_argument("--paths-file", default=None, help="newline-separated path list")
	parser.add_argument("--paths", default=None, help="comma-separated paths")
	parser.add_argument("--repo-root", default=os.getcwd())
	parser.add_argument("--max-bytes", type=positive_int, default=102400)
	parser.add_argument("--header-text", default=DEFAULT_HEADER_TEXT)
	parser.add_argument("--semble-bin", default=None)
	parser.add_argument("--semble-index", default=None)
	parser.add_argument("--semble-query-from", default=None)
	parser.add_argument("--semble-max-chunks", type=positive_int, default=6)
	parser.add_argument("--semble-fallback", choices=("marker", "read", "off"), default="marker")
	parser.add_argument("--output", required=True)
	args = parser.parse_args()

	merged: list[str] = []
	seen: set[str] = set()
	plan_text = ""

	def _extend(items: list[str]) -> None:
		for item in items:
			if item not in seen:
				seen.add(item)
				merged.append(item)

	if args.plan_file:
		try:
			plan_text = Path(args.plan_file).read_text(encoding="utf-8", errors="replace")
		except OSError:
			plan_text = ""
		_extend(extract_paths_from_plan(plan_text))
	if args.paths_file:
		_extend(parse_paths_file(args.paths_file))
	if args.paths:
		_extend(parse_paths_arg(args.paths))

	context = emit_context(
		merged,
		Path(args.repo_root),
		args.max_bytes,
		args.header_text,
		semble_bin=args.semble_bin,
		semble_index=args.semble_index,
		semble_query_text=_read_optional_query_text(args.semble_query_from) or (plan_text.strip() or None),
		semble_max_chunks=args.semble_max_chunks,
		semble_fallback=args.semble_fallback,
	)
	Path(args.output).write_text(context, encoding="utf-8")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
