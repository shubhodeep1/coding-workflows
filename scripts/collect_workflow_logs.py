#!/usr/bin/env python3
"""Collect GitHub Actions workflow run logs and extract structured metrics.

Fetches completed workflow runs for a set of repositories within a lookback
window, extracts step-level timing via the Jobs API, and optionally downloads
full log zips for the top-N slowest and failed runs for deeper LLM analysis.

Usage:
    python3 scripts/collect_workflow_logs.py \
        --lookback-days 7 \
        [--repos owner/repo1,owner/repo2] \
        [--phases plan,implement,review] \
        [--output-dir /tmp/analysis-data] \
        [--deep-dive-count 10]

Environment:
    GH_TOKEN          GitHub PAT with repo scope (required)
    GITHUB_REPOSITORY Current repo in owner/repo format (optional fallback)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase mapping
# ---------------------------------------------------------------------------

PHASE_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"clarify", re.IGNORECASE), "clarify"),
    (re.compile(r"\bplan\b", re.IGNORECASE), "plan"),
    (re.compile(r"\bimplement\b", re.IGNORECASE), "implement"),
    (re.compile(r"\breview|autofix\b", re.IGNORECASE), "review"),
    (re.compile(r"\bvalidat", re.IGNORECASE), "validate"),
    (re.compile(r"\borchestrat", re.IGNORECASE), "orchestrate"),
]


def map_phase(workflow_name: str) -> str:
    """Map a workflow name to a pipeline phase label."""
    for pattern, phase in PHASE_MAP:
        if pattern.search(workflow_name):
            return phase
    return "other"


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5


def _gh_request(
    path: str,
    token: str,
    *,
    accept: str = "application/vnd.github+json",
    base_url: str = "https://api.github.com",
    stream: bool = False,
) -> Any:
    """Make an authenticated GitHub API GET request with retry/backoff."""
    url = path if path.startswith("http") else f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers)
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if stream:
                    return resp.read()
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in _RETRY_STATUSES:
                raise
            wait = min(2 ** attempt * 5, 60)
            logger.warning(
                "HTTP %s from %s — retrying in %ds (attempt %d/%d)",
                exc.code,
                url,
                wait,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(wait)
        except urllib.error.URLError as exc:
            last_exc = exc
            wait = min(2 ** attempt * 5, 60)
            logger.warning(
                "URLError for %s: %s — retrying in %ds",
                url,
                exc.reason,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"Exhausted retries for {url}") from last_exc


def _paginate_runs(
    repo: str,
    token: str,
    since: str,
    phases_filter: set[str] | None,
) -> list[dict]:
    """Return all completed runs for a repo within the lookback window."""
    runs: list[dict] = []
    page = 1
    while True:
        path = (
            f"/repos/{repo}/actions/runs"
            f"?status=completed&created=>={since}&per_page=100&page={page}"
        )
        try:
            data = _gh_request(path, token)
        except Exception:
            logger.exception("Failed to list runs for %s page %d", repo, page)
            break
        batch = data.get("workflow_runs", [])
        if not batch:
            break
        for run in batch:
            phase = map_phase(run.get("name", ""))
            if phases_filter and phase not in phases_filter:
                continue
            runs.append(run)
        if len(batch) < 100:
            break
        page += 1
    return runs


def _get_jobs(repo: str, run_id: int, token: str) -> list[dict]:
    """Return all jobs for a workflow run."""
    jobs: list[dict] = []
    page = 1
    while True:
        path = f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}"
        try:
            data = _gh_request(path, token)
        except Exception:
            logger.warning("Failed to fetch jobs for %s run %d", repo, run_id)
            break
        batch = data.get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return jobs


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

_DT_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, _DT_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _duration_s(started: str | None, completed: str | None) -> float | None:
    s = _parse_dt(started)
    c = _parse_dt(completed)
    if s and c:
        return (c - s).total_seconds()
    return None


# ---------------------------------------------------------------------------
# Log extraction helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"(?:total_tokens|tokens_used|TOKENS_USED)[^\d]*(\d+)", re.IGNORECASE
)
_MODEL_RE = re.compile(
    r"""(?:MODEL_EDITOR|model)\s*[=:]\s*["']?([\w/.:-]+)""",
    re.IGNORECASE,
)
_THINKING_RE = re.compile(
    r"""(?:MODEL_REASONING_EFFORT|reasoning_effort)\s*[=:]\s*["']?([\w]+)""",
    re.IGNORECASE,
)
_STALL_RE = re.compile(r"(?:stall.*recover|STALL_DETECTED)", re.IGNORECASE)
_AUTOFIX_RE = re.compile(r"\[ai-autofix\]|\[judge-fix\]", re.IGNORECASE)
_FAILURE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"timeout|timed.out", re.IGNORECASE), "timeout"),
    (re.compile(r"rate.limit|429", re.IGNORECASE), "rate_limit"),
    (re.compile(r"merge.conflict|CONFLICT", re.IGNORECASE), "merge_conflict"),
    (re.compile(r"codex.*error|error.*codex", re.IGNORECASE), "codex_error"),
    (re.compile(r"label.*error|error.*label", re.IGNORECASE), "label_error"),
]


def _extract_metrics_from_text(text: str) -> dict:
    """Best-effort extraction of embedded metrics from raw log text."""
    tokens = None
    m = _TOKEN_RE.search(text)
    if m:
        tokens = int(m.group(1))

    model = None
    m = _MODEL_RE.search(text)
    if m:
        model = m.group(1).strip("\"'")

    thinking = None
    m = _THINKING_RE.search(text)
    if m:
        thinking = m.group(1).strip("\"'")

    stall_count = len(_STALL_RE.findall(text))
    autofix_count = len(_AUTOFIX_RE.findall(text))

    failure_category = "other"
    for pattern, category in _FAILURE_PATTERNS:
        if pattern.search(text):
            failure_category = category
            break

    return {
        "token_usage": tokens,
        "model": model,
        "thinking_level": thinking,
        "stall_recoveries": stall_count,
        "autofix_iterations": autofix_count,
        "failure_category": failure_category,
    }


def _download_and_extract_logs(
    repo: str,
    run_id: int,
    token: str,
    output_dir: Path,
) -> str | None:
    """Download the log zip for a run and return a truncated text excerpt."""
    path = f"/repos/{repo}/actions/runs/{run_id}/logs"
    try:
        raw = _gh_request(path, token, stream=True)
    except Exception:
        logger.warning("Failed to download logs for %s run %d", repo, run_id)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            lines: list[str] = []
            for name in zf.namelist():
                with zf.open(name) as fh:
                    content = fh.read().decode("utf-8", errors="replace")
                    lines.extend(content.splitlines())
            # Keep last 200 lines + any stderr lines
            stderr_lines = [ln for ln in lines if "##[error]" in ln or "Error:" in ln]
            tail = lines[-200:]
            excerpt = "\n".join(stderr_lines[:50] + tail)
            # Truncate to ~4KB
            if len(excerpt) > 4096:
                excerpt = excerpt[-4096:]
    except Exception:
        logger.warning("Failed to parse log zip for %s run %d", repo, run_id)
        return None

    out_file = output_dir / f"{repo.replace('/', '__')}_{run_id}.txt"
    out_file.write_text(excerpt, encoding="utf-8")
    return excerpt


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def _compute_summary(records: list[dict]) -> dict:
    """Compute per-phase and per-repo aggregate statistics."""

    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_v = sorted(values)
        idx = (len(sorted_v) - 1) * p / 100.0
        lo, hi = int(idx), min(int(math.ceil(idx)), len(sorted_v) - 1)
        return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (idx - lo)

    def _aggregate(group: list[dict]) -> dict:
        durations = [r["duration_s"] for r in group if r.get("duration_s") is not None]
        tokens = [r["token_usage"] for r in group if r.get("token_usage") is not None]
        failures = sum(1 for r in group if r.get("conclusion") not in ("success", "skipped"))
        return {
            "count": len(group),
            "failure_count": failures,
            "failure_rate": round(failures / len(group), 3) if group else 0.0,
            "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
            "p50_duration_s": round(_percentile(durations, 50), 1) if durations else None,
            "p95_duration_s": round(_percentile(durations, 95), 1) if durations else None,
            "avg_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        }

    # Per-phase
    phases: dict[str, list[dict]] = {}
    for r in records:
        phases.setdefault(r["phase"], []).append(r)

    # Per-repo
    repos: dict[str, list[dict]] = {}
    for r in records:
        repos.setdefault(r["repo"], []).append(r)

    return {
        "total_runs": len(records),
        "by_phase": {phase: _aggregate(items) for phase, items in phases.items()},
        "by_repo": {repo: _aggregate(items) for repo, items in repos.items()},
        "overall": _aggregate(records),
    }


# ---------------------------------------------------------------------------
# Main collection logic
# ---------------------------------------------------------------------------

def collect(
    repos: list[str],
    token: str,
    lookback_days: int,
    phases_filter: set[str] | None,
    output_dir: Path,
    deep_dive_count: int,
) -> tuple[list[dict], dict]:
    """Collect run metrics for all repos and return (records, summary_stats)."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    logs_dir = output_dir / "run_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []

    for repo in repos:
        logger.info("Collecting runs for %s since %s", repo, since)
        try:
            runs = _paginate_runs(repo, token, since, phases_filter)
        except Exception:
            logger.exception("Skipping %s — failed to list runs", repo)
            continue
        logger.info("  Found %d runs", len(runs))

        for run in runs:
            run_id: int = run["id"]
            conclusion: str = run.get("conclusion") or "unknown"
            phase = map_phase(run.get("name", ""))
            run_duration = _duration_s(run.get("started_at"), run.get("updated_at"))

            # Fetch step-level timing via Jobs API
            jobs = _get_jobs(repo, run_id, token)
            steps_data: list[dict] = []
            for job in jobs:
                for step in job.get("steps", []):
                    step_dur = _duration_s(step.get("started_at"), step.get("completed_at"))
                    steps_data.append(
                        {
                            "job": job.get("name", ""),
                            "name": step.get("name", ""),
                            "conclusion": step.get("conclusion"),
                            "duration_s": step_dur,
                        }
                    )

            record: dict = {
                "repo": repo,
                "workflow": run.get("name", ""),
                "run_id": run_id,
                "conclusion": conclusion,
                "phase": phase,
                "duration_s": run_duration,
                "html_url": run.get("html_url"),
                "created_at": run.get("created_at"),
                "steps": steps_data,
                "token_usage": None,
                "model": None,
                "thinking_level": None,
                "stall_recoveries": 0,
                "autofix_iterations": 0,
                "failure_category": None,
                "has_deep_dive_log": False,
            }
            all_records.append(record)

    # Deep-dive: download full logs for top-N slowest + top-N failed
    failed_records = [r for r in all_records if r["conclusion"] not in ("success", "skipped")]
    slow_records = sorted(
        [r for r in all_records if r.get("duration_s") is not None],
        key=lambda r: r["duration_s"],  # type: ignore[return-value]
        reverse=True,
    )

    deep_dive_set: set[tuple[str, int]] = set()
    for r in failed_records[:deep_dive_count]:
        deep_dive_set.add((r["repo"], r["run_id"]))
    for r in slow_records[:deep_dive_count]:
        deep_dive_set.add((r["repo"], r["run_id"]))

    for record in all_records:
        key = (record["repo"], record["run_id"])
        if key not in deep_dive_set:
            continue
        excerpt = _download_and_extract_logs(
            record["repo"], record["run_id"], token, logs_dir
        )
        if excerpt:
            metrics = _extract_metrics_from_text(excerpt)
            record.update(
                {
                    "token_usage": metrics["token_usage"],
                    "model": metrics["model"],
                    "thinking_level": metrics["thinking_level"],
                    "stall_recoveries": metrics["stall_recoveries"],
                    "autofix_iterations": metrics["autofix_iterations"],
                    "failure_category": metrics["failure_category"] if record["conclusion"] not in ("success", "skipped") else None,
                    "has_deep_dive_log": True,
                }
            )

    summary = _compute_summary(all_records)
    return all_records, summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Number of days of history to analyze (default: 7, max: 30)",
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated owner/repo list. Defaults to consumer_repos.json + GITHUB_REPOSITORY.",
    )
    parser.add_argument(
        "--phases",
        default="",
        help="Comma-separated phase filter (clarify,plan,implement,review,validate,orchestrate).",
    )
    parser.add_argument(
        "--output-dir",
        default="/tmp/analysis-data",
        help="Directory for output files (default: /tmp/analysis-data)",
    )
    parser.add_argument(
        "--deep-dive-count",
        type=int,
        default=10,
        help="Number of slowest + failed runs to download full logs for (default: 10).",
    )
    return parser.parse_args(argv)


def _resolve_repos(repos_arg: str) -> list[str]:
    """Resolve the target repo list from CLI arg or consumer_repos.json."""
    if repos_arg.strip():
        return [r.strip() for r in repos_arg.split(",") if r.strip()]
    repos: list[str] = []
    # Add this repo if running in GitHub Actions
    current = os.environ.get("GITHUB_REPOSITORY", "")
    if current:
        repos.append(current)
    # Add consumer repos
    consumer_file = Path(".github/ai/consumer_repos.json")
    if consumer_file.exists():
        try:
            data = json.loads(consumer_file.read_text())
            repos.extend([r for r in data if r not in repos])
        except Exception:
            logger.warning("Failed to parse %s", consumer_file)
    return repos


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args(argv)

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        logger.error("GH_TOKEN environment variable is required")
        return 1

    lookback = min(max(args.lookback_days, 1), 30)
    repos = _resolve_repos(args.repos)
    if not repos:
        logger.error("No repositories to analyze. Provide --repos or set GITHUB_REPOSITORY.")
        return 1

    phases_filter: set[str] | None = None
    if args.phases.strip():
        phases_filter = {p.strip() for p in args.phases.split(",") if p.strip()}

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records, summary = collect(
        repos=repos,
        token=token,
        lookback_days=lookback,
        phases_filter=phases_filter,
        output_dir=output_dir,
        deep_dive_count=args.deep_dive_count,
    )

    metrics_file = output_dir / "run_metrics.json"
    metrics_file.write_text(json.dumps(records, indent=2), encoding="utf-8")
    logger.info("Wrote %d run records to %s", len(records), metrics_file)

    summary_file = output_dir / "summary_stats.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Wrote summary stats to %s", summary_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
