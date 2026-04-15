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
import re
import shutil
import subprocess
import tempfile
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

_log = logging.getLogger(__name__)

MEMORY_RECORD_SCHEMA_VERSION = "memory_record.v1"
RUN_LEDGER_SCHEMA_VERSION = "run_ledger_entry.v1"
TASK_LINEAGE_SCHEMA_VERSION = "task_lineage.v1"
PROCESSED_COMMAND_SCHEMA_VERSION = "processed_command_entry.v1"
RETRIEVAL_PROFILE_SCHEMA_VERSION = "retrieval_profiles.v1"
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
    if not source_root.exists():
        return

    source_schemas = source_root / "schemas"
    destination_schemas = destination_root / "schemas"
    if source_schemas.exists():
        for schema_file in sorted(source_schemas.glob("*.json")):
            if not schema_file.is_file():
                continue
            destination_schemas.mkdir(parents=True, exist_ok=True)
            destination_file = destination_schemas / schema_file.name
            if destination_file.is_symlink():
                raise MemoryValidationError(f"Refusing to overwrite symlinked schema file: {destination_file}")
            shutil.copy2(schema_file, destination_file)

    source_config = source_root / "config" / "retrieval_profiles.v1.json"
    if source_config.is_file():
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
        if command_filter and str(payload.get("command") or "").strip().lower() != command_filter:
            continue
        if workflow_filter and str(payload.get("workflow") or "").strip().lower() != workflow_filter:
            continue
        if status_filter and str(payload.get("status") or "").strip().lower() != status_filter:
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

_KEYWORD_MODEL_DEFAULT = "openai/gpt-5-mini"
_KEYWORD_MAX_RETRIES = 3


def _extract_keywords_plain(title: str, body: str) -> set[str]:
    """Extract keywords from issue title and body using simple tokenisation."""
    text = f"{title} {body}".lower()
    tokens = re.findall(r"[a-z][a-z0-9_]+", text)
    return {t for t in tokens if t not in _KEYWORD_STOP_WORDS and len(t) > 2}


def _parse_keyword_response(raw: str) -> list[str] | None:
    """Parse LLM response expecting a JSON array of strings. Returns None on failure."""
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
) -> set[str]:
    """Extract keywords using LLM when available, falling back to plain tokenisation."""
    safe_title = title or ""
    safe_body = body or ""
    if not safe_title and not safe_body:
        return set()

    if api_key:
        llm_keywords = _extract_keywords_llm(safe_title, safe_body, api_key=api_key)
        if llm_keywords is not None:
            # Combine LLM concepts with plain tokens for broader coverage
            plain = _extract_keywords_plain(safe_title, safe_body)
            return plain | {kw.lower() for kw in llm_keywords}

    return _extract_keywords_plain(safe_title, safe_body)


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
    keywords = _extract_keywords(issue_title, issue_body, api_key=api_key)

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


def _run_git(cwd: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=True,
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
    process = _run_git(repo_root, ["remote", "get-url", "origin"], check=False)
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

                _run_git(
                    clone_dir,
                    ["fetch", "--no-tags", "origin", f"refs/heads/{memory_branch}:refs/remotes/origin/{memory_branch}"],
                    check=False,
                )
                if _run_git(clone_dir, ["show-ref", "--verify", f"refs/remotes/origin/{memory_branch}"], check=False).returncode == 0:
                    rebase = _run_git(clone_dir, ["rebase", f"refs/remotes/origin/{memory_branch}"], check=False)
                    if rebase.returncode != 0:
                        _run_git(clone_dir, ["rebase", "--abort"], check=False)
                        raise MemoryGitError(
                            f"Memory branch rebase failed while retrying push: {rebase.stderr.strip()}"
                        )

            raise MemoryGitError("Unexpected push retry flow")
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
