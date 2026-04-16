#!/usr/bin/env python3
"""Collect GitHub Actions run/job telemetry for core AI workflow families."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

SCHEMA_VERSION = "workflow_log_collector.v1"
CORE_WORKFLOW_FAMILIES = (
    "clarify",
    "plan",
    "implement",
    "review_autofix",
    "validate",
    "orchestrate",
    "orchestrate_poll",
    "orchestrate_clarify_respond",
    "issue_pr_status",
    "cancel_on_pr_close",
    "memory_maintenance",
)
LOG_EXPORT_CATEGORIES = ("errors", "slow", "recent")
LOG_EXCERPT_MAX_CHARS = 4000
SLOW_RUNS_PER_REPO = 10
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
        "--log-output-dir",
        default=None,
        help="Optional directory path for categorized full-log export artifacts.",
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
    normalized = re.sub(r"[^a-z0-9]+", "_", f"{workflow_name or ''} {workflow_path or ''}".lower()).strip("_")

    family_matchers: tuple[tuple[str, str], ...] = (
        ("orchestrate_clarify_respond", "orchestrate_clarify_respond"),
        ("orchestrate_poller", "orchestrate_poll"),
        ("orchestrate_poll", "orchestrate_poll"),
        ("issue_pr_status", "issue_pr_status"),
        ("cancel_runs_on_pr_close", "cancel_on_pr_close"),
        ("cancel_on_pr_close", "cancel_on_pr_close"),
        ("memory_maintenance", "memory_maintenance"),
        ("review_autofix", "review_autofix"),
        ("codex_pr_self_healing_semantic_agent", "review_autofix"),
        ("ai_review", "review_autofix"),
        ("internal_review", "review_autofix"),
        ("validate", "validate"),
        ("clarify", "clarify"),
        ("plan", "plan"),
        ("implement", "implement"),
        ("orchestrate", "orchestrate"),
    )
    for marker, family in family_matchers:
        if marker in normalized:
            return family
    return None


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


def _fetch_run_log_archive(
    repo: str,
    run_id: int,
    *,
    token: str,
    cache: dict[tuple[str, int], bytes | Exception] | None = None,
) -> bytes:
    identity = (repo, run_id)
    if cache is not None and identity in cache:
        cached = cache[identity]
        if isinstance(cached, Exception):
            raise cached
        return cached

    try:
        payload = gh_api_bytes(_build_logs_endpoint(repo, run_id), token=token)
    except Exception as exc:  # noqa: BLE001
        if cache is not None:
            cache[identity] = exc
        raise
    if cache is not None:
        cache[identity] = payload
    return payload


def list_run_log_excerpts(
    repo: str,
    run_id: int,
    *,
    token: str,
    max_chars: int = LOG_EXCERPT_MAX_CHARS,
) -> list[dict[str, str]]:
    payload = _fetch_run_log_archive(repo, run_id, token=token)
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


def _dedupe_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for error in errors:
        key = (
            str(error.get("repository") or ""),
            str(error.get("run_id") or ""),
            str(error.get("scope") or ""),
            str(error.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(error)
    return deduped


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

    avg_duration = float(sum(durations) / len(durations)) if durations else 0.0

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _format_iso8601(datetime.now(timezone.utc)),
        "scope": {
            "repositories": repositories,
            "workflow_families": list(CORE_WORKFLOW_FAMILIES),
            "source": "github_actions_api",
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


def _eligible_runs_for_log_selection(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        run
        for run in runs
        if isinstance(run, dict) and bool(run.get("repository")) and _to_int(run.get("run_id"), 0) > 0
    ]


def select_notable_runs_for_logs(runs: list[dict[str, Any]], max_log_runs: int) -> list[dict[str, Any]]:
    if max_log_runs <= 0:
        return []

    eligible = _eligible_runs_for_log_selection(runs)
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
    return ordered


def select_runs_for_log_export_categories(
    runs: list[dict[str, Any]],
    max_log_runs: int,
) -> dict[str, list[dict[str, Any]]]:
    categories: dict[str, list[dict[str, Any]]] = {key: [] for key in LOG_EXPORT_CATEGORIES}
    if max_log_runs <= 0:
        return categories

    eligible = _eligible_runs_for_log_selection(runs)
    if not eligible:
        return categories

    error_runs = _sort_runs_by_created_desc(
        [run for run in eligible if (run.get("conclusion") or "").lower() == "failure"]
    )

    runs_by_repository: dict[str, list[dict[str, Any]]] = {}
    for run in eligible:
        repository = str(run.get("repository"))
        runs_by_repository.setdefault(repository, []).append(run)

    slow_runs: list[dict[str, Any]] = []
    for repository in sorted(runs_by_repository):
        repo_runs = runs_by_repository[repository]
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

    recent_runs = _sort_runs_by_created_desc(eligible)

    categories["errors"] = error_runs[:max_log_runs]
    categories["slow"] = slow_runs[:max_log_runs]
    categories["recent"] = recent_runs[:max_log_runs]
    return categories


def extract_full_logs(log_archive: bytes) -> list[dict[str, str]]:
    full_logs: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(log_archive)) as archive:
        names = sorted(name for name in archive.namelist() if not name.endswith("/"))
        for name in names:
            with archive.open(name, "r") as log_file:
                raw = log_file.read()
            text = raw.decode("utf-8", errors="replace")
            if not text:
                continue
            full_logs.append(
                {
                    "step_name": _extract_step_name(name),
                    "content": text,
                }
            )
    return full_logs


def _sanitize_path_component(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    candidate = candidate.strip("._-")
    return candidate or fallback


def _repo_slug(repository: str) -> str:
    return _sanitize_path_component(repository.replace("/", "_"), "unknown_repo")


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _write_run_log_bundle(
    output_dir: Path,
    category: str,
    run: dict[str, Any],
    full_logs: list[dict[str, str]] | None,
) -> None:
    repository = str(run.get("repository") or "")
    family = _sanitize_path_component(str(run.get("workflow_family") or "unknown"), "unknown")
    run_id = _sanitize_path_component(str(_to_int(run.get("run_id"), 0)), "0")

    run_dir = output_dir / category / _repo_slug(repository) / family / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(run_dir / "metadata.json", run)

    if full_logs is None:
        return
    for index, step in enumerate(full_logs, start=1):
        step_name = _sanitize_path_component(str(step.get("step_name") or ""), f"step_{index:03d}")
        log_path = run_dir / f"step-{index:03d}-{step_name}.log"
        log_path.write_text(str(step.get("content") or ""), encoding="utf-8")


def export_categorized_logs(
    output_dir: Path,
    runs: list[dict[str, Any]],
    *,
    max_log_runs: int,
    token: str,
    errors: list[dict[str, str]],
    log_archive_cache: dict[tuple[str, int], bytes | Exception] | None = None,
) -> None:
    categories = select_runs_for_log_export_categories(runs, max_log_runs)
    output_dir.mkdir(parents=True, exist_ok=True)
    for category in LOG_EXPORT_CATEGORIES:
        (output_dir / category).mkdir(parents=True, exist_ok=True)

    selected_runs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for category in LOG_EXPORT_CATEGORIES:
        for run in categories[category]:
            identity = _run_identity(run)
            if identity in seen:
                continue
            seen.add(identity)
            selected_runs.append(run)

    full_logs_by_identity: dict[tuple[str, int], list[dict[str, str]]] = {}
    for run in selected_runs:
        repository = str(run.get("repository") or "")
        run_id = _to_int(run.get("run_id"), 0)
        identity = (repository, run_id)
        if not repository or run_id <= 0:
            continue
        try:
            payload = _fetch_run_log_archive(
                repository,
                run_id,
                token=token,
                cache=log_archive_cache,
            )
            full_logs_by_identity[identity] = extract_full_logs(payload)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "repository": repository,
                    "run_id": str(run_id),
                    "scope": "logs",
                    "message": str(exc),
                }
            )

    for category in LOG_EXPORT_CATEGORIES:
        for run in categories[category]:
            _write_run_log_bundle(output_dir, category, run, full_logs_by_identity.get(_run_identity(run)))


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

    try:
        since_utc = _resolve_since_utc(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")

    errors: list[dict[str, str]] = []
    run_rows: list[dict[str, Any]] = []
    successful_repo_queries = 0
    log_archive_cache: dict[tuple[str, int], bytes | Exception] | None = {} if args.log_output_dir else None

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

    for run in select_notable_runs_for_logs(run_rows, args.max_log_runs):
        repository = str(run.get("repository") or "")
        run_id = _to_int(run.get("run_id"), 0)
        if not repository or run_id <= 0:
            continue
        try:
            if log_archive_cache is not None:
                payload = _fetch_run_log_archive(repository, run_id, token=token, cache=log_archive_cache)
                run["log_excerpts"] = extract_log_excerpts(payload)
            else:
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
    if args.log_output_dir:
        try:
            export_categorized_logs(
                Path(args.log_output_dir),
                run_rows,
                max_log_runs=args.max_log_runs,
                token=token,
                errors=errors,
                log_archive_cache=log_archive_cache,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "repository": "",
                    "run_id": "",
                    "scope": "log_export",
                    "message": str(exc),
                }
            )

    errors = _dedupe_errors(errors)
    report = build_report(repositories, run_rows, errors)
    _write_json_atomic(Path(args.output), report)
    if args.log_output_dir:
        _write_json_atomic(Path(args.log_output_dir) / "summary.json", report)

    if successful_repo_queries == 0 and errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
