#!/usr/bin/env python3
"""Generate a symbol-level diff summary from a unified diff and changed files list.

This script parses a unified diff to extract changed file paths and line ranges,
then uses a regex heuristic to map changed lines to symbol names (functions,
classes, methods). The output is a compact summary that can replace or
supplement raw unified diffs for PR reviewers, reducing token usage.

Usage:
    python3 scripts/generate_symbol_diff_summary.py \
        --diff-file <path-to-unified-diff> \
        --changed-files <path-to-changed-files-list> \
        --output <output-file-path> \
        [--project-dir <repo-root>]

Output format:
    FILE: src/auth/login.py
      MODIFIED: function authenticate_user (lines 45-67)
      ADDED: function validate_token (after line 89)
      MODIFIED: class AuthProvider.refresh_session (lines 102-118)

    FILE: src/api/routes.py
      MODIFIED: function register_routes (lines 23-25)
"""

import argparse
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

CALLER_SEARCH_CAP = 10

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")
FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")

# Common symbol definition patterns for heuristic fallback
SYMBOL_PATTERNS = {
    ".py": [
        re.compile(r"^\s*(async\s+)?def\s+(\w+)\s*\("),
        re.compile(r"^\s*class\s+(\w+)"),
    ],
    ".ts": [
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
        re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\("),
        re.compile(r"^\s*(?:public|private|protected)?\s*(?:async\s+)?(\w+)\s*\("),
    ],
    ".tsx": None,  # same as .ts
    ".js": None,  # same as .ts
    ".jsx": None,  # same as .ts
    ".go": [
        re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\("),
        re.compile(r"^type\s+(\w+)\s+struct"),
        re.compile(r"^type\s+(\w+)\s+interface"),
    ],
    ".rs": [
        re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)"),
        re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)"),
        re.compile(r"^\s*(?:pub\s+)?enum\s+(\w+)"),
        re.compile(r"^\s*(?:pub\s+)?trait\s+(\w+)"),
        re.compile(r"^\s*impl(?:<[^>]+>)?\s+(\w+)"),
    ],
    ".java": [
        re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:[\w<>\[\]]+\s+)?(\w+)\s*\("),
        re.compile(r"^\s*(?:public|private|protected)?\s*(?:abstract\s+)?class\s+(\w+)"),
        re.compile(r"^\s*(?:public|private|protected)?\s*interface\s+(\w+)"),
    ],
}
# Alias extensions
SYMBOL_PATTERNS[".tsx"] = SYMBOL_PATTERNS[".ts"]
SYMBOL_PATTERNS[".js"] = SYMBOL_PATTERNS[".ts"]
SYMBOL_PATTERNS[".jsx"] = SYMBOL_PATTERNS[".ts"]
SYMBOL_PATTERNS[".mjs"] = SYMBOL_PATTERNS[".ts"]
SYMBOL_PATTERNS[".cjs"] = SYMBOL_PATTERNS[".ts"]


def parse_diff_hunks(diff_text):
    """Parse unified diff to extract per-file changed line ranges."""
    file_hunks = defaultdict(list)
    current_file = None

    for line in diff_text.splitlines():
        file_match = FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue

        hunk_match = HUNK_RE.match(line)
        if hunk_match and current_file:
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4) or "1")
            hunk_header = hunk_match.group(5).strip()
            if new_count > 0:
                file_hunks[current_file].append({
                    "start": new_start,
                    "end": new_start + new_count - 1,
                    "count": new_count,
                    "header": hunk_header,
                })

    return dict(file_hunks)


def get_symbols_heuristic(file_path, project_dir):
    """Heuristic fallback: parse file for symbol definitions using regex."""
    ext = Path(file_path).suffix.lower()
    patterns = SYMBOL_PATTERNS.get(ext)
    if not patterns:
        return []

    full_path = os.path.join(project_dir, file_path)
    if not os.path.isfile(full_path):
        return []

    symbols = []
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
            for line_num, line_text in enumerate(fh, 1):
                for pattern in patterns:
                    match = pattern.match(line_text)
                    if match:
                        # Get the last non-None group as the symbol name
                        name = None
                        for group in reversed(match.groups()):
                            if group and not group.strip().startswith("async"):
                                name = group
                                break
                        if name:
                            symbols.append({
                                "name": name,
                                "line": line_num,
                                "text": line_text.rstrip(),
                            })
                        break
    except (OSError, IOError):
        pass

    return symbols


def find_enclosing_symbol(line_num, symbols):
    """Find which symbol definition encloses a given line number."""
    best = None
    for sym in symbols:
        if sym["line"] <= line_num:
            if best is None or sym["line"] > best["line"]:
                best = sym
    return best


def classify_hunk(hunk, symbols):
    """Classify a hunk as MODIFIED, ADDED, or REMOVED relative to symbols."""
    start_symbol = find_enclosing_symbol(hunk["start"], symbols)
    find_enclosing_symbol(hunk["end"], symbols)

    if start_symbol:
        return {
            "type": "MODIFIED",
            "symbol": start_symbol["name"],
            "lines": f"{hunk['start']}-{hunk['end']}",
        }
    elif hunk.get("header"):
        # Try to extract symbol from hunk header (git often includes function name)
        header_match = re.search(r"(?:def|function|fn|func|class|type)\s+(\w+)", hunk["header"])
        if header_match:
            return {
                "type": "MODIFIED",
                "symbol": header_match.group(1),
                "lines": f"{hunk['start']}-{hunk['end']}",
            }

    return {
        "type": "MODIFIED",
        "symbol": None,
        "lines": f"{hunk['start']}-{hunk['end']}",
    }


def find_callers(symbol_name, project_dir, exclude_file):
    """Find files that reference/call symbol_name using grep-based detection.

    Args:
        symbol_name: The symbol (function/class/method) name to search for.
        project_dir: Absolute or relative path to the project root.
        exclude_file: File path (relative to project_dir) where the symbol is
            defined -- excluded from results.

    Returns:
        List of file paths (relative to project_dir) that reference the symbol,
        capped at CALLER_SEARCH_CAP. Returns empty list on any error.
    """
    try:
        abs_project_dir = os.path.abspath(project_dir)
        grep_cmd = [
            "grep", "-rlF",
            "--exclude-dir=.git",
            "--exclude-dir=node_modules",
            "--exclude-dir=__pycache__",
        ]
        # For identifier-like symbols, require word boundaries to avoid
        # substring false positives (e.g. "run" matching "runner").
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol_name):
            grep_cmd.append("-w")
        grep_cmd.extend([symbol_name, "."])
        proc = subprocess.run(
            grep_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=abs_project_dir,
        )
        if proc.returncode not in (0, 1):
            return []

        callers = []
        exclude_normalized = os.path.normpath(exclude_file)
        for line in proc.stdout.splitlines():
            rel_path = line.strip()
            if not rel_path:
                continue
            # grep -rl with "." prefix returns paths like ./src/foo.py
            if rel_path.startswith("./"):
                rel_path = rel_path[2:]
            if os.path.normpath(rel_path) == exclude_normalized:
                continue
            callers.append(rel_path)
            if len(callers) >= CALLER_SEARCH_CAP:
                break
        return callers
    except Exception:
        return []


def generate_summary(diff_text, changed_files_text, project_dir, include_callers=True):
    """Generate symbol-level diff summary."""
    file_hunks = parse_diff_hunks(diff_text)

    # Also include files from the changed-files list that might not be in the diff
    if changed_files_text:
        for line in changed_files_text.strip().splitlines():
            fname = line.strip()
            if fname and fname not in file_hunks:
                file_hunks[fname] = []

    output_lines = []

    for file_path in sorted(file_hunks.keys()):
        hunks = file_hunks[file_path]

        if not hunks:
            output_lines.append(f"FILE: {file_path}")
            output_lines.append("  (file in changed-files list but no diff hunks)")
            output_lines.append("")
            continue

        symbols = get_symbols_heuristic(file_path, project_dir)

        output_lines.append(f"FILE: {file_path}")

        seen_symbols = set()
        for hunk in hunks:
            info = classify_hunk(hunk, symbols)
            if info["symbol"]:
                output_lines.append(
                    f"  {info['type']}: {info['symbol']} (lines {info['lines']})"
                )
                if include_callers and info["symbol"] not in seen_symbols:
                    seen_symbols.add(info["symbol"])
                    callers = find_callers(info["symbol"], project_dir, file_path)
                    if callers:
                        output_lines.append(
                            f"    CALLERS ({len(callers)}): {', '.join(callers)}"
                        )
                    else:
                        output_lines.append(
                            "    CALLERS (0): (none found)"
                        )
            else:
                output_lines.append(
                    f"  {info['type']}: lines {info['lines']}"
                )

        # Summary stats
        total_lines = sum(h["count"] for h in hunks)
        output_lines.append(f"  ({len(hunks)} hunks, ~{total_lines} lines changed)")
        output_lines.append("")

    if not output_lines:
        output_lines.append("No changed files detected in diff.")

    header = [
        "SYMBOL-LEVEL DIFF SUMMARY",
        f"Files changed: {len(file_hunks)}",
        "Symbol resolution: heuristic",
        "",
    ]

    return "\n".join(header + output_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate symbol-level diff summary for PR reviewers"
    )
    parser.add_argument(
        "--diff-file",
        required=True,
        help="Path to unified diff file",
    )
    parser.add_argument(
        "--changed-files",
        required=True,
        help="Path to changed files list (one per line)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output file path for symbol diff summary",
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Project root directory (default: current directory)",
    )
    parser.add_argument(
        "--include-callers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include 1-hop caller detection per modified symbol (default: True, disable with --no-include-callers)",
    )
    args = parser.parse_args()

    # Read inputs
    diff_text = ""
    if os.path.isfile(args.diff_file):
        with open(args.diff_file, "r", encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()

    changed_files_text = ""
    if os.path.isfile(args.changed_files):
        with open(args.changed_files, "r", encoding="utf-8", errors="replace") as fh:
            changed_files_text = fh.read()

    if not diff_text and not changed_files_text:
        summary = "No diff or changed files available for symbol summary."
    else:
        summary = generate_summary(diff_text, changed_files_text, args.project_dir, args.include_callers)

    # Write output
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(summary)

    print(f"Symbol diff summary written to {args.output} ({len(summary)} bytes)")


if __name__ == "__main__":
    main()
