#!/usr/bin/env python3
"""Build weekly workflow-retro context from workflow-log-analysis telemetry."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from ai_memory_lib import read_memory_root_from_branch, resolve_memory_root_dir
except ModuleNotFoundError:
    from scripts.ai_memory_lib import read_memory_root_from_branch, resolve_memory_root_dir

try:
    from cost_audit import aggregate_run_cost_telemetry
except ModuleNotFoundError:
    from scripts.cost_audit import aggregate_run_cost_telemetry

SCHEMA_VERSION = "workflow_retro.v1"
MERGED_PR_LIST_LIMIT = 20
REVIEW_ITERATION_LIST_LIMIT = 20
REVIEW_MISSING_PR_LIST_LIMIT = 20
STALL_REASON_LIMIT = 5
WORKFLOW_FAMILY_LIMIT = 8
GH_CLI_MAX_ATTEMPTS = 5
GH_CLI_RETRYABLE_PATTERN = re.compile(
    r"rate limit|abuse detection|secondary rate|http 429|http 5[0-9]{2}|"
    r"bad gateway|timed out|timeout|connection reset|connection refused|tls handshake timeout",
    re.IGNORECASE,
)
GH_CLI_RATE_LIMIT_PATTERN = re.compile(r"rate limit|abuse detection|secondary rate|http 429", re.IGNORECASE)
GH_CLI_RATE_LIMIT_RESET_PATTERN = re.compile(r"^x-ratelimit-reset:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


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


def _format_iso8601(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"input file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 in {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"unable to read input file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _percentile(values: list[int], pct: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = max(0.0, min(1.0, pct / 100.0)) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return float(sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight)


def _truncate_list(items: list[Any], limit: int) -> tuple[list[Any], int]:
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(items) <= limit:
        return list(items), 0
    return list(items[:limit]), len(items) - limit


def _week_label(window_end_utc: datetime) -> str:
    reference = window_end_utc - timedelta(seconds=1)
    iso_year, iso_week, _ = reference.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _gh_error_text(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    return stderr or stdout or f"gh exited with {exc.returncode}"


def _gh_rate_limit_wait_seconds() -> int:
    try:
        proc = subprocess.run(
            ["gh", "api", "-i", "/rate_limit"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return 30

    match = GH_CLI_RATE_LIMIT_RESET_PATTERN.search(proc.stdout or "")
    if match is None:
        return 30
    try:
        reset_epoch = int(match.group(1))
    except ValueError:
        return 30

    wait_seconds = max(1, reset_epoch - int(time.time()) + 1)
    return min(wait_seconds, 600)


def _gh_retry_delay_seconds(attempt: int, error_text: str) -> int:
    if GH_CLI_RATE_LIMIT_PATTERN.search(error_text):
        return _gh_rate_limit_wait_seconds()
    return min(2 ** max(0, attempt - 1), 30)


def _gh_json(args: list[str], *, stdin_text: str | None = None) -> Any:
    last_error = "gh returned no output"
    for attempt in range(1, GH_CLI_MAX_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                ["gh", *args],
                input=stdin_text,
                capture_output=True,
                check=True,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError as exc:
            raise RuntimeError("gh CLI not found in PATH") from exc
        except subprocess.CalledProcessError as exc:
            last_error = _gh_error_text(exc)
            if attempt < GH_CLI_MAX_ATTEMPTS and GH_CLI_RETRYABLE_PATTERN.search(last_error):
                time.sleep(_gh_retry_delay_seconds(attempt, last_error))
                continue
            raise RuntimeError(last_error) from exc

        stdout = (proc.stdout or "").strip()
        if not stdout:
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gh returned invalid JSON: {exc}") from exc

    raise RuntimeError(last_error)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build bounded weekly-retro context from workflow_log_report.json telemetry."
    )
    parser.add_argument("--report", required=True, help="Path to workflow_log_report.json")
    parser.add_argument("--output", required=True, help="Path to write the markdown retro context")
    parser.add_argument("--json-output", default="", help="Optional path to write normalized retro JSON")
    parser.add_argument("--repo", default="", help="Repository slug to summarize (defaults to GITHUB_REPOSITORY)")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="Repository root for ai-memory branch access")
    parser.add_argument("--lookback-days", type=int, default=7, help="Window size in days")
    parser.add_argument("--now", default="", help="Optional UTC ISO-8601 override for deterministic runs")
    parser.add_argument("--memory-branch", default="", help="Override AI memory branch (defaults to AI_MEMORY_BRANCH or ai-memory)")
    parser.add_argument("--memory-root", default="", help="Override AI memory root (defaults to AI_MEMORY_ROOT or ai-memory)")
    return parser


def _resolve_now_utc(now_override: str | None) -> datetime:
    if now_override:
        parsed = _parse_iso8601(now_override)
        if parsed is None:
            raise ValueError(f"invalid --now value: {now_override}")
        return parsed
    return datetime.now(timezone.utc)


def _resolve_repo(explicit_repo: str) -> str:
    repo = explicit_repo.strip()
    if not repo:
        repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not repo:
        raise ValueError("repository slug required; pass --repo or set GITHUB_REPOSITORY")
    if "/" not in repo:
        raise ValueError(f"repository slug must be owner/repo: {repo}")
    return repo


def load_report(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ValueError("workflow_log_report.json must contain a runs array")
    return payload


def _filtered_runs(
    report: dict[str, Any],
    *,
    repo: str,
    since_utc: datetime,
    until_utc: datetime,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in report.get("runs", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("repository") or "") != repo:
            continue
        created_at = _parse_iso8601(item.get("created_at"))
        if created_at is None:
            continue
        if created_at < since_utc or created_at > until_utc:
            continue
        filtered.append(dict(item))
    filtered.sort(
        key=lambda item: (
            _parse_iso8601(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
            _to_int(item.get("run_id"), 0),
        ),
        reverse=True,
    )
    return filtered


def _summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [_to_int(run.get("duration_seconds"), 0) for run in runs]
    success_count = sum(1 for run in runs if str(run.get("conclusion") or "").lower() == "success")
    failure_count = sum(1 for run in runs if str(run.get("conclusion") or "").lower() == "failure")
    cancelled_count = sum(1 for run in runs if str(run.get("conclusion") or "").lower() == "cancelled")
    other_count = len(runs) - success_count - failure_count - cancelled_count
    average = float(sum(durations) / len(durations)) if durations else 0.0
    return {
        "total_runs": len(runs),
        "success_count": success_count,
        "failure_count": failure_count,
        "cancelled_count": cancelled_count,
        "other_count": other_count,
        "avg_duration_seconds": average,
        "p50_duration_seconds": _percentile(durations, 50),
        "p95_duration_seconds": _percentile(durations, 95),
    }


def _aggregate_workflow_families(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        family = str(run.get("workflow_family") or run.get("_workflow_family") or "other")
        grouped[family].append(run)

    rows: list[dict[str, Any]] = []
    for family, family_runs in grouped.items():
        summary = _summarize_runs(family_runs)
        rows.append(
            {
                "workflow_family": family,
                **summary,
            }
        )

    rows.sort(
        key=lambda item: (
            -_to_int(item.get("failure_count"), 0),
            -_to_int(item.get("total_runs"), 0),
            str(item.get("workflow_family") or ""),
        )
    )
    return rows


def _iter_label_repair_lines(run: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    text_blobs: list[str] = []

    log_summary = run.get("log_summary")
    if isinstance(log_summary, str) and log_summary.strip():
        text_blobs.append(log_summary)

    log_excerpts = run.get("log_excerpts")
    if isinstance(log_excerpts, list):
        for excerpt in log_excerpts:
            if not isinstance(excerpt, dict):
                continue
            text = excerpt.get("excerpt")
            if isinstance(text, str) and text.strip():
                text_blobs.append(text)

    for blob in text_blobs:
        for raw_line in blob.splitlines():
            line = raw_line.strip()
            if "LABEL_REPAIR" not in line or line in seen:
                continue
            seen.add(line)
            lines.append(line[:400])
    return lines


def _extract_log_field(line: str, field: str) -> str:
    marker = f"{field}="
    if marker not in line:
        return ""
    value = line.split(marker, 1)[1].split()[0].strip()
    return value


def _label_repair_bucket(line: str) -> str:
    if "LABEL_REPAIR_DIFF" in line:
        return "label_repair_diff"
    if line.startswith("::warning::LABEL_REPAIR"):
        return "label_repair_failed"

    pr_merged = _extract_log_field(line, "pr_merged").lower()
    pr_state = _extract_log_field(line, "pr_state").lower()
    if pr_merged == "true":
        return "merged_pr_label_repair"
    if pr_state == "open":
        return "open_pr_label_repair"
    if pr_state == "closed":
        return "closed_pr_label_repair"
    if pr_state == "none":
        return "unlinked_issue_label_repair"
    return "other_label_repair_signal"


def _extract_stall_reasons(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}

    for run in runs:
        for line in _iter_label_repair_lines(run):
            bucket = _label_repair_bucket(line)
            counts[bucket] += 1
            samples.setdefault(bucket, line)

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:STALL_REASON_LIMIT]
    return [
        {
            "bucket": bucket,
            "count": count,
            "sample": samples.get(bucket, ""),
        }
        for bucket, count in ranked
    ]


def fetch_merged_pull_requests(
    *,
    repo: str,
    since_utc: datetime,
    until_utc: datetime,
) -> list[dict[str, Any]]:
    # One paginated GraphQL search query covers the full weekly PR window for a
    # repo and avoids the per-PR REST lookups forbidden by the repo's GH API
    # hygiene rules.
    query = """
query($searchQuery: String!, $after: String) {
  search(query: $searchQuery, type: ISSUE, first: 50, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        number
        title
        url
        mergedAt
      }
    }
  }
}
""".strip()

    # GitHub search date qualifiers are day-granular, so extend the end date by
    # one day and keep the exact timestamp bound in the mergedAt post-filter.
    search_query = (
        f"repo:{repo} is:pr is:merged "
        f"merged:{since_utc.date().isoformat()}..{(until_utc + timedelta(days=1)).date().isoformat()} sort:updated-desc"
    )

    merged_prs: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"searchQuery={search_query}",
        ]
        if after:
            args.extend(["-f", f"after={after}"])
        payload = _gh_json(args)
        search_payload = ((payload or {}).get("data") or {}).get("search") if isinstance(payload, dict) else None
        if not isinstance(search_payload, dict):
            raise RuntimeError("GraphQL search response missing data.search")
        nodes = search_payload.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                merged_at_text = str(node.get("mergedAt") or "").strip()
                merged_at = _parse_iso8601(merged_at_text)
                if merged_at is None or merged_at < since_utc or merged_at > until_utc:
                    continue
                merged_prs.append(
                    {
                        "number": _to_int(node.get("number"), 0),
                        "title": str(node.get("title") or "").strip(),
                        "url": str(node.get("url") or "").strip(),
                        "merged_at": _format_iso8601(merged_at),
                    }
                )

        page_info = search_payload.get("pageInfo") if isinstance(search_payload, dict) else None
        if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
            break
        end_cursor = str(page_info.get("endCursor") or "").strip()
        if not end_cursor:
            break
        after = end_cursor

    merged_prs.sort(key=lambda item: (str(item.get("merged_at") or ""), _to_int(item.get("number"), 0)))
    return [item for item in merged_prs if _to_int(item.get("number"), 0) > 0]


def load_ai_memory_run_events(
    *,
    repo_root: Path,
    since_utc: datetime,
    until_utc: datetime,
    memory_branch: str,
    memory_root_relative: str,
) -> list[dict[str, Any]]:
    diagnostics = {"json_decode_errors": 0, "non_dict_payloads": 0}
    load_ai_memory_run_events.last_diagnostics = diagnostics
    branch_dir: Path | None = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=memory_branch,
            memory_root_relative=memory_root_relative,
        )
        memory_root = resolve_memory_root_dir(branch_dir, memory_root_relative)
        runs_dir = memory_root / "runs"
        if not runs_dir.exists():
            return []

        events: list[dict[str, Any]] = []
        for ledger_path in sorted(runs_dir.glob("*/ledger/events.jsonl")):
            try:
                with ledger_path.open("r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            diagnostics["json_decode_errors"] += 1
                            continue
                        if not isinstance(payload, dict):
                            diagnostics["non_dict_payloads"] += 1
                            continue
                        timestamp = _parse_iso8601(payload.get("timestamp"))
                        if timestamp is None or timestamp < since_utc or timestamp > until_utc:
                            continue
                        events.append(payload)
            except OSError:
                continue
        events.sort(key=lambda item: (str(item.get("timestamp") or ""), str(item.get("run_id") or "")))
        return events
    finally:
        if branch_dir is not None:
            shutil.rmtree(branch_dir, ignore_errors=True)


def _build_review_iteration_summary(
    *,
    merged_prs: list[dict[str, Any]],
    run_events: list[dict[str, Any]],
) -> dict[str, Any]:
    review_runs_by_pr: dict[int, set[str]] = defaultdict(set)
    for event in run_events:
        if str(event.get("workflow") or "") != "review_autofix":
            continue
        if str(event.get("event_type") or "") != "phase_started":
            continue
        pr_number = _to_int(event.get("pr_number"), 0)
        run_id = str(event.get("run_id") or "").strip()
        if pr_number <= 0 or not run_id:
            continue
        review_runs_by_pr[pr_number].add(run_id)

    rows: list[dict[str, Any]] = []
    missing_pr_numbers: list[int] = []
    for pr in merged_prs:
        pr_number = _to_int(pr.get("number"), 0)
        iteration_count = len(review_runs_by_pr.get(pr_number, set()))
        if iteration_count <= 0:
            missing_pr_numbers.append(pr_number)
            continue
        rows.append(
            {
                "number": pr_number,
                "title": str(pr.get("title") or "").strip(),
                "url": str(pr.get("url") or "").strip(),
                "iterations": iteration_count,
            }
        )

    rows.sort(key=lambda item: (-_to_int(item.get("iterations"), 0), _to_int(item.get("number"), 0)))
    iteration_counts = [_to_int(item.get("iterations"), 0) for item in rows]
    median_iterations: float | None = None
    if iteration_counts:
        median_iterations = float(statistics.median(iteration_counts))

    return {
        "counted_prs": len(rows),
        "total_review_runs": sum(iteration_counts),
        "median_iterations": median_iterations,
        "max_iterations": max(iteration_counts) if iteration_counts else 0,
        "by_pr": rows,
        "merged_prs_without_review_data": missing_pr_numbers,
    }


def build_weekly_retro_payload(
    report: dict[str, Any],
    *,
    repo: str,
    since_utc: datetime,
    until_utc: datetime,
    repo_root: Path,
    memory_branch: str,
    memory_root_relative: str,
) -> dict[str, Any]:
    runs = _filtered_runs(report, repo=repo, since_utc=since_utc, until_utc=until_utc)
    warnings: list[str] = []
    data_gaps: list[str] = [
        "Reviewer finding categories are not emitted by workflow_log_report.json or ai-memory run ledgers in Phase F v1.",
        "Stall-reason counts come from collector excerpts and log summaries, not full logs for every run in the window.",
    ]

    try:
        merged_prs = fetch_merged_pull_requests(repo=repo, since_utc=since_utc, until_utc=until_utc)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Merged PR query failed open: {exc}")
        merged_prs = []

    try:
        run_events = load_ai_memory_run_events(
            repo_root=repo_root,
            since_utc=since_utc,
            until_utc=until_utc,
            memory_branch=memory_branch,
            memory_root_relative=memory_root_relative,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"AI memory read failed open: {exc}")
        run_events = []

    ledger_diagnostics = getattr(load_ai_memory_run_events, "last_diagnostics", {})
    if _to_int(ledger_diagnostics.get("json_decode_errors"), 0) or _to_int(ledger_diagnostics.get("non_dict_payloads"), 0):
        warnings.append(
            "AI memory run ledger skipped malformed entries "
            f"(json_decode_errors={_to_int(ledger_diagnostics.get('json_decode_errors'), 0)}, non_dict_payloads={_to_int(ledger_diagnostics.get('non_dict_payloads'), 0)})."
        )

    review_iterations = _build_review_iteration_summary(merged_prs=merged_prs, run_events=run_events)
    stall_reasons = _extract_stall_reasons(runs)
    workflow_families = _aggregate_workflow_families(runs)
    summary = _summarize_runs(runs)
    summary["merged_pr_count"] = len(merged_prs)

    review_rows = [item for item in review_iterations.get("by_pr", []) if isinstance(item, dict)]
    missing_review_data = review_iterations.get("merged_prs_without_review_data")
    if not isinstance(missing_review_data, list):
        missing_review_data = []

    bounded_merged_prs, merged_prs_omitted = _truncate_list(merged_prs, MERGED_PR_LIST_LIMIT)
    bounded_review_rows, review_rows_omitted = _truncate_list(review_rows, REVIEW_ITERATION_LIST_LIMIT)
    bounded_missing_review_data, missing_review_data_omitted = _truncate_list(
        missing_review_data,
        REVIEW_MISSING_PR_LIST_LIMIT,
    )
    bounded_workflow_families, workflow_families_omitted = _truncate_list(workflow_families, WORKFLOW_FAMILY_LIMIT)

    if not merged_prs:
        data_gaps.append("No merged pull requests were found in the selected window.")
    if review_iterations["counted_prs"] == 0:
        data_gaps.append("No merged pull request in the window had review_autofix phase_started events in ai-memory.")
    if not stall_reasons:
        data_gaps.append("No LABEL_REPAIR signals were captured in the available collector evidence for this window.")
    if merged_prs_omitted > 0:
        data_gaps.append(
            f"Truncated merged_prs JSON list to the first {MERGED_PR_LIST_LIMIT} entries; "
            f"{merged_prs_omitted} additional merged PR(s) were omitted to keep the retro prompt bounded."
        )
    if review_rows_omitted > 0:
        data_gaps.append(
            f"Truncated review_autofix.by_pr JSON list to the first {REVIEW_ITERATION_LIST_LIMIT} entries; "
            f"{review_rows_omitted} additional PR iteration row(s) were omitted to keep the retro prompt bounded."
        )
    if missing_review_data_omitted > 0:
        data_gaps.append(
            f"Truncated merged_prs_without_review_data JSON list to the first {REVIEW_MISSING_PR_LIST_LIMIT} PR numbers; "
            f"{missing_review_data_omitted} additional PR(s) were omitted to keep the retro prompt bounded."
        )
    if workflow_families_omitted > 0:
        data_gaps.append(
            f"Truncated workflow_families JSON list to the first {WORKFLOW_FAMILY_LIMIT} rows; "
            f"{workflow_families_omitted} additional workflow family row(s) were omitted to keep the retro prompt bounded."
        )

    cost_telemetry = aggregate_run_cost_telemetry(runs)
    judge_cycle_proxy_count = sum(
        1 for run in runs if str(run.get("workflow_family") or run.get("_workflow_family") or "") == "orchestrate_poll"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _format_iso8601(until_utc),
        "repository": repo,
        "window": {
            "since": _format_iso8601(since_utc),
            "until": _format_iso8601(until_utc),
            "lookback_days": int((until_utc - since_utc).total_seconds() // 86400),
            "week_label": _week_label(until_utc),
        },
        # Additive workflow_retro.v1 field: False marks a zero-activity window
        # (no workflow runs and no merged PRs) so the caller can skip the LLM
        # retro pass and the tracker comment for that week.
        "has_activity": bool(
            _to_int(summary.get("total_runs"), 0) > 0 or _to_int(summary.get("merged_pr_count"), 0) > 0
        ),
        "warnings": warnings,
        "data_gaps": data_gaps,
        "bounded_lists": {
            "merged_prs": {
                "limit": MERGED_PR_LIST_LIMIT,
                "returned": len(bounded_merged_prs),
                "omitted": merged_prs_omitted,
            },
            "review_autofix_by_pr": {
                "limit": REVIEW_ITERATION_LIST_LIMIT,
                "returned": len(bounded_review_rows),
                "omitted": review_rows_omitted,
            },
            "review_autofix_missing_prs": {
                "limit": REVIEW_MISSING_PR_LIST_LIMIT,
                "returned": len(bounded_missing_review_data),
                "omitted": missing_review_data_omitted,
            },
            "workflow_families": {
                "limit": WORKFLOW_FAMILY_LIMIT,
                "returned": len(bounded_workflow_families),
                "omitted": workflow_families_omitted,
            },
        },
        "summary": summary,
        "merged_prs": bounded_merged_prs,
        "review_autofix": {
            **review_iterations,
            "by_pr": bounded_review_rows,
            "merged_prs_without_review_data": bounded_missing_review_data,
        },
        "judge_cycle_proxy": {
            "workflow_family": "orchestrate_poll",
            "count": judge_cycle_proxy_count,
        },
        "stall_reasons": stall_reasons,
        "workflow_families": bounded_workflow_families,
        "cost_telemetry": cost_telemetry,
        "reviewer_finding_categories": {
            "available": False,
            "categories": [],
            "note": "Collector and ai-memory inputs do not provide stable reviewer category aggregates in Phase F v1.",
        },
    }


def _fmt_float(value: float | None, *, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def _fmt_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def render_retro_context_markdown(payload: dict[str, Any]) -> str:
    merged_prs = [item for item in payload.get("merged_prs", []) if isinstance(item, dict)]
    review_rows = [item for item in (payload.get("review_autofix") or {}).get("by_pr", []) if isinstance(item, dict)]
    workflow_rows = [item for item in payload.get("workflow_families", []) if isinstance(item, dict)]
    stall_rows = [item for item in payload.get("stall_reasons", []) if isinstance(item, dict)]
    bounds = payload.get("bounded_lists") if isinstance(payload.get("bounded_lists"), dict) else {}
    warnings = [str(item) for item in payload.get("warnings", []) if str(item).strip()]
    data_gaps = [str(item) for item in payload.get("data_gaps", []) if str(item).strip()]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    cost_telemetry = payload.get("cost_telemetry") if isinstance(payload.get("cost_telemetry"), dict) else {}
    review_summary = payload.get("review_autofix") if isinstance(payload.get("review_autofix"), dict) else {}
    judge_cycle_proxy = payload.get("judge_cycle_proxy") if isinstance(payload.get("judge_cycle_proxy"), dict) else {}
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}

    def _bounded_omitted_count(key: str) -> int:
        entry = bounds.get(key)
        if not isinstance(entry, dict):
            return 0
        return _to_int(entry.get("omitted"), 0)

    merged_prs_omitted = _bounded_omitted_count("merged_prs")
    review_rows_omitted = _bounded_omitted_count("review_autofix_by_pr")
    missing_review_data_omitted = _bounded_omitted_count("review_autofix_missing_prs")
    workflow_families_omitted = _bounded_omitted_count("workflow_families")

    lines = [
        "# Weekly Workflow Retro Context",
        "",
        f"- Repository: `{payload.get('repository', '')}`",
        f"- Window: `{window.get('since', '')}` → `{window.get('until', '')}`",
        f"- ISO week label: `{window.get('week_label', '')}`",
        f"- Generated at: `{payload.get('generated_at', '')}`",
        "",
        "## Snapshot",
        f"- Merged PRs: {_to_int(summary.get('merged_pr_count'), 0)}",
        (
            f"- Workflow runs analyzed: {_to_int(summary.get('total_runs'), 0)} "
            f"({_to_int(summary.get('failure_count'), 0)} failures, {_to_int(summary.get('success_count'), 0)} successes)"
        ),
        (
            f"- Review/autofix median iterations: "
            f"{_fmt_float(review_summary.get('median_iterations') if isinstance(review_summary.get('median_iterations'), (int, float)) else None)} "
            f"across {_to_int(review_summary.get('counted_prs'), 0)} merged PR(s) with review data"
        ),
        (
            f"- Judge-cycle proxy (`{judge_cycle_proxy.get('workflow_family', 'orchestrate_poll')}` runs): "
            f"{_to_int(judge_cycle_proxy.get('count'), 0)}"
        ),
        f"- Prompt cache hit rate: {_fmt_percent(cost_telemetry.get('cache_hit_rate') if isinstance(cost_telemetry.get('cache_hit_rate'), (int, float)) else None)}",
        f"- OpenRouter total tokens: {_to_int(cost_telemetry.get('or_total_tokens'), 0)}",
        "",
        "## Merged PRs",
    ]

    if merged_prs:
        lines.append("| PR | Title | Merged At |")
        lines.append("| --- | --- | --- |")
        for pr in merged_prs[:MERGED_PR_LIST_LIMIT]:
            lines.append(
                f"| #{_to_int(pr.get('number'), 0)} | {str(pr.get('title') or '').replace('|', '\\|')} | {pr.get('merged_at', '')} |"
            )
        if merged_prs_omitted > 0:
            lines.append("")
            lines.append(f"- {merged_prs_omitted} additional merged PR(s) omitted for brevity.")
    else:
        lines.append("- None")

    lines.extend(["", "## Review Autofix Iterations"])
    if review_rows:
        lines.append("| PR | Iterations | Title |")
        lines.append("| --- | ---: | --- |")
        for row in review_rows[:REVIEW_ITERATION_LIST_LIMIT]:
            lines.append(
                f"| #{_to_int(row.get('number'), 0)} | {_to_int(row.get('iterations'), 0)} | {str(row.get('title') or '').replace('|', '\\|')} |"
            )
        if review_rows_omitted > 0:
            lines.append("")
            lines.append(f"- {review_rows_omitted} additional PR iteration row(s) omitted for brevity.")
    else:
        lines.append("- No merged PRs in the window had review_autofix iteration data.")

    missing_review_data = review_summary.get("merged_prs_without_review_data")
    if isinstance(missing_review_data, list) and missing_review_data:
        lines.append("")
        lines.append(
            "- Merged PRs without review_autofix iteration data: "
            + ", ".join(f"#{_to_int(value, 0)}" for value in missing_review_data if _to_int(value, 0) > 0)
        )
        if missing_review_data_omitted > 0:
            lines.append(
                f"- {missing_review_data_omitted} additional merged PR(s) without review_autofix data omitted for brevity."
            )

    lines.extend(["", "## Workflow Families"])
    if workflow_rows:
        lines.append("| Family | Runs | Failures | P50 (s) | P95 (s) |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for row in workflow_rows[:WORKFLOW_FAMILY_LIMIT]:
            lines.append(
                "| {family} | {runs} | {failures} | {p50} | {p95} |".format(
                    family=str(row.get("workflow_family") or "other").replace("|", "\\|"),
                    runs=_to_int(row.get("total_runs"), 0),
                    failures=_to_int(row.get("failure_count"), 0),
                    p50=_fmt_float(float(row.get("p50_duration_seconds") or 0.0)),
                    p95=_fmt_float(float(row.get("p95_duration_seconds") or 0.0)),
                )
            )
        if workflow_families_omitted > 0:
            lines.append("")
            lines.append(f"- {workflow_families_omitted} additional workflow family row(s) omitted for brevity.")
    else:
        lines.append("- None")

    lines.extend(["", "## Stall Signals"])
    if stall_rows:
        lines.append("| Bucket | Count | Sample |")
        lines.append("| --- | ---: | --- |")
        for row in stall_rows:
            lines.append(
                f"| {str(row.get('bucket') or '').replace('|', '\\|')} | {_to_int(row.get('count'), 0)} | {str(row.get('sample') or '').replace('|', '\\|')} |"
            )
    else:
        lines.append("- No `LABEL_REPAIR*` signals were captured in the available collector evidence.")

    lines.extend(
        [
            "",
            "## Prompt Cache & Token Telemetry",
            f"- Prompt cache hit rate: {_fmt_percent(cost_telemetry.get('cache_hit_rate') if isinstance(cost_telemetry.get('cache_hit_rate'), (int, float)) else None)}",
            f"- OpenRouter calls: {_to_int(cost_telemetry.get('or_calls'), 0)}",
            f"- OpenRouter total tokens: {_to_int(cost_telemetry.get('or_total_tokens'), 0)}",
            f"- OpenRouter prompt tokens: {_to_int(cost_telemetry.get('or_prompt_tokens'), 0)}",
            f"- OpenRouter completion tokens: {_to_int(cost_telemetry.get('or_completion_tokens'), 0)}",
            f"- Cache write tokens: {_to_int(cost_telemetry.get('or_cache_write_tokens'), 0)}",
            f"- Cache read tokens: {_to_int(cost_telemetry.get('or_cache_read_tokens'), 0)}",
            f"- Break-glass count: {_to_int(cost_telemetry.get('break_glass_count'), 0)}",
            f"- Context-budget warnings: {_to_int(cost_telemetry.get('context_budget_warn_count'), 0)}",
            "",
            "## Data Gaps",
        ]
    )

    for warning in warnings:
        lines.append(f"- Warning: {warning}")
    for gap in data_gaps:
        lines.append(f"- {gap}")
    if not warnings and not data_gaps:
        lines.append("- None")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.lookback_days < 0:
        print("ERROR: --lookback-days must be >= 0", file=sys.stderr)
        return 2

    try:
        now_utc = _resolve_now_utc(args.now)
        repo = _resolve_repo(args.repo)
        report = load_report(Path(args.report))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    since_utc = now_utc - timedelta(days=args.lookback_days)
    repo_root = Path(args.repo_root).resolve()
    memory_branch = args.memory_branch.strip() or os.getenv("AI_MEMORY_BRANCH", "ai-memory").strip() or "ai-memory"
    memory_root_relative = args.memory_root.strip() or os.getenv("AI_MEMORY_ROOT", "ai-memory").strip() or "ai-memory"

    payload = build_weekly_retro_payload(
        report,
        repo=repo,
        since_utc=since_utc,
        until_utc=now_utc,
        repo_root=repo_root,
        memory_branch=memory_branch,
        memory_root_relative=memory_root_relative,
    )

    markdown = render_retro_context_markdown(payload)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
