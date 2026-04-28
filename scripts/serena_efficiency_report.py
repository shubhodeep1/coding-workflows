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
from collections import Counter

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
    if os.path.basename(path) in PROMPT_FILE_NAMES:
        return serena_counts, file_ops
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



# Files that contain prompt/instruction text — NOT actual tool call evidence.
# Scanning these inflates Serena counts with mentions from system instructions.
PROMPT_FILE_NAMES = frozenset((
    "codex_prompt.txt",
    "codex_prompt_retry.txt",
    "implementation_context.txt",
    "issue_summary_prompt.txt",
    "pre_assembled_static.txt",
))


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
                # Skip prompt/instruction files — they mention Serena tools
                # in guidance text, not as actual tool invocations.
                if fname in PROMPT_FILE_NAMES:
                    continue
                counts, ops = scan_file(os.path.join(root, fname))
                serena_total += counts
                file_ops_total += ops
    return serena_total, file_ops_total


def load_serena_tool_stats(scan_dir: str) -> dict:
    """Load Serena's own tool_usage_stats.json if present in the scan dir or repo."""
    for candidate in [
        os.path.join(scan_dir, "tool_usage_stats.json"),
        os.path.join(scan_dir, "..", ".serena", "tool_usage_stats.json"),
        ".serena/tool_usage_stats.json",
    ]:
        if os.path.isfile(candidate):
            try:
                import json
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
    return {}


# Conservative per-operation token estimates for savings calculation.
# These represent the average tokens saved by using Serena vs file-based ops.
TOKEN_ESTIMATES = {
    "get_symbols_overview": {"serena": 30, "file_equiv": 500},
    "find_symbol": {"serena": 60, "file_equiv": 400},
    "find_referencing_symbols": {"serena": 80, "file_equiv": 600},
    "find_referencing_code_snippets": {"serena": 120, "file_equiv": 800},
    "search_for_pattern": {"serena": 50, "file_equiv": 300},
    "replace_symbol_body": {"serena": 100, "file_equiv": 1000},
    "insert_after_symbol": {"serena": 80, "file_equiv": 800},
    "insert_before_symbol": {"serena": 80, "file_equiv": 800},
    "insert_at_line": {"serena": 60, "file_equiv": 600},
    "delete_lines": {"serena": 30, "file_equiv": 400},
    "rename_symbol": {"serena": 40, "file_equiv": 500},
}
DEFAULT_ESTIMATE = {"serena": 60, "file_equiv": 500}


def estimate_token_savings(serena_counts: Counter, file_ops: int) -> dict:
    """Estimate token savings from Serena usage vs file-based equivalent."""
    serena_tokens = 0
    equiv_tokens = 0
    for tool, count in serena_counts.items():
        est = TOKEN_ESTIMATES.get(tool, DEFAULT_ESTIMATE)
        serena_tokens += est["serena"] * count
        equiv_tokens += est["file_equiv"] * count

    # File ops: estimate what they cost (these are the actual file reads/writes)
    file_op_tokens = file_ops * DEFAULT_ESTIMATE["file_equiv"]

    actual_total = serena_tokens + file_op_tokens
    baseline_total = equiv_tokens + file_op_tokens

    savings = baseline_total - actual_total
    savings_pct = (savings / baseline_total * 100) if baseline_total > 0 else 0

    return {
        "serena_tokens": serena_tokens,
        "equiv_tokens": equiv_tokens,
        "file_op_tokens": file_op_tokens,
        "actual_total": actual_total,
        "baseline_total": baseline_total,
        "savings": savings,
        "savings_pct": savings_pct,
    }


def format_report(
    serena_counts: Counter,
    file_ops: int,
    scan_dir: str = ".",
    warn_threshold: float = 50,
    min_ops_for_warning: int = 5,
) -> str:
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

    # Token savings estimate
    savings = estimate_token_savings(serena_counts, file_ops)

    lines = [
        "### Serena MCP efficiency",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Serena tool calls | {total_serena} |",
        f"| File-based fallback ops | {file_ops} |",
        f"| **Serena efficiency** | **{efficiency:.0f}%** |",
        f"| Top Serena tools | {top_str} |",
    ]

    if total_serena > 0:
        lines.extend([
            f"| Est. tokens (with Serena) | ~{savings['actual_total']:,} |",
            f"| Est. tokens (without Serena) | ~{savings['baseline_total']:,} |",
            f"| **Est. token savings** | **~{savings['savings']:,} ({savings['savings_pct']:.0f}%)** |",
        ])

    lines.append("")

    # Load Serena's own stats if available
    tool_stats = load_serena_tool_stats(scan_dir)
    if tool_stats:
        lines.append(
            f"> Serena recorded {len(tool_stats)} tool invocations in "
            "`tool_usage_stats.json`."
        )
        lines.append("")

    if total_ops < min_ops_for_warning:
        # Adoption rate is denominator-driven; on no-op or tiny iterations
        # (e.g. single-line follow-ups in a converged autofix loop) the
        # ratio is noise and the red-flag footer is misleading.  Skip
        # both the "not used" and "below threshold" footers — the metrics
        # table above is still emitted so downstream consumers can audit
        # the raw counts.
        pass
    elif efficiency == 0:
        lines.append(
            "> Serena was not used this run. The LLM fell back to "
            "file-based operations (or Serena was unavailable)."
        )
    elif efficiency < warn_threshold:
        lines.append(
            "> Serena adoption is below the configured warning threshold "
            f"({efficiency:.0f}% < {warn_threshold:.0f}%)."
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
    parser.add_argument(
        "--warn-threshold",
        type=float,
        default=50,
        help="Warn when Serena efficiency falls below this percentage (default: 50).",
    )
    parser.add_argument(
        "--min-ops-for-warning",
        type=int,
        default=5,
        help=(
            "Suppress the in-report adoption-warning footer and the "
            "::warning:: stdout annotation when total tool ops is below "
            "this count (default: 5).  Adoption rate is denominator-driven "
            "and tiny-iteration noise should not be flagged.  Values <= 0 "
            "are clamped to the default; otherwise the suppression branch "
            "would never trigger."
        ),
    )
    args = parser.parse_args()
    if args.min_ops_for_warning <= 0:
        args.min_ops_for_warning = 5

    serena_counts, file_ops = scan_directory(args.scan_dir)
    for fpath in args.extra_files:
        if os.path.isfile(fpath):
            c, o = scan_file(fpath)
            serena_counts += c
            file_ops += o

    report = format_report(
        serena_counts,
        file_ops,
        scan_dir=args.scan_dir,
        warn_threshold=args.warn_threshold,
        min_ops_for_warning=args.min_ops_for_warning,
    )
    total_serena = sum(serena_counts.values())
    total_ops = total_serena + file_ops
    efficiency = (total_serena / total_ops * 100) if total_ops > 0 else 0

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)

    # Also print to stdout for workflow logs
    print(report)
    # Use the same `min_ops_for_warning` threshold as the in-report footer
    # so the stdout `::warning::` annotation and the markdown adoption
    # footer fire together (or are suppressed together).  The earlier
    # `max(..., 5)` defensive floor diverged from `format_report` and
    # could leave the footer visible while the stdout warning was
    # suppressed (or vice versa) when an operator passed
    # `--min-ops-for-warning < 5`.
    if total_ops >= args.min_ops_for_warning and efficiency < args.warn_threshold:
        print(
            "::warning::Serena MCP adoption is below threshold "
            f"({file_ops} file-based fallback ops vs {total_serena} Serena tool calls; "
            f"{efficiency:.0f}% efficiency below {args.warn_threshold:.0f}% threshold)."
        )


if __name__ == "__main__":
    main()
