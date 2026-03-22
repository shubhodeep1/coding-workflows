#!/usr/bin/env python3
"""Parse Codex CLI output/stderr to compute Serena MCP tool usage efficiency.

Scans log files and output files for evidence of Serena MCP tool calls versus
fallback file-based operations (read_file, write_file, cat, etc.).  Produces a
compact markdown summary suitable for embedding in a PR comment.

Usage:
    python3 scripts/serena_efficiency_report.py \
        --scan-dir <directory-with-log-and-output-files> \
        [--extra-files <file1> <file2> ...] \
        --output <output-file-path>

Output (markdown):
    ### Serena MCP efficiency
    | Metric | Value |
    |--------|-------|
    | Serena tool calls | 42 |
    | File-based fallback ops | 8 |
    | Serena efficiency | 84% |
    | Top Serena tools | get_symbols_overview (12), replace_symbol_body (9), ... |
"""

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Serena MCP tool invocations — match various formats Codex CLI may emit:
#   mcp__serena__<tool_name>
#   serena.<tool_name>
#   tool_call: serena/<tool_name>
#   "name": "mcp__serena__<tool_name>"
SERENA_TOOL_RE = re.compile(
    r"(?:mcp__serena__|serena[./])(\w+)", re.IGNORECASE
)

# File-based fallback operations that indicate Serena was NOT used:
#   read_file, write_file, create_file, patch_file — Codex sandbox tools
#   cat <path>, head <path>, tail <path> — shell commands
FILE_OP_RE = re.compile(
    r"\b(?:read_file|write_file|create_file|patch_file|apply_diff|apply_patch)\b"
    r"|"
    r'\b(?:cat|head|tail)\b(?:\s+-[^\s]+)*\s+["\']?[~./\w$-]',
    re.IGNORECASE,
)

# Exclude known false-positive lines (e.g. system instructions mentioning tools)
INSTRUCTION_HINT_RE = re.compile(
    r"(?:INSTEAD\s+of|MANDATORY|NEVER\s+read|use\s+Serena|fallback|Rules:)",
    re.IGNORECASE,
)


def scan_file(path: str) -> tuple[Counter, int]:
    """Scan a single file and return (serena_tool_counts, file_op_count)."""
    serena_counts: Counter = Counter()
    file_ops = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                # Skip lines that look like system instructions / prompt text
                if INSTRUCTION_HINT_RE.search(line):
                    continue
                for m in SERENA_TOOL_RE.finditer(line):
                    tool_name = m.group(1).lower()
                    serena_counts[tool_name] += 1
                file_ops += sum(1 for _ in FILE_OP_RE.finditer(line))
    except OSError:
        pass
    return serena_counts, file_ops


def scan_directory(dirpath: str) -> tuple[Counter, int]:
    """Recursively scan a directory for log/output/err files."""
    serena_total: Counter = Counter()
    file_ops_total = 0
    if not os.path.isdir(dirpath):
        return serena_total, file_ops_total
    for root, _dirs, files in os.walk(dirpath):
        for fname in files:
            # Only scan text-like files
            if fname.endswith((".log", ".err", ".txt", ".json", ".stderr")):
                counts, ops = scan_file(os.path.join(root, fname))
                serena_total += counts
                file_ops_total += ops
    return serena_total, file_ops_total


def format_report(serena_counts: Counter, file_ops: int) -> str:
    """Return a markdown-formatted efficiency report."""
    total_serena = sum(serena_counts.values())
    total_ops = total_serena + file_ops
    if total_ops == 0:
        return (
            "### Serena MCP efficiency\n"
            "No tool call evidence found in logs — "
            "Serena stats unavailable for this run.\n"
        )

    efficiency = (total_serena / total_ops * 100) if total_ops > 0 else 0

    # Top tools (up to 5)
    top_tools = serena_counts.most_common(5)
    top_str = ", ".join(f"`{name}` ({count})" for name, count in top_tools)
    if not top_str:
        top_str = "none"

    lines = [
        "### Serena MCP efficiency",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Serena tool calls | {total_serena} |",
        f"| File-based fallback ops | {file_ops} |",
        f"| **Serena efficiency** | **{efficiency:.0f}%** |",
        f"| Top Serena tools | {top_str} |",
        "",
    ]

    if efficiency == 0:
        lines.append(
            "> Serena was not used this run. The LLM fell back to "
            "file-based operations (or Serena was unavailable)."
        )
    elif efficiency < 50:
        lines.append(
            "> Low Serena adoption — the LLM used file-based operations "
            "more often than Serena tools."
        )
    elif efficiency >= 90:
        lines.append(
            "> Excellent Serena adoption — nearly all code operations "
            "used semantic tools."
        )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute Serena MCP tool usage efficiency from Codex logs."
    )
    parser.add_argument(
        "--scan-dir",
        required=True,
        help="Directory to recursively scan for log/output files.",
    )
    parser.add_argument(
        "--extra-files",
        nargs="*",
        default=[],
        help="Additional files to scan (e.g. editor stderr, codex output).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the markdown report.",
    )
    args = parser.parse_args()

    serena_counts, file_ops = scan_directory(args.scan_dir)
    for fpath in args.extra_files:
        if os.path.isfile(fpath):
            c, o = scan_file(fpath)
            serena_counts += c
            file_ops += o

    report = format_report(serena_counts, file_ops)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)

    # Also print to stdout for workflow logs
    print(report)


if __name__ == "__main__":
    main()
