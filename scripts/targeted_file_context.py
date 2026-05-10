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

  --max-bytes (default 102400)    Total bytes across all inlined files
                                  (~25k tokens at ~4 b/t). A file that
                                  would push the cumulative size over
                                  this cap is NOT head-truncated — it
                                  gets a "(would overflow total budget —
                                  read with read tool)" marker so the
                                  model uses its native targeted-read
                                  flow instead of being misled by a
                                  truncated head. There is no separate
                                  file-count cap: every path the caller
                                  passes is reported, either inlined or
                                  as a marker.

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


def _log_semble_event(prefix: str, **fields: object) -> None:
	parts = [prefix]
	for key, value in fields.items():
		# Keep the Python-side overflow telemetry aligned with
		# scripts/semble_helpers.sh: single-line, unquoted key=value fields on
		# stderr. Normalizing whitespace prevents multiline exception payloads
		# from spilling extra log lines while preserving the shared log shape.
		rendered = " ".join(str(value).split())
		parts.append(f"{key}={rendered}")
	print(" ".join(parts), file=sys.stderr)


def is_probable_path(value: str) -> bool:
	value = value.strip().strip(".,;:")
	if not value or value.startswith(("http://", "https://", "#")):
		return False
	if any(part in {"", ".", ".."} for part in value.split("/")):
		return False
	if value.startswith(("/", "~")) or "\x00" in value:
		return False
	suffix = Path(value).suffix.lower()
	return "/" in value or suffix in SAFE_EXTENSIONS or value in {"Dockerfile", "Makefile", "Procfile"}


def normalize_path(value: str) -> str | None:
	value = value.strip().strip(".,;:")
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


def _append_semble_block(output: list[str], rel: str, raw_size: int, chunk_text: str) -> int:
	output.append(f"--- FILE: {rel} ({raw_size} bytes — chunk-retrieved via semble) ---")
	output.extend(chunk_text.splitlines())
	output.append(f"--- END FILE: {rel} ---")
	output.append("")
	return len(chunk_text.encode("utf-8"))


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
	output: list[str] = []
	included = 0
	inlined = 0
	used_bytes = 0
	skipped_too_large: list[tuple[str, int]] = []
	semble_rendered = 0
	read_fallback_rendered = 0
	off_suppressed = 0
	overflow_rendered_bytes = 0
	semble_max_chunks = _normalize_semble_max_chunks(semble_max_chunks)

	output.append("=== TARGETED FILE CONTEXT ===")
	output.append(header_text)
	output.append("")

	if max_bytes <= 0:
		output.append(f"(targeted context disabled: max_bytes={max_bytes})")
		return "\n".join(output) + "\n"

	repo_root_resolved = repo_root.resolve()

	for rel in paths:
		abs_path = (repo_root / rel).resolve()
		try:
			abs_path.relative_to(repo_root_resolved)
		except ValueError:
			# Outside repo root — refuse silently. Caller's path source
			# may include adversarial / typo paths from PR/issue bodies.
			continue
		if not abs_path.is_file():
			output.extend([f"--- FILE: {rel} (missing) ---", ""])
			included += 1
			continue
		raw_size = abs_path.stat().st_size
		if used_bytes + raw_size > max_bytes:
			if semble_query_text:
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
					rendered_bytes = _append_semble_block(output, rel, raw_size, payload)
					_log_semble_event(
						"SEMBLE_QUERY",
						target="overflow",
						file=rel,
						chunks=semble_max_chunks,
						bytes=rendered_bytes,
						ms=elapsed_ms,
					)
					overflow_rendered_bytes += rendered_bytes
					used_bytes += rendered_bytes
					included += 1
					semble_rendered += 1
					continue
				_log_semble_event(
					"SEMBLE_FALLBACK",
					target="overflow",
					file=rel,
					reason=payload or "unknown",
					ms=elapsed_ms,
				)
			if semble_fallback == "read":
				remaining_bytes = max_bytes - used_bytes
				if remaining_bytes <= 0:
					output.extend([
						f"--- FILE: {rel} ({raw_size} bytes; would overflow total "
						f"budget — read with read tool, max_bytes={max_bytes}, "
						f"used={used_bytes}) ---",
						"",
					])
					skipped_too_large.append((rel, raw_size))
					included += 1
					continue
				head_bytes = min(raw_size, min(remaining_bytes, SEMBLE_READ_FALLBACK_MAX_BYTES))
				with abs_path.open("rb") as handle:
					truncated = handle.read(head_bytes)
				rendered_bytes = _append_read_fallback_block(output, rel, truncated, raw_size)
				overflow_rendered_bytes += rendered_bytes
				used_bytes += rendered_bytes
				included += 1
				read_fallback_rendered += 1
				continue
			if semble_fallback == "off":
				off_suppressed += 1
				continue
			# Including this file would overflow the total budget. Mark
			# rather than head-truncate — a truncated head of a 500KB
			# file misleads the editor when the edit target lives at
			# the bottom. Tell the model to read it normally with its
			# read tool instead.
			output.extend([
				f"--- FILE: {rel} ({raw_size} bytes; would overflow total "
				f"budget — read with read tool, max_bytes={max_bytes}, "
				f"used={used_bytes}) ---",
				"",
			])
			skipped_too_large.append((rel, raw_size))
			included += 1
			continue
		raw = abs_path.read_bytes()
		if b"\x00" in raw:
			output.extend([f"--- FILE: {rel} (binary skipped) ---", ""])
			included += 1
			continue
		used_bytes += len(raw)
		_append_inlined_file_block(output, rel, raw)
		included += 1
		inlined += 1

	if included == 0 and off_suppressed == 0:
		output.append("(no existing target files could be inlined)")
	else:
		if semble_rendered == 0 and read_fallback_rendered == 0 and off_suppressed == 0:
			summary = (
				f"Included {included} entr{'y' if included == 1 else 'ies'} "
				f"({inlined} inlined, {included - inlined} marker-only), "
				f"{used_bytes} byte(s) of source content."
			)
		else:
			summary = (
				f"Included {included} entr{'y' if included == 1 else 'ies'} "
				f"({inlined} inlined, {len(skipped_too_large)} marker-only, "
				f"{semble_rendered} semble, {read_fallback_rendered} read), "
				f"{used_bytes} byte(s) of source content."
			)
			if overflow_rendered_bytes:
				summary += f" Overflow-rendered content: {overflow_rendered_bytes} byte(s)."
			if off_suppressed:
				summary += f" Suppressed overflow entries: {off_suppressed}."
		if skipped_too_large:
			skipped_summary = ", ".join(f"{p} ({n} bytes)" for p, n in skipped_too_large[:5])
			if len(skipped_too_large) > 5:
				skipped_summary += f", +{len(skipped_too_large) - 5} more"
			summary += f" Marker-only (would overflow): {skipped_summary}."
		output.append(summary)
	return "\n".join(output) + "\n"


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
