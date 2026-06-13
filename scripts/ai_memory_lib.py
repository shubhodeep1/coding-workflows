#!/usr/bin/env python3
"""Shared AI memory helpers for GitHub workflows.

This module provides schema validation, deterministic retrieval, governance checks,
lineage handling, compaction, and branch-safe persistence for `ai-memory`.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from openrouter_prompt_cache import add_ephemeral_cache_breakpoint, should_retry_without_breakpoint
except ModuleNotFoundError:
    from scripts.openrouter_prompt_cache import add_ephemeral_cache_breakpoint, should_retry_without_breakpoint

try:
    from memory_injection_patterns import scan as _scan_memory_injection_patterns
except ModuleNotFoundError:
    try:
        from scripts.memory_injection_patterns import scan as _scan_memory_injection_patterns
    except ModuleNotFoundError:
        _scan_memory_injection_patterns = None

_log = logging.getLogger(__name__)

MEMORY_RECORD_SCHEMA_VERSION = "memory_record.v1"
RUN_LEDGER_SCHEMA_VERSION = "run_ledger_entry.v1"
TASK_LINEAGE_SCHEMA_VERSION = "task_lineage.v1"
PROCESSED_COMMAND_SCHEMA_VERSION = "processed_command_entry.v1"
RETRIEVAL_PROFILE_SCHEMA_VERSION = "retrieval_profiles.v1"
ACTIONS_RUNS_CACHE_SCHEMA_VERSION = "v1"
FINGERPRINT_QUARANTINE_SCHEMA_VERSION = "v1"
BRANCH_REBUILD_AUDIT_SCHEMA_VERSION = "v1"
VALIDATION_HISTORY_SCHEMA_VERSION = "v1"
OPERATOR_BYPASS_AUDIT_SCHEMA_VERSION = "v1"
REVALIDATE_EVENTS_SCHEMA_VERSION = "v1"
VALIDATION_DISCOVERY_SCHEMA_VERSION = "v1"
STATE_SNAPSHOT_SCHEMA_VERSION = "v1"
MAX_MEMORY_DETAILS_LENGTH = 12000
LEGACY_MEMORY_ROOT_RELATIVE = ".github/ai-memory"
CANONICAL_MEMORY_ROOT_RELATIVE = "ai-memory"

ALLOWED_CATEGORIES = {
    "decisions",
    "constraints",
    "patterns",
    "incidents",
    "run_events",
    "task_summaries",
}

SENSITIVE_CATEGORIES = {"incidents"}
SENSITIVE_KEYWORDS = {
    "security",
    "reliability",
    "availability",
    "incident",
    "outage",
    "breach",
    "data loss",
    "critical",
    "high-impact",
}

DEFAULT_GOVERNANCE = {
    "min_confidence": 0.6,
    "min_confidence_sensitive": 0.85,
    "require_source_refs_for_sensitive": 1,
}


class MemoryValidationError(ValueError):
    """Raised when a memory artifact fails schema/governance validation."""


class MemoryGitError(RuntimeError):
    """Raised when branch synchronization or persistence fails."""


@dataclass(frozen=True)
class RetrievalResult:
    context: str
    selected_record_ids: list[str]
    estimated_tokens: int
    role: str
    keyword_method: str  # "llm", "plain", or "none"


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def scan_candidate_text_for_injection(summary: str, details: str) -> list[str]:
    if not parse_bool(os.getenv("MEMORY_INJECTION_SCAN_ENABLED", "true"), default=True):
        return []
    if _scan_memory_injection_patterns is None:
        return []

    candidate_text = "\n".join(
        item for item in (summary.strip(), details.strip()) if item.strip()
    )
    if not candidate_text:
        return []

    try:
        return _scan_memory_injection_patterns(candidate_text)
    except Exception:
        _log.warning("AI memory injection scan failed; continuing without flag", exc_info=True)
        return []


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_segment(value: str, fallback: str = "unknown") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", (value or "").strip())
    normalized = normalized.strip("._")
    return normalized or fallback


def ensure_memory_layout(memory_root: Path) -> None:
    required_dirs = [
        memory_root / "schemas",
        memory_root / "config",
        memory_root / "global" / "canonical",
        memory_root / "tasks",
        memory_root / "runs",
        memory_root / "archive" / "monthly",
    ]
    for directory in required_dirs:
        directory.mkdir(parents=True, exist_ok=True)


def _resolve_within_base_dir(base_dir: Path, relative_path: str) -> Path:
    path_text = str(relative_path or "").strip()
    if path_text in {"", "."}:
        raise MemoryValidationError("memory_root_relative must be a non-empty relative path")

    candidate = Path(path_text)
    if candidate.is_absolute():
        raise MemoryValidationError(f"memory_root_relative must be relative: {relative_path!r}")

    resolved = (base_dir / candidate).resolve()
    base_resolved = base_dir.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise MemoryValidationError(f"memory_root_relative escapes repository root: {relative_path!r}") from exc
    return resolved


def resolve_memory_root_dir(base_dir: Path, memory_root_relative: str) -> Path:
    preferred = _resolve_within_base_dir(base_dir, memory_root_relative)
    legacy = _resolve_within_base_dir(base_dir, LEGACY_MEMORY_ROOT_RELATIVE)

    if preferred.exists():
        return preferred
    if memory_root_relative != LEGACY_MEMORY_ROOT_RELATIVE and legacy.exists():
        return legacy

    preferred.parent.mkdir(parents=True, exist_ok=True)
    return preferred


def resolve_memory_reference_source_dir(base_dir: Path, memory_root_relative: str) -> Path:
    requested = _resolve_within_base_dir(base_dir, memory_root_relative)
    canonical = _resolve_within_base_dir(base_dir, CANONICAL_MEMORY_ROOT_RELATIVE)
    legacy = _resolve_within_base_dir(base_dir, LEGACY_MEMORY_ROOT_RELATIVE)

    candidates = [requested]
    if canonical not in candidates:
        candidates.append(canonical)
    if legacy not in candidates:
        candidates.append(legacy)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return requested


def _sync_memory_reference_files(source_root: Path, destination_root: Path) -> None:
    # Resolve schemas dir: prefer the consumer-repo source tree, then fall
    # back to the workflow-source clone advertised via SUPPORT_AI_MEMORY_DIR.
    # Symmetric with the config resolution below; protects validation when
    # the consumer repo does not vendor ai-memory/schemas/.
    source_schemas: Path | None = None
    if source_root.exists():
        candidate_schemas = source_root / "schemas"
        if candidate_schemas.exists():
            source_schemas = candidate_schemas
    if source_schemas is None:
        support_dir = os.environ.get("SUPPORT_AI_MEMORY_DIR")
        if support_dir:
            candidate_support_schemas = Path(support_dir) / "schemas"
            if candidate_support_schemas.exists():
                source_schemas = candidate_support_schemas

    if source_schemas is not None:
        destination_schemas = destination_root / "schemas"
        for schema_file in sorted(source_schemas.glob("*.json")):
            if not schema_file.is_file():
                continue
            destination_schemas.mkdir(parents=True, exist_ok=True)
            destination_file = destination_schemas / schema_file.name
            if destination_file.is_symlink():
                raise MemoryValidationError(f"Refusing to overwrite symlinked schema file: {destination_file}")
            shutil.copy2(schema_file, destination_file)

    # Resolve retrieval_profiles.v1.json: prefer the consumer-repo source
    # tree (preserves prior behaviour), then fall back to the workflow-source
    # clone advertised via SUPPORT_AI_MEMORY_DIR.  Without the fallback, every
    # consumer repo that does not vendor ai-memory/config/ silently degrades
    # to an empty retrieval set with an AI_MEMORY_ERROR warning.
    config_filename = "retrieval_profiles.v1.json"
    source_config: Path | None = None
    if source_root.exists():
        candidate_in_tree = source_root / "config" / config_filename
        if candidate_in_tree.is_file():
            source_config = candidate_in_tree
    if source_config is None:
        support_dir = os.environ.get("SUPPORT_AI_MEMORY_DIR")
        if support_dir:
            candidate_support = Path(support_dir) / "config" / config_filename
            if candidate_support.is_file():
                source_config = candidate_support

    if source_config is not None:
        destination_config = destination_root / "config"
        destination_config.mkdir(parents=True, exist_ok=True)
        destination_file = destination_config / source_config.name
        if destination_file.is_symlink():
            raise MemoryValidationError(f"Refusing to overwrite symlinked config file: {destination_file}")
        shutil.copy2(source_config, destination_file)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _iter_json_files(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(path for path in base_dir.rglob("*.json") if path.is_file())


def _iter_jsonl_files(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(path for path in base_dir.rglob("*.jsonl") if path.is_file())


def _json_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "null":
        return value is None
    return False


def _validate_json_schema(document: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type is not None:
        if isinstance(expected_type, list):
            if not any(_json_type_matches(document, item_type) for item_type in expected_type):
                errors.append(f"{path}: expected one of types {expected_type}, got {type(document).__name__}")
                return errors
        else:
            if not _json_type_matches(document, expected_type):
                errors.append(f"{path}: expected type {expected_type}, got {type(document).__name__}")
                return errors

    if "const" in schema and document != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {document!r}")

    if "enum" in schema and document not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']}, got {document!r}")

    if isinstance(document, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(document) < int(min_length):
            errors.append(f"{path}: length {len(document)} is below minLength {min_length}")
        max_length = schema.get("maxLength")
        if max_length is not None and len(document) > int(max_length):
            errors.append(f"{path}: length {len(document)} exceeds maxLength {max_length}")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, document) is None:
            errors.append(f"{path}: value {document!r} does not match pattern {pattern!r}")
        format_name = schema.get("format")
        if format_name == "date-time":
            try:
                parsed = datetime.fromisoformat(document.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timezone is required")
            except ValueError:
                errors.append(f"{path}: value {document!r} is not a valid date-time string")

    if isinstance(document, (int, float)) and not isinstance(document, bool):
        if isinstance(document, float) and not math.isfinite(document):
            errors.append(f"{path}: value {document!r} must be finite")
            return errors
        minimum = schema.get("minimum")
        if minimum is not None and document < minimum:
            errors.append(f"{path}: value {document} is below minimum {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and document > maximum:
            errors.append(f"{path}: value {document} exceeds maximum {maximum}")

    if isinstance(document, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in document:
                errors.append(f"{path}: missing required field {key!r}")

        properties = schema.get("properties", {})
        for key, value in document.items():
            if key in properties:
                errors.extend(_validate_json_schema(value, properties[key], f"{path}.{key}"))

        additional_properties = schema.get("additionalProperties", True)
        if additional_properties is False:
            unknown_keys = sorted(set(document.keys()) - set(properties.keys()))
            for key in unknown_keys:
                errors.append(f"{path}: additional property {key!r} is not allowed")

    if isinstance(document, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(document):
                errors.extend(_validate_json_schema(item, item_schema, f"{path}[{index}]"))

    return errors


def _validate_with_schema_file(document: dict[str, Any], schema_file: Path) -> None:
    if not schema_file.exists():
        raise MemoryValidationError(f"Missing schema file: {schema_file}")
    schema = _load_json(schema_file)
    errors = _validate_json_schema(document, schema)
    if errors:
        error_text = "\n".join(f"- {item}" for item in errors)
        raise MemoryValidationError(f"Schema validation failed for {schema_file.name}:\n{error_text}")


def _schema_file(memory_root: Path, name: str) -> Path:
    return memory_root / "schemas" / name


def validate_memory_record(record: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(record, _schema_file(memory_root, "memory_record.v1.json"))


def validate_run_ledger_entry(entry: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(entry, _schema_file(memory_root, "run_ledger_entry.v1.json"))


def validate_task_lineage(lineage: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(lineage, _schema_file(memory_root, "task_lineage.v1.json"))


def validate_processed_command_entry(entry: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(entry, _schema_file(memory_root, "processed_command_entry.v1.json"))


def _default_workflow_log_analysis_cache() -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "repositories": {},
    }


def validate_workflow_log_analysis_cache(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "workflow_log_analysis_cache.v1.json"))


def get_workflow_log_analysis_cache(memory_root: Path) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    cache_path = memory_root / "global" / "cache" / "workflow_log_analysis_cache.v1.json"
    if not cache_path.exists():
        return _default_workflow_log_analysis_cache()
    try:
        payload = _load_json(cache_path)
        if not isinstance(payload, dict):
            raise MemoryValidationError("workflow_log_analysis_cache must be a JSON object")
        validate_workflow_log_analysis_cache(payload, memory_root)
        return payload
    except (MemoryValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"::warning::rate_limit_audit_fallback workflow-log-analysis cache read failed: {exc}", file=sys.stderr)
        return _default_workflow_log_analysis_cache()


def put_workflow_log_analysis_cache(memory_root: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    try:
        if not isinstance(payload, dict):
            raise MemoryValidationError("workflow_log_analysis_cache payload must be an object")
        validate_workflow_log_analysis_cache(payload, memory_root)
        cache_path = memory_root / "global" / "cache" / "workflow_log_analysis_cache.v1.json"
        _atomic_write_json(cache_path, payload)
        return payload
    except (MemoryValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"::warning::rate_limit_audit_fallback workflow-log-analysis cache write failed: {exc}", file=sys.stderr)
        return None


def infer_sensitive(category: str, summary: str, details: str, explicit_sensitive: bool | None = None) -> bool:
    if explicit_sensitive is not None:
        return bool(explicit_sensitive)
    if category in SENSITIVE_CATEGORIES:
        return True
    if category not in {"constraints", "decisions"}:
        return False
    haystack = f"{summary} {details}".lower()
    return any(keyword in haystack for keyword in SENSITIVE_KEYWORDS)


def compute_fingerprint(category: str, summary: str, details: str, issue_number: int | None, pr_number: int | None) -> str:
    seed = "\n".join(
        [
            category.strip().lower(),
            str(issue_number or ""),
            str(pr_number or ""),
            " ".join(summary.split()).strip().lower(),
            " ".join(details.split()).strip().lower(),
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def normalize_text_for_hash(text: str) -> str:
    """Normalize free-form text for stable loop-detection hashing.

    The normalizer intentionally strips markdown punctuation, lowercases the
    content, and collapses whitespace so semantically identical clarify prompts
    hash to the same value across minor formatting drift.
    """

    lowered = (text or "").lower()
    without_markdown = re.sub(r"[*_`>#~\[\]()|:-]", " ", lowered)
    return re.sub(r"\s+", " ", without_markdown).strip()


def compute_normalized_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text_for_hash(text).encode("utf-8")).hexdigest()


def make_record_id(prefix: str = "mem") -> str:
    return f"{sanitize_segment(prefix, 'mem')}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:10]}"


def _build_scope(issue_number: int | None, pr_number: int | None, run_id: str | None) -> dict[str, Any]:
    if run_id:
        level = "run"
    elif issue_number is not None:
        level = "task"
    else:
        level = "global"
    return {
        "level": level,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "run_id": run_id,
    }


def _build_lineage(
    issue_number: int | None,
    pr_number: int | None,
    run_id: str | None,
    parent_ids: list[str] | None,
    supersedes: str | None,
) -> dict[str, Any]:
    return {
        "issue_number": issue_number,
        "pr_number": pr_number,
        "run_id": run_id,
        "parent_ids": sorted(set(parent_ids or [])),
        "supersedes": supersedes,
        "superseded_by": None,
    }


def _build_provenance(
    workflow: str,
    run_id: str | None,
    run_attempt: int | None,
    actor: str,
    source_refs: list[str] | None,
) -> dict[str, Any]:
    return {
        "workflow": workflow,
        "run_id": run_id,
        "run_attempt": int(run_attempt or 0),
        "actor": actor,
        "source_refs": sorted(set(source_refs or [])),
    }


def _record_path_for_candidate(memory_root: Path, record: dict[str, Any]) -> Path:
    issue_number = record.get("lineage", {}).get("issue_number")
    issue_key = f"issue-{int(issue_number)}" if issue_number else "issue-unscoped"
    return memory_root / "tasks" / issue_key / "candidates" / f"{record['record_id']}.json"


def _record_path_for_canonical(memory_root: Path, record: dict[str, Any]) -> Path:
    category = sanitize_segment(str(record.get("category") or "uncategorized"), "uncategorized")
    return memory_root / "global" / "canonical" / category / f"{record['record_id']}.json"


def _lineage_path(memory_root: Path, issue_number: int) -> Path:
    return memory_root / "tasks" / f"issue-{int(issue_number)}" / "lineage" / "task_lineage.v1.json"


def _run_ledger_path(memory_root: Path, run_id: str) -> Path:
    safe_run_id = sanitize_segment(run_id, "run-unknown")
    return memory_root / "runs" / safe_run_id / "ledger" / "events.jsonl"


def load_run_ledger_entries(memory_root: Path, run_id: str) -> list[dict[str, Any]]:
    ensure_memory_layout(memory_root)
    ledger_path = _run_ledger_path(memory_root, run_id)
    if not ledger_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with ledger_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MemoryValidationError(
                    f"Invalid JSON in run ledger {ledger_path} at line {line_number}"
                ) from exc
            if not isinstance(payload, dict):
                raise MemoryValidationError(
                    f"Run ledger entry in {ledger_path} at line {line_number} must be a JSON object"
                )
            validate_run_ledger_entry(payload, memory_root)
            entries.append(payload)
    return entries


def _validate_repository_name(repository: str) -> str:
    normalized = (repository or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", normalized) is None:
        raise MemoryValidationError(
            "repository must match owner/repo using only alnum, '.', '_', and '-'"
        )
    return normalized


def _validate_positive_int_field(value: int | str, field_name: str) -> int:
    if isinstance(value, bool):
        raise MemoryValidationError(f"{field_name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(f"{field_name} must be a positive integer") from exc
    if parsed < 1:
        raise MemoryValidationError(f"{field_name} must be a positive integer")
    return parsed


def _validate_integration_branch_name(integration_branch: str) -> str:
    normalized = (integration_branch or "").strip()
    if not normalized:
        raise MemoryValidationError("integration_branch is required")
    if len(normalized) > 255:
        raise MemoryValidationError("integration_branch must be 255 characters or fewer")
    return normalized


def _normalize_repository_name(repository: str) -> str:
    return _validate_repository_name(repository).lower()


def _validate_integration_sha(integration_sha: str) -> str:
    normalized = (integration_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{7,64}", normalized) is None:
        raise MemoryValidationError("integration_sha must be 7-64 hexadecimal characters")
    return normalized


def _normalize_required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MemoryValidationError(f"{field_name} is required")
    return text


def _normalize_optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _normalize_optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _validate_positive_int_field(value, field_name)


def _normalize_datetime_utc(value: Any, field_name: str) -> str:
    text = _normalize_required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryValidationError(f"{field_name} must be a valid date-time string") from exc
    if parsed.tzinfo is None:
        raise MemoryValidationError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _actions_runs_cache_path(memory_root: Path, repository: str) -> Path:
    normalized_repo = _validate_repository_name(repository)
    owner, repo = normalized_repo.split("/", 1)
    owner_key = sanitize_segment(owner.lower(), "owner")
    repo_key = sanitize_segment(repo.lower(), "repo")
    return memory_root / "orchestrator" / "actions_runs_cache" / f"{owner_key}__{repo_key}.json"


def _branch_rebuild_audit_path(
    memory_root: Path,
    repository: str,
    tracking_issue_number: int,
    integration_branch: str,
) -> Path:
    normalized_repo = _validate_repository_name(repository)
    tracking_issue = _validate_positive_int_field(tracking_issue_number, "tracking_issue_number")
    normalized_branch = _validate_integration_branch_name(integration_branch)
    owner, repo = normalized_repo.split("/", 1)
    owner_key = sanitize_segment(owner.lower(), "owner")
    repo_key = sanitize_segment(repo.lower(), "repo")
    branch_key = sanitize_segment(normalized_branch.lower(), "branch")
    return (
        memory_root
        / "orchestrator"
        / "branch_rebuild_audits"
        / f"{owner_key}__{repo_key}"
        / f"issue-{tracking_issue}__{branch_key}.json"
    )


def validate_actions_runs_cache_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "actions_runs_cache.v1.json"))


def _validation_history_path(memory_root: Path, repository: str, integration_sha: str) -> Path:
    normalized_repo = _normalize_repository_name(repository)
    normalized_sha = _validate_integration_sha(integration_sha)
    owner, repo = normalized_repo.split("/", 1)
    repo_key = f"{sanitize_segment(owner, 'owner')}__{sanitize_segment(repo, 'repo')}"
    return memory_root / "orchestrator" / "validation_history" / repo_key / normalized_sha[:2] / f"{normalized_sha}.json"


def _operator_bypass_audit_path(
    memory_root: Path,
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
) -> Path:
    normalized_repo = _normalize_repository_name(repository)
    tracking_issue = _validate_positive_int_field(tracking_issue_number, "tracking_issue_number")
    normalized_sha = _validate_integration_sha(integration_sha)
    owner, repo = normalized_repo.split("/", 1)
    repo_key = f"{sanitize_segment(owner, 'owner')}__{sanitize_segment(repo, 'repo')}"
    return (
        memory_root
        / "orchestrator"
        / "operator_bypass_audits"
        / repo_key
        / f"issue-{tracking_issue}"
        / f"{normalized_sha}.json"
    )


def _revalidate_events_path(
    memory_root: Path,
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
) -> Path:
    normalized_repo = _normalize_repository_name(repository)
    tracking_issue = _validate_positive_int_field(tracking_issue_number, "tracking_issue_number")
    normalized_sha = _validate_integration_sha(integration_sha)
    owner, repo = normalized_repo.split("/", 1)
    repo_key = f"{sanitize_segment(owner, 'owner')}__{sanitize_segment(repo, 'repo')}"
    return (
        memory_root
        / "orchestrator"
        / "revalidate_events"
        / repo_key
        / f"issue-{tracking_issue}"
        / f"{normalized_sha}.json"
    )


def validate_validation_history_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "validation_history.v1.json"))


def validate_operator_bypass_audit_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "operator_bypass_audit.v1.json"))


def validate_revalidate_events_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "revalidate_events.v1.json"))


def validate_state_snapshot_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "state_snapshot.v1.json"))


def _default_validation_history_payload(repository: str, integration_sha: str) -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_HISTORY_SCHEMA_VERSION,
        "repository": _normalize_repository_name(repository),
        "integration_sha": _validate_integration_sha(integration_sha),
        "entries": [],
    }


def _default_operator_bypass_audit_payload(
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": OPERATOR_BYPASS_AUDIT_SCHEMA_VERSION,
        "repository": _normalize_repository_name(repository),
        "tracking_issue_number": _validate_positive_int_field(tracking_issue_number, "tracking_issue_number"),
        "integration_sha": _validate_integration_sha(integration_sha),
        "entries": [],
    }


def _default_revalidate_events_payload(
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
) -> dict[str, Any]:
    return {
        "schema_version": REVALIDATE_EVENTS_SCHEMA_VERSION,
        "repository": _normalize_repository_name(repository),
        "tracking_issue_number": _validate_positive_int_field(tracking_issue_number, "tracking_issue_number"),
        "integration_sha": _validate_integration_sha(integration_sha),
        "entries": [],
    }


def _normalize_validation_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MemoryValidationError("validation_history entry must be a JSON object")
    return {
        "outcome": _normalize_required_text(entry.get("outcome"), "validation_history.entries[].outcome"),
        "raw_status": _normalize_optional_text(entry.get("raw_status")),
        "raw_conclusion": _normalize_optional_text(entry.get("raw_conclusion")),
        "run_id": _normalize_optional_positive_int(entry.get("run_id"), "validation_history.entries[].run_id"),
        "run_attempt": _normalize_optional_positive_int(
            entry.get("run_attempt"), "validation_history.entries[].run_attempt"
        ),
        "run_url": _normalize_optional_text(entry.get("run_url")),
        "recorded_at": _normalize_datetime_utc(entry.get("recorded_at"), "validation_history.entries[].recorded_at"),
        "cycle": _normalize_optional_positive_int(entry.get("cycle"), "validation_history.entries[].cycle"),
        "context": _normalize_optional_text(entry.get("context")),
        "source": _normalize_optional_text(entry.get("source")),
    }


def _normalize_operator_bypass_audit_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MemoryValidationError("operator_bypass_audit entry must be a JSON object")
    return {
        "actor": _normalize_required_text(entry.get("actor"), "operator_bypass_audit.entries[].actor"),
        "timestamp_utc": _normalize_datetime_utc(
            entry.get("timestamp_utc"), "operator_bypass_audit.entries[].timestamp_utc"
        ),
        "bypass_kind": _normalize_required_text(
            entry.get("bypass_kind"), "operator_bypass_audit.entries[].bypass_kind"
        ),
        "reason": _normalize_optional_text(entry.get("reason")),
        "validation_context": _normalize_optional_text(entry.get("validation_context")),
        "source_comment_id": _normalize_optional_positive_int(
            entry.get("source_comment_id"), "operator_bypass_audit.entries[].source_comment_id"
        ),
        "source_comment_url": _normalize_optional_text(entry.get("source_comment_url")),
    }


def _normalize_revalidate_event_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MemoryValidationError("revalidate_events entry must be a JSON object")
    return {
        "actor": _normalize_required_text(entry.get("actor"), "revalidate_events.entries[].actor"),
        "timestamp_utc": _normalize_datetime_utc(entry.get("timestamp_utc"), "revalidate_events.entries[].timestamp_utc"),
        "prior_outcome": _normalize_optional_text(entry.get("prior_outcome")),
        "prior_context": _normalize_optional_text(entry.get("prior_context")),
        "reason": _normalize_optional_text(entry.get("reason")),
        "source_comment_id": _normalize_optional_positive_int(
            entry.get("source_comment_id"), "revalidate_events.entries[].source_comment_id"
        ),
        "source_comment_url": _normalize_optional_text(entry.get("source_comment_url")),
    }


def _stable_sort_entries_by_field(entries: list[dict[str, Any]], *, field_name: str) -> list[dict[str, Any]]:
    indexed_entries = list(enumerate(entries))
    indexed_entries.sort(key=lambda item: (str(item[1].get(field_name) or ""), item[0]))
    return [entry for _, entry in indexed_entries]


def _normalize_validation_history_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MemoryValidationError("validation_history payload must be a JSON object")
    entries = [_normalize_validation_history_entry(entry) for entry in payload.get("entries") or []]
    entries = _stable_sort_entries_by_field(entries, field_name="recorded_at")
    return {
        "schema_version": VALIDATION_HISTORY_SCHEMA_VERSION,
        "repository": _normalize_repository_name(str(payload.get("repository") or "")),
        "integration_sha": _validate_integration_sha(str(payload.get("integration_sha") or "")),
        "entries": entries,
    }


def _normalize_operator_bypass_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MemoryValidationError("operator_bypass_audit payload must be a JSON object")
    entries = [_normalize_operator_bypass_audit_entry(entry) for entry in payload.get("entries") or []]
    entries = _stable_sort_entries_by_field(entries, field_name="timestamp_utc")
    return {
        "schema_version": OPERATOR_BYPASS_AUDIT_SCHEMA_VERSION,
        "repository": _normalize_repository_name(str(payload.get("repository") or "")),
        "tracking_issue_number": _validate_positive_int_field(payload.get("tracking_issue_number"), "tracking_issue_number"),
        "integration_sha": _validate_integration_sha(str(payload.get("integration_sha") or "")),
        "entries": entries,
    }


def _normalize_revalidate_events_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MemoryValidationError("revalidate_events payload must be a JSON object")
    entries = [_normalize_revalidate_event_entry(entry) for entry in payload.get("entries") or []]
    entries = _stable_sort_entries_by_field(entries, field_name="timestamp_utc")
    return {
        "schema_version": REVALIDATE_EVENTS_SCHEMA_VERSION,
        "repository": _normalize_repository_name(str(payload.get("repository") or "")),
        "tracking_issue_number": _validate_positive_int_field(payload.get("tracking_issue_number"), "tracking_issue_number"),
        "integration_sha": _validate_integration_sha(str(payload.get("integration_sha") or "")),
        "entries": entries,
    }


def _default_fingerprint_quarantine_payload() -> dict[str, Any]:
    return {
        "schema_version": FINGERPRINT_QUARANTINE_SCHEMA_VERSION,
        "entries": [],
    }


def _fingerprint_quarantine_path(memory_root: Path) -> Path:
    return memory_root / "orchestrator" / "fingerprint_quarantine.v1.json"


def validate_fingerprint_quarantine_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "fingerprint_quarantine.v1.json"))


def _normalize_fingerprint_quarantine_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entries.append(
            {
                "fp_key": list(entry.get("fp_key") or []),
                "issue_key": entry.get("issue_key"),
                "first_seen_run_id": entry.get("first_seen_run_id"),
                "last_seen_run_id": entry.get("last_seen_run_id"),
                "consecutive_unchanged_runs": int(entry.get("consecutive_unchanged_runs") or 1),
            }
        )
    entries.sort(key=lambda item: (str(item.get("issue_key") or ""), tuple(item.get("fp_key") or [])))
    return {
        "schema_version": payload.get("schema_version"),
        "entries": entries,
    }


def get_fingerprint_quarantine(memory_root: Path) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    quarantine_path = _fingerprint_quarantine_path(memory_root)
    if not quarantine_path.exists():
        return _default_fingerprint_quarantine_payload()
    payload = _load_json(quarantine_path)
    if not isinstance(payload, dict):
        raise MemoryValidationError("fingerprint_quarantine payload must be a JSON object")
    validate_fingerprint_quarantine_payload(payload, memory_root)
    return _normalize_fingerprint_quarantine_payload(payload)


def put_fingerprint_quarantine(memory_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not isinstance(payload, dict):
        raise MemoryValidationError("fingerprint_quarantine payload must be an object")
    validate_fingerprint_quarantine_payload(payload, memory_root)
    normalized = _normalize_fingerprint_quarantine_payload(payload)
    validate_fingerprint_quarantine_payload(normalized, memory_root)
    _atomic_write_json(_fingerprint_quarantine_path(memory_root), normalized)
    return normalized


def validate_branch_rebuild_audit_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "branch_rebuild_audit.v1.json"))


def get_validation_history(memory_root: Path, repository: str, integration_sha: str) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    history_path = _validation_history_path(memory_root, repository, integration_sha)
    if not history_path.exists():
        return None
    payload = _load_json(history_path)
    if not isinstance(payload, dict):
        raise MemoryValidationError("validation_history payload must be a JSON object")
    validate_validation_history_payload(payload, memory_root)
    normalized = _normalize_validation_history_payload(payload)
    validate_validation_history_payload(normalized, memory_root)
    return normalized


def append_validation_history_entry(
    memory_root: Path,
    *,
    repository: str,
    integration_sha: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not isinstance(entry, dict):
        raise MemoryValidationError("validation history entry must be a JSON object")
    normalized_repo = _normalize_repository_name(repository)
    normalized_sha = _validate_integration_sha(integration_sha)
    with _file_lock(f"validation-history:{normalized_repo}:{normalized_sha}"):
        payload = get_validation_history(memory_root, normalized_repo, normalized_sha)
        if payload is None:
            payload = _default_validation_history_payload(normalized_repo, normalized_sha)
        payload["entries"] = [*(payload.get("entries") or []), _normalize_validation_history_entry(entry)]
        normalized = _normalize_validation_history_payload(payload)
        validate_validation_history_payload(normalized, memory_root)
        _atomic_write_json(_validation_history_path(memory_root, normalized_repo, normalized_sha), normalized)
        return normalized


_VALIDATION_DISCOVERY_OUTCOMES = (
    "success_seeded",
    "success_agree",
    "success_disagree",
    "dry_run",
    "failed",
    "push_denied",
)


def _validation_discovery_path(memory_root: Path, repository: str) -> Path:
    normalized_repo = _normalize_repository_name(repository)
    owner, repo = normalized_repo.split("/", 1)
    repo_key = f"{sanitize_segment(owner, 'owner')}__{sanitize_segment(repo, 'repo')}"
    return memory_root / "orchestrator" / "validation_discovery" / repo_key / "history.json"


def validate_validation_discovery_payload(payload: dict[str, Any], memory_root: Path) -> None:
    _validate_with_schema_file(payload, _schema_file(memory_root, "validation_discovery.v1.json"))


def _default_validation_discovery_payload(repository: str) -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_DISCOVERY_SCHEMA_VERSION,
        "repository": _normalize_repository_name(repository),
        "entries": [],
    }


def _normalize_validation_discovery_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MemoryValidationError("validation_discovery entry must be a JSON object")
    outcome = _normalize_required_text(entry.get("outcome"), "validation_discovery.entries[].outcome")
    if outcome not in _VALIDATION_DISCOVERY_OUTCOMES:
        raise MemoryValidationError(
            f"validation_discovery.entries[].outcome must be one of {_VALIDATION_DISCOVERY_OUTCOMES}, got {outcome!r}"
        )
    head_sha_raw = entry.get("consumer_head_sha")
    head_sha: str | None
    if head_sha_raw in (None, ""):
        head_sha = None
    else:
        head_sha = _validate_integration_sha(str(head_sha_raw))
    return {
        "outcome": outcome,
        "recorded_at": _normalize_datetime_utc(
            entry.get("recorded_at"), "validation_discovery.entries[].recorded_at"
        ),
        "consumer_head_sha": head_sha,
        "consumer_default_branch": _normalize_optional_text(entry.get("consumer_default_branch")),
        "discovered_type": _normalize_optional_text(entry.get("discovered_type")),
        "committed_type": _normalize_optional_text(entry.get("committed_type")),
        "pr_url": _normalize_optional_text(entry.get("pr_url")),
        "pr_branch": _normalize_optional_text(entry.get("pr_branch")),
        "codex_attempts_used": _normalize_optional_positive_int(
            entry.get("codex_attempts_used"), "validation_discovery.entries[].codex_attempts_used"
        ),
        "failure_reason": _normalize_optional_text(entry.get("failure_reason")),
    }


def _normalize_validation_discovery_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MemoryValidationError("validation_discovery payload must be a JSON object")
    entries = [_normalize_validation_discovery_entry(entry) for entry in payload.get("entries") or []]
    entries = _stable_sort_entries_by_field(entries, field_name="recorded_at")
    return {
        "schema_version": VALIDATION_DISCOVERY_SCHEMA_VERSION,
        "repository": _normalize_repository_name(str(payload.get("repository") or "")),
        "entries": entries,
    }


def get_validation_discovery(memory_root: Path, repository: str) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    discovery_path = _validation_discovery_path(memory_root, repository)
    if not discovery_path.exists():
        return None
    payload = _load_json(discovery_path)
    if not isinstance(payload, dict):
        raise MemoryValidationError("validation_discovery payload must be a JSON object")
    validate_validation_discovery_payload(payload, memory_root)
    normalized = _normalize_validation_discovery_payload(payload)
    validate_validation_discovery_payload(normalized, memory_root)
    return normalized


def append_validation_discovery_entry(
    memory_root: Path,
    *,
    repository: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not isinstance(entry, dict):
        raise MemoryValidationError("validation discovery entry must be a JSON object")
    normalized_repo = _normalize_repository_name(repository)
    with _file_lock(f"validation-discovery:{normalized_repo}"):
        payload = get_validation_discovery(memory_root, normalized_repo)
        if payload is None:
            payload = _default_validation_discovery_payload(normalized_repo)
        payload["entries"] = [
            *(payload.get("entries") or []),
            _normalize_validation_discovery_entry(entry),
        ]
        normalized = _normalize_validation_discovery_payload(payload)
        validate_validation_discovery_payload(normalized, memory_root)
        _atomic_write_json(_validation_discovery_path(memory_root, normalized_repo), normalized)
        return normalized


def get_operator_bypass_audit(
    memory_root: Path,
    *,
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    audit_path = _operator_bypass_audit_path(memory_root, repository, tracking_issue_number, integration_sha)
    if not audit_path.exists():
        return None
    payload = _load_json(audit_path)
    if not isinstance(payload, dict):
        raise MemoryValidationError("operator_bypass_audit payload must be a JSON object")
    validate_operator_bypass_audit_payload(payload, memory_root)
    normalized = _normalize_operator_bypass_audit_payload(payload)
    validate_operator_bypass_audit_payload(normalized, memory_root)
    return normalized


def append_operator_bypass_audit_entry(
    memory_root: Path,
    *,
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not isinstance(entry, dict):
        raise MemoryValidationError("operator bypass audit entry must be a JSON object")
    normalized_repo = _normalize_repository_name(repository)
    normalized_tracking_issue = _validate_positive_int_field(tracking_issue_number, "tracking_issue_number")
    normalized_sha = _validate_integration_sha(integration_sha)
    with _file_lock(f"operator-bypass-audit:{normalized_repo}:{normalized_tracking_issue}:{normalized_sha}"):
        payload = get_operator_bypass_audit(
            memory_root,
            repository=normalized_repo,
            tracking_issue_number=normalized_tracking_issue,
            integration_sha=normalized_sha,
        )
        if payload is None:
            payload = _default_operator_bypass_audit_payload(normalized_repo, normalized_tracking_issue, normalized_sha)
        payload["entries"] = [*(payload.get("entries") or []), _normalize_operator_bypass_audit_entry(entry)]
        normalized = _normalize_operator_bypass_audit_payload(payload)
        validate_operator_bypass_audit_payload(normalized, memory_root)
        _atomic_write_json(
            _operator_bypass_audit_path(memory_root, normalized_repo, normalized_tracking_issue, normalized_sha),
            normalized,
        )
        return normalized


def get_revalidate_events(
    memory_root: Path,
    *,
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    events_path = _revalidate_events_path(memory_root, repository, tracking_issue_number, integration_sha)
    if not events_path.exists():
        return None
    payload = _load_json(events_path)
    if not isinstance(payload, dict):
        raise MemoryValidationError("revalidate_events payload must be a JSON object")
    validate_revalidate_events_payload(payload, memory_root)
    normalized = _normalize_revalidate_events_payload(payload)
    validate_revalidate_events_payload(normalized, memory_root)
    return normalized


def append_revalidate_event(
    memory_root: Path,
    *,
    repository: str,
    tracking_issue_number: int,
    integration_sha: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not isinstance(entry, dict):
        raise MemoryValidationError("revalidate event entry must be a JSON object")
    normalized_repo = _normalize_repository_name(repository)
    normalized_tracking_issue = _validate_positive_int_field(tracking_issue_number, "tracking_issue_number")
    normalized_sha = _validate_integration_sha(integration_sha)
    with _file_lock(f"revalidate-events:{normalized_repo}:{normalized_tracking_issue}:{normalized_sha}"):
        payload = get_revalidate_events(
            memory_root,
            repository=normalized_repo,
            tracking_issue_number=normalized_tracking_issue,
            integration_sha=normalized_sha,
        )
        if payload is None:
            payload = _default_revalidate_events_payload(normalized_repo, normalized_tracking_issue, normalized_sha)
        payload["entries"] = [*(payload.get("entries") or []), _normalize_revalidate_event_entry(entry)]
        normalized = _normalize_revalidate_events_payload(payload)
        validate_revalidate_events_payload(normalized, memory_root)
        _atomic_write_json(
            _revalidate_events_path(memory_root, normalized_repo, normalized_tracking_issue, normalized_sha),
            normalized,
        )
        return normalized


def get_actions_runs_cache(memory_root: Path, repository: str) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    cache_path = _actions_runs_cache_path(memory_root, repository)
    if not cache_path.exists():
        return None
    payload = _load_json(cache_path)
    validate_actions_runs_cache_payload(payload, memory_root)
    return payload


def put_actions_runs_cache(
    memory_root: Path,
    *,
    repository: str,
    runs: list[dict[str, Any]],
    etag: str | None,
    ttl_seconds: int,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    payload = {
        "schema_version": ACTIONS_RUNS_CACHE_SCHEMA_VERSION,
        "repository": _validate_repository_name(repository),
        "fetched_at": fetched_at or utc_now_iso(),
        "ttl_seconds": int(ttl_seconds),
        "etag": (etag or None),
        "runs": runs,
    }
    validate_actions_runs_cache_payload(payload, memory_root)
    _atomic_write_json(_actions_runs_cache_path(memory_root, repository), payload)
    return payload


def get_branch_rebuild_audit(
    memory_root: Path,
    *,
    repository: str,
    tracking_issue_number: int,
    integration_branch: str,
) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    audit_path = _branch_rebuild_audit_path(memory_root, repository, tracking_issue_number, integration_branch)
    if not audit_path.exists():
        return None
    payload = _load_json(audit_path)
    validate_branch_rebuild_audit_payload(payload, memory_root)
    return payload


def put_branch_rebuild_audit(
    memory_root: Path,
    *,
    repository: str,
    tracking_issue_number: int,
    integration_branch: str,
    audit: dict[str, Any],
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not isinstance(audit, dict):
        raise MemoryValidationError("branch rebuild audit must be a JSON object")
    normalized_repo = _validate_repository_name(repository)
    normalized_tracking_issue = _validate_positive_int_field(tracking_issue_number, "tracking_issue_number")
    normalized_branch = _validate_integration_branch_name(integration_branch)
    payload = dict(audit)
    payload["schema_version"] = BRANCH_REBUILD_AUDIT_SCHEMA_VERSION
    payload["repository"] = normalized_repo
    payload["tracking_issue_number"] = normalized_tracking_issue
    payload["integration_branch"] = normalized_branch
    validate_branch_rebuild_audit_payload(payload, memory_root)
    _atomic_write_json(
        _branch_rebuild_audit_path(memory_root, normalized_repo, normalized_tracking_issue, normalized_branch),
        payload,
    )
    return payload


def make_processed_command_entry_id(issue_number: int, comment_id: int, command: str) -> str:
    normalized_command = sanitize_segment(command.strip().lower(), "command")
    return f"processed_command_{issue_number}_{comment_id}_{normalized_command}"


def _processed_command_path(memory_root: Path, issue_number: int, comment_id: int, command: str) -> Path:
    entry_id = make_processed_command_entry_id(issue_number, comment_id, command)
    return memory_root / "tasks" / f"issue-{int(issue_number)}" / "processed_commands" / f"{entry_id}.json"


def get_processed_command_entry(
    memory_root: Path,
    *,
    issue_number: int,
    comment_id: int,
    command: str,
) -> dict[str, Any] | None:
    ensure_memory_layout(memory_root)
    entry_path = _processed_command_path(memory_root, issue_number, comment_id, command)
    if not entry_path.exists():
        return None
    payload = _load_json(entry_path)
    validate_processed_command_entry(payload, memory_root)
    return payload


def list_processed_command_entries(
    memory_root: Path,
    *,
    issue_number: int,
    command: str | None = None,
    workflow: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    ensure_memory_layout(memory_root)
    processed_dir = memory_root / "tasks" / f"issue-{int(issue_number)}" / "processed_commands"
    entries: list[dict[str, Any]] = []
    command_filter = command.strip().lower() if command else None
    workflow_filter = workflow.strip().lower() if workflow else None
    status_filter = status.strip().lower() if status else None
    for entry_path in _iter_json_files(processed_dir):
        try:
            payload = _load_json(entry_path)
            validate_processed_command_entry(payload, memory_root)
        except (MemoryValidationError, OSError, ValueError) as exc:
            _log.warning("Skipping invalid processed command entry %s: %s", entry_path, exc)
            continue
        if command and str(payload.get("command") or "").strip().lower() != command_filter:
            continue
        if workflow and str(payload.get("workflow") or "").strip().lower() != workflow_filter:
            continue
        if status and str(payload.get("status") or "").strip().lower() != status_filter:
            continue
        entries.append(payload)
    return sorted(entries, key=lambda item: (str(item.get("timestamp") or ""), int(item.get("comment_id") or 0)))


def evaluate_clarify_loop_guard(
    entries: list[dict[str, Any]],
    *,
    clarify_hash: str,
    max_cycles: int,
    current_comment_id: int | None = None,
) -> dict[str, Any]:
    normalized_hash = sanitize_segment((clarify_hash or "").strip().lower(), "")
    if not normalized_hash:
        raise MemoryValidationError("clarify_hash is required for loop guard evaluation")

    cycle_limit = max(1, int(max_cycles))
    prior_entries: list[dict[str, Any]] = []
    for entry in entries:
        if current_comment_id is not None and int(entry.get("comment_id") or 0) == int(current_comment_id):
            continue
        prior_entries.append(entry)

    answered_entries = [entry for entry in prior_entries if str(entry.get("status") or "").strip().lower() == "answered"]
    answered_entries.sort(key=lambda item: (str(item.get("timestamp") or ""), int(item.get("comment_id") or 0)))
    next_cycle = len(answered_entries) + 1

    same_hash_entries = []
    for entry in answered_entries:
        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            continue
        existing_hash = str(metadata.get("clarify_hash") or "").strip().lower()
        if existing_hash == normalized_hash:
            same_hash_entries.append(entry)

    if same_hash_entries:
        previous_entry = same_hash_entries[-1]
        return {
            "blocked": True,
            "reason": "repeat_clarify_hash",
            "cycle": next_cycle,
            "max_cycles": cycle_limit,
            "previous_clarify_comment_id": int(previous_entry.get("comment_id") or 0),
            "previous_answer_comment_id": int((previous_entry.get("metadata") or {}).get("answer_comment_id") or 0),
            "prior_answer_count": len(answered_entries),
        }

    if next_cycle > cycle_limit:
        previous_entry = answered_entries[-1] if answered_entries else None
        previous_comment_id = int(previous_entry.get("comment_id") or 0) if previous_entry else 0
        previous_answer_id = int((previous_entry.get("metadata") or {}).get("answer_comment_id") or 0) if previous_entry else 0
        return {
            "blocked": True,
            "reason": "max_cycles_exceeded",
            "cycle": next_cycle,
            "max_cycles": cycle_limit,
            "previous_clarify_comment_id": previous_comment_id,
            "previous_answer_comment_id": previous_answer_id,
            "prior_answer_count": len(answered_entries),
        }

    return {
        "blocked": False,
        "reason": "none",
        "cycle": next_cycle,
        "max_cycles": cycle_limit,
        "previous_clarify_comment_id": 0,
        "previous_answer_comment_id": 0,
        "prior_answer_count": len(answered_entries),
    }


def claim_processed_command(
    memory_root: Path,
    *,
    issue_number: int,
    comment_id: int,
    command: str,
    workflow: str,
    actor: str,
    run_id: str,
    run_attempt: int,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    ensure_memory_layout(memory_root)
    entry_id = make_processed_command_entry_id(issue_number, comment_id, command)
    entry_path = _processed_command_path(memory_root, issue_number, comment_id, command)
    with _file_lock(f"processed-command-{entry_id}"):
        if entry_path.exists():
            payload = _load_json(entry_path)
            validate_processed_command_entry(payload, memory_root)
            return payload, False

        entry = {
            "entry_id": entry_id,
            "schema_version": PROCESSED_COMMAND_SCHEMA_VERSION,
            "issue_number": int(issue_number),
            "comment_id": int(comment_id),
            "command": command.strip().lower(),
            "workflow": workflow,
            "status": "claimed",
            "actor": actor,
            "run_id": sanitize_segment(run_id, "run-unknown"),
            "run_attempt": int(run_attempt),
            "timestamp": utc_now_iso(),
            "metadata": metadata or {},
        }
        validate_processed_command_entry(entry, memory_root)
        _atomic_write_json(entry_path, entry)
        return entry, True


def complete_processed_command(
    memory_root: Path,
    *,
    issue_number: int,
    comment_id: int,
    command: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    entry_id = make_processed_command_entry_id(issue_number, comment_id, command)
    entry_path = _processed_command_path(memory_root, issue_number, comment_id, command)
    with _file_lock(f"processed-command-{entry_id}"):
        if not entry_path.exists():
            raise MemoryValidationError(
                f"Processed command entry not found for issue={issue_number} comment={comment_id} command={command}"
            )
        payload = _load_json(entry_path)
        payload["status"] = sanitize_segment(status.strip().lower(), "completed")
        payload["timestamp"] = utc_now_iso()
        merged_metadata = dict(payload.get("metadata") or {})
        merged_metadata.update(metadata or {})
        payload["metadata"] = merged_metadata
        validate_processed_command_entry(payload, memory_root)
        _atomic_write_json(entry_path, payload)
        return payload


def record_run_event(
    memory_root: Path,
    *,
    run_id: str,
    workflow: str,
    event_type: str,
    status: str,
    message: str,
    issue_number: int | None,
    pr_number: int | None,
    actor: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    entry = {
        "entry_id": make_record_id("run_event"),
        "schema_version": RUN_LEDGER_SCHEMA_VERSION,
        "run_id": sanitize_segment(run_id, "run-unknown"),
        "workflow": workflow,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "event_type": sanitize_segment(event_type, "event"),
        "status": sanitize_segment(status, "info"),
        "message": message.strip(),
        "actor": actor,
        "metadata": metadata or {},
        "timestamp": utc_now_iso(),
    }
    validate_run_ledger_entry(entry, memory_root)
    _append_jsonl(_run_ledger_path(memory_root, run_id), entry)
    return entry


def record_candidate(
    memory_root: Path,
    *,
    category: str,
    summary: str,
    details: str,
    confidence: float,
    workflow: str,
    run_id: str | None,
    run_attempt: int | None,
    actor: str,
    issue_number: int | None,
    pr_number: int | None,
    source_refs: list[str] | None,
    parent_ids: list[str] | None = None,
    supersedes: str | None = None,
    sensitive: bool | None = None,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    normalized_category = category.strip().lower()
    if normalized_category not in ALLOWED_CATEGORIES:
        raise MemoryValidationError(f"Unsupported memory category: {category}")

    summary_text = summary.strip()
    details_text = details.strip()
    if not summary_text:
        raise MemoryValidationError("Candidate summary cannot be empty")
    if not details_text:
        raise MemoryValidationError("Candidate details cannot be empty")

    resolved_sensitive = infer_sensitive(normalized_category, summary_text, details_text, sensitive)
    injection_matches = scan_candidate_text_for_injection(summary_text, details_text)
    fingerprint = compute_fingerprint(normalized_category, summary_text, details_text, issue_number, pr_number)
    record = {
        "record_id": make_record_id("mem"),
        "schema_version": MEMORY_RECORD_SCHEMA_VERSION,
        "category": normalized_category,
        "status": "candidate",
        "scope": _build_scope(issue_number, pr_number, run_id),
        "summary": summary_text,
        "details": details_text,
        "confidence": float(confidence),
        "sensitive": bool(resolved_sensitive),
        "fingerprint": fingerprint,
        "provenance": _build_provenance(workflow, run_id, run_attempt, actor, source_refs),
        "lineage": _build_lineage(issue_number, pr_number, run_id, parent_ids, supersedes),
        "timestamps": {
            "created_at": utc_now_iso(),
            "promoted_at": None,
            "superseded_at": None,
        },
    }
    if injection_matches:
        record["injection_suspected"] = True

    validate_memory_record(record, memory_root)
    _atomic_write_json(_record_path_for_candidate(memory_root, record), record)
    return record


def _load_canonical_records(memory_root: Path) -> list[dict[str, Any]]:
    canonical_dir = memory_root / "global" / "canonical"
    records: list[dict[str, Any]] = []
    for path in _iter_json_files(canonical_dir):
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        records.append(payload)
    return records


def _load_candidate_records(memory_root: Path, issue_number: int | None = None) -> list[tuple[Path, dict[str, Any]]]:
    task_root = memory_root / "tasks"
    if issue_number is not None:
        candidate_dir = task_root / f"issue-{int(issue_number)}" / "candidates"
        file_paths = _iter_json_files(candidate_dir)
    else:
        file_paths = _iter_json_files(task_root)
    results: list[tuple[Path, dict[str, Any]]] = []
    for path in file_paths:
        if path.parent.name != "candidates":
            continue
        payload = _load_json(path)
        results.append((path, payload))
    return results


def _governance_errors(record: dict[str, Any], canonical_by_fingerprint: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []

    if record.get("schema_version") != MEMORY_RECORD_SCHEMA_VERSION:
        errors.append("schema_version_mismatch")

    confidence = float(record.get("confidence", 0.0))
    threshold = DEFAULT_GOVERNANCE["min_confidence_sensitive"] if record.get("sensitive") else DEFAULT_GOVERNANCE["min_confidence"]
    if confidence < threshold:
        errors.append(f"confidence_below_threshold:{confidence:.2f}<{threshold:.2f}")

    provenance = record.get("provenance") or {}
    if not provenance.get("workflow"):
        errors.append("missing_provenance_workflow")
    if not provenance.get("run_id"):
        errors.append("missing_provenance_run_id")
    source_refs = provenance.get("source_refs") or []
    if record.get("sensitive") and len(source_refs) < DEFAULT_GOVERNANCE["require_source_refs_for_sensitive"]:
        errors.append("sensitive_requires_source_refs")

    fingerprint = record.get("fingerprint")
    existing = canonical_by_fingerprint.get(str(fingerprint))
    if existing and existing.get("status") == "active":
        errors.append(f"duplicate_fingerprint:{existing.get('record_id')}")

    return errors


def _set_record_status(record: dict[str, Any], status: str, reason: str | None = None) -> dict[str, Any]:
    updated = json.loads(json.dumps(record))
    updated["status"] = status
    if status == "active":
        updated.setdefault("timestamps", {})["promoted_at"] = utc_now_iso()
    if status == "superseded":
        updated.setdefault("timestamps", {})["superseded_at"] = utc_now_iso()
    if reason:
        details = str(updated.get("details") or "").strip()
        note = f"Governance note: {reason}".strip()
        if details:
            available = MAX_MEMORY_DETAILS_LENGTH - len(note) - 2
            if available > 0:
                updated["details"] = f"{details[:available]}\n\n{note}"
            else:
                updated["details"] = note[:MAX_MEMORY_DETAILS_LENGTH]
        else:
            updated["details"] = note[:MAX_MEMORY_DETAILS_LENGTH]
    elif isinstance(updated.get("details"), str) and len(updated["details"]) > MAX_MEMORY_DETAILS_LENGTH:
        updated["details"] = updated["details"][:MAX_MEMORY_DETAILS_LENGTH]
    return updated


def _index_canonical_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("record_id")): item for item in records if item.get("record_id")}


def promote_candidates(
    memory_root: Path,
    *,
    issue_number: int | None,
    record_id: str | None,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)

    canonical_records = _load_canonical_records(memory_root)
    canonical_by_fingerprint = {
        str(item.get("fingerprint")): item
        for item in canonical_records
        if item.get("fingerprint")
    }
    canonical_by_id = _index_canonical_by_id(canonical_records)

    promoted: list[str] = []
    rejected: list[dict[str, str]] = []
    superseded: list[str] = []
    skipped: list[str] = []

    for candidate_path, candidate in _load_candidate_records(memory_root, issue_number=issue_number):
        if record_id and candidate.get("record_id") != record_id:
            continue
        if candidate.get("status") not in {"candidate", "rejected"}:
            skipped.append(str(candidate.get("record_id")))
            continue

        try:
            validate_memory_record(candidate, memory_root)
        except MemoryValidationError as exc:
            rejected_reason = f"schema_error:{exc}"
            rejected_record = _set_record_status(candidate, "rejected", rejected_reason)
            _atomic_write_json(candidate_path, rejected_record)
            rejected.append({"record_id": str(candidate.get("record_id")), "reason": rejected_reason})
            continue

        governance_issues = _governance_errors(candidate, canonical_by_fingerprint)
        if governance_issues:
            reason = ",".join(governance_issues)
            rejected_record = _set_record_status(candidate, "rejected", reason)
            _atomic_write_json(candidate_path, rejected_record)
            rejected.append({"record_id": str(candidate.get("record_id")), "reason": reason})
            continue

        canonical_record = _set_record_status(candidate, "active")

        supersedes = canonical_record.get("lineage", {}).get("supersedes")
        if supersedes:
            prior = canonical_by_id.get(str(supersedes))
            if prior and prior.get("status") == "active":
                prior_path = _record_path_for_canonical(memory_root, prior)
                prior_updated = _set_record_status(prior, "superseded")
                prior_updated.setdefault("lineage", {})["superseded_by"] = canonical_record["record_id"]
                validate_memory_record(prior_updated, memory_root)
                _atomic_write_json(prior_path, prior_updated)
                canonical_by_id[str(prior_updated["record_id"])] = prior_updated
                superseded.append(str(prior_updated["record_id"]))

        validate_memory_record(canonical_record, memory_root)
        _atomic_write_json(_record_path_for_canonical(memory_root, canonical_record), canonical_record)
        promoted_candidate = _set_record_status(candidate, "promoted")
        promoted_candidate.setdefault("timestamps", {})["promoted_at"] = canonical_record["timestamps"]["promoted_at"]
        validate_memory_record(promoted_candidate, memory_root)
        _atomic_write_json(candidate_path, promoted_candidate)

        canonical_by_fingerprint[str(canonical_record.get("fingerprint"))] = canonical_record
        canonical_by_id[str(canonical_record.get("record_id"))] = canonical_record
        promoted.append(str(canonical_record.get("record_id")))

    return {
        "promoted": promoted,
        "rejected": rejected,
        "superseded": superseded,
        "skipped": skipped,
    }


_KEYWORD_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "as", "be", "was", "are",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "this", "that", "these", "those", "i", "we", "you", "he", "she",
    "they", "me", "us", "him", "her", "them", "my", "our", "your",
    "his", "its", "their", "what", "which", "who", "whom", "how",
    "when", "where", "why", "if", "then", "else", "so", "not", "no",
    "all", "each", "every", "any", "some", "such", "than", "too",
    "very", "just", "also", "into", "over", "after", "before", "between",
    "under", "about", "up", "out", "off", "more", "most", "other",
    "new", "old", "one", "two", "only", "own", "same", "like",
    "here", "there", "now", "still", "well", "get", "got", "make",
    "made", "take", "use", "used", "using", "add", "added", "see",
    "set", "let", "try", "want", "please", "think", "know",
})

_KEYWORD_EXTRACT_PROMPT = """\
Extract 10-15 semantic keywords or short concept phrases from the following \
GitHub issue. Return ONLY a JSON array of lowercase strings, nothing else. \
Focus on domain concepts, technical terms, component names, and action verbs \
that capture what the issue is about.

Example output: ["payment processing", "retry logic", "redis caching", "rate limit", "webhook"]

Title: {title}

Body:
{body}"""

_KEYWORD_MODEL_DEFAULT = "openai/gpt-5.4-nano"
_KEYWORD_MAX_RETRIES = 3


def _extract_keywords_plain(title: str, body: str) -> set[str]:
    """Extract keywords from issue title and body using simple tokenisation."""
    text = f"{title} {body}".lower()
    tokens = re.findall(r"[a-z][a-z0-9_]+", text)
    return {t for t in tokens if t not in _KEYWORD_STOP_WORDS and len(t) > 2}


def _parse_keyword_response(raw: str | None) -> list[str] | None:
    """Parse LLM response expecting a JSON array of strings. Returns None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    # Handle markdown code fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    result = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            result.append(item.strip().lower())
    return result if result else None


def _extract_keywords_llm(
    title: str,
    body: str,
    *,
    api_key: str,
    model: str | None = None,
    base_url: str | None = None,
) -> list[str] | None:
    """Call an LLM to extract semantic keywords. Returns None on failure."""
    resolved_model = model or os.getenv("AI_MEMORY_KEYWORD_MODEL", _KEYWORD_MODEL_DEFAULT)
    resolved_base_url = (base_url or os.getenv("AI_MEMORY_KEYWORD_BASE_URL", "https://openrouter.ai/api/v1")).rstrip("/")

    # Truncate body to avoid excessive prompt size
    truncated_body = body[:4000] if body else ""
    prompt = _KEYWORD_EXTRACT_PROMPT.format(title=title, body=truncated_body)

    base_messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    messages_with_breakpoint, breakpoint_enabled = add_ephemeral_cache_breakpoint(
        base_messages,
        model_id=resolved_model,
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(1, _KEYWORD_MAX_RETRIES + 1):
        payload_messages: list[dict[str, Any]] = messages_with_breakpoint
        had_cache_fallback_retry = False
        try:
            while True:
                payload = json.dumps({
                    "model": resolved_model,
                    "messages": payload_messages,
                    "temperature": 0.0,
                    "max_tokens": 300,
                }).encode("utf-8")
                req = urllib.request.Request(
                    f"{resolved_base_url}/chat/completions",
                    data=payload,
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        resp_data = json.loads(resp.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    try:
                        error_text = exc.read().decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        error_text = ""
                    if (
                        breakpoint_enabled
                        and not had_cache_fallback_retry
                        and should_retry_without_breakpoint(exc.code, error_text)
                    ):
                        had_cache_fallback_retry = True
                        payload_messages = base_messages
                        continue
                    raise
            content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
            keywords = _parse_keyword_response(content)
            if keywords is not None:
                _log.debug("LLM keyword extraction succeeded on attempt %d: %s", attempt, keywords)
                return keywords
            _log.warning(
                "LLM keyword extraction returned unparseable response on attempt %d/%d: %s",
                attempt, _KEYWORD_MAX_RETRIES, content[:200],
            )
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError, KeyError, IndexError) as exc:
            _log.warning(
                "LLM keyword extraction failed on attempt %d/%d: %s",
                attempt, _KEYWORD_MAX_RETRIES, exc,
            )
    return None


def _extract_keywords(
    title: str | None,
    body: str | None,
    *,
    api_key: str | None = None,
) -> tuple[set[str], str]:
    """Extract keywords using LLM when available, falling back to plain tokenisation.

    Returns (keywords, method) where method is "llm", "plain", or "none".
    """
    safe_title = title or ""
    safe_body = body or ""
    if not safe_title and not safe_body:
        return set(), "none"

    if api_key:
        llm_keywords = _extract_keywords_llm(safe_title, safe_body, api_key=api_key)
        if llm_keywords is not None:
            # Combine LLM concepts with plain tokens for broader coverage
            plain = _extract_keywords_plain(safe_title, safe_body)
            return plain | {kw.lower() for kw in llm_keywords}, "llm"

    return _extract_keywords_plain(safe_title, safe_body), "plain"


def _keyword_overlap_ratio(keywords: set[str], text: str) -> float:
    """Calculate the fraction of keywords that appear in the given text."""
    if not keywords or not text:
        return 0.0
    text_lower = text.lower()
    matched = sum(1 for kw in keywords if kw in text_lower)
    return matched / len(keywords)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _load_retrieval_profiles(profiles_path: Path) -> dict[str, Any]:
    payload = _load_json(profiles_path)
    if payload.get("schema_version") != RETRIEVAL_PROFILE_SCHEMA_VERSION:
        raise MemoryValidationError(
            f"Invalid retrieval profile schema version: {payload.get('schema_version')!r}"
        )
    roles = payload.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise MemoryValidationError("Retrieval profiles must define a non-empty roles map")
    return payload


def _record_score(
    record: dict[str, Any],
    profile: dict[str, Any],
    issue_number: int | None,
    pr_number: int | None,
    keywords: set[str] | None = None,
) -> tuple[float, str, str]:
    category_weights = profile.get("category_weights") or {}
    score = float(category_weights.get(record.get("category"), 0.0))
    score += float(record.get("confidence", 0.0)) * float(profile.get("confidence_weight", 10.0))

    lineage = record.get("lineage") or {}
    scope = record.get("scope") or {}
    if issue_number is not None:
        if lineage.get("issue_number") == issue_number:
            score += float(profile.get("same_issue_boost", 20.0))
        elif scope.get("issue_number") == issue_number:
            score += float(profile.get("scope_issue_boost", 10.0))
    if pr_number is not None:
        if lineage.get("pr_number") == pr_number:
            score += float(profile.get("same_pr_boost", 14.0))
        elif scope.get("pr_number") == pr_number:
            score += float(profile.get("scope_pr_boost", 8.0))

    if record.get("status") == "active":
        score += float(profile.get("active_status_boost", 4.0))

    if keywords:
        summary = record.get("summary") or ""
        details = record.get("details") or ""
        overlap = _keyword_overlap_ratio(keywords, f"{summary} {details}")
        score += overlap * float(profile.get("keyword_match_boost", 10.0))

    created_at = str((record.get("timestamps") or {}).get("created_at") or "")
    record_id = str(record.get("record_id") or "")
    return score, created_at, record_id


def _resolve_token_budget(profile: dict[str, Any], role: str) -> int:
    """Resolve token budget: env var override > profile value > default."""
    env_key = f"AI_MEMORY_TOKEN_BUDGET_{role.upper()}"
    env_val = os.getenv(env_key)
    if env_val is not None:
        try:
            return int(env_val)
        except (ValueError, TypeError):
            pass
    return int(profile.get("token_budget", 900))


def retrieve_memory_context(
    memory_root: Path,
    profiles_path: Path,
    *,
    role: str,
    issue_number: int | None,
    pr_number: int | None,
    issue_title: str | None = None,
    issue_body: str | None = None,
    api_key: str | None = None,
) -> RetrievalResult:
    ensure_memory_layout(memory_root)
    profiles = _load_retrieval_profiles(profiles_path)
    roles = profiles["roles"]
    default_role = profiles.get("default_role")
    resolved_role = role if role in roles else default_role
    if resolved_role not in roles:
        raise MemoryValidationError(f"Unknown retrieval role: {role}")

    profile = roles[resolved_role]
    token_budget = _resolve_token_budget(profile, resolved_role)

    # Extract keywords for content-aware scoring
    keywords, keyword_method = _extract_keywords(issue_title, issue_body, api_key=api_key)

    records: list[dict[str, Any]] = []
    for record in _load_canonical_records(memory_root):
        if record.get("status") == "active":
            records.append(record)

    if issue_number is not None:
        for _path, record in _load_candidate_records(memory_root, issue_number=issue_number):
            if record.get("status") in {"candidate", "active"}:
                records.append(record)

    scored: list[tuple[float, str, str, dict[str, Any]]] = []
    seen_record_ids: set[str] = set()
    for record in records:
        try:
            validate_memory_record(record, memory_root)
        except MemoryValidationError:
            continue
        score, created_at, record_id = _record_score(
            record, profile, issue_number, pr_number, keywords=keywords or None,
        )
        if not record_id or record_id in seen_record_ids:
            continue
        seen_record_ids.add(record_id)
        scored.append((score, created_at, record_id, record))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for _score, _created_at, _record_id, record in scored:
        line = (
            f"[{record['category']}|{record['status']}|conf={record['confidence']:.2f}|"
            f"id={record['record_id']}] {record['summary']}"
        )
        line_tokens = _estimate_tokens(line)
        if selected and used_tokens + line_tokens > token_budget:
            continue
        if not selected and line_tokens > token_budget:
            # Always include at least one record if available, even when oversized.
            selected.append(record)
            used_tokens = line_tokens
            break
        selected.append(record)
        used_tokens += line_tokens

    lines = [
        "AI MEMORY CONTEXT",
        f"role: {resolved_role}",
        f"token_budget: {token_budget}",
        f"estimated_tokens_used: {used_tokens}",
        f"records_selected: {len(selected)}",
    ]
    for index, record in enumerate(selected):
        lineage = record.get("lineage") or {}
        issue_label = lineage.get("issue_number") if lineage.get("issue_number") is not None else "-"
        pr_label = lineage.get("pr_number") if lineage.get("pr_number") is not None else "-"
        lines.append(
            "- "
            f"[{index}] [{record['category']}|{record['status']}|conf={record['confidence']:.2f}|"
            f"id={record['record_id']}] issue={issue_label} pr={pr_label} :: {record['summary']}"
        )

    if not selected:
        lines.append("- none")

    return RetrievalResult(
        context="\n".join(lines) + "\n",
        selected_record_ids=[str(item.get("record_id")) for item in selected],
        estimated_tokens=used_tokens,
        role=resolved_role,
        keyword_method=keyword_method,
    )


def _load_lineage(memory_root: Path, issue_number: int) -> dict[str, Any] | None:
    path = _lineage_path(memory_root, issue_number)
    if not path.exists():
        return None
    return _load_json(path)


def _dedupe_by_key(items: list[dict[str, Any]], key_name: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get(key_name) or "")
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    output.sort(key=lambda value: str(value.get(key_name) or ""))
    return output


def _dedupe_runs_by_attempt(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        run_id = str(item.get("run_id") or "")
        run_attempt = int(item.get("run_attempt") or 0)
        if not run_id or run_attempt < 1:
            continue
        key = (run_id, run_attempt)
        current = latest_by_key.get(key)
        if current is None or str(item.get("timestamp") or "") > str(current.get("timestamp") or ""):
            latest_by_key[key] = item
    return [latest_by_key[key] for key in sorted(latest_by_key)]


def finalize_task_lineage(
    memory_root: Path,
    *,
    issue_number: int,
    issue_url: str,
    final_state: str,
    workflow: str,
    run_id: str,
    run_attempt: int,
    pr_number: int | None = None,
    pr_url: str | None = None,
    memory_record_ids: list[str] | None = None,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)

    allowed_states = {"open", "in_progress", "merged", "closed", "cancelled"}
    normalized_state = final_state.strip().lower()
    if normalized_state not in allowed_states:
        raise MemoryValidationError(f"Unsupported lineage final_state: {final_state}")

    existing = _load_lineage(memory_root, issue_number)
    created_at = utc_now_iso()

    if existing:
        lineage = existing
        created_at = str(existing.get("created_at") or created_at)
    else:
        lineage = {
            "schema_version": TASK_LINEAGE_SCHEMA_VERSION,
            "lineage_id": f"issue-{int(issue_number)}",
            "issue_number": int(issue_number),
            "issue_url": issue_url,
            "state": "open",
            "prs": [],
            "runs": [],
            "memory_record_ids": [],
            "created_at": created_at,
            "updated_at": created_at,
        }

    lineage["issue_url"] = issue_url
    lineage["state"] = normalized_state

    runs = list(lineage.get("runs") or [])
    runs.append(
        {
            "run_id": sanitize_segment(run_id, "run-unknown"),
            "workflow": workflow,
            "run_attempt": int(run_attempt),
            "status": normalized_state,
            "timestamp": utc_now_iso(),
        }
    )
    lineage["runs"] = _dedupe_runs_by_attempt(runs)

    prs = list(lineage.get("prs") or [])
    if pr_number is not None:
        prs.append(
            {
                "pr_number": int(pr_number),
                "url": pr_url or "",
                "state": normalized_state,
            }
        )
    deduped_prs = {}
    for item in prs:
        key = int(item.get("pr_number"))
        deduped_prs[key] = {
            "pr_number": key,
            "url": str(item.get("url") or ""),
            "state": str(item.get("state") or normalized_state),
        }
    lineage["prs"] = [deduped_prs[key] for key in sorted(deduped_prs)]

    records = sorted(set((lineage.get("memory_record_ids") or []) + (memory_record_ids or [])))
    lineage["memory_record_ids"] = records
    lineage["updated_at"] = utc_now_iso()
    lineage["created_at"] = created_at

    validate_task_lineage(lineage, memory_root)
    _atomic_write_json(_lineage_path(memory_root, issue_number), lineage)
    return lineage


def compact_memory(
    memory_root: Path,
    *,
    month_yyyy_mm: str,
    prune: bool = False,
) -> dict[str, Any]:
    ensure_memory_layout(memory_root)
    if not re.fullmatch(r"\d{4}-\d{2}", month_yyyy_mm):
        raise MemoryValidationError("month_yyyy_mm must match YYYY-MM")

    archive_dir = memory_root / "archive" / "monthly" / month_yyyy_mm
    archive_dir.mkdir(parents=True, exist_ok=True)

    candidate_files = [path for path in _iter_json_files(memory_root / "tasks") if path.parent.name == "candidates"]
    ledger_files = _iter_jsonl_files(memory_root / "runs")

    archived_candidates = 0
    archived_ledgers = 0
    removed_candidates = 0
    removed_ledgers = 0

    for candidate_path in candidate_files:
        payload = _load_json(candidate_path)
        created_at = str((payload.get("timestamps") or {}).get("created_at") or "")
        if not created_at.startswith(month_yyyy_mm):
            continue
        relative = candidate_path.relative_to(memory_root)
        destination = archive_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate_path, destination)
        archived_candidates += 1
        if prune and payload.get("status") in {"promoted", "rejected", "superseded"}:
            candidate_path.unlink(missing_ok=True)
            removed_candidates += 1

    for ledger_path in ledger_files:
        ledger_lines = [line for line in ledger_path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
        has_target_month_entry = False
        has_non_target_month_entry = False
        for line in ledger_lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                has_non_target_month_entry = True
                continue
            timestamp = str(entry.get("timestamp") or "") if isinstance(entry, dict) else ""
            if timestamp.startswith(month_yyyy_mm):
                has_target_month_entry = True
            else:
                has_non_target_month_entry = True
        if not has_target_month_entry:
            continue
        relative = ledger_path.relative_to(memory_root)
        destination = archive_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ledger_path, destination)
        archived_ledgers += 1
        if prune:
            if has_non_target_month_entry:
                continue
            ledger_path.unlink(missing_ok=True)
            removed_ledgers += 1

    summary = {
        "schema_version": "ai_memory_compaction_summary.v1",
        "month": month_yyyy_mm,
        "generated_at": utc_now_iso(),
        "archived_candidates": archived_candidates,
        "archived_ledgers": archived_ledgers,
        "removed_candidates": removed_candidates,
        "removed_ledgers": removed_ledgers,
        "prune": bool(prune),
    }
    _atomic_write_json(archive_dir / "summary.json", summary)
    return summary


# Repository-location git environment variables. The workspace shell context
# exports GIT_DIR and GIT_WORK_TREE so later steps' git commands operate on the
# main checkout. Every memory-helper git command instead operates on a dedicated
# /tmp clone selected via ``cwd``. If these leak in, they can make ``git clone``
# abort with ``fatal: working tree '<path>' already exists`` or silently retarget
# add/commit/push at the host repo rather than the clone. Strip these and related
# repo-pinning variables so every memory git subprocess resolves its repository
# purely from ``cwd``.
_GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


def _git_subprocess_env() -> dict[str, str]:
    """Return a copy of the environment with repo-pinning git vars removed.

    Ensures git subprocesses spawned by the memory helpers discover their
    repository from the ``cwd`` argument instead of inheriting GIT_DIR /
    GIT_WORK_TREE (and friends) from a workflow step that pointed them at
    the main checkout.
    """
    env = dict(os.environ)
    for name in _GIT_LOCATION_ENV_VARS:
        env.pop(name, None)
    return env


def _run_git(
    cwd: Path,
    args: list[str],
    check: bool = True,
    *,
    inherit_location_env: bool = False,
) -> subprocess.CompletedProcess[str]:
    # Mutating memory git subprocesses (clone/fetch/checkout/commit/push) MUST
    # run with the repo-pinning git vars stripped so they resolve their
    # throwaway /tmp repo from ``cwd`` (see ``_git_subprocess_env``). A read
    # that legitimately needs to discover the *host* repository — e.g.
    # ``git remote get-url origin`` when ``cwd`` is a bare work tree whose
    # ``.git`` lives elsewhere via GIT_DIR — sets ``inherit_location_env`` so
    # the inherited GIT_DIR/GIT_WORK_TREE locate the host repo instead.
    process = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=True,
        env=dict(os.environ) if inherit_location_env else _git_subprocess_env(),
    )
    if check and process.returncode != 0:
        message = (
            f"git {' '.join(args)} failed with exit {process.returncode}\n"
            f"stdout:\n{process.stdout}\n"
            f"stderr:\n{process.stderr}"
        )
        raise MemoryGitError(message)
    return process


@contextlib.contextmanager
def _file_lock(lock_name: str) -> Any:
    lock_key = hashlib.sha256(lock_name.encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"ai-memory-{lock_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(fd, False)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _inject_token_into_url(url: str, token: str) -> str:
    """Embed a GitHub token into an HTTPS origin URL for authenticated clones.

    Converts ``https://github.com/owner/repo`` →
    ``https://x-access-token:TOKEN@github.com/owner/repo``.
    SSH and file URLs are returned unchanged.
    """
    if not url.startswith("https://"):
        return url
    # Already has embedded credentials — leave as-is.
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        return url
    return url.replace("https://", f"https://x-access-token:{token}@", 1)


def _resolve_origin_url(repo_root: Path) -> str:
    # ``git remote get-url origin`` is a read against the *host* repository so
    # the memory branch can be cloned from /tmp. Under the implement /
    # review_autofix / validate workflows ``repo_root`` is the per-issue work
    # tree (the exported GIT_WORK_TREE), which is frequently not itself a
    # discoverable git directory — the host repo is reachable only via the
    # exported GIT_DIR. Stripping the location env here (as every mutating
    # memory git subprocess does) makes this read fail and fall back to the
    # bare work-tree path, which is not a clonable repository, so the
    # subsequent clone aborts with exit 128. Preserve the inherited location
    # env for this read only so origin resolves correctly; the clone/fetch/
    # checkout that follow still run with the env stripped.
    process = _run_git(
        repo_root,
        ["remote", "get-url", "origin"],
        check=False,
        inherit_location_env=True,
    )
    if process.returncode == 0:
        url = process.stdout.strip()
    else:
        url = str(repo_root.resolve())
    # When running in CI the origin URL from actions/checkout is bare HTTPS
    # (https://github.com/owner/repo) with no embedded credentials.  Subprocess
    # git-clone calls therefore fail with "could not read Username".  If a
    # GH_TOKEN env-var is available, inject it so clones authenticate properly.
    token = os.environ.get("GH_TOKEN", "")
    if token and url.startswith("https://"):
        url = _inject_token_into_url(url, token)
    return url


def _clone_for_memory_branch(repo_root: Path, memory_branch: str) -> Path:
    origin_url = _resolve_origin_url(repo_root)
    temp_dir = Path(tempfile.mkdtemp(prefix="ai-memory-branch-"))
    _run_git(temp_dir.parent, ["clone", "--no-tags", "--quiet", origin_url, str(temp_dir)])

    branch_exists = (
        _run_git(temp_dir, ["ls-remote", "--heads", "origin", memory_branch], check=False)
        .stdout.strip()
        != ""
    )
    if branch_exists:
        _run_git(temp_dir, ["fetch", "--no-tags", "origin", f"refs/heads/{memory_branch}:refs/remotes/origin/{memory_branch}"])
        _run_git(temp_dir, ["checkout", "-B", memory_branch, f"refs/remotes/origin/{memory_branch}"])
    else:
        _run_git(temp_dir, ["checkout", "-B", memory_branch])

    return temp_dir


# Memory-branch push retries all contend on the single shared `ai-memory`
# ref: every concurrent workflow run pushes there.  Without a randomized
# delay between attempts the contenders retry in lockstep and keep losing
# the same server-side ref-lock race ("cannot lock ref ... is at X but
# expected Y"), exhausting the retry budget.  Full-jitter exponential
# backoff decorrelates them so a push lands within the budget.
_PUSH_RETRY_BACKOFF_BASE_SECONDS = 0.5
_PUSH_RETRY_BACKOFF_CAP_SECONDS = 8.0


def _push_retry_backoff_seconds(attempt: int) -> float:
    """Return a randomized delay (seconds) to wait before the next push retry.

    ``attempt`` is the 1-based number of the push attempt that just failed.
    Full-jitter exponential backoff: the delay is drawn uniformly from
    ``[0, min(cap, base * 2 ** (attempt - 1))]`` so concurrent pushers
    spread across the retry window instead of colliding in lockstep.
    """
    if attempt < 1:
        attempt = 1
    ceiling = min(_PUSH_RETRY_BACKOFF_BASE_SECONDS, _PUSH_RETRY_BACKOFF_CAP_SECONDS)
    for _ in range(1, attempt):
        if ceiling >= _PUSH_RETRY_BACKOFF_CAP_SECONDS:
            break
        ceiling = min(_PUSH_RETRY_BACKOFF_CAP_SECONDS, ceiling * 2.0)
    return random.uniform(0.0, ceiling)


def persist_memory_operation(
    repo_root: Path,
    *,
    memory_branch: str,
    memory_root_relative: str,
    push_retries: int,
    commit_message: str,
    operation: Callable[[Path], dict[str, Any] | None],
) -> dict[str, Any]:
    if push_retries < 1:
        raise MemoryValidationError("push_retries must be >= 1")

    repo_root = repo_root.resolve()
    with _file_lock(f"ai-memory-{repo_root}-{memory_branch}"):
        clone_dir = _clone_for_memory_branch(repo_root, memory_branch)
        try:
            memory_root = resolve_memory_root_dir(clone_dir, memory_root_relative)
            ensure_memory_layout(memory_root)

            source_memory_root = resolve_memory_reference_source_dir(repo_root, memory_root_relative)
            _sync_memory_reference_files(source_memory_root, memory_root)

            op_result = operation(clone_dir) or {}

            _run_git(clone_dir, ["config", "user.name", "codex-bot"])
            _run_git(clone_dir, ["config", "user.email", "codex@users.noreply.github.com"])
            _run_git(clone_dir, ["add", str(memory_root.relative_to(clone_dir))])

            if _run_git(clone_dir, ["diff", "--cached", "--quiet"], check=False).returncode == 0:
                return {
                    "did_commit": False,
                    "did_push": False,
                    "commit_sha": None,
                    "push_attempts": 0,
                    "operation_result": op_result,
                }

            _run_git(clone_dir, ["commit", "-m", commit_message])
            _run_git(clone_dir, ["rev-parse", "HEAD"])

            for attempt in range(1, push_retries + 1):
                push = _run_git(
                    clone_dir,
                    ["push", "origin", f"HEAD:refs/heads/{memory_branch}"],
                    check=False,
                )
                if push.returncode == 0:
                    pushed_commit_sha = _run_git(clone_dir, ["rev-parse", "HEAD"]).stdout.strip()
                    return {
                        "did_commit": True,
                        "did_push": True,
                        "commit_sha": pushed_commit_sha,
                        "push_attempts": attempt,
                        "operation_result": op_result,
                    }

                if attempt >= push_retries:
                    raise MemoryGitError(
                        f"Failed to push memory branch after {push_retries} attempts: {push.stderr.strip()}"
                    )

                # Wait a jittered, exponentially growing interval before
                # re-syncing and retrying so concurrent runs stop losing the
                # ref-lock race against the shared memory branch in lockstep.
                time.sleep(_push_retry_backoff_seconds(attempt))

                fetch = _run_git(
                    clone_dir,
                    ["fetch", "--no-tags", "origin", f"refs/heads/{memory_branch}:refs/remotes/origin/{memory_branch}"],
                    check=False,
                )
                if fetch.returncode != 0:
                    raise MemoryGitError(f"Memory branch fetch failed while retrying push: {fetch.stderr.strip()}")
                if _run_git(clone_dir, ["show-ref", "--verify", f"refs/remotes/origin/{memory_branch}"], check=False).returncode == 0:
                    rebase = _run_git(clone_dir, ["rebase", f"refs/remotes/origin/{memory_branch}"], check=False)
                    if rebase.returncode != 0:
                        rebase_abort = _run_git(clone_dir, ["rebase", "--abort"], check=False)
                        if rebase_abort.returncode != 0:
                            raise MemoryGitError(
                                "Memory branch rebase failed while retrying push: "
                                f"{rebase.stderr.strip()} (rebase --abort also failed: {rebase_abort.stderr.strip()})"
                            )
                        raise MemoryGitError(
                            f"Memory branch rebase failed while retrying push: {rebase.stderr.strip()}"
                        )
        finally:
            shutil.rmtree(clone_dir, ignore_errors=True)


def read_memory_root_from_branch(
    repo_root: Path,
    *,
    memory_branch: str,
    memory_root_relative: str,
) -> Path:
    """Return a temporary directory path containing memory data from the branch.

    Caller owns lifecycle and must remove the returned directory when done.
    """

    repo_root = repo_root.resolve()
    origin_url = _resolve_origin_url(repo_root)
    temp_dir = Path(tempfile.mkdtemp(prefix="ai-memory-read-"))

    clone = _run_git(
        temp_dir.parent,
        [
            "clone",
            "--no-tags",
            "--depth",
            "1",
            "--branch",
            memory_branch,
            origin_url,
            str(temp_dir),
        ],
        check=False,
    )
    if clone.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise MemoryGitError(
            f"Unable to read memory branch {memory_branch}: {clone.stderr.strip() or clone.stdout.strip()}"
        )

    memory_root = resolve_memory_root_dir(temp_dir, memory_root_relative)
    source_memory_root = resolve_memory_reference_source_dir(repo_root, memory_root_relative)
    _sync_memory_reference_files(source_memory_root, memory_root)
    if not memory_root.exists():
        ensure_memory_layout(memory_root)
    return temp_dir


def summarize_candidate_for_event(record: dict[str, Any]) -> str:
    summary = str(record.get("summary") or "").strip()
    if len(summary) > 240:
        return summary[:237] + "..."
    return summary
