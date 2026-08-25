#!/usr/bin/env python3
"""cost_audit.py — Per-workflow LLM token-spend audit from GitHub Actions logs.

Pulls the last N runs per workflow via `gh`, fetches each run's log, and
aggregates token usage per workflow. Four patterns are recognised:

  1. Codex CLI ("tokens used\\n<N>") — emitted by clarify, plan, implement,
     validate, orchestrate*, orchestrate_clarify_respond.
  2. OpenRouter structured line ("INFO: openrouter usage ... prompt_tokens=N
     completion_tokens=N total_tokens=N cache_creation_input_tokens=N
     cache_read_input_tokens=N") — emitted by review_autofix via
     scripts/review_run_reviewers.sh.
  3. Semble telemetry (`SEMBLE_QUERY ... bytes=N` and
     `SEMBLE_FALLBACK ...`) — emitted by Semble-backed prompt-context
     helpers, with optional additive `context=contract-test` markers
     for fixture-only fallbacks.
  4. Serena / generic MCP telemetry (`SERENA_QUERY`, `SERENA_FALLBACK`,
     `SERENA_PROBE`, plus `<NAME>_QUERY|FALLBACK|PROBE` for other MCP
     servers).

Output: stdout markdown table + per-run JSON file.

Requires only `gh` (authenticated) and Python 3.8+. The repo does NOT need to
be cloned locally; everything is fetched through the GitHub API.

Usage:
    python3 cost_audit.py [--repo OWNER/REPO] [--limit N] \\
        [--workflows w1.yml,w2.yml] [--since YYYY-MM-DD] [--json out.json]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_WORKFLOWS = [
    "clarify.yml",
    "plan.yml",
    "implement.yml",
    "validate.yml",
    "review_autofix.yml",
    "orchestrate.yml",
    "orchestrate_poll.yml",
    "orchestrate_clarify_respond.yml",
]

# Multi-line "tokens used\n<digits>" emitted by Codex; we capture the number
# on the line following "tokens used" (case-insensitive). The single-line
# variant "tokens used 123,456" is also handled.
CODEX_TOKENS_RE = re.compile(
    r"tokens\s+used[^0-9\n]*\n?\s*([0-9][0-9,]*)", re.IGNORECASE
)

# OpenRouter structured usage line — strict field order matches the producers
# in scripts/review_run_reviewers.sh and direct HTTP callers using
# scripts/openrouter_prompt_cache.py. Numeric fields tolerate the producers'
# fail-open placeholders when a value is unavailable.
_NUM = r"(?:[0-9][0-9,]*|na|null|none|-|\?)"
OPENROUTER_RE = re.compile(
    r"INFO:\s*openrouter\s+usage\s+"
    r"phase=(?P<phase>\S+)\s+"
    r"call=(?P<call>\S+)\s+"
    r"model=(?P<model>\S+)\s+"
    r"cache_enabled=\S+\s+"
    r"cache_breakpoint_enabled=\S+\s+"
    r"cache_breakpoint_fallback_retry=\S+\s+"
    r"prompt_tokens=(?P<pt>" + _NUM + r")\s+"
    r"completion_tokens=(?P<ct>" + _NUM + r")\s+"
    r"total_tokens=(?P<tt>" + _NUM + r")\s+"
    r"cache_creation_input_tokens=(?P<cw>" + _NUM + r")\s+"
    r"cache_read_input_tokens=(?P<cr>" + _NUM + r")",
    re.IGNORECASE,
)

SEMBLE_QUERY_RE = re.compile(r"(?:^|\s)SEMBLE_QUERY(?:\s|$)")
SEMBLE_FALLBACK_RE = re.compile(r"(?:^|\s)SEMBLE_FALLBACK(?:\s|$)")
SERENA_QUERY_RE = re.compile(r"(?:^|\s)SERENA_QUERY(?:\s|$)")
SERENA_FALLBACK_RE = re.compile(r"(?:^|\s)SERENA_FALLBACK(?:\s|$)")
SERENA_PROBE_RE = re.compile(r"(?:^|\s)SERENA_PROBE(?:\s|$)")
MCP_QUERY_GENERIC_RE = re.compile(
    r"(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_QUERY(?:\s|$)"
)
MCP_FALLBACK_GENERIC_RE = re.compile(
    r"(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_FALLBACK(?:\s|$)"
)
MCP_PROBE_GENERIC_RE = re.compile(
    r"(?:^|\s)(?P<server>[A-Z][A-Z0-9_]*)_PROBE(?:\s|$)"
)

KNOWN_MCP_SERVERS = frozenset({"SEMBLE", "SERENA"})
BREAK_GLASS_RE = re.compile(r"(?:^|\s)BREAK_GLASS:(?:\s|$)")
CONTEXT_BUDGET_WARN_RE = re.compile(
    r"(?:^|\s)CONTEXT_BUDGET_WARN:\s+"
    r"phase=(?P<phase>\S+)\s+"
    r"prompt_tokens=(?P<prompt_tokens>\d+)\s+"
    r"model_context_window=(?P<model_context_window>\d+)\s+"
    r"ratio=(?P<ratio>\d+(?:\.\d+)?)\s+"
    r"threshold=(?P<threshold>\d+)(?:\s|$)"
)
DEFAULT_CONTEXT_BUDGET_WARN_RATIO = 0.7
DEFAULT_MODEL_CATALOG_PATH = Path(__file__).resolve().with_name("codex_model_catalog.json")
RUN_COST_TELEMETRY_FIELDS = (
    "codex_tokens_used",
    "codex_calls",
    "or_prompt_tokens",
    "or_completion_tokens",
    "or_total_tokens",
    "or_cache_write_tokens",
    "or_cache_read_tokens",
    "or_calls",
    "semble_query_calls",
    "semble_query_bytes",
    "semble_fallbacks",
    "semble_contract_test_fallbacks",
    "semble_runtime_fallbacks",
    "serena_query_calls",
    "serena_query_response_bytes",
    "serena_query_tool_calls",
    "serena_query_ms",
    "serena_fallbacks",
    "serena_probe_ok",
    "serena_probe_failed",
    "serena_probe_skipped",
    "break_glass_count",
    "context_budget_warn_count",
    "cache_hit_rate",
    "wall_clock_p50_ms",
    "wall_clock_p99_ms",
)
AGGREGATABLE_COST_FIELDS = (
    "codex_tokens_used",
    "codex_calls",
    "or_prompt_tokens",
    "or_completion_tokens",
    "or_total_tokens",
    "or_cache_write_tokens",
    "or_cache_read_tokens",
    "or_calls",
    "semble_query_calls",
    "semble_query_bytes",
    "semble_fallbacks",
    "semble_contract_test_fallbacks",
    "semble_runtime_fallbacks",
    "serena_query_calls",
    "serena_query_response_bytes",
    "serena_query_tool_calls",
    "serena_query_ms",
    "serena_fallbacks",
    "serena_probe_ok",
    "serena_probe_failed",
    "serena_probe_skipped",
    "break_glass_count",
    "context_budget_warn_count",
)


def _extract_log_field(line: str, field: str) -> Optional[str]:
    m = re.search(rf"(?:^|\s){re.escape(field)}=([^\s]+)", line)
    return m.group(1) if m else None


def _is_valid_mcp_numeric_field(line: str, field: str) -> bool:
    value = _extract_log_field(line, field)
    return value is not None and re.fullmatch(r"[0-9]+", value) is not None


def _validated_mcp_telemetry_event(line: str) -> Optional[tuple[str, str]]:
    """Return the canonical MCP server/event pair, or None for malformed text."""
    if SEMBLE_QUERY_RE.search(line):
        if (
            _extract_log_field(line, "target")
            and _is_valid_mcp_numeric_field(line, "chunks")
            and _is_valid_mcp_numeric_field(line, "bytes")
            and _is_valid_mcp_numeric_field(line, "ms")
        ):
            return ("SEMBLE", "query")
        return None
    if SEMBLE_FALLBACK_RE.search(line):
        if _extract_log_field(line, "target") and _extract_log_field(line, "reason"):
            return ("SEMBLE", "fallback")
        return None
    if SERENA_QUERY_RE.search(line):
        if (
            _extract_log_field(line, "target")
            and _extract_log_field(line, "tool")
            and _is_valid_mcp_numeric_field(line, "calls")
            and _is_valid_mcp_numeric_field(line, "response_bytes")
            and _is_valid_mcp_numeric_field(line, "ms")
        ):
            return ("SERENA", "query")
        return None
    if SERENA_FALLBACK_RE.search(line):
        if _extract_log_field(line, "target") and _extract_log_field(line, "reason"):
            return ("SERENA", "fallback")
        return None
    if SERENA_PROBE_RE.search(line):
        result = (_extract_log_field(line, "result") or "").lower()
        if _extract_log_field(line, "target") and result in ("ok", "failed", "skipped"):
            return ("SERENA", "probe")
        return None

    for regex, kind in (
        (MCP_QUERY_GENERIC_RE, "query"),
        (MCP_FALLBACK_GENERIC_RE, "fallback"),
        (MCP_PROBE_GENERIC_RE, "probe"),
    ):
        match = regex.search(line)
        if not match:
            continue
        server = match.group("server")
        if server in KNOWN_MCP_SERVERS:
            return None
        if not _extract_log_field(line, "target"):
            return None
        if kind == "query":
            if not (
                _is_valid_mcp_numeric_field(line, "bytes")
                or _is_valid_mcp_numeric_field(line, "response_bytes")
            ):
                return None
        elif kind == "fallback":
            if not _extract_log_field(line, "reason"):
                return None
        else:
            result = (_extract_log_field(line, "result") or "").lower()
            if result not in ("ok", "failed", "skipped"):
                return None
        return (server, kind)
    return None


# Public alias for cross-module collectors; keep the underscored name for compatibility.
validated_mcp_telemetry_event = _validated_mcp_telemetry_event


def _normalize_log_label(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _is_contract_test_semble_line(line: str) -> bool:
    return _normalize_log_label(_extract_log_field(line, "context")) == "contract-test"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _duration_ms_from_run(run: dict[str, Any]) -> Optional[int]:
    started_at = _parse_iso8601(run.get("startedAt") or run.get("started_at"))
    updated_at = _parse_iso8601(run.get("updatedAt") or run.get("updated_at"))
    if started_at and updated_at:
        return max(int((updated_at - started_at).total_seconds() * 1000), 0)
    return None


def _percentile(values: list[int], pct: int) -> Optional[float]:
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = max(0.0, min(1.0, pct / 100.0)) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def _percentile_int(values: list[int], pct: int) -> Optional[int]:
    value = _percentile(values, pct)
    if value is None:
        return None
    return int(round(value))


def _format_ratio(value: float) -> str:
    formatted = f"{value:.4f}".rstrip("0").rstrip(".")
    return formatted or "0"


def estimate_prompt_tokens_from_bytes(prompt_bytes: int) -> int:
    prompt_bytes = max(_to_int(prompt_bytes, 0), 0)
    return (prompt_bytes + 3) // 4


def compute_cache_hit_rate(values: dict[str, Any]) -> Optional[float]:
    prompt_tokens = _to_int(values.get("or_prompt_tokens"), 0)
    cache_write_tokens = _to_int(values.get("or_cache_write_tokens"), 0)
    cache_read_tokens = _to_int(values.get("or_cache_read_tokens"), 0)
    denominator = prompt_tokens + cache_write_tokens + cache_read_tokens
    if denominator <= 0:
        return None
    ratio = cache_read_tokens / denominator
    ratio = max(0.0, min(1.0, ratio))
    return round(ratio, 6)


@lru_cache(maxsize=4)
def _load_model_catalog(catalog_path: str) -> dict[str, dict[str, Any]]:
    path = Path(catalog_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    models = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        return {}

    indexed: dict[str, dict[str, Any]] = {}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            indexed[slug] = entry
    return indexed


def get_model_context_window(model: str, catalog_path: str | None = None) -> Optional[int]:
    if not model:
        return None
    resolved_catalog = str(Path(catalog_path) if catalog_path else DEFAULT_MODEL_CATALOG_PATH)
    entry = _load_model_catalog(resolved_catalog).get(model)
    if not isinstance(entry, dict):
        return None
    context_window = _to_int(entry.get("context_window"), 0)
    return context_window if context_window > 0 else None


def _phase_override_keys(phase: str) -> list[str]:
    normalized_phase = re.sub(r"[^A-Za-z0-9]+", "_", phase.strip().upper()).strip("_")
    keys = []
    if normalized_phase:
        keys.append(f"MAX_PROMPT_TOKENS_FOR_{normalized_phase}")
    keys.append("MAX_PROMPT_TOKENS_FOR_PHASE")
    return keys


def resolve_context_budget_warn_threshold(
    phase: str,
    model_context_window: int,
    *,
    env: dict[str, str] | None = None,
    ratio: float | None = None,
) -> tuple[int, float]:
    env_map = env if env is not None else dict(os.environ)  # type: ignore[name-defined]
    ratio_value = ratio
    if ratio_value is None:
        ratio_value = _to_float(
            env_map.get("CONTEXT_BUDGET_WARN_RATIO", str(DEFAULT_CONTEXT_BUDGET_WARN_RATIO)),
            DEFAULT_CONTEXT_BUDGET_WARN_RATIO,
        )
    if ratio_value <= 0:
        ratio_value = DEFAULT_CONTEXT_BUDGET_WARN_RATIO

    for key in _phase_override_keys(phase):
        override_raw = env_map.get(key)
        override_value = _to_int(override_raw, 0)
        if override_value > 0:
            return override_value, ratio_value

    return max(int(model_context_window * ratio_value), 1), ratio_value


def build_context_budget_warning(
    *,
    phase: str,
    prompt_tokens: int,
    model: str,
    catalog_path: str | None = None,
    env: dict[str, str] | None = None,
    ratio: float | None = None,
) -> dict[str, Any] | None:
    if not phase or prompt_tokens <= 0 or not model:
        return None

    model_context_window = get_model_context_window(model, catalog_path=catalog_path)
    if model_context_window is None:
        return None

    threshold, _configured_ratio = resolve_context_budget_warn_threshold(
        phase,
        model_context_window,
        env=env,
        ratio=ratio,
    )
    if prompt_tokens <= threshold:
        return None

    actual_ratio = prompt_tokens / model_context_window
    return {
        "phase": phase,
        "prompt_tokens": prompt_tokens,
        "model_context_window": model_context_window,
        "ratio": round(actual_ratio, 6),
        "threshold": threshold,
    }


def format_context_budget_warn_line(warning: dict[str, Any] | None) -> str | None:
    if not isinstance(warning, dict):
        return None
    return (
        "CONTEXT_BUDGET_WARN: "
        f"phase={warning['phase']} "
        f"prompt_tokens={warning['prompt_tokens']} "
        f"model_context_window={warning['model_context_window']} "
        f"ratio={_format_ratio(_to_float(warning.get('ratio'), 0.0))} "
        f"threshold={warning['threshold']}"
    )


def build_context_budget_warn_line_for_file(
    *,
    phase: str,
    prompt_path: str | Path,
    model: str,
    catalog_path: str | None = None,
    env: dict[str, str] | None = None,
    ratio: float | None = None,
) -> str | None:
    try:
        prompt_bytes = Path(prompt_path).stat().st_size
    except OSError:
        return None

    warning = build_context_budget_warning(
        phase=phase,
        prompt_tokens=estimate_prompt_tokens_from_bytes(prompt_bytes),
        model=model,
        catalog_path=catalog_path,
        env=env,
        ratio=ratio,
    )
    return format_context_budget_warn_line(warning)


def build_run_cost_telemetry(log: str, *, fallback_wall_clock_ms: int | None = None) -> dict[str, Any]:
    parsed = parse_log(log, fallback_wall_clock_ms=fallback_wall_clock_ms)
    telemetry = {field: parsed[field] for field in RUN_COST_TELEMETRY_FIELDS}
    telemetry["log_parsed"] = True
    return telemetry


def aggregate_run_cost_telemetry(runs: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = {
        "runs_with_log_telemetry": 0,
        "wall_clock_sample_count": 0,
        "cache_hit_rate": None,
        "wall_clock_p50_ms": None,
        "wall_clock_p99_ms": None,
        "wall_clock_samples_ms": [],
    }
    for field in AGGREGATABLE_COST_FIELDS:
        aggregate[field] = 0

    for run in runs:
        telemetry = run.get("cost_telemetry")
        if not isinstance(telemetry, dict) or telemetry.get("log_parsed") is not True:
            continue
        aggregate["runs_with_log_telemetry"] += 1
        for field in AGGREGATABLE_COST_FIELDS:
            aggregate[field] += _to_int(telemetry.get(field), 0)
        sample_ms = _to_int(telemetry.get("wall_clock_p99_ms"), 0)
        if sample_ms <= 0:
            sample_ms = _to_int(telemetry.get("wall_clock_p50_ms"), 0)
        if sample_ms > 0:
            aggregate["wall_clock_samples_ms"].append(sample_ms)

    aggregate["cache_hit_rate"] = compute_cache_hit_rate(aggregate)
    aggregate["wall_clock_sample_count"] = len(aggregate["wall_clock_samples_ms"])
    aggregate["wall_clock_p50_ms"] = _percentile_int(aggregate["wall_clock_samples_ms"], 50)
    aggregate["wall_clock_p99_ms"] = _percentile_int(aggregate["wall_clock_samples_ms"], 99)
    aggregate.pop("wall_clock_samples_ms", None)
    return aggregate


def gh(args: List[str]) -> Optional[str]:
    """Run gh and return stdout, or None on failure (stderr to our stderr)."""
    try:
        r = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True
        )
        return r.stdout
    except FileNotFoundError:
        sys.stderr.write("ERROR: `gh` not found in PATH. Install GitHub CLI.\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            f"  gh failed ({' '.join(args)[:80]}...): "
            f"{(e.stderr or '').strip()[:200]}\n"
        )
        return None


def list_runs(repo: str, workflow: str, limit: int, since: Optional[str]) -> List[dict]:
    out = gh([
        "run", "list",
        "-R", repo,
        "--workflow", workflow,
        "--limit", str(limit),
        "--json",
        "databaseId,workflowName,createdAt,startedAt,updatedAt,conclusion,event,headBranch,status",
    ])
    if not out:
        return []
    runs = json.loads(out)
    if since:
        runs = [r for r in runs if r.get("createdAt", "") >= since]
    return runs


def parse_log(log: str, *, fallback_wall_clock_ms: int | None = None) -> dict:
    """Return aggregated token counts and per-call breakdown for one run."""
    out = {
        "codex_tokens_used": 0,
        "codex_calls": 0,
        "or_prompt_tokens": 0,
        "or_completion_tokens": 0,
        "or_total_tokens": 0,
        "or_cache_write_tokens": 0,
        "or_cache_read_tokens": 0,
        "or_calls": 0,
        "or_phases": defaultdict(lambda: defaultdict(int)),
        "semble_query_calls": 0,
        "semble_query_bytes": 0,
        "semble_fallbacks": 0,
        "semble_contract_test_fallbacks": 0,
        "semble_runtime_fallbacks": 0,
        "semble_targets": defaultdict(lambda: defaultdict(int)),
        "serena_query_calls": 0,
        "serena_query_response_bytes": 0,
        "serena_query_tool_calls": 0,
        "serena_query_ms": 0,
        "serena_fallbacks": 0,
        "serena_probe_ok": 0,
        "serena_probe_failed": 0,
        "serena_probe_skipped": 0,
        "serena_targets": defaultdict(lambda: defaultdict(int)),
        "serena_tools": defaultdict(lambda: defaultdict(int)),
        "other_mcp": defaultdict(lambda: defaultdict(int)),
        "break_glass_count": 0,
        "context_budget_warn_count": 0,
        "cache_hit_rate": None,
        "wall_clock_p50_ms": None,
        "wall_clock_p99_ms": None,
    }
    wall_clock_samples_ms: list[int] = []

    for m in CODEX_TOKENS_RE.finditer(log):
        try:
            out["codex_tokens_used"] += int(m.group(1).replace(",", ""))
            out["codex_calls"] += 1
        except ValueError:
            continue

    for m in OPENROUTER_RE.finditer(log):
        pt = _to_int(m.group("pt"))
        ct = _to_int(m.group("ct"))
        tt = _to_int(m.group("tt"))
        cw = _to_int(m.group("cw"))
        cr = _to_int(m.group("cr"))
        out["or_prompt_tokens"] += pt
        out["or_completion_tokens"] += ct
        out["or_total_tokens"] += tt
        out["or_cache_write_tokens"] += cw
        out["or_cache_read_tokens"] += cr
        out["or_calls"] += 1
        phase = m.group("phase")
        out["or_phases"][phase]["prompt_tokens"] += pt
        out["or_phases"][phase]["completion_tokens"] += ct
        out["or_phases"][phase]["total_tokens"] += tt
        out["or_phases"][phase]["calls"] += 1

    for line in log.splitlines():
        if BREAK_GLASS_RE.search(line):
            out["break_glass_count"] += 1

        if CONTEXT_BUDGET_WARN_RE.search(line):
            out["context_budget_warn_count"] += 1

        validated_mcp_event = validated_mcp_telemetry_event(line)
        if validated_mcp_event == ("SEMBLE", "query"):
            target = _extract_log_field(line, "target") or "unknown"
            logged_bytes = _to_int(_extract_log_field(line, "bytes") or "0")
            out["semble_query_calls"] += 1
            out["semble_query_bytes"] += logged_bytes
            out["semble_targets"][target]["query_calls"] += 1
            out["semble_targets"][target]["bytes"] += logged_bytes
        elif validated_mcp_event == ("SEMBLE", "fallback"):
            target = _extract_log_field(line, "target") or "unknown"
            out["semble_fallbacks"] += 1
            out["semble_targets"][target]["fallbacks"] += 1
            if _is_contract_test_semble_line(line):
                out["semble_contract_test_fallbacks"] += 1
                out["semble_targets"][target]["contract_test_fallbacks"] += 1
            else:
                out["semble_runtime_fallbacks"] += 1
                out["semble_targets"][target]["runtime_fallbacks"] += 1
        elif validated_mcp_event == ("SERENA", "query"):
            target = _extract_log_field(line, "target") or "unknown"
            tool = _extract_log_field(line, "tool") or "unknown"
            response_bytes = _to_int(_extract_log_field(line, "response_bytes") or "0")
            tool_calls = _to_int(_extract_log_field(line, "calls") or "0")
            ms = _to_int(_extract_log_field(line, "ms") or "0")
            out["serena_query_calls"] += 1
            out["serena_query_response_bytes"] += response_bytes
            out["serena_query_tool_calls"] += tool_calls
            out["serena_query_ms"] += ms
            out["serena_targets"][target]["query_calls"] += 1
            out["serena_targets"][target]["response_bytes"] += response_bytes
            out["serena_targets"][target]["tool_calls"] += tool_calls
            out["serena_targets"][target]["ms"] += ms
            out["serena_tools"][tool]["calls"] += tool_calls
            out["serena_tools"][tool]["response_bytes"] += response_bytes
            out["serena_tools"][tool]["ms"] += ms
        elif validated_mcp_event == ("SERENA", "fallback"):
            target = _extract_log_field(line, "target") or "unknown"
            out["serena_fallbacks"] += 1
            out["serena_targets"][target]["fallbacks"] += 1
        elif validated_mcp_event == ("SERENA", "probe"):
            target = _extract_log_field(line, "target") or "unknown"
            result = (_extract_log_field(line, "result") or "").lower()
            if result not in ("ok", "failed", "skipped"):
                continue
            out[f"serena_probe_{result}"] += 1
            out["serena_targets"][target][f"probe_{result}"] += 1
        elif validated_mcp_event is not None:
            server, kind = validated_mcp_event
            if kind == "query":
                out["other_mcp"][server]["query_calls"] += 1
                bytes_value = _extract_log_field(line, "bytes")
                if bytes_value is not None:
                    out["other_mcp"][server]["query_bytes"] += _to_int(bytes_value)
                response_bytes = _extract_log_field(line, "response_bytes")
                if response_bytes is not None:
                    out["other_mcp"][server]["query_response_bytes"] += _to_int(
                        response_bytes
                    )
            elif kind == "fallback":
                out["other_mcp"][server]["fallbacks"] += 1
            else:
                result = (_extract_log_field(line, "result") or "").lower()
                if result not in ("ok", "failed", "skipped"):
                    continue
                out["other_mcp"][server][f"probe_{result}"] += 1

    if fallback_wall_clock_ms and fallback_wall_clock_ms > 0:
        wall_clock_samples_ms.append(fallback_wall_clock_ms)

    out["cache_hit_rate"] = compute_cache_hit_rate(out)
    out["wall_clock_p50_ms"] = _percentile_int(wall_clock_samples_ms, 50)
    out["wall_clock_p99_ms"] = _percentile_int(wall_clock_samples_ms, 99)

    out["or_phases"] = {p: dict(v) for p, v in out["or_phases"].items()}
    out["semble_targets"] = {p: dict(v) for p, v in out["semble_targets"].items()}
    out["serena_targets"] = {p: dict(v) for p, v in out["serena_targets"].items()}
    out["serena_tools"] = {p: dict(v) for p, v in out["serena_tools"].items()}
    out["other_mcp"] = {s: dict(v) for s, v in out["other_mcp"].items()}
    return out


def fmt(n: int) -> str:
    return f"{n:,}" if n else "-"


def fmt_rate(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1%}"


def fmt_ms(value: int | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--repo", default="shubhodeep1/coding-workflows",
                    help="owner/repo (default: %(default)s)")
    ap.add_argument("--limit", type=int, default=5,
                    help="runs per workflow (default: 5; logs are big — start small)")
    ap.add_argument("--workflows", default=",".join(DEFAULT_WORKFLOWS),
                    help="comma-separated workflow files (default: all AI workflows)")
    ap.add_argument("--since", help="ISO date YYYY-MM-DD; skip runs older than this")
    ap.add_argument("--json", default="cost_audit_report.json",
                    help="output JSON path (default: %(default)s)")
    args = ap.parse_args()

    workflows = [w.strip() for w in args.workflows.split(",") if w.strip()]
    per_wf: Dict[str, dict] = {}
    per_run: List[dict] = []

    for wf in workflows:
        sys.stderr.write(f"\n=== {wf} ===\n")
        runs = list_runs(args.repo, wf, args.limit, args.since)
        sys.stderr.write(f"  {len(runs)} run(s)\n")

        agg = {
            "run_count": len(runs),
            "runs_with_data": 0,
            "codex_tokens_used": 0,
            "codex_calls": 0,
            "or_prompt_tokens": 0,
            "or_completion_tokens": 0,
            "or_total_tokens": 0,
            "or_cache_write_tokens": 0,
            "or_cache_read_tokens": 0,
            "or_calls": 0,
            "or_phases": defaultdict(lambda: defaultdict(int)),
            "semble_query_calls": 0,
            "semble_query_bytes": 0,
            "semble_fallbacks": 0,
            "semble_contract_test_fallbacks": 0,
            "semble_runtime_fallbacks": 0,
            "semble_targets": defaultdict(lambda: defaultdict(int)),
            "serena_query_calls": 0,
            "serena_query_response_bytes": 0,
            "serena_query_tool_calls": 0,
            "serena_query_ms": 0,
            "serena_fallbacks": 0,
            "serena_probe_ok": 0,
            "serena_probe_failed": 0,
            "serena_probe_skipped": 0,
            "serena_targets": defaultdict(lambda: defaultdict(int)),
            "serena_tools": defaultdict(lambda: defaultdict(int)),
            "other_mcp": defaultdict(lambda: defaultdict(int)),
            "break_glass_count": 0,
            "context_budget_warn_count": 0,
            "cache_hit_rate": None,
            "wall_clock_p50_ms": None,
            "wall_clock_p99_ms": None,
            "wall_clock_samples_ms": [],
        }

        for i, r in enumerate(runs, 1):
            rid = r["databaseId"]
            sys.stderr.write(
                f"  [{i}/{len(runs)}] run {rid} {r.get('createdAt','')[:19]} "
                f"{r.get('conclusion') or r.get('status','?')} ... "
            )
            sys.stderr.flush()
            log = gh(["run", "view", str(rid), "-R", args.repo, "--log"])
            if log is None:
                sys.stderr.write("skip (log unavailable)\n")
                continue
            fallback_wall_clock_ms = _duration_ms_from_run(r)
            parsed = parse_log(log, fallback_wall_clock_ms=fallback_wall_clock_ms)
            if (
                parsed["codex_tokens_used"]
                or parsed["or_calls"]
                or parsed["semble_query_calls"]
                or parsed["semble_fallbacks"]
                or parsed["serena_query_calls"]
                or parsed["serena_fallbacks"]
                or parsed["serena_probe_ok"]
                or parsed["serena_probe_failed"]
                or parsed["serena_probe_skipped"]
                or parsed["break_glass_count"]
                or parsed["context_budget_warn_count"]
                or parsed["other_mcp"]
            ):
                agg["runs_with_data"] += 1
            if fallback_wall_clock_ms and fallback_wall_clock_ms > 0:
                agg["wall_clock_samples_ms"].append(fallback_wall_clock_ms)
            for k in ("codex_tokens_used", "codex_calls", "or_prompt_tokens",
                      "or_completion_tokens", "or_total_tokens",
                      "or_cache_write_tokens", "or_cache_read_tokens",
                      "or_calls", "semble_query_calls",
                      "semble_query_bytes", "semble_fallbacks",
                      "semble_contract_test_fallbacks", "semble_runtime_fallbacks",
                      "serena_query_calls", "serena_query_response_bytes",
                      "serena_query_tool_calls", "serena_query_ms",
                      "serena_fallbacks", "serena_probe_ok",
                      "serena_probe_failed", "serena_probe_skipped",
                      "break_glass_count", "context_budget_warn_count"):
                agg[k] += parsed[k]
            for phase, vals in parsed["or_phases"].items():
                for k, v in vals.items():
                    agg["or_phases"][phase][k] += v
            for target, vals in parsed["semble_targets"].items():
                for k, v in vals.items():
                    agg["semble_targets"][target][k] += v
            for target, vals in parsed["serena_targets"].items():
                for k, v in vals.items():
                    agg["serena_targets"][target][k] += v
            for tool, vals in parsed["serena_tools"].items():
                for k, v in vals.items():
                    agg["serena_tools"][tool][k] += v
            for server, vals in parsed["other_mcp"].items():
                for k, v in vals.items():
                    agg["other_mcp"][server][k] += v
            per_run.append({
                "workflow": wf,
                "run_id": rid,
                "created_at": r.get("createdAt"),
                "started_at": r.get("startedAt"),
                "updated_at": r.get("updatedAt"),
                "conclusion": r.get("conclusion"),
                "head_branch": r.get("headBranch"),
                **{k: parsed[k] for k in (
                    "codex_tokens_used", "codex_calls", "or_prompt_tokens",
                    "or_completion_tokens", "or_total_tokens",
                    "or_cache_write_tokens", "or_cache_read_tokens", "or_calls",
                    "semble_query_calls", "semble_query_bytes", "semble_fallbacks",
                    "semble_contract_test_fallbacks", "semble_runtime_fallbacks",
                    "serena_query_calls", "serena_query_response_bytes",
                    "serena_query_tool_calls", "serena_query_ms",
                    "serena_fallbacks", "serena_probe_ok",
                    "serena_probe_failed", "serena_probe_skipped",
                    "break_glass_count", "context_budget_warn_count",
                    "cache_hit_rate", "wall_clock_p50_ms", "wall_clock_p99_ms",
                )},
                "semble_targets": parsed["semble_targets"],
                "serena_targets": parsed["serena_targets"],
                "serena_tools": parsed["serena_tools"],
                "other_mcp": parsed["other_mcp"],
            })
            sys.stderr.write(
                f"codex={fmt(parsed['codex_tokens_used'])} "
                f"or_total={fmt(parsed['or_total_tokens'])} "
                f"or_calls={parsed['or_calls']} "
                f"semble_bytes={fmt(parsed['semble_query_bytes'])} "
                f"semble_fallbacks={fmt(parsed['semble_fallbacks'])} "
                f"semble_contract_test={fmt(parsed['semble_contract_test_fallbacks'])} "
                f"serena_calls={fmt(parsed['serena_query_calls'])} "
                f"serena_fallbacks={fmt(parsed['serena_fallbacks'])} "
                f"serena_probe_failed={fmt(parsed['serena_probe_failed'])} "
                f"cache_hit={fmt_rate(parsed['cache_hit_rate'])} "
                f"wall_p99_ms={fmt_ms(parsed['wall_clock_p99_ms'])} "
                f"break_glass={fmt(parsed['break_glass_count'])} "
                f"context_warn={fmt(parsed['context_budget_warn_count'])} "
                f"other_mcp={len(parsed['other_mcp'])}\n"
            )

        agg["cache_hit_rate"] = compute_cache_hit_rate(agg)
        agg["wall_clock_p50_ms"] = _percentile_int(agg["wall_clock_samples_ms"], 50)
        agg["wall_clock_p99_ms"] = _percentile_int(agg["wall_clock_samples_ms"], 99)
        agg.pop("wall_clock_samples_ms", None)
        agg["or_phases"] = {p: dict(v) for p, v in agg["or_phases"].items()}
        agg["semble_targets"] = {p: dict(v) for p, v in agg["semble_targets"].items()}
        agg["serena_targets"] = {p: dict(v) for p, v in agg["serena_targets"].items()}
        agg["serena_tools"] = {p: dict(v) for p, v in agg["serena_tools"].items()}
        agg["other_mcp"] = {s: dict(v) for s, v in agg["other_mcp"].items()}
        per_wf[wf] = agg

    # Markdown summary on stdout
    print("\n# Cost audit summary\n")
    print(f"- Repo: `{args.repo}`")
    print(f"- Window: last {args.limit} run(s) per workflow"
          + (f" since {args.since}" if args.since else ""))
    print()
    print("| Workflow | runs | with_data | codex_tokens | codex_calls | "
          "or_prompt | or_completion | or_total | or_cache_write | "
          "or_cache_read | or_calls | cache_hit_rate | wall_clock_p50_ms | "
          "wall_clock_p99_ms | break_glass | context_budget_warn |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for wf, a in per_wf.items():
        print(
            f"| {wf} | {a['run_count']} | {a['runs_with_data']} | "
            f"{fmt(a['codex_tokens_used'])} | {fmt(a['codex_calls'])} | "
            f"{fmt(a['or_prompt_tokens'])} | {fmt(a['or_completion_tokens'])} | "
            f"{fmt(a['or_total_tokens'])} | {fmt(a['or_cache_write_tokens'])} | "
            f"{fmt(a['or_cache_read_tokens'])} | {fmt(a['or_calls'])} | "
            f"{fmt_rate(a['cache_hit_rate'])} | {fmt_ms(a['wall_clock_p50_ms'])} | "
            f"{fmt_ms(a['wall_clock_p99_ms'])} | {fmt(a['break_glass_count'])} | "
            f"{fmt(a['context_budget_warn_count'])} |"
        )

    # OpenRouter phase breakdown (review_autofix only emits phases)
    or_workflows = [wf for wf, a in per_wf.items() if a["or_phases"]]
    if or_workflows:
        print("\n## OpenRouter phase breakdown\n")
        for wf in or_workflows:
            print(f"### {wf}\n")
            print("| phase | calls | prompt | completion | total |")
            print("|---|---:|---:|---:|---:|")
            phases = per_wf[wf]["or_phases"]
            for phase in sorted(phases, key=lambda p: -phases[p].get("total_tokens", 0)):
                v = phases[phase]
                print(f"| {phase} | {fmt(v.get('calls', 0))} | "
                      f"{fmt(v.get('prompt_tokens', 0))} | "
                      f"{fmt(v.get('completion_tokens', 0))} | "
                      f"{fmt(v.get('total_tokens', 0))} |")
            print()

    semble_workflows = [
        wf for wf, a in per_wf.items()
        if a["semble_query_calls"] or a["semble_fallbacks"]
    ]
    if semble_workflows:
        print("\n## Semble telemetry breakdown\n")
        print("| Workflow | query_calls | logged_bytes | fallbacks | contract_test_fallbacks | runtime_fallbacks |")
        print("|---|---:|---:|---:|---:|---:|")
        for wf in semble_workflows:
            a = per_wf[wf]
            print(
                f"| {wf} | {fmt(a['semble_query_calls'])} | "
                f"{fmt(a['semble_query_bytes'])} | {fmt(a['semble_fallbacks'])} | "
                f"{fmt(a['semble_contract_test_fallbacks'])} | {fmt(a['semble_runtime_fallbacks'])} |"
            )

        print()
        for wf in semble_workflows:
            targets = per_wf[wf]["semble_targets"]
            if not targets:
                continue
            print(f"### {wf}\n")
            print("| target | query_calls | logged_bytes | fallbacks | contract_test_fallbacks | runtime_fallbacks |")
            print("|---|---:|---:|---:|---:|---:|")
            ordered_targets = sorted(
                targets,
                key=lambda t: (
                    -targets[t].get("bytes", 0),
                    -targets[t].get("query_calls", 0),
                    -targets[t].get("fallbacks", 0),
                    t,
                ),
            )
            for target in ordered_targets:
                vals = targets[target]
                print(
                    f"| {target} | {fmt(vals.get('query_calls', 0))} | "
                    f"{fmt(vals.get('bytes', 0))} | "
                    f"{fmt(vals.get('fallbacks', 0))} | "
                    f"{fmt(vals.get('contract_test_fallbacks', 0))} | "
                    f"{fmt(vals.get('runtime_fallbacks', 0))} |"
                )
            print()

    serena_workflows = [
        wf for wf, a in per_wf.items()
        if (
            a["serena_query_calls"]
            or a["serena_fallbacks"]
            or a["serena_probe_ok"]
            or a["serena_probe_failed"]
            or a["serena_probe_skipped"]
        )
    ]
    if serena_workflows:
        print("\n## Serena telemetry breakdown\n")
        print("| Workflow | query_calls | response_bytes | tool_calls | ms | "
              "fallbacks | probe_ok | probe_failed | probe_skipped |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for wf in serena_workflows:
            a = per_wf[wf]
            print(
                f"| {wf} | {fmt(a['serena_query_calls'])} | "
                f"{fmt(a['serena_query_response_bytes'])} | "
                f"{fmt(a['serena_query_tool_calls'])} | "
                f"{fmt(a['serena_query_ms'])} | "
                f"{fmt(a['serena_fallbacks'])} | "
                f"{fmt(a['serena_probe_ok'])} | "
                f"{fmt(a['serena_probe_failed'])} | "
                f"{fmt(a['serena_probe_skipped'])} |"
            )

        print()
        for wf in serena_workflows:
            targets = per_wf[wf]["serena_targets"]
            tools = per_wf[wf]["serena_tools"]
            print(f"### {wf}\n")
            print("| target | query_calls | response_bytes | tool_calls | ms | fallbacks | "
                  "probe_ok | probe_failed | probe_skipped |")
            print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
            ordered_targets = sorted(
                targets,
                key=lambda t: (
                    -targets[t].get("response_bytes", 0),
                    -targets[t].get("query_calls", 0),
                    t,
                ),
            )
            for target in ordered_targets:
                vals = targets[target]
                print(
                    f"| {target} | {fmt(vals.get('query_calls', 0))} | "
                    f"{fmt(vals.get('response_bytes', 0))} | "
                    f"{fmt(vals.get('tool_calls', 0))} | "
                    f"{fmt(vals.get('ms', 0))} | "
                    f"{fmt(vals.get('fallbacks', 0))} | "
                    f"{fmt(vals.get('probe_ok', 0))} | "
                    f"{fmt(vals.get('probe_failed', 0))} | "
                    f"{fmt(vals.get('probe_skipped', 0))} |"
                )

            if tools:
                print()
                print("#### tools\n")
                print("| tool | calls | response_bytes | ms |")
                print("|---|---:|---:|---:|")
                ordered_tools = sorted(
                    tools,
                    key=lambda t: (
                        -tools[t].get("response_bytes", 0),
                        -tools[t].get("calls", 0),
                        t,
                    ),
                )
                for tool in ordered_tools:
                    vals = tools[tool]
                    print(
                        f"| {tool} | {fmt(vals.get('calls', 0))} | "
                        f"{fmt(vals.get('response_bytes', 0))} | "
                        f"{fmt(vals.get('ms', 0))} |"
                    )
            print()

    other_mcp_workflows = [wf for wf, a in per_wf.items() if a["other_mcp"]]
    if other_mcp_workflows:
        print("\n## Other MCP servers observed\n")
        print("| Workflow | Server | query_calls | query_bytes | query_response_bytes | "
              "fallbacks | probe_ok | probe_failed | probe_skipped |")
        print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for wf in other_mcp_workflows:
            servers = per_wf[wf]["other_mcp"]
            ordered_servers = sorted(
                servers,
                key=lambda s: (
                    -servers[s].get("query_response_bytes", 0),
                    -servers[s].get("query_bytes", 0),
                    -servers[s].get("query_calls", 0),
                    s,
                ),
            )
            for server in ordered_servers:
                vals = servers[server]
                print(
                    f"| {wf} | {server} | {fmt(vals.get('query_calls', 0))} | "
                    f"{fmt(vals.get('query_bytes', 0))} | "
                    f"{fmt(vals.get('query_response_bytes', 0))} | "
                    f"{fmt(vals.get('fallbacks', 0))} | "
                    f"{fmt(vals.get('probe_ok', 0))} | "
                    f"{fmt(vals.get('probe_failed', 0))} | "
                    f"{fmt(vals.get('probe_skipped', 0))} |"
                )

    payload = {
        "repo": args.repo,
        "limit": args.limit,
        "since": args.since,
        "per_workflow": per_wf,
        "per_run": per_run,
    }
    with open(args.json, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    sys.stderr.write(f"\nWrote per-run JSON to {args.json}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
