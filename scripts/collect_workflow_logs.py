#!/usr/bin/env python3
"""Collect GitHub Actions run/job telemetry for core AI workflow families."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

SCHEMA_VERSION = "workflow_log_collector.v2"
LOG_EXCERPT_MAX_CHARS = 4000
SLOW_RUNS_PER_REPO = 10
DEFAULT_SUCCESS_SAMPLE_RATE = 0.07
RETRY_MARKERS = (
    "rate limit",
    "secondary",
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "502",
    "503",
    "504",
)


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


def _format_iso8601(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect workflow run logs + reliability/timing metrics into JSON"
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repository in owner/repo format (repeatable). Defaults to GITHUB_REPOSITORY.",
    )
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument(
        "--lookback-days",
        type=int,
        help="Collect runs created within the last N days.",
    )
    window.add_argument(
        "--since",
        help="Collect runs created on/after ISO-8601 timestamp.",
    )
    parser.add_argument(
        "--output",
        default="workflow_log_report.json",
        help="Output report path.",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="GitHub API page size.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum pages fetched per endpoint.",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Optional cap on total runs per repository (0 = unlimited).",
    )
    parser.add_argument(
        "--max-log-runs",
        type=int,
        default=15,
        help="Optional cap on notable runs to fetch raw logs for (0 = disabled).",
    )
    parser.add_argument(
        "--success-sample-rate",
        type=float,
        default=DEFAULT_SUCCESS_SAMPLE_RATE,
        help="Fraction of successful runs to randomly sample for log analysis (default 0.07 = ~7%%).",
    )
    return parser


def gh_api_json(
    endpoint: str,
    *,
    token: str,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    base_env = os.environ.copy()
    if token:
        base_env["GH_TOKEN"] = token

    cmd = [
        "gh",
        "api",
        endpoint,
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=base_env, timeout=60)
        except subprocess.TimeoutExpired as exc:
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
                continue
            raise RuntimeError(f"gh api timed out for {endpoint} after {exc.timeout}s") from exc

        if proc.returncode == 0:
            output = proc.stdout.strip()
            if not output:
                return {}
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON from gh api ({endpoint}): {exc}") from exc
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError(f"Expected JSON object from gh api ({endpoint})")

        stderr_text = (proc.stderr or "").strip()
        stderr_lower = stderr_text.lower()
        retryable = any(marker in stderr_lower for marker in RETRY_MARKERS)
        if retryable and attempt < retries:
            time.sleep(backoff_seconds * attempt)
            continue
        raise RuntimeError(
            f"gh api failed for {endpoint} (exit={proc.returncode}): {stderr_text or proc.stdout.strip()}"
        )

    raise RuntimeError(f"gh api failed for {endpoint} after retries")


def gh_api_bytes(
    endpoint: str,
    *,
    token: str,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> bytes:
    base_env = os.environ.copy()
    if token:
        base_env["GH_TOKEN"] = token

    cmd = [
        "gh",
        "api",
        endpoint,
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
    ]

    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, env=base_env, timeout=300)
        except subprocess.TimeoutExpired as exc:
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
                continue
            raise RuntimeError(f"gh api timed out for {endpoint} after {exc.timeout}s") from exc

        if proc.returncode == 0:
            return proc.stdout

        stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        stderr_lower = stderr_text.lower()
        retryable = any(marker in stderr_lower for marker in RETRY_MARKERS)
        if retryable and attempt < retries:
            time.sleep(backoff_seconds * attempt)
            continue
        stdout_text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"gh api failed for {endpoint} (exit={proc.returncode}): {stderr_text or stdout_text}"
        )

    raise RuntimeError(f"gh api failed for {endpoint} after retries")


def normalize_workflow_family(workflow_name: str | None, workflow_path: str | None) -> str | None:
    name = (workflow_name or "").lower()
    path = (workflow_path or "").lower()
    combined = f"{name} {path}"

    if any(tag in combined for tag in ("clarify_respond", "orchestrate_clarify_respond")):
        return "orchestrate_clarify_respond"
    if any(tag in combined for tag in ("clarify", "ai-clarify", "internal-clarify")):
        return "clarify"
    if any(tag in combined for tag in ("plan", "ai-plan", "internal-plan")):
        return "plan"
    if any(tag in combined for tag in ("implement", "ai-implement", "internal-implement")):
        return "implement"
    if any(
        tag in combined
        for tag in (
            "review_autofix",
            "ai-review",
            "internal-review",
            "review",
        )
    ):
        return "review_autofix"
    if any(tag in combined for tag in ("orchestrate_poll", "orchestrate-poll")):
        return "orchestrate_poll"
    if any(tag in combined for tag in ("orchestrate", "ai-orchestrate", "internal-orchestrate")):
        return "orchestrate"
    if any(tag in combined for tag in ("validate", "ai-validate", "internal-validate")):
        return "validate"
    if any(tag in combined for tag in ("issue_pr_status", "issue-pr-status")):
        return "issue_pr_status"
    if any(tag in combined for tag in ("memory_maintenance", "memory-maintenance")):
        return "memory_maintenance"
    if "ci" in combined:
        return "ci"
    if "workflow-log-analysis" in combined or "workflow_log_analysis" in combined:
        return "workflow_log_analysis"

    # Derive family from workflow path filename as fallback
    if path:
        stem = Path(path).stem.lower()
        stem = re.sub(r"^(ai-|internal-)", "", stem)
        stem = stem.replace("-", "_")
        if stem:
            return stem

    return "other"


def _build_runs_endpoint(repo: str, since_utc: datetime, per_page: int, page: int) -> str:
    query = urlencode(
        {
            "status": "completed",
            "created": f">={_format_iso8601(since_utc)}",
            "per_page": str(per_page),
            "page": str(page),
        }
    )
    return f"repos/{repo}/actions/runs?{query}"


def list_runs_for_repo(
    repo: str,
    *,
    since_utc: datetime,
    per_page: int,
    max_pages: int,
    max_runs: int,
    token: str,
) -> tuple[list[dict[str, Any]], bool]:
    runs: list[dict[str, Any]] = []
    capped = False

    for page in range(1, max_pages + 1):
        payload = gh_api_json(
            _build_runs_endpoint(repo, since_utc, per_page, page),
            token=token,
        )
        page_runs = payload.get("workflow_runs") or []
        if not page_runs:
            break

        for run in page_runs:
            family = normalize_workflow_family(run.get("name"), run.get("path"))
            if family is None:
                continue
            run_copy = dict(run)
            run_copy["_workflow_family"] = family
            runs.append(run_copy)
            if max_runs > 0 and len(runs) >= max_runs:
                capped = True
                return runs, capped

    return runs, capped


def _build_jobs_endpoint(repo: str, run_id: int, per_page: int, page: int) -> str:
    query = urlencode({"filter": "all", "per_page": str(per_page), "page": str(page)})
    return f"repos/{repo}/actions/runs/{run_id}/jobs?{query}"


def _build_logs_endpoint(repo: str, run_id: int) -> str:
    return f"repos/{repo}/actions/runs/{run_id}/logs"


def list_jobs_for_run(
    repo: str,
    run_id: int,
    *,
    per_page: int,
    max_pages: int,
    token: str,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = gh_api_json(
            _build_jobs_endpoint(repo, run_id, per_page, page),
            token=token,
        )
        page_jobs = payload.get("jobs") or []
        if not page_jobs:
            break
        jobs.extend(page_jobs)
    return jobs


def _extract_step_name(log_file_name: str) -> str:
    parts: list[str] = []
    for raw_part in Path(log_file_name).with_suffix("").parts:
        normalized = raw_part.replace("_", " ").strip()
        normalized = re.sub(r"^\d+\s*[- ]?\s*", "", normalized)
        if not normalized:
            continue
        if not parts and normalized.lower() == "logs":
            continue
        parts.append(normalized)
    return "/".join(parts) or log_file_name


def extract_log_excerpts(log_archive: bytes, max_chars: int = LOG_EXCERPT_MAX_CHARS) -> list[dict[str, str]]:
    excerpts: list[dict[str, str]] = []
    read_limit = max(max_chars, 1) * 4
    with zipfile.ZipFile(io.BytesIO(log_archive)) as archive:
        names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        for name in names:
            with archive.open(name, "r") as log_file:
                raw = log_file.read(read_limit)
            text = raw.decode("utf-8", errors="replace")
            if not text:
                continue
            excerpts.append(
                {
                    "step_name": _extract_step_name(name),
                    "excerpt": text[:max_chars],
                }
            )
    return excerpts


def list_run_log_excerpts(
    repo: str,
    run_id: int,
    *,
    token: str,
    max_chars: int = LOG_EXCERPT_MAX_CHARS,
) -> list[dict[str, str]]:
    payload = gh_api_bytes(_build_logs_endpoint(repo, run_id), token=token)
    return extract_log_excerpts(payload, max_chars=max_chars)


def extract_failure_point(jobs: list[dict[str, Any]]) -> dict[str, str | None]:
    for job in jobs:
        for step in job.get("steps") or []:
            if (step.get("conclusion") or "").lower() == "failure":
                return {
                    "job_name": job.get("name"),
                    "step_name": step.get("name"),
                }

    for job in jobs:
        if (job.get("conclusion") or "").lower() == "failure":
            return {
                "job_name": job.get("name"),
                "step_name": None,
            }

    return {
        "job_name": None,
        "step_name": None,
    }


def compute_run_metrics(
    repository: str,
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    run_attempt = max(1, _to_int(run.get("run_attempt"), 1))
    retries = max(0, run_attempt - 1)

    started_at = _parse_iso8601(run.get("run_started_at"))
    updated_at = _parse_iso8601(run.get("updated_at"))
    duration_seconds = 0
    if started_at and updated_at:
        duration_seconds = max(0, int((updated_at - started_at).total_seconds()))

    conclusion = run.get("conclusion")
    failure_point = {"job_name": None, "step_name": None}
    if (conclusion or "").lower() == "failure":
        failure_point = extract_failure_point(jobs)

    return {
        "repository": repository,
        "run_id": run.get("id"),
        "workflow_name": run.get("name"),
        "workflow_path": run.get("path"),
        "workflow_family": run.get("_workflow_family"),
        "status": run.get("status"),
        "conclusion": conclusion,
        "run_attempt": run_attempt,
        "retries": retries,
        "created_at": run.get("created_at"),
        "run_started_at": run.get("run_started_at"),
        "updated_at": run.get("updated_at"),
        "duration_seconds": duration_seconds,
        "failure_point": failure_point,
    }


def _percentile(values: list[int], percentile: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    rank = int(math.ceil((percentile / 100.0) * len(sorted_values)))
    index = max(0, min(len(sorted_values) - 1, rank - 1))
    return float(sorted_values[index])


def build_report(
    repositories: list[str],
    runs: list[dict[str, Any]],
    errors: list[dict[str, str]],
    *,
    success_sample_rate: float = DEFAULT_SUCCESS_SAMPLE_RATE,
) -> dict[str, Any]:
    durations = [int(item.get("duration_seconds", 0)) for item in runs]
    success_count = sum(1 for item in runs if (item.get("conclusion") or "").lower() == "success")
    failure_count = sum(1 for item in runs if (item.get("conclusion") or "").lower() == "failure")
    cancelled_count = sum(1 for item in runs if (item.get("conclusion") or "").lower() == "cancelled")
    other_count = sum(
        1
        for item in runs
        if (item.get("conclusion") or "").lower() not in {"success", "failure", "cancelled"}
    )

    observed_families = sorted({str(item.get("workflow_family") or "other") for item in runs})
    avg_duration = float(sum(durations) / len(durations)) if durations else 0.0
    sampled_success_count = sum(1 for item in runs if item.get("_success_sampled"))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _format_iso8601(datetime.now(timezone.utc)),
        "scope": {
            "repositories": repositories,
            "workflow_families": observed_families,
            "source": "github_actions_api",
            "success_sample_rate": success_sample_rate,
        },
        "runs": runs,
        "summary": {
            "total_runs": len(runs),
            "success_count": success_count,
            "failure_count": failure_count,
            "cancelled_count": cancelled_count,
            "other_count": other_count,
            "avg_duration_seconds": avg_duration,
            "p50_duration_seconds": _percentile(durations, 50),
            "p95_duration_seconds": _percentile(durations, 95),
            "sampled_success_runs": sampled_success_count,
        },
        "errors": errors,
    }


def _sort_runs_by_created_desc(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        runs,
        key=lambda item: _parse_iso8601(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def _run_created_at(run: dict[str, Any]) -> datetime:
    return _parse_iso8601(run.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)


def _run_identity(run: dict[str, Any]) -> tuple[str, int]:
    return str(run.get("repository") or ""), _to_int(run.get("run_id"), 0)


def _deterministic_seed(runs: list[dict[str, Any]]) -> str:
    """Build a deterministic seed from the collection window for reproducible sampling."""
    timestamps = sorted(run.get("created_at") or "" for run in runs if run.get("created_at"))
    seed_input = f"{timestamps[0]}:{timestamps[-1]}:{len(runs)}" if timestamps else f"empty:{len(runs)}"
    return hashlib.sha256(seed_input.encode("utf-8")).hexdigest()


def select_notable_runs_for_logs(
    runs: list[dict[str, Any]],
    max_log_runs: int,
    success_sample_rate: float = DEFAULT_SUCCESS_SAMPLE_RATE,
) -> list[dict[str, Any]]:
    if max_log_runs <= 0:
        return []

    eligible = [
        run
        for run in runs
        if isinstance(run, dict) and bool(run.get("repository")) and _to_int(run.get("run_id"), 0) > 0
    ]
    if not eligible:
        return []

    failed_runs = _sort_runs_by_created_desc(
        [run for run in eligible if (run.get("conclusion") or "").lower() == "failure"]
    )
    retried_runs = _sort_runs_by_created_desc([run for run in eligible if _to_int(run.get("retries"), 0) > 0])

    slow_runs: list[dict[str, Any]] = []
    repositories = sorted({str(run.get("repository")) for run in eligible})
    for repository in repositories:
        repo_runs = [run for run in eligible if str(run.get("repository")) == repository]
        repo_runs.sort(
            key=lambda item: (
                _to_int(item.get("duration_seconds"), 0),
                _run_created_at(item),
            ),
            reverse=True,
        )
        slow_runs.extend(repo_runs[:SLOW_RUNS_PER_REPO])
    slow_runs.sort(
        key=lambda item: (
            _to_int(item.get("duration_seconds"), 0),
            _run_created_at(item),
        ),
        reverse=True,
    )

    ordered: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for bucket in (failed_runs, retried_runs, slow_runs):
        for run in bucket:
            identity = _run_identity(run)
            if identity in seen:
                continue
            seen.add(identity)
            ordered.append(run)
            if len(ordered) >= max_log_runs:
                return ordered

    # Random sampling of successful runs for baseline visibility
    if success_sample_rate > 0 and len(ordered) < max_log_runs:
        success_runs = [
            run for run in eligible
            if (run.get("conclusion") or "").lower() == "success" and _run_identity(run) not in seen
        ]
        if success_runs:
            sample_count = max(1, math.ceil(len(success_runs) * success_sample_rate))
            remaining_slots = max_log_runs - len(ordered)
            sample_count = min(sample_count, remaining_slots)
            seed = _deterministic_seed(eligible)
            rng = random.Random(seed)
            sampled = rng.sample(success_runs, min(sample_count, len(success_runs)))
            sampled = _sort_runs_by_created_desc(sampled)
            for run in sampled:
                identity = _run_identity(run)
                if identity in seen:
                    continue
                seen.add(identity)
                run["_success_sampled"] = True
                ordered.append(run)
                if len(ordered) >= max_log_runs:
                    break

    return ordered


def _resolve_since_utc(args: argparse.Namespace) -> datetime:
    if args.since:
        since_dt = _parse_iso8601(args.since)
        if since_dt is None:
            raise ValueError(f"Invalid --since value: {args.since}")
        return since_dt

    if args.lookback_days is None:
        raise ValueError("Either --since or --lookback-days is required")
    if args.lookback_days < 0:
        raise ValueError("--lookback-days must be >= 0")
    return datetime.now(timezone.utc) - timedelta(days=args.lookback_days)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repositories = [repo.strip() for repo in args.repo if repo.strip()]
    if not repositories:
        env_repo = os.getenv("GITHUB_REPOSITORY", "").strip()
        if env_repo:
            repositories = [env_repo]
    repositories = list(dict.fromkeys(repositories))
    if not repositories:
        print("ERROR: no repositories specified; pass --repo or set GITHUB_REPOSITORY", file=sys.stderr)
        return 2

    if args.per_page <= 0 or args.max_pages <= 0 or args.max_runs < 0 or args.max_log_runs < 0:
        print(
            "ERROR: --per-page and --max-pages must be > 0; --max-runs and --max-log-runs must be >= 0",
            file=sys.stderr,
        )
        return 2

    success_sample_rate = max(0.0, min(1.0, args.success_sample_rate))

    try:
        since_utc = _resolve_since_utc(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")

    errors: list[dict[str, str]] = []
    run_rows: list[dict[str, Any]] = []
    successful_repo_queries = 0

    for repo in repositories:
        try:
            runs, capped = list_runs_for_repo(
                repo,
                since_utc=since_utc,
                per_page=args.per_page,
                max_pages=args.max_pages,
                max_runs=args.max_runs,
                token=token,
            )
            successful_repo_queries += 1
            if capped:
                errors.append(
                    {
                        "repository": repo,
                        "scope": "runs",
                        "message": f"Run collection stopped after max-runs={args.max_runs}",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "repository": repo,
                    "scope": "runs",
                    "message": str(exc),
                }
            )
            continue

        for run in runs:
            run_id = _to_int(run.get("id"), 0)
            jobs: list[dict[str, Any]] = []
            if run_id > 0 and (run.get("conclusion") or "").lower() == "failure":
                try:
                    jobs = list_jobs_for_run(
                        repo,
                        run_id,
                        per_page=args.per_page,
                        max_pages=args.max_pages,
                        token=token,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "repository": repo,
                            "run_id": str(run.get("id")),
                            "scope": "jobs",
                            "message": str(exc),
                        }
                    )

            run_rows.append(compute_run_metrics(repo, run, jobs))

    for run in select_notable_runs_for_logs(run_rows, args.max_log_runs, success_sample_rate=success_sample_rate):
        repository = str(run.get("repository") or "")
        run_id = _to_int(run.get("run_id"), 0)
        if not repository or run_id <= 0:
            continue
        try:
            run["log_excerpts"] = list_run_log_excerpts(repository, run_id, token=token)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "repository": repository,
                    "run_id": str(run_id),
                    "scope": "logs",
                    "message": str(exc),
                }
            )

    run_rows.sort(key=lambda item: (item.get("created_at") or ""), reverse=True)
    report = build_report(repositories, run_rows, errors, success_sample_rate=success_sample_rate)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(output_path)

    if successful_repo_queries == 0 and errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
