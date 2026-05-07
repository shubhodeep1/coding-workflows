#!/usr/bin/env python3
"""Inline likely-to-change files from an implementation plan for Codex.

The implement workflow feeds Codex a large static + dynamic prompt.  For small
plans, the best way to prevent repeated exploration/no-op turns is to place the
exact target files in the prompt and explicitly tell the editor to write first.
This helper extracts paths from the approved plan's "Files likely to change"
section and emits a bounded, line-numbered context block.
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Iterable

PATH_IN_BACKTICKS_RE = re.compile(r"`([^`]+)`")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
SECTION_HEADER_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?([A-Za-z][A-Za-z0-9 /_-]{2,})\s*:?[ \t]*$")
LIKELY_HEADER_RE = re.compile(r"files?\s+(?:likely|expected|to)\s+(?:to\s+)?(?:change|modify|edit)|files?\s+to\s+(?:change|modify|edit)", re.I)

SAFE_EXTENSIONS = {
    ".c", ".cc", ".cfg", ".conf", ".contract", ".cpp", ".cs", ".css",
    ".csv", ".go", ".h", ".hpp", ".html", ".java", ".js", ".json",
    ".jsx", ".kt", ".md", ".py", ".rb", ".rs", ".sh", ".sol", ".sql",
    ".svelte", ".toml", ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml",
}


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


def extract_target_paths(plan_text: str) -> list[str]:
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


def iter_line_numbered(text: str) -> Iterable[str]:
    lines = text.splitlines()
    if text.endswith("\n"):
        # splitlines() intentionally drops the terminal empty item; that is OK.
        pass
    width = max(1, len(str(len(lines))))
    for index, line in enumerate(lines, start=1):
        yield f"{index:>{width}}\t{line}"


def emit_context(paths: list[str], repo_root: Path, max_files: int, max_bytes: int) -> str:
    output: list[str] = []
    included = 0
    used_bytes = 0

    output.append("=== TARGETED FILE CONTEXT ===")
    output.append("The approved plan named these files as likely to change. Their current contents are inlined so you can edit immediately without re-reading them. If a file is included here, your first tool call should be a write to one of these files unless the plan is already satisfied.")
    output.append("")

    if max_files <= 0 or max_bytes <= 0:
        output.append(f"(targeted context disabled by limits: max_files={max_files}, max_bytes={max_bytes})")
        return "\n".join(output) + "\n"

    for rel in paths:
        if included >= max_files or used_bytes >= max_bytes:
            break
        abs_path = (repo_root / rel).resolve()
        try:
            abs_path.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if not abs_path.is_file():
            output.extend([f"--- FILE: {rel} (missing) ---", ""])
            included += 1
            continue
        raw = abs_path.read_bytes()
        if b"\x00" in raw:
            output.extend([f"--- FILE: {rel} (binary skipped) ---", ""])
            included += 1
            continue
        remaining = max_bytes - used_bytes
        if remaining <= 0:
            break
        truncated = len(raw) > remaining
        chunk = raw[:remaining]
        text = chunk.decode("utf-8", errors="replace")
        used_bytes += len(chunk)
        output.append(f"--- FILE: {rel} ({len(raw)} bytes{' ; truncated' if truncated else ''}) ---")
        output.extend(iter_line_numbered(text))
        output.append(f"--- END FILE: {rel} ---")
        output.append("")
        included += 1

    if included == 0:
        output.append("(no existing target files could be inlined from the plan)")
    else:
        output.append(f"Included {included} target file(s), {used_bytes} byte(s) of source content.")
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
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--repo-root", default=os.getcwd())
    parser.add_argument("--max-files", type=positive_int, default=10)
    parser.add_argument("--max-bytes", type=positive_int, default=102400)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    plan_text = Path(args.plan_file).read_text(encoding="utf-8", errors="replace")
    paths = extract_target_paths(plan_text)
    context = emit_context(paths, Path(args.repo_root), args.max_files, args.max_bytes)
    Path(args.output).write_text(context, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
