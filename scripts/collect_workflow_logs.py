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
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from ai_memory_lib import (
        get_workflow_log_analysis_cache,
        persist_memory_operation,
        put_workflow_log_analysis_cache,
        read_memory_root_from_branch,
        resolve_memory_root_dir,
    )
except ModuleNotFoundError:
    from scripts.ai_memory_lib import (
        get_workflow_log_analysis_cache,
        persist_memory_operation,
        put_workflow_log_analysis_cache,
        read_memory_root_from_branch,
        resolve_memory_root_dir,
    )

try:
    from cost_audit import (
        BREAK_GLASS_RE,
        CONTEXT_BUDGET_WARN_RE,
        OPENROUTER_RE,
        _validated_mcp_telemetry_event,
        aggregate_run_cost_telemetry,
        build_run_cost_telemetry,
    )
except ModuleNotFoundError:
    from scripts.cost_audit import (
        BREAK_GLASS_RE,
        CONTEXT_BUDGET_WARN_RE,
        OPENROUTER_RE,
        _validated_mcp_telemetry_event,
        aggregate_run_cost_telemetry,
        build_run_cost_telemetry,
    )

SCHEMA_VERSION = "workflow_log_collector.v2"
LOG_EXPORT_CATEGORIES = ("errors", "slow", "recent")
LOG_EXCERPT_MAX_CHARS = 4000
SLOW_RUNS_PER_REPO = 10
DEFAULT_SUCCESS_SAMPLE_RATE = 0.07
LOG_ARCHIVE_FETCH_RETRIES = 3
LOG_ARCHIVE_FETCH_BACKOFF_SECONDS = 1.0
MISSING_LOG_ARCHIVE_MARKER = "partial_data:missing_log_archive"
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
WORKFLOW_LOG_CACHE_SCHEMA_VERSION = "v1"
WORKFLOW_LOG_CACHE_SEEN_SET_LIMIT = 500
WORKFLOW_LOG_CACHE_CANCELLED_FAILURE_POINT_VERSION = 1
STRUCTURED_COST_TELEMETRY_PATTERNS = (
    OPENROUTER_RE,
    BREAK_GLASS_RE,
    CONTEXT_BUDGET_WARN_RE,
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


def _parse_http_response(raw_output: str) -> tuple[int, dict[str, str], str]:
    normalized = raw_output.replace("\r\n", "\n")
    if not normalized.startswith("HTTP/"):
        return 200, {}, normalized.strip()

    status_code = 200
    headers: dict[str, str] = {}
    body = ""
    remainder = normalized

    while remainder.startswith("HTTP/"):
        header_end = remainder.find("\n\n")
        if header_end == -1:
            header_block = remainder
            remainder = ""
        else:
            header_block = remainder[:header_end]
            remainder = remainder[header_end + 2 :]

        lines = [line for line in header_block.split("\n") if line.strip()]
        if lines:
            match = re.match(r"HTTP/\S+\s+(\d{3})", lines[0].strip())
            if match:
                status_code = int(match.group(1))
            parsed_headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                parsed_headers[key.strip().lower()] = value.strip()
            headers = parsed_headers

        if not remainder.startswith("HTTP/"):
            body = remainder
            break

    return status_code, headers, body.strip()


def _cache_default_payload() -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_LOG_CACHE_SCHEMA_VERSION,
        "repositories": {},
    }


def _cache_enabled() -> bool:
    enabled = str(os.getenv("AI_MEMORY_ENABLED", "true")).strip().lower()
    return enabled in {"1", "true", "yes", "on", "y"}


def _normalize_seen_set(values: Any, *, limit: int = WORKFLOW_LOG_CACHE_SEEN_SET_LIMIT) -> list[int]:
    if not isinstance(values, list):
        return []

    ordered: list[int] = []
    seen: set[int] = set()
    for value in values:
        run_id = _to_int(value, 0)
        if run_id <= 0 or run_id in seen:
            continue
        ordered.append(run_id)
        seen.add(run_id)

    if len(ordered) > limit:
        ordered = ordered[-limit:]
    return ordered


def _touch_seen_set(seen_set: list[int], run_id: int, *, limit: int = WORKFLOW_LOG_CACHE_SEEN_SET_LIMIT) -> list[int]:
    if run_id <= 0:
        return seen_set

    next_values = [item for item in seen_set if item != run_id]
    next_values.append(run_id)
    if len(next_values) > limit:
        next_values = next_values[-limit:]
    return next_values


def _normalize_runs_snapshot(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


def _cache_run_key(run_id: int, run_attempt: int) -> tuple[int, int]:
    return run_id, max(1, run_attempt)


def _run_key_from_payload(payload: dict[str, Any]) -> tuple[int, int] | None:
    run_id = _to_int(payload.get("run_id"), 0)
    if run_id <= 0:
        run_id = _to_int(payload.get("id"), 0)
    if run_id <= 0:
        return None

    run_attempt = _to_int(payload.get("run_attempt"), 1)
    return _cache_run_key(run_id, run_attempt)


def _index_cached_rows(values: Any) -> dict[tuple[int, int], dict[str, Any]]:
    indexed: dict[tuple[int, int], dict[str, Any]] = {}
    if not isinstance(values, list):
        return indexed

    for row in values:
        if not isinstance(row, dict):
            continue
        cache_key = _run_key_from_payload(row)
        if cache_key is None:
            continue
        indexed[cache_key] = dict(row)
    return indexed


def _cached_log_excerpts(row: dict[str, Any] | None) -> list[dict[str, str]] | None:
    if not isinstance(row, dict):
        return None

    values = row.get("log_excerpts")
    if not isinstance(values, list):
        return None

    excerpts: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        excerpts.append(
            {
                "step_name": str(item.get("step_name") or ""),
                "excerpt": str(item.get("excerpt") or ""),
            }
        )
    return excerpts


def _cached_cost_telemetry(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None

    telemetry = row.get("cost_telemetry")
    if not isinstance(telemetry, dict):
        return None

    return dict(telemetry)


def _run_snapshot_for_cache(run: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "path",
        "status",
        "conclusion",
        "run_attempt",
        "created_at",
        "run_started_at",
        "updated_at",
    )
    snapshot = {key: run.get(key) for key in keys}
    snapshot["_workflow_family"] = run.get("_workflow_family")
    return snapshot


def _cache_warning(scope: str, error: Exception) -> None:
    print(f"::warning::rate_limit_audit_fallback {scope}: {error}", file=sys.stderr)


def _cache_read_context(
    *,
    repo_root: Path,
    memory_branch: str,
    memory_root_relative: str,
) -> tuple[dict[str, Any], str | None, Path | None]:
    if not _cache_enabled():
        return _cache_default_payload(), None, None

    branch_dir: Path | None = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=memory_branch,
            memory_root_relative=memory_root_relative,
        )
        memory_root = resolve_memory_root_dir(branch_dir, memory_root_relative)
        payload = get_workflow_log_analysis_cache(memory_root)
        if not isinstance(payload, dict):
            raise RuntimeError("cache payload is not an object")
        return payload, None, branch_dir
    except Exception as exc:  # noqa: BLE001
        _cache_warning("workflow-log-analysis cache read failed", exc)
        return _cache_default_payload(), str(exc), branch_dir


def _cache_write_context(
    *,
    repo_root: Path,
    memory_branch: str,
    memory_root_relative: str,
    push_retries: int,
    payload: dict[str, Any],
) -> bool:
    if not _cache_enabled():
        return False

    try:
        def _write_operation(clone_dir: Path) -> dict[str, Any] | None:
            memory_root = resolve_memory_root_dir(clone_dir, memory_root_relative)
            if put_workflow_log_analysis_cache(memory_root, payload) is None:
                return None
            return payload

        result = persist_memory_operation(
            repo_root,
            memory_branch=memory_branch,
            memory_root_relative=memory_root_relative,
            push_retries=max(1, push_retries),
            commit_message="Update workflow log analysis cache",
            operation=_write_operation,
        )
    except Exception as exc:  # noqa: BLE001
        _cache_warning("workflow-log-analysis cache persist failed", exc)
        return False

    if result.get("operation_result") is None:
        _cache_warning(
            "workflow-log-analysis cache persist failed",
            RuntimeError("cache write operation returned no payload"),
        )
        return False

    return True


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
    request_headers: dict[str, str] | None = None,
    include_response_meta: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
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
    if request_headers:
        for key, value in request_headers.items():
            if not key or value is None:
                continue
            cmd.extend(["-H", f"{key}: {value}"])
    if include_response_meta:
        cmd.append("--include")

    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=base_env, timeout=60)
        except subprocess.TimeoutExpired as exc:
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
                continue
            raise RuntimeError(f"gh api timed out for {endpoint} after {exc.timeout}s") from exc

        if proc.returncode == 0:
            status_code = 200
            response_headers: dict[str, str] = {}
            output = proc.stdout.strip()
            if include_response_meta:
                status_code, response_headers, output = _parse_http_response(proc.stdout)
            if not output:
                payload: dict[str, Any] = {}
                if include_response_meta:
                    return payload, {"status_code": status_code, "headers": response_headers}
                return payload
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON from gh api ({endpoint}): {exc}") from exc
            if isinstance(parsed, dict):
                if include_response_meta:
                    return parsed, {"status_code": status_code, "headers": response_headers}
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

    if "ci" in normalized:
        return "ci"
    if "workflow_log_analysis" in normalized:
        return "workflow_log_analysis"

    path = (workflow_path or "").lower()
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
    etag: str | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    capped = False
    response_meta: dict[str, Any] = {
        "status_code": 200,
        "etag": etag,
        "not_modified": False,
    }

    for page in range(1, max_pages + 1):
        request_headers: dict[str, str] | None = None
        if page == 1 and etag:
            request_headers = {"If-None-Match": etag}

        payload_obj, meta = gh_api_json(
            _build_runs_endpoint(repo, since_utc, per_page, page),
            token=token,
            request_headers=request_headers,
            include_response_meta=True,
        )
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        status_code = _to_int(meta.get("status_code"), 200) if isinstance(meta, dict) else 200
        response_headers = (meta.get("headers") if isinstance(meta, dict) else {}) or {}
        response_meta["status_code"] = status_code
        response_meta["etag"] = response_headers.get("etag") or response_meta.get("etag")

        if page == 1 and status_code == 304:
            response_meta["not_modified"] = True
            return [], False, response_meta

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
                return runs, capped, response_meta

    return runs, capped, response_meta


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


def _normalize_error_text(exc: Exception) -> str:
    text = " ".join(str(exc).split()).strip()
    return text or exc.__class__.__name__


def _sanitize_missing_log_archive_detail(detail: str) -> str:
    sanitized = detail.replace("=", ":")
    return sanitized[:512] or "unknown"


def _is_missing_log_archive_error(error_text: str, run_id: int) -> bool:
    normalized = error_text.lower()
    has_http_missing = any(
        marker in normalized
        for marker in (
            "http 404",
            "404 not found",
            "status code 404",
            "404 error",
            "http 410",
            "410 gone",
            "status code 410",
            "410 error",
        )
    )
    has_archive_endpoint = f"actions/runs/{run_id}/logs" in normalized
    return (has_http_missing and has_archive_endpoint) or "log archive not found" in normalized


def _normalize_log_archive_exception(repo: str, run_id: int, exc: Exception) -> Exception:
    detail = _normalize_error_text(exc)
    if _is_missing_log_archive_error(detail, run_id):
        sanitized_detail = _sanitize_missing_log_archive_detail(detail)
        wrapped = RuntimeError(
            f"{MISSING_LOG_ARCHIVE_MARKER} repository={repo} run_id={run_id} detail={sanitized_detail}"
        )
        wrapped.__cause__ = exc
        return wrapped
    return exc


def _is_retryable_log_archive_exception(exc: Exception) -> bool:
    message = _normalize_error_text(exc).lower()
    return any(marker in message for marker in RETRY_MARKERS)


def _append_log_error(errors: list[dict[str, str]], repository: str, run_id: int, exc: Exception) -> None:
    errors.append(
        {
            "repository": repository,
            "run_id": str(run_id),
            "scope": "logs",
            "message": _normalize_error_text(exc),
        }
    )


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

    endpoint = _build_logs_endpoint(repo, run_id)
    for attempt in range(1, LOG_ARCHIVE_FETCH_RETRIES + 1):
        try:
            payload = gh_api_bytes(endpoint, token=token, retries=1)
        except Exception as exc:  # noqa: BLE001
            normalized_exc = _normalize_log_archive_exception(repo, run_id, exc)
            is_missing_archive = _normalize_error_text(normalized_exc).startswith(
                f"{MISSING_LOG_ARCHIVE_MARKER} "
            )
            if (
                not is_missing_archive
                and attempt < LOG_ARCHIVE_FETCH_RETRIES
                and _is_retryable_log_archive_exception(normalized_exc)
            ):
                time.sleep(LOG_ARCHIVE_FETCH_BACKOFF_SECONDS * attempt)
                continue
            if cache is not None:
                cache[identity] = normalized_exc
            if is_missing_archive:
                raise normalized_exc from exc
            raise

        if not payload:
            empty_exc = RuntimeError("empty_log_archive_payload")
            wrapped_empty_exc = RuntimeError(
                f"{MISSING_LOG_ARCHIVE_MARKER} repository={repo} run_id={run_id} "
                f"detail={_sanitize_missing_log_archive_detail(_normalize_error_text(empty_exc))}"
            )
            wrapped_empty_exc.__cause__ = empty_exc
            if cache is not None:
                cache[identity] = wrapped_empty_exc
            raise wrapped_empty_exc from empty_exc
        if cache is not None:
            cache[identity] = payload
        return payload


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


def _extract_cancelled_point(jobs: list[dict[str, Any]]) -> dict[str, str | None]:
    for job in jobs:
        for step in job.get("steps") or []:
            if (step.get("conclusion") or "").lower() == "cancelled":
                return {
                    "job_name": job.get("name"),
                    "step_name": step.get("name") or "cancelled_before_first_step",
                }

    for job in jobs:
        if (job.get("conclusion") or "").lower() == "cancelled":
            return {
                "job_name": job.get("name"),
                "step_name": "cancelled_before_first_step",
            }

    return {
        "job_name": None,
        "step_name": None,
    }


def _cached_row_needs_cancelled_failure_point_backfill(
    run: dict[str, Any],
    cached_row: dict[str, Any] | None,
    cancelled_jobs_seen_lookup: set[int],
    cancelled_failure_point_version: int,
) -> bool:
    run_id = _to_int(run.get("id"), 0)
    if run_id <= 0 or (run.get("conclusion") or "").lower() != "cancelled" or not isinstance(cached_row, dict):
        return False

    failure_point = cached_row.get("failure_point")
    has_failure_point = isinstance(failure_point, dict) and bool(
        failure_point.get("job_name") or failure_point.get("step_name")
    )
    if cancelled_failure_point_version < WORKFLOW_LOG_CACHE_CANCELLED_FAILURE_POINT_VERSION:
        return True
    if run_id in cancelled_jobs_seen_lookup:
        return False
    if not isinstance(failure_point, dict):
        return True
    return not has_failure_point


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
    normalized_conclusion = (conclusion or "").lower()
    failure_point = {"job_name": None, "step_name": None}
    if normalized_conclusion == "failure":
        failure_point = extract_failure_point(jobs)
    elif normalized_conclusion == "cancelled":
        failure_point = _extract_cancelled_point(jobs)
        if not (failure_point.get("job_name") or failure_point.get("step_name")):
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
            "cost_telemetry": aggregate_run_cost_telemetry(runs),
        },
        "errors": errors,
    }


def _sort_runs_by_created_desc(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        runs,
        key=lambda item: (
            _parse_iso8601(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
            str(item.get("repository") or ""),
            _to_int(item.get("run_id"), _to_int(item.get("id"), 0)),
        ),
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

    eligible = _eligible_runs_for_log_selection(runs)
    if not eligible:
        return []

    failed_runs = _sort_runs_by_created_desc(
        [run for run in eligible if (run.get("conclusion") or "").lower() == "failure"]
    )
    retried_runs = _sort_runs_by_created_desc([run for run in eligible if _to_int(run.get("retries"), 0) > 0])

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
                _to_int(item.get("run_id"), _to_int(item.get("id"), 0)),
            ),
            reverse=True,
        )
        slow_runs.extend(repo_runs[:SLOW_RUNS_PER_REPO])
    slow_runs.sort(
        key=lambda item: (
            _to_int(item.get("duration_seconds"), 0),
            _run_created_at(item),
            str(item.get("repository") or ""),
            _to_int(item.get("run_id"), _to_int(item.get("id"), 0)),
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
                _to_int(item.get("run_id"), _to_int(item.get("id"), 0)),
            ),
            reverse=True,
        )
        slow_runs.extend(repo_runs[:SLOW_RUNS_PER_REPO])
    slow_runs.sort(
        key=lambda item: (
            _to_int(item.get("duration_seconds"), 0),
            _run_created_at(item),
            str(item.get("repository") or ""),
            _to_int(item.get("run_id"), _to_int(item.get("id"), 0)),
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


def build_log_excerpts_from_full_logs(
    full_logs: list[dict[str, str]],
    max_chars: int = LOG_EXCERPT_MAX_CHARS,
) -> list[dict[str, str]]:
    excerpts: list[dict[str, str]] = []
    for step in full_logs:
        text = str(step.get("content") or "")
        if not text:
            continue
        excerpts.append(
            {
                "step_name": str(step.get("step_name") or "unknown_step"),
                "excerpt": text[:max_chars],
            }
        )
    return excerpts


def _full_logs_to_text(full_logs: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for step in full_logs:
        content = str(step.get("content") or "")
        if not content:
            continue
        parts.append(content)
    return "\n".join(parts)


def _structured_cost_telemetry_line_key(line: str) -> str | None:
    if not isinstance(line, str):
        return None
    line_text = line.rstrip()
    if not line_text:
        return None
    if (
        _validated_mcp_telemetry_event(line_text) is not None
        or any(pattern.search(line_text) for pattern in STRUCTURED_COST_TELEMETRY_PATTERNS)
    ):
        return line_text
    return None


def _normalized_full_log_step(step: Any) -> dict[str, str] | None:
    if not isinstance(step, dict):
        return None
    step_name = step.get("step_name")
    content = step.get("content")
    return {
        "step_name": step_name if isinstance(step_name, str) else "",
        "content": content if isinstance(content, str) else "",
    }


def _step_name_path_parts(step_name: str) -> tuple[str, ...]:
    normalized = step_name.strip("/")
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part)


def _step_name_has_descendant_match(step_name: str, candidate_step_names: set[str]) -> bool:
    current_parts = _step_name_path_parts(step_name)
    if not current_parts:
        return False

    for candidate_step_name in candidate_step_names:
        if candidate_step_name == step_name:
            continue
        candidate_parts = _step_name_path_parts(candidate_step_name)
        if len(candidate_parts) <= len(current_parts):
            continue
        if candidate_parts[: len(current_parts)] == current_parts:
            return True
    return False


def _dedupe_structured_cost_telemetry_full_logs(full_logs: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(full_logs, list):
        return []

    structured_line_step_names: dict[str, set[str]] = {}
    normalized_full_logs: list[dict[str, str]] = []
    for raw_step in full_logs:
        step = _normalized_full_log_step(raw_step)
        if step is None:
            continue
        normalized_full_logs.append(step)
        step_name = step["step_name"]
        content = step["content"]
        if not content:
            continue
        for line in content.splitlines(keepends=True):
            line_key = _structured_cost_telemetry_line_key(line)
            if line_key is None:
                continue
            structured_line_step_names.setdefault(line_key, set()).add(step_name)

    deduped_full_logs: list[dict[str, str]] = []
    for step in normalized_full_logs:
        step_name = step["step_name"]
        content = step["content"]
        if not content:
            deduped_full_logs.append(dict(step))
            continue

        filtered_lines: list[str] = []
        for line in content.splitlines(keepends=True):
            line_key = _structured_cost_telemetry_line_key(line)
            if line_key is not None and _step_name_has_descendant_match(
                step_name,
                structured_line_step_names.get(line_key, set()),
            ):
                continue
            filtered_lines.append(line)

        step_copy = dict(step)
        step_copy["content"] = "".join(filtered_lines)
        deduped_full_logs.append(step_copy)

    return deduped_full_logs


def _cost_telemetry_text_from_full_logs(full_logs: list[dict[str, str]]) -> str:
    return _full_logs_to_text(_dedupe_structured_cost_telemetry_full_logs(full_logs))


def _run_wall_clock_ms(run: dict[str, Any]) -> int | None:
    duration_seconds = _to_int(run.get("duration_seconds"), 0)
    if duration_seconds <= 0:
        return None
    return duration_seconds * 1000


def _apply_cost_telemetry_from_full_logs(run: dict[str, Any], full_logs: list[dict[str, str]]) -> None:
    telemetry = build_run_cost_telemetry(
        _cost_telemetry_text_from_full_logs(full_logs),
        fallback_wall_clock_ms=_run_wall_clock_ms(run),
    )
    run["cost_telemetry"] = telemetry


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
            full_logs = extract_full_logs(payload)
            full_logs_by_identity[identity] = full_logs
            _apply_cost_telemetry_from_full_logs(run, full_logs)
        except Exception as exc:  # noqa: BLE001
            _append_log_error(errors, repository, run_id, exc)

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

    success_sample_rate = max(0.0, min(1.0, args.success_sample_rate))

    try:
        since_utc = _resolve_since_utc(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    token = os.getenv("GH_TOKEN", "") or os.getenv("GITHUB_TOKEN", "")
    repo_root = REPO_ROOT
    memory_branch = str(os.getenv("AI_MEMORY_BRANCH", "ai-memory")).strip() or "ai-memory"
    memory_root_relative = str(os.getenv("AI_MEMORY_ROOT", "ai-memory")).strip() or "ai-memory"
    # Keep the AI_MEMORY_PUSH_RETRIES default in sync with scripts/ai_memory.py
    # (raised 5 -> 8 to survive shared ai-memory branch contention).
    push_retries = _to_int(os.getenv("AI_MEMORY_PUSH_RETRIES", "8"), 8)

    cache_payload, _cache_load_error, cache_branch_dir = _cache_read_context(
        repo_root=repo_root,
        memory_branch=memory_branch,
        memory_root_relative=memory_root_relative,
    )
    if cache_branch_dir is not None:
        shutil.rmtree(cache_branch_dir, ignore_errors=True)

    cached_repositories = cache_payload.get("repositories") if isinstance(cache_payload, dict) else {}
    if not isinstance(cached_repositories, dict):
        cached_repositories = {}

    errors: list[dict[str, str]] = []
    run_rows: list[dict[str, Any]] = []
    successful_repo_queries = 0
    log_archive_cache: dict[tuple[str, int], bytes | Exception] | None = {} if args.log_output_dir else None

    for repo in repositories:
        repo_cache = cached_repositories.get(repo) if isinstance(cached_repositories, dict) else None
        if not isinstance(repo_cache, dict):
            repo_cache = {}

        effective_since = since_utc

        jobs_seen_set = _normalize_seen_set(repo_cache.get("jobs_seen_set"))
        cancelled_jobs_seen_set = _normalize_seen_set(repo_cache.get("cancelled_jobs_seen_set"))
        cancelled_failure_point_version = _to_int(repo_cache.get("cancelled_failure_point_version"), 0)
        logs_seen_set = _normalize_seen_set(repo_cache.get("logs_seen_set"))
        jobs_seen_lookup = set(jobs_seen_set)
        cancelled_jobs_seen_lookup = set(cancelled_jobs_seen_set)
        cached_rows_by_run_id = _index_cached_rows(repo_cache.get("rows_snapshot"))
        cached_runs_snapshot = _normalize_runs_snapshot(repo_cache.get("runs_snapshot"))
        cached_etag = repo_cache.get("runs_etag")
        if not isinstance(cached_etag, str) or not cached_etag.strip():
            cached_etag = None

        try:
            runs, capped, run_meta = list_runs_for_repo(
                repo,
                since_utc=effective_since,
                per_page=args.per_page,
                max_pages=args.max_pages,
                max_runs=args.max_runs,
                token=token,
                etag=cached_etag,
            )
            used_cached_runs = bool(run_meta.get("not_modified"))
            if used_cached_runs:
                if cached_runs_snapshot:
                    runs = [dict(item) for item in cached_runs_snapshot]
                else:
                    runs, capped, run_meta = list_runs_for_repo(
                        repo,
                        since_utc=effective_since,
                        per_page=args.per_page,
                        max_pages=args.max_pages,
                        max_runs=args.max_runs,
                        token=token,
                        etag=None,
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

        collected_rows_for_repo: list[dict[str, Any]] = []
        runs_snapshot_for_repo: list[dict[str, Any]] = []

        for run in runs:
            run_id = _to_int(run.get("id"), 0)
            run_attempt = max(1, _to_int(run.get("run_attempt"), 1))
            cache_key = _cache_run_key(run_id, run_attempt) if run_id > 0 else None
            if run_id > 0:
                runs_snapshot_for_repo.append(_run_snapshot_for_cache(run))

            reused_row = cached_rows_by_run_id.get(cache_key) if cache_key is not None else None
            needs_cancelled_backfill = _cached_row_needs_cancelled_failure_point_backfill(
                run,
                reused_row,
                cancelled_jobs_seen_lookup,
                cancelled_failure_point_version,
            )
            if run_id > 0 and run_id in jobs_seen_lookup and isinstance(reused_row, dict) and not needs_cancelled_backfill:
                row_copy = dict(reused_row)
                row_copy.pop("_success_sampled", None)
                run_rows.append(row_copy)
                collected_rows_for_repo.append(dict(row_copy))
                continue

            jobs: list[dict[str, Any]] = []
            if run_id > 0 and (run.get("conclusion") or "").lower() in {"failure", "cancelled"}:
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
                else:
                    jobs_seen_set = _touch_seen_set(jobs_seen_set, run_id)
                    jobs_seen_lookup = set(jobs_seen_set)
                    if (run.get("conclusion") or "").lower() == "cancelled":
                        cancelled_jobs_seen_set = _touch_seen_set(cancelled_jobs_seen_set, run_id)
                        cancelled_jobs_seen_lookup = set(cancelled_jobs_seen_set)

            elif run_id > 0:
                jobs_seen_set = _touch_seen_set(jobs_seen_set, run_id)
                jobs_seen_lookup = set(jobs_seen_set)

            row = compute_run_metrics(repo, run, jobs)
            run_rows.append(row)
            collected_rows_for_repo.append(dict(row))

        repo_cache_updated = {
            "runs_etag": run_meta.get("etag") if isinstance(run_meta.get("etag"), str) else cached_etag,
            "runs_window_start": _format_iso8601(effective_since),
            "jobs_seen_set": jobs_seen_set,
            "cancelled_jobs_seen_set": cancelled_jobs_seen_set,
            "cancelled_failure_point_version": max(
                cancelled_failure_point_version,
                WORKFLOW_LOG_CACHE_CANCELLED_FAILURE_POINT_VERSION,
            ),
            "logs_seen_set": logs_seen_set,
            "last_updated": _format_iso8601(datetime.now(timezone.utc)),
            "runs_snapshot": runs_snapshot_for_repo,
            "rows_snapshot": collected_rows_for_repo,
        }
        cached_repositories[repo] = repo_cache_updated

    cache_payload["repositories"] = cached_repositories

    run_rows_by_identity: dict[tuple[str, int], dict[str, Any]] = {
        _run_identity(row): row
        for row in run_rows
        if _to_int(row.get("run_id"), 0) > 0 and row.get("repository")
    }

    for run in select_notable_runs_for_logs(run_rows, args.max_log_runs, success_sample_rate=success_sample_rate):
        repository = str(run.get("repository") or "")
        run_id = _to_int(run.get("run_id"), 0)
        if not repository or run_id <= 0:
            continue

        repo_cache = cached_repositories.get(repository)
        if isinstance(repo_cache, dict):
            logs_seen_set = _normalize_seen_set(repo_cache.get("logs_seen_set"))
            logs_seen_lookup = set(logs_seen_set)
            cached_rows_by_run_id = _index_cached_rows(repo_cache.get("rows_snapshot"))
            cache_key = _cache_run_key(run_id, _to_int(run.get("run_attempt"), 1))
            if run_id in logs_seen_lookup:
                cached_row = cached_rows_by_run_id.get(cache_key)
                cached_excerpts = _cached_log_excerpts(cached_row)
                cached_cost_telemetry = _cached_cost_telemetry(cached_row)
                if cached_excerpts is not None and cached_cost_telemetry is not None:
                    run["log_excerpts"] = cached_excerpts
                    run["cost_telemetry"] = cached_cost_telemetry
                    identity = (repository, run_id)
                    canonical = run_rows_by_identity.get(identity)
                    if canonical is not None:
                        canonical["log_excerpts"] = cached_excerpts
                        canonical["cost_telemetry"] = cached_cost_telemetry
                    continue

        try:
            payload = _fetch_run_log_archive(repository, run_id, token=token, cache=log_archive_cache)
            full_logs = extract_full_logs(payload)
            log_excerpts = build_log_excerpts_from_full_logs(full_logs)
            _apply_cost_telemetry_from_full_logs(run, full_logs)

            run["log_excerpts"] = log_excerpts
            identity = (repository, run_id)
            canonical = run_rows_by_identity.get(identity)
            if canonical is not None:
                canonical["log_excerpts"] = log_excerpts
                if "cost_telemetry" in run:
                    canonical["cost_telemetry"] = dict(run["cost_telemetry"])

            if isinstance(repo_cache, dict):
                logs_seen_set = _touch_seen_set(_normalize_seen_set(repo_cache.get("logs_seen_set")), run_id)
                repo_cache["logs_seen_set"] = logs_seen_set
                rows_snapshot = _normalize_runs_snapshot(repo_cache.get("rows_snapshot"))
                updated_rows: list[dict[str, Any]] = []
                replaced = False
                for row in rows_snapshot:
                    if _cache_run_key(_to_int(row.get("run_id"), 0), _to_int(row.get("run_attempt"), 1)) == cache_key:
                        merged_row = dict(row)
                        merged_row["run_attempt"] = _to_int(run.get("run_attempt"), 1)
                        merged_row["log_excerpts"] = log_excerpts
                        if "cost_telemetry" in run:
                            merged_row["cost_telemetry"] = dict(run["cost_telemetry"])
                        updated_rows.append(merged_row)
                        replaced = True
                    else:
                        updated_rows.append(dict(row))
                if not replaced:
                    fallback_row = dict(run)
                    fallback_row.pop("_success_sampled", None)
                    updated_rows.append(fallback_row)
                repo_cache["rows_snapshot"] = updated_rows
                repo_cache["last_updated"] = _format_iso8601(datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001
            _append_log_error(errors, repository, run_id, exc)

    _cache_write_context(
        repo_root=repo_root,
        memory_branch=memory_branch,
        memory_root_relative=memory_root_relative,
        push_retries=push_retries,
        payload=cache_payload,
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
    report = build_report(repositories, run_rows, errors, success_sample_rate=success_sample_rate)
    _write_json_atomic(Path(args.output), report)
    if args.log_output_dir:
        _write_json_atomic(Path(args.log_output_dir) / "summary.json", report)

    if successful_repo_queries == 0 and errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
