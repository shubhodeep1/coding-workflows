#!/usr/bin/env python3
"""Assemble an LLM prompt from collected workflow metrics and generate an optimization report.

Reads run_metrics.json and summary_stats.json produced by collect_workflow_logs.py,
assembles a prompt for an LLM via OpenRouter, and writes a dated markdown report
to the analysis/ directory.

Usage:
    python3 scripts/analyze_workflow_logs.py \
        --data-dir /tmp/analysis-data \
        --output analysis/workflow-optimization-2026-04-09.md \
        [--model openai/gpt-5.3-codex] \
        [--max-prompt-tokens 100000]

Environment:
    OPENROUTER_API_KEY   OpenRouter API key (required)
    WORKFLOW_EDITOR_MODEL  Default model override (optional)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "openai/gpt-5.3-codex"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_MAX_RETRIES = 3
_PROMPT_FILE = Path("prompts/mode-workflow-analysis.txt")
_REFERENCE_ANALYSIS = Path("analysis/plan-workflow-log-analysis.md")

# Approximate characters per token (conservative estimate for safety)
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Load the LLM system prompt from prompts/mode-workflow-analysis.txt."""
    if _PROMPT_FILE.exists():
        return _PROMPT_FILE.read_text(encoding="utf-8")
    logger.warning("Prompt file not found: %s — using built-in fallback", _PROMPT_FILE)
    return (
        "You are a GitHub Actions workflow optimization analyst. "
        "Analyze the provided metrics and produce an actionable report covering "
        "speed, cost (token usage), and reliability."
    )


def _load_reference_analysis() -> str:
    """Load the reference analysis document as a few-shot example."""
    if _REFERENCE_ANALYSIS.exists():
        text = _REFERENCE_ANALYSIS.read_text(encoding="utf-8")
        # Truncate to 6000 chars to stay within budget
        if len(text) > 6000:
            text = text[:6000] + "\n\n[... truncated for context budget ...]"
        return text
    return ""


def _format_summary_table(summary: dict) -> str:
    """Format summary_stats.json as a compact markdown table."""
    lines: list[str] = []
    overall = summary.get("overall", {})
    lines.append(f"**Total runs:** {overall.get('count', 'N/A')}")
    lines.append(
        f"**Overall failure rate:** {overall.get('failure_rate', 'N/A')}"
    )
    lines.append("")

    by_phase = summary.get("by_phase", {})
    if by_phase:
        lines.append("| Phase | Runs | Failures | Failure Rate | Avg Duration | P95 Duration | Avg Tokens |")
        lines.append("|-------|------|----------|-------------|-------------|-------------|-----------|")
        for phase, stats in sorted(by_phase.items()):
            avg_d = f"{stats['avg_duration_s']}s" if stats.get("avg_duration_s") else "N/A"
            p95_d = f"{stats['p95_duration_s']}s" if stats.get("p95_duration_s") else "N/A"
            avg_t = str(stats["avg_tokens"]) if stats.get("avg_tokens") else "N/A"
            lines.append(
                f"| {phase} | {stats['count']} | {stats['failure_count']} "
                f"| {stats['failure_rate']:.1%} | {avg_d} | {p95_d} | {avg_t} |"
            )
        lines.append("")

    by_repo = summary.get("by_repo", {})
    if by_repo:
        lines.append("| Repo | Runs | Failure Rate | Avg Duration |")
        lines.append("|------|------|-------------|-------------|")
        for repo, stats in sorted(by_repo.items()):
            avg_d = f"{stats['avg_duration_s']}s" if stats.get("avg_duration_s") else "N/A"
            lines.append(
                f"| {repo} | {stats['count']} | {stats['failure_rate']:.1%} | {avg_d} |"
            )

    return "\n".join(lines)


def _format_run_records(records: list[dict], max_chars: int = 10000) -> str:
    """Format run records as a compact table (truncated to budget)."""
    lines: list[str] = [
        "| Repo | Workflow | Run ID | Phase | Conclusion | Duration | Tokens |",
        "|------|----------|--------|-------|-----------|---------|--------|",
    ]
    for r in records:
        dur = f"{r['duration_s']}s" if r.get("duration_s") is not None else "N/A"
        tok = str(r["token_usage"]) if r.get("token_usage") else "N/A"
        lines.append(
            f"| {r['repo']} | {r['workflow']} | {r['run_id']} "
            f"| {r['phase']} | {r['conclusion']} | {dur} | {tok} |"
        )
        if sum(len(line) for line in lines) > max_chars:
            lines.append("| ... | (truncated) | | | | | |")
            break
    return "\n".join(lines)


def _load_deep_dive_logs(logs_dir: Path, max_chars_total: int = 40000) -> str:
    """Load and concatenate truncated log excerpts from the deep-dive directory."""
    if not logs_dir.exists():
        return ""
    sections: list[str] = []
    total = 0
    for log_file in sorted(logs_dir.iterdir()):
        if not log_file.suffix == ".txt":
            continue
        text = log_file.read_text(encoding="utf-8", errors="replace")
        header = f"\n### Log excerpt: {log_file.stem}\n```\n"
        footer = "\n```\n"
        entry = header + text + footer
        if total + len(entry) > max_chars_total:
            remaining = max_chars_total - total
            if remaining > len(header) + len(footer) + 100:
                entry = header + text[: remaining - len(header) - len(footer)] + footer
                sections.append(entry)
            break
        sections.append(entry)
        total += len(entry)
    return "\n".join(sections)


def assemble_prompt(
    summary: dict,
    records: list[dict],
    logs_dir: Path,
    analysis_date: str,
    repos: list[str],
    lookback_days: int,
    max_prompt_tokens: int,
) -> tuple[str, str]:
    """Build (system_prompt, user_message) for the LLM call."""
    system_prompt = _load_system_prompt()

    budget_chars = max_prompt_tokens * _CHARS_PER_TOKEN
    # Reserve chars for system prompt, reference analysis, and header
    used = len(system_prompt) + 2000  # 2000 for header overhead

    reference = _load_reference_analysis()
    ref_section = ""
    if reference:
        ref_section = (
            "\n## Reference Analysis Example\n\n"
            "The following is a previous analysis report in the expected output format. "
            "Use it as a style and quality reference only — do NOT repeat its findings.\n\n"
            + reference
        )
        used += len(ref_section)

    summary_section = "\n## Summary Statistics\n\n" + _format_summary_table(summary)
    used += len(summary_section)

    # Dynamic budget for run records and logs
    records_budget = min(10000, (budget_chars - used) // 2)
    logs_budget = min(40000, budget_chars - used - records_budget)

    records_section = "\n## Run Records\n\n" + _format_run_records(records, max_chars=max(0, records_budget))
    logs_section = ""
    if logs_dir.exists():
        raw_logs = _load_deep_dive_logs(logs_dir, max_chars_total=max(0, logs_budget))
        if raw_logs:
            logs_section = "\n## Deep-Dive Log Excerpts\n" + raw_logs

    header = (
        f"## Workflow Optimization Analysis Request\n\n"
        f"**Analysis date:** {analysis_date}\n"
        f"**Repos analyzed:** {', '.join(repos) if repos else 'unknown'}\n"
        f"**Lookback window:** {lookback_days} days\n\n"
        "Please produce an optimization report following the format specified in your system prompt.\n"
    )

    user_message = header + summary_section + records_section + logs_section + ref_section
    return system_prompt, user_message


# ---------------------------------------------------------------------------
# OpenRouter API call
# ---------------------------------------------------------------------------

def _call_openrouter(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
) -> str:
    """Call OpenRouter chat/completions and return the assistant's reply."""
    payload = json.dumps(
        {
            "model": model,
            "reasoning_effort": "high",
            "temperature": 0,
            "max_tokens": 16000,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
    ).encode()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/shubhodeep1/coding-workflows",
    }

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                _OPENROUTER_URL, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
                choices = data.get("choices", [])
                if choices:
                    return choices[0]["message"]["content"]
                raise ValueError(f"Unexpected response shape: {data!r}")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            body = exc.read().decode(errors="replace")
            logger.warning(
                "HTTP %s from OpenRouter (attempt %d/%d): %s",
                exc.code,
                attempt + 1,
                _MAX_RETRIES,
                body[:200],
            )
            if exc.code in {429, 500, 502, 503, 504}:
                time.sleep(min(2 ** attempt * 10, 60))
            else:
                raise
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "OpenRouter call failed (attempt %d/%d): %s",
                attempt + 1,
                _MAX_RETRIES,
                exc,
            )
            time.sleep(min(2 ** attempt * 10, 60))
    raise RuntimeError("Exhausted OpenRouter retries") from last_exc


# ---------------------------------------------------------------------------
# Report file management
# ---------------------------------------------------------------------------

def _choose_output_path(output_arg: str | None, analysis_date: str) -> Path:
    """Resolve the output path, appending a counter suffix if the file already exists."""
    if output_arg:
        base = Path(output_arg)
    else:
        base = Path(f"analysis/workflow-optimization-{analysis_date}.md")
    base.parent.mkdir(parents=True, exist_ok=True)
    if not base.exists():
        return base
    # Append counter suffix
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _build_report(
    analysis_date: str,
    repos: list[str],
    lookback_days: int,
    summary: dict,
    model: str,
    llm_content: str,
) -> str:
    """Wrap the LLM output in a standard report header."""
    overall = summary.get("overall", {})
    total = overall.get("count", "N/A")
    return (
        f"# Workflow Optimization Report — {analysis_date}\n\n"
        f"**Analysis window:** {lookback_days} days ending {analysis_date}\n"
        f"**Repos analyzed:** {', '.join(repos)}\n"
        f"**Total runs:** {total}\n"
        f"**Model:** {model} via OpenRouter\n\n"
        "---\n\n"
        + llm_content
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default="/tmp/analysis-data",
        help="Directory containing run_metrics.json and summary_stats.json",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output file path (default: analysis/workflow-optimization-YYYY-MM-DD.md)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="OpenRouter model ID (falls back to WORKFLOW_EDITOR_MODEL env var)",
    )
    parser.add_argument(
        "--max-prompt-tokens",
        type=int,
        default=100000,
        help="Maximum total prompt size in tokens (default: 100000)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Lookback window for report header (informational only)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args(argv)

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        logger.error("OPENROUTER_API_KEY environment variable is required")
        return 1

    model = (
        args.model
        or os.environ.get("WORKFLOW_EDITOR_MODEL", "")
        or _DEFAULT_MODEL
    )

    data_dir = Path(args.data_dir)
    metrics_file = data_dir / "run_metrics.json"
    summary_file = data_dir / "summary_stats.json"

    if not metrics_file.exists():
        logger.error("run_metrics.json not found in %s", data_dir)
        return 1
    if not summary_file.exists():
        logger.error("summary_stats.json not found in %s", data_dir)
        return 1

    records: list[dict] = json.loads(metrics_file.read_text(encoding="utf-8"))
    summary: dict[str, Any] = json.loads(summary_file.read_text(encoding="utf-8"))
    logs_dir = data_dir / "run_logs"

    analysis_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repos = list(summary.get("by_repo", {}).keys()) or ["unknown"]

    logger.info("Assembling prompt (model=%s, max_tokens=%d)", model, args.max_prompt_tokens)
    system_prompt, user_message = assemble_prompt(
        summary=summary,
        records=records,
        logs_dir=logs_dir,
        analysis_date=analysis_date,
        repos=repos,
        lookback_days=args.lookback_days,
        max_prompt_tokens=args.max_prompt_tokens,
    )
    approx_tokens = (len(system_prompt) + len(user_message)) // _CHARS_PER_TOKEN
    logger.info("Estimated prompt size: ~%d tokens", approx_tokens)

    logger.info("Calling OpenRouter API...")
    llm_content = _call_openrouter(system_prompt, user_message, model, api_key)

    report = _build_report(
        analysis_date=analysis_date,
        repos=repos,
        lookback_days=args.lookback_days,
        summary=summary,
        model=model,
        llm_content=llm_content,
    )

    output_path = _choose_output_path(args.output or None, analysis_date)
    output_path.write_text(report, encoding="utf-8")
    logger.info("Report written to %s", output_path)

    # Emit the output path for the workflow to pick up
    print(output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
