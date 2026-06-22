#!/usr/bin/env python3
"""CLI for AI memory operations used by GitHub workflows."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_memory_lib import (
    MemoryGitError,
    MemoryValidationError,
    append_operator_bypass_audit_entry,
    append_revalidate_event,
    append_validation_discovery_entry,
    append_validation_history_entry,
    claim_processed_command,
    compact_memory,
    compute_normalized_sha256,
    complete_processed_command,
    evaluate_clarify_loop_guard,
    finalize_task_lineage,
    get_actions_runs_cache,
    get_branch_rebuild_audit,
    get_fingerprint_quarantine,
    get_operator_bypass_audit,
    get_processed_command_entry,
    get_revalidate_events,
    get_validation_discovery,
    get_validation_history,
    list_processed_command_entries,
    parse_bool,
    persist_memory_operation,
    put_actions_runs_cache,
    put_branch_rebuild_audit,
    put_fingerprint_quarantine,
    promote_candidates,
    read_memory_root_from_branch,
    record_candidate,
    record_run_event,
    resolve_memory_root_dir,
    retrieve_memory_context,
    scan_candidate_text_for_injection,
    summarize_candidate_for_event,
)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _emit_telemetry(op: str, **fields: Any) -> None:
    """Emit a structured telemetry line to stderr for log analysis visibility."""
    entry: dict[str, Any] = {"op": op}
    entry.update(fields)
    print(f"AI_MEMORY_TELEMETRY: {json.dumps(entry, ensure_ascii=True, sort_keys=True)}", file=sys.stderr)


def _sanitize_git_error(text: str) -> str:
    if not text:
        return text
    return re.sub(r"(https?://)([^/@\s]+)@", r"\1<redacted>@", text)


def _require_nonempty(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise MemoryValidationError(f"{field_name} is required")
    return text


def _resolve_repo_root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    return Path.cwd().resolve()


def _resolve_memory_root(memory_dir: Path, memory_root_relative: str) -> Path:
    return resolve_memory_root_dir(memory_dir, memory_root_relative)


def _require_positive_int(value: str | None, field_name: str) -> int:
    try:
        parsed = _safe_int(value)
    except ValueError as exc:
        raise MemoryValidationError(f"{field_name} must be a positive integer") from exc
    if parsed is None or parsed < 1:
        raise MemoryValidationError(f"{field_name} must be a positive integer")
    return parsed


def _read_env_defaults(args: argparse.Namespace) -> argparse.Namespace:
    if not getattr(args, "memory_branch", None):
        args.memory_branch = os.getenv("AI_MEMORY_BRANCH", "ai-memory")
    if not getattr(args, "memory_root", None):
        args.memory_root = os.getenv("AI_MEMORY_ROOT", "ai-memory")
    if getattr(args, "push_retries", None) is None:
        # The single shared `ai-memory` branch is a hot contention point under
        # orchestrator bursts: every concurrent clarify/plan/implement/review run
        # pushes to it. A non-fast-forward rejection is transient — the retry
        # loop fetches+rebases+re-pushes — but 5 attempts only sleeps for the
        # first 4 (backoff ceilings 0.5/1/2/4s, never reaching the 8s cap the
        # jitter was designed around), so heavy bursts exhaust the budget and the
        # fail-closed claim aborts the whole phase before it can post. 8
        # attempts activates the 8s-cap sleeps and widens the decorrelation
        # window to ~30s without weakening the mutex semantics.
        push_retries_env = os.getenv("AI_MEMORY_PUSH_RETRIES")
        args.push_retries = _require_positive_int(
            "8" if push_retries_env is None else push_retries_env,
            "AI_MEMORY_PUSH_RETRIES",
        )
    if not getattr(args, "enabled", None):
        args.enabled = parse_bool(os.getenv("AI_MEMORY_ENABLED", "true"), default=True)
    if not getattr(args, "retrieval_profiles", None):
        args.retrieval_profiles = os.getenv(
            "AI_MEMORY_RETRIEVAL_PROFILES", "ai-memory/config/retrieval_profiles.v1.json"
        )
    return args


def _safe_int(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _safe_float(value: str | None, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return sorted({item.strip() for item in value.split(",") if item.strip()})


def cmd_retrieve(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        context = "AI MEMORY CONTEXT\nstatus: disabled\n"
        if args.output_file:
            Path(args.output_file).write_text(context, encoding="utf-8")
        else:
            print(context, end="")
        _print_json({"ok": True, "enabled": False, "records_selected": 0, "estimated_tokens": 0})
        _emit_telemetry("retrieve", ok=True, enabled=False, records_selected=0)
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        try:
            branch_dir = read_memory_root_from_branch(
                repo_root,
                memory_branch=args.memory_branch,
                memory_root_relative=args.memory_root,
            )
        except MemoryGitError as exc:
            error_text = _sanitize_git_error(str(exc))
            lowered = error_text.lower()
            is_missing_branch = (
                ("remote branch" in lowered and ("not found" in lowered or "does not exist" in lowered))
                or "could not find remote ref" in lowered
                or "couldn't find remote ref" in lowered
                or "remote ref does not exist" in lowered
                or "did not match any file(s) known to git" in lowered
            )
            print(f"AI_MEMORY_WARNING: {error_text}", file=sys.stderr)
            context = "AI MEMORY CONTEXT\nstatus: unavailable\n"
            if args.output_file:
                Path(args.output_file).write_text(context, encoding="utf-8")
            else:
                print(context, end="")
            _print_json({"ok": True, "enabled": False, "records_selected": 0, "estimated_tokens": 0, "warning": error_text})
            _emit_telemetry(
                "retrieve",
                ok=True,
                enabled=False,
                records_selected=0,
                warning="branch_unavailable" if is_missing_branch else "git_error",
            )
            return 0
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        profiles_path = branch_dir / args.retrieval_profiles
        if not profiles_path.exists():
            profiles_path = memory_root / "config" / "retrieval_profiles.v1.json"
        if not profiles_path.exists():
            raise MemoryValidationError(f"Retrieval profiles not found: {profiles_path}")

        issue_body = None
        if getattr(args, "issue_body_file", None):
            body_path = Path(args.issue_body_file)
            if body_path.exists():
                issue_body = body_path.read_text(encoding="utf-8")

        api_key = os.getenv("OPENROUTER_API_KEY")

        result = retrieve_memory_context(
            memory_root,
            profiles_path,
            role=_require_nonempty(args.role, "role"),
            issue_number=_safe_int(args.issue_number),
            pr_number=_safe_int(args.pr_number),
            issue_title=getattr(args, "issue_title", None),
            issue_body=issue_body,
            api_key=api_key,
            category_filter=getattr(args, "category", None),
            scope_level_filter=getattr(args, "scope_level", None),
            max_records=_safe_int(getattr(args, "max", None)),
        )

        if args.output_file:
            Path(args.output_file).write_text(result.context, encoding="utf-8")
        else:
            print(result.context, end="")

        _print_json(
            {
                "ok": True,
                "enabled": True,
                "role": result.role,
                "records_selected": len(result.selected_record_ids),
                "record_ids": result.selected_record_ids,
                "estimated_tokens": result.estimated_tokens,
                "keyword_method": result.keyword_method,
            }
        )
        _emit_telemetry(
            "retrieve",
            ok=True,
            enabled=True,
            role=result.role,
            records_selected=len(result.selected_record_ids),
            estimated_tokens=result.estimated_tokens,
            keyword_method=result.keyword_method,
        )
        return 0
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_record_run_event(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "event": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry = record_run_event(
            memory_root,
            run_id=_require_nonempty(args.run_id, "run_id"),
            workflow=_require_nonempty(args.workflow, "workflow"),
            event_type=_require_nonempty(args.event_type, "event_type"),
            status=_require_nonempty(args.status, "status"),
            message=_require_nonempty(args.message, "message"),
            issue_number=_safe_int(args.issue_number),
            pr_number=_safe_int(args.pr_number),
            actor=_require_nonempty(args.actor, "actor"),
            metadata=_json_or_empty(args.metadata_json),
        )
        return {"event": entry}

    result = persist_memory_operation(
        repo_root,
        memory_branch=args.memory_branch,
        memory_root_relative=args.memory_root,
        push_retries=int(args.push_retries),
        commit_message=f"ai-memory: record run event [{args.workflow}]",
        operation=_op,
    )
    _print_json({"ok": True, **result})
    _emit_telemetry(
        "record-run-event",
        ok=True,
        workflow=args.workflow,
        event_type=args.event_type,
        did_push=result.get("did_push", False),
        push_attempts=result.get("push_attempts", 0),
    )
    return 0


def _json_or_empty(payload: str | None) -> dict[str, Any]:
    if payload is None or payload.strip() == "":
        return {}
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise MemoryValidationError("metadata JSON must be an object")
    return parsed


def _emit_actions_runs_cache_fallback(*, mode: str, reason: str, repo: str) -> None:
    print(
        "::warning::rate_limit_audit_fallback "
        f"helper=actions_runs_cache mode={mode} reason={reason} repo={repo}",
        file=sys.stderr,
    )


def _emit_fingerprint_quarantine_fallback(*, mode: str, reason: str) -> None:
    print(
        "::warning::fingerprint_quarantine_fallback "
        f"helper=fingerprint_quarantine mode={mode} reason={reason}",
        file=sys.stderr,
    )


def _is_missing_memory_branch_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return (
        ("remote branch" in lowered and ("not found" in lowered or "does not exist" in lowered))
        or "could not find remote ref" in lowered
        or "couldn't find remote ref" in lowered
        or "remote ref does not exist" in lowered
        or "did not match any file(s) known to git" in lowered
    )


def _quarantine_env_defaults(
    *,
    repo_root: Path | None = None,
    memory_branch: str | None = None,
    memory_root: str | None = None,
    push_retries: int | None = None,
    enabled: bool | None = None,
) -> argparse.Namespace:
    args = argparse.Namespace(
        repo_root=(str(repo_root) if repo_root is not None else None),
        memory_branch=memory_branch,
        memory_root=memory_root,
        push_retries=push_retries,
        enabled=enabled,
        retrieval_profiles=None,
    )
    args = _read_env_defaults(args)
    if enabled is not None:
        args.enabled = enabled
    if memory_branch is not None:
        args.memory_branch = memory_branch
    if memory_root is not None:
        args.memory_root = memory_root
    if push_retries is not None:
        args.push_retries = push_retries
    return args


def _load_quarantine_list(
    *,
    repo_root: Path | None = None,
    memory_branch: str | None = None,
    memory_root: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    args = _quarantine_env_defaults(
        repo_root=repo_root,
        memory_branch=memory_branch,
        memory_root=memory_root,
        enabled=enabled,
    )
    if not args.enabled:
        return {
            "ok": True,
            "enabled": False,
            "quarantine": {"schema_version": "v1", "entries": []},
        }

    resolved_repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            resolved_repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root_dir = _resolve_memory_root(branch_dir, args.memory_root)
        quarantine = get_fingerprint_quarantine(memory_root_dir)
        return {
            "ok": True,
            "enabled": True,
            "quarantine": quarantine,
        }
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        if _is_missing_memory_branch_error(error_text):
            return {
                "ok": True,
                "enabled": False,
                "quarantine": {"schema_version": "v1", "entries": []},
                "warning": error_text,
            }
        return {
            "ok": False,
            "enabled": True,
            "quarantine": {"schema_version": "v1", "entries": []},
            "error": error_text,
        }
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
        _emit_fingerprint_quarantine_fallback(mode="get", reason="load_failed")
        return {
            "ok": True,
            "enabled": True,
            "quarantine": {"schema_version": "v1", "entries": []},
            "warning": str(exc),
        }
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def _persist_quarantine_list(
    *,
    payload: dict[str, Any],
    repo_root: Path | None = None,
    memory_branch: str | None = None,
    memory_root: str | None = None,
    push_retries: int | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    args = _quarantine_env_defaults(
        repo_root=repo_root,
        memory_branch=memory_branch,
        memory_root=memory_root,
        push_retries=push_retries,
        enabled=enabled,
    )
    if not args.enabled:
        return {
            "ok": True,
            "enabled": False,
            "stored": False,
            "quarantine": payload,
        }

    resolved_repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root_dir = _resolve_memory_root(clone_dir, args.memory_root)
        quarantine = put_fingerprint_quarantine(memory_root_dir, payload)
        return {"quarantine": quarantine, "stored": True}

    try:
        result = persist_memory_operation(
            resolved_repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message="ai-memory: fingerprint quarantine",
            operation=_op,
        )
        return {"ok": True, "enabled": True, **result}
    except MemoryGitError as exc:
        return {
            "ok": True,
            "enabled": True,
            "stored": False,
            "quarantine": None,
            "warning": _sanitize_git_error(str(exc)),
        }
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
        _emit_fingerprint_quarantine_fallback(mode="put", reason="persist_failed")
        return {
            "ok": True,
            "enabled": True,
            "stored": False,
            "quarantine": None,
            "warning": _sanitize_git_error(str(exc)),
        }


def _emit_branch_rebuild_audit_fallback(
    *, mode: str, reason: str, tracking_issue: int, integration_branch: str
) -> None:
    print(
        "::warning::branch_rebuild_audit_fallback "
        f"helper=branch_rebuild_audit mode={mode} reason={reason} "
        f"tracking_issue={tracking_issue} integration_branch={integration_branch}",
        file=sys.stderr,
    )


def _emit_validation_history_fallback(*, mode: str, reason: str, repo: str, integration_sha: str) -> None:
    print(
        "::warning::validation_history_fallback "
        f"helper=validation_history mode={mode} reason={reason} repo={repo} integration_sha={integration_sha}",
        file=sys.stderr,
    )


def _emit_validation_discovery_fallback(*, mode: str, reason: str, repo: str) -> None:
    print(
        "::warning::validation_discovery_fallback "
        f"helper=validation_discovery mode={mode} reason={reason} repo={repo}",
        file=sys.stderr,
    )


def _emit_operator_bypass_audit_fallback(
    *, mode: str, reason: str, tracking_issue: int | str, integration_sha: str
) -> None:
    print(
        "::warning::operator_bypass_audit_fallback "
        f"helper=operator_bypass_audit mode={mode} reason={reason} "
        f"tracking_issue={tracking_issue} integration_sha={integration_sha}",
        file=sys.stderr,
    )


def _emit_revalidate_events_fallback(
    *, mode: str, reason: str, tracking_issue: int | str, integration_sha: str
) -> None:
    print(
        "::warning::revalidate_events_fallback "
        f"helper=revalidate_events mode={mode} reason={reason} "
        f"tracking_issue={tracking_issue} integration_sha={integration_sha}",
        file=sys.stderr,
    )


def _read_runs_file(path_text: str) -> list[dict[str, Any]]:
    path = Path(_require_nonempty(path_text, "runs_file"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        runs = payload
    elif isinstance(payload, dict) and isinstance(payload.get("workflow_runs"), list):
        runs = payload["workflow_runs"]
    else:
        raise MemoryValidationError("runs_file must contain a JSON array or {\"workflow_runs\": [...]} object")
    if not all(isinstance(item, dict) for item in runs):
        raise MemoryValidationError("runs_file entries must be JSON objects")
    return runs


def _read_json_object_file(path_text: str, field_name: str) -> dict[str, Any]:
    path = Path(_require_nonempty(path_text, field_name))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise MemoryValidationError(f"{field_name} must contain a JSON object")
    return payload


def cmd_record_candidate(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "record_id": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    category = _require_nonempty(args.category, "category")
    summary = _require_nonempty(args.summary, "summary")
    details = _require_nonempty(args.details, "details")
    confidence = _safe_float(args.confidence, default=0.7)
    workflow = _require_nonempty(args.workflow, "workflow")
    run_attempt = _safe_int(args.run_attempt)
    actor = _require_nonempty(args.actor, "actor")
    issue_number = _safe_int(args.issue_number)
    pr_number = _safe_int(args.pr_number)
    source_refs = _split_csv(args.source_refs)
    parent_ids = _split_csv(args.parent_ids)
    injection_matches = scan_candidate_text_for_injection(summary, details)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        record = record_candidate(
            memory_root,
            category=category,
            summary=summary,
            details=details,
            confidence=confidence,
            workflow=workflow,
            run_id=args.run_id,
            run_attempt=run_attempt,
            actor=actor,
            issue_number=issue_number,
            pr_number=pr_number,
            source_refs=source_refs,
            parent_ids=parent_ids,
            supersedes=args.supersedes,
            sensitive=True if args.sensitive is True else None,
            scope_level=getattr(args, "scope_level", None),
        )
        record_run_event(
            memory_root,
            run_id=_require_nonempty(args.run_id or "run-unknown", "run_id"),
            workflow=workflow,
            event_type="candidate_written",
            status="ok",
            message=f"Candidate {record['record_id']} written: {summarize_candidate_for_event(record)}",
            issue_number=issue_number,
            pr_number=pr_number,
            actor=actor,
            metadata={"record_id": record["record_id"], "category": record["category"]},
        )
        return {"record": record}

    result = persist_memory_operation(
        repo_root,
        memory_branch=args.memory_branch,
        memory_root_relative=args.memory_root,
        push_retries=int(args.push_retries),
        commit_message=f"ai-memory: record candidate [{args.category}]",
        operation=_op,
    )
    op_result = result.get("operation_result") or {}
    record = op_result.get("record") or {}
    _print_json({"ok": True, **result})
    if record.get("injection_suspected") is True:
        _emit_telemetry(
            "injection_scan",
            ok=True,
            category=record.get("category"),
            issue_number=issue_number,
            matches=injection_matches,
            record_id=record.get("record_id"),
            workflow=workflow,
        )
    _emit_telemetry(
        "record-candidate",
        ok=True,
        category=category,
        record_id=record.get("record_id"),
        issue_number=issue_number,
        did_push=result.get("did_push", False),
        push_attempts=result.get("push_attempts", 0),
    )
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "promoted": []})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        results = promote_candidates(
            memory_root,
            issue_number=_safe_int(args.issue_number),
            record_id=args.record_id,
        )
        run_id = _require_nonempty(args.run_id, "run_id")
        workflow = _require_nonempty(args.workflow, "workflow")
        actor = _require_nonempty(args.actor, "actor")
        issue_number = _safe_int(args.issue_number)
        pr_number = _safe_int(args.pr_number)

        promoted_count = len(results["promoted"])
        rejected_count = len(results["rejected"])
        if rejected_count > 0:
            record_run_event(
                memory_root,
                run_id=run_id,
                workflow=workflow,
                event_type="promotion_failed_closed",
                status="error",
                message=f"Promotion rejected {rejected_count} candidate(s)",
                issue_number=issue_number,
                pr_number=pr_number,
                actor=actor,
                metadata={"rejected": results["rejected"]},
            )
        record_run_event(
            memory_root,
            run_id=run_id,
            workflow=workflow,
            event_type="promotion_completed",
            status="ok",
            message=f"Promoted {promoted_count} candidate(s)",
            issue_number=issue_number,
            pr_number=pr_number,
            actor=actor,
            metadata={
                "promoted": results["promoted"],
                "rejected": results["rejected"],
                "superseded": results["superseded"],
            },
        )
        return results

    result = persist_memory_operation(
        repo_root,
        memory_branch=args.memory_branch,
        memory_root_relative=args.memory_root,
        push_retries=int(args.push_retries),
        commit_message="ai-memory: promote candidates",
        operation=_op,
    )
    op_result = result.get("operation_result") or {}
    _print_json({"ok": True, **result})
    _emit_telemetry(
        "promote",
        ok=True,
        promoted=len(op_result.get("promoted") or []),
        rejected=len(op_result.get("rejected") or []),
        superseded=len(op_result.get("superseded") or []),
        did_push=result.get("did_push", False),
    )
    return 0


def cmd_finalize_task(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "lineage": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        issue_number = _safe_int(args.issue_number)
        if issue_number is None:
            raise MemoryValidationError("issue_number is required for finalize-task")
        lineage = finalize_task_lineage(
            memory_root,
            issue_number=issue_number,
            issue_url=_require_nonempty(args.issue_url, "issue_url"),
            final_state=_require_nonempty(args.final_state, "final_state"),
            workflow=_require_nonempty(args.workflow, "workflow"),
            run_id=_require_nonempty(args.run_id, "run_id"),
            run_attempt=int(_safe_int(args.run_attempt) or 1),
            pr_number=_safe_int(args.pr_number),
            pr_url=args.pr_url,
            memory_record_ids=_split_csv(args.memory_record_ids),
        )
        record_run_event(
            memory_root,
            run_id=_require_nonempty(args.run_id, "run_id"),
            workflow=_require_nonempty(args.workflow, "workflow"),
            event_type="finalized",
            status="ok",
            message=f"Task issue-{issue_number} finalized as {lineage['state']}",
            issue_number=issue_number,
            pr_number=_safe_int(args.pr_number),
            actor=_require_nonempty(args.actor, "actor"),
            metadata={"lineage_id": lineage["lineage_id"], "state": lineage["state"]},
        )
        return {"lineage": lineage}

    result = persist_memory_operation(
        repo_root,
        memory_branch=args.memory_branch,
        memory_root_relative=args.memory_root,
        push_retries=int(args.push_retries),
        commit_message=f"ai-memory: finalize issue-{args.issue_number}",
        operation=_op,
    )
    op_result = result.get("operation_result") or {}
    lineage = op_result.get("lineage") or {}
    _print_json({"ok": True, **result})
    _emit_telemetry(
        "finalize-task",
        ok=True,
        issue_number=_safe_int(args.issue_number),
        final_state=lineage.get("state"),
        did_push=result.get("did_push", False),
    )
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "summary": None})
        return 0

    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")
    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        summary = compact_memory(
            memory_root,
            month_yyyy_mm=month,
            prune=parse_bool(args.prune, default=False),
        )
        return {"summary": summary}

    result = persist_memory_operation(
        repo_root,
        memory_branch=args.memory_branch,
        memory_root_relative=args.memory_root,
        push_retries=int(args.push_retries),
        commit_message=f"ai-memory: compact {month}",
        operation=_op,
    )
    op_result = result.get("operation_result") or {}
    summary = op_result.get("summary") or {}
    _print_json({"ok": True, **result})
    _emit_telemetry(
        "compact",
        ok=True,
        month=month,
        archived_candidates=summary.get("archived_candidates", 0),
        archived_ledgers=summary.get("archived_ledgers", 0),
        removed_candidates=summary.get("removed_candidates", 0),
        removed_ledgers=summary.get("removed_ledgers", 0),
        did_push=result.get("did_push", False),
    )
    return 0


def cmd_actions_runs_cache_get(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "hit": False, "cache": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        try:
            cache = get_actions_runs_cache(memory_root, repository)
        except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
            _emit_actions_runs_cache_fallback(mode="get", reason="cache_corrupt", repo=repository)
            _print_json(
                {
                    "ok": True,
                    "enabled": True,
                    "hit": False,
                    "cache": None,
                    "warning": str(exc),
                }
            )
            return 0
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": bool(cache),
                "cache": cache,
            }
        )
        return 0
    except MemoryGitError as exc:
        error_text = str(exc)
        lowered = error_text.lower()
        is_missing_branch = (
            ("remote branch" in lowered and ("not found" in lowered or "does not exist" in lowered))
            or "could not find remote ref" in lowered
            or "couldn't find remote ref" in lowered
            or "remote ref does not exist" in lowered
        )
        if is_missing_branch:
            _print_json({"ok": True, "enabled": False, "hit": False, "cache": None, "warning": error_text})
            return 0
        print(f"AI_MEMORY_ERROR: {exc}", file=sys.stderr)
        _print_json({"ok": False, "enabled": True, "hit": False, "cache": None, "error": error_text})
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_actions_runs_cache_put(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "stored": False, "cache": None})
        return 0

    try:
        ttl_seconds = _require_positive_int(str(args.ttl_seconds), "ttl_seconds")
    except MemoryValidationError:
        _emit_actions_runs_cache_fallback(mode="put", reason="invalid_ttl", repo=repository)
        _print_json({"ok": True, "enabled": True, "stored": False, "cache": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        runs = _read_runs_file(args.runs_file)
        cache = put_actions_runs_cache(
            memory_root,
            repository=repository,
            runs=runs,
            etag=(args.etag if args.etag else None),
            ttl_seconds=ttl_seconds,
            fetched_at=(args.fetched_at if args.fetched_at else None),
        )
        return {"cache": cache, "stored": True}

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: actions runs cache [{repository}]",
            operation=_op,
        )
        _print_json({"ok": True, **result})
        return 0
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError, MemoryGitError) as exc:
        _emit_actions_runs_cache_fallback(mode="put", reason="cache_write_failed", repo=repository)
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": False,
                "cache": None,
                "warning": str(exc),
            }
        )
        return 0


def cmd_branch_rebuild_audit_get(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    tracking_issue = _require_positive_int(args.tracking_issue, "tracking_issue")
    integration_branch = _require_nonempty(args.integration_branch, "integration_branch")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "hit": False, "audit": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        try:
            audit = get_branch_rebuild_audit(
                memory_root,
                repository=repository,
                tracking_issue_number=tracking_issue,
                integration_branch=integration_branch,
            )
        except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
            _emit_branch_rebuild_audit_fallback(
                mode="get",
                reason="audit_corrupt",
                tracking_issue=tracking_issue,
                integration_branch=integration_branch,
            )
            _print_json(
                {
                    "ok": True,
                    "enabled": True,
                    "hit": False,
                    "audit": None,
                    "warning": str(exc),
                }
            )
            return 0
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": bool(audit),
                "audit": audit,
            }
        )
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        if _is_missing_memory_branch_error(error_text):
            _print_json({"ok": True, "enabled": False, "hit": False, "audit": None, "warning": error_text})
            return 0
        print(f"AI_MEMORY_ERROR: {error_text}", file=sys.stderr)
        _print_json({"ok": False, "enabled": True, "hit": False, "audit": None, "error": error_text})
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_branch_rebuild_audit_put(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    tracking_issue = _require_positive_int(args.tracking_issue, "tracking_issue")
    integration_branch = _require_nonempty(args.integration_branch, "integration_branch")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "stored": False, "audit": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        audit = _read_json_object_file(args.audit_file, "audit_file")
        stored_audit = put_branch_rebuild_audit(
            memory_root,
            repository=repository,
            tracking_issue_number=tracking_issue,
            integration_branch=integration_branch,
            audit=audit,
        )
        return {"audit": stored_audit, "stored": True}

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: branch rebuild audit [{integration_branch}]",
            operation=_op,
        )
        _print_json({"ok": True, **result})
        return 0
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError, MemoryGitError) as exc:
        _emit_branch_rebuild_audit_fallback(
            mode="put",
            reason="audit_write_failed",
            tracking_issue=tracking_issue,
            integration_branch=integration_branch,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": False,
                "audit": None,
                "warning": str(exc),
            }
        )
        return 0


def cmd_validation_history_get(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    integration_sha = _require_nonempty(args.integration_sha, "integration_sha")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "hit": False, "validation_history": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        try:
            validation_history = get_validation_history(memory_root, repository, integration_sha)
        except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
            _emit_validation_history_fallback(
                mode="get",
                reason="history_corrupt",
                repo=repository,
                integration_sha=integration_sha,
            )
            _print_json(
                {
                    "ok": True,
                    "enabled": True,
                    "hit": False,
                    "validation_history": None,
                    "warning_code": "history_corrupt",
                    "warning": _sanitize_git_error(str(exc)),
                }
            )
            return 0
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": bool(validation_history),
                "validation_history": validation_history,
            }
        )
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        if _is_missing_memory_branch_error(error_text):
            _print_json(
                {
                    "ok": True,
                    "enabled": False,
                    "hit": False,
                    "validation_history": None,
                    "warning": error_text,
                }
            )
            return 0
        _emit_validation_history_fallback(
            mode="get",
            reason="history_read_failed",
            repo=repository,
            integration_sha=integration_sha,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": False,
                "validation_history": None,
                "warning_code": "history_read_failed",
                "warning": error_text,
            }
        )
        return 0
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_validation_history_append(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    integration_sha = _require_nonempty(args.integration_sha, "integration_sha")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "stored": False, "validation_history": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry = _read_json_object_file(args.entry_file, "entry_file")
        validation_history = append_validation_history_entry(
            memory_root,
            repository=repository,
            integration_sha=integration_sha,
            entry=entry,
        )
        return {"stored": True, "validation_history": validation_history}

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: validation history [{integration_sha[:12]}]",
            operation=_op,
        )
        op_result = result.get("operation_result") or {}
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": bool(op_result.get("stored")),
                "validation_history": op_result.get("validation_history"),
                **result,
            }
        )
        return 0
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError, MemoryGitError) as exc:
        _emit_validation_history_fallback(
            mode="append",
            reason="history_write_failed",
            repo=repository,
            integration_sha=integration_sha,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": False,
                "validation_history": None,
                "warning": _sanitize_git_error(str(exc)),
            }
        )
        return 0


def cmd_validation_discovery_get(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "hit": False, "validation_discovery": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        try:
            validation_discovery = get_validation_discovery(memory_root, repository)
        except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
            _emit_validation_discovery_fallback(
                mode="get", reason="discovery_corrupt", repo=repository
            )
            _print_json(
                {
                    "ok": True,
                    "enabled": True,
                    "hit": False,
                    "validation_discovery": None,
                    "warning_code": "discovery_corrupt",
                    "warning": _sanitize_git_error(str(exc)),
                }
            )
            return 0
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": bool(validation_discovery),
                "validation_discovery": validation_discovery,
            }
        )
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        if _is_missing_memory_branch_error(error_text):
            _print_json(
                {
                    "ok": True,
                    "enabled": False,
                    "hit": False,
                    "validation_discovery": None,
                    "warning": error_text,
                }
            )
            return 0
        _emit_validation_discovery_fallback(
            mode="get", reason="discovery_read_failed", repo=repository
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": False,
                "validation_discovery": None,
                "warning_code": "discovery_read_failed",
                "warning": error_text,
            }
        )
        return 0
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_validation_discovery_append(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "stored": False, "validation_discovery": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry = _read_json_object_file(args.entry_file, "entry_file")
        validation_discovery = append_validation_discovery_entry(
            memory_root,
            repository=repository,
            entry=entry,
        )
        return {"stored": True, "validation_discovery": validation_discovery}

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: validation discovery [{repository}]",
            operation=_op,
        )
        op_result = result.get("operation_result") or {}
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": bool(op_result.get("stored")),
                "validation_discovery": op_result.get("validation_discovery"),
                **result,
            }
        )
        return 0
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError, MemoryGitError) as exc:
        _emit_validation_discovery_fallback(
            mode="append", reason="discovery_write_failed", repo=repository
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": False,
                "validation_discovery": None,
                "warning": _sanitize_git_error(str(exc)),
            }
        )
        return 0


def cmd_operator_bypass_audit_get(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    integration_sha = _require_nonempty(args.integration_sha, "integration_sha")
    tracking_issue = _require_positive_int(args.tracking_issue, "tracking_issue")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "hit": False, "audit": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        try:
            audit = get_operator_bypass_audit(
                memory_root,
                repository=repository,
                tracking_issue_number=tracking_issue,
                integration_sha=integration_sha,
            )
        except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
            _emit_operator_bypass_audit_fallback(
                mode="get",
                reason="audit_corrupt",
                tracking_issue=tracking_issue,
                integration_sha=integration_sha,
            )
            _print_json(
                {
                    "ok": True,
                    "enabled": True,
                    "hit": False,
                    "audit": None,
                    "warning_code": "audit_corrupt",
                    "warning": _sanitize_git_error(str(exc)),
                }
            )
            return 0
        _print_json({"ok": True, "enabled": True, "hit": bool(audit), "audit": audit})
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        if _is_missing_memory_branch_error(error_text):
            _print_json({"ok": True, "enabled": False, "hit": False, "audit": None, "warning": error_text})
            return 0
        _emit_operator_bypass_audit_fallback(
            mode="get",
            reason="audit_read_failed",
            tracking_issue=tracking_issue,
            integration_sha=integration_sha,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": False,
                "audit": None,
                "warning_code": "audit_read_failed",
                "warning": error_text,
            }
        )
        return 0
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_operator_bypass_audit_append(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    integration_sha = _require_nonempty(args.integration_sha, "integration_sha")
    tracking_issue = _require_positive_int(args.tracking_issue, "tracking_issue")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "stored": False, "audit": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry = _read_json_object_file(args.entry_file, "entry_file")
        audit = append_operator_bypass_audit_entry(
            memory_root,
            repository=repository,
            tracking_issue_number=tracking_issue,
            integration_sha=integration_sha,
            entry=entry,
        )
        return {"stored": True, "audit": audit}

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: operator bypass audit [{tracking_issue}:{integration_sha[:12]}]",
            operation=_op,
        )
        op_result = result.get("operation_result") or {}
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": bool(op_result.get("stored")),
                "audit": op_result.get("audit"),
                **result,
            }
        )
        return 0
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError, MemoryGitError) as exc:
        _emit_operator_bypass_audit_fallback(
            mode="append",
            reason="audit_write_failed",
            tracking_issue=tracking_issue,
            integration_sha=integration_sha,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": False,
                "audit": None,
                "warning": _sanitize_git_error(str(exc)),
            }
        )
        return 0


def cmd_revalidate_events_get(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    integration_sha = _require_nonempty(args.integration_sha, "integration_sha")
    tracking_issue = _require_positive_int(args.tracking_issue, "tracking_issue")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "hit": False, "events": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        try:
            events = get_revalidate_events(
                memory_root,
                repository=repository,
                tracking_issue_number=tracking_issue,
                integration_sha=integration_sha,
            )
        except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError) as exc:
            _emit_revalidate_events_fallback(
                mode="get",
                reason="events_corrupt",
                tracking_issue=tracking_issue,
                integration_sha=integration_sha,
            )
            _print_json(
                {
                    "ok": True,
                    "enabled": True,
                    "hit": False,
                    "events": None,
                    "warning_code": "events_corrupt",
                    "warning": _sanitize_git_error(str(exc)),
                }
            )
            return 0
        _print_json({"ok": True, "enabled": True, "hit": bool(events), "events": events})
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        if _is_missing_memory_branch_error(error_text):
            _print_json({"ok": True, "enabled": False, "hit": False, "events": None, "warning": error_text})
            return 0
        _emit_revalidate_events_fallback(
            mode="get",
            reason="events_read_failed",
            tracking_issue=tracking_issue,
            integration_sha=integration_sha,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "hit": False,
                "events": None,
                "warning_code": "events_read_failed",
                "warning": error_text,
            }
        )
        return 0
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_revalidate_events_append(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    repository = _require_nonempty(args.repo, "repo")
    integration_sha = _require_nonempty(args.integration_sha, "integration_sha")
    tracking_issue = _require_positive_int(args.tracking_issue, "tracking_issue")
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "stored": False, "events": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry = _read_json_object_file(args.entry_file, "entry_file")
        events = append_revalidate_event(
            memory_root,
            repository=repository,
            tracking_issue_number=tracking_issue,
            integration_sha=integration_sha,
            entry=entry,
        )
        return {"stored": True, "events": events}

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: revalidate events [{tracking_issue}:{integration_sha[:12]}]",
            operation=_op,
        )
        op_result = result.get("operation_result") or {}
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": bool(op_result.get("stored")),
                "events": op_result.get("events"),
                **result,
            }
        )
        return 0
    except (MemoryValidationError, json.JSONDecodeError, OSError, ValueError, MemoryGitError) as exc:
        _emit_revalidate_events_fallback(
            mode="append",
            reason="events_write_failed",
            tracking_issue=tracking_issue,
            integration_sha=integration_sha,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "stored": False,
                "events": None,
                "warning": _sanitize_git_error(str(exc)),
            }
        )
        return 0


def cmd_processed_command_check(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "exists": False, "entry": None})
        _emit_telemetry("processed-command-check", ok=True, enabled=False, exists=False)
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        entry = get_processed_command_entry(
            memory_root,
            issue_number=_require_positive_int(args.issue_number, "issue_number"),
            comment_id=_require_positive_int(args.comment_id, "comment_id"),
            command=_require_nonempty(args.command, "command"),
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "exists": bool(entry),
                "entry": entry,
            }
        )
        _emit_telemetry("processed-command-check", ok=True, enabled=True, exists=bool(entry))
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        lowered = error_text.lower()
        is_missing_branch = (
            ("remote branch" in lowered and ("not found" in lowered or "does not exist" in lowered))
            or "could not find remote ref" in lowered
            or "couldn't find remote ref" in lowered
            or "remote ref does not exist" in lowered
            or "did not match any file(s) known to git" in lowered
        )
        if is_missing_branch:
            _print_json({"ok": True, "enabled": False, "exists": False, "entry": None, "warning": error_text})
            _emit_telemetry("processed-command-check", ok=True, enabled=False, exists=False, warning="branch_unavailable")
            return 0
        print(f"AI_MEMORY_ERROR: {error_text}", file=sys.stderr)
        _print_json({"ok": False, "enabled": True, "exists": False, "entry": None, "error": error_text})
        _emit_telemetry("processed-command-check", ok=False, enabled=True, exists=False, error="git_error", warning="git_error")
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_processed_command_list(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "entries": [], "count": 0})
        _emit_telemetry("processed-command-list", ok=True, enabled=False, count=0)
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        entries = list_processed_command_entries(
            memory_root,
            issue_number=_require_positive_int(args.issue_number, "issue_number"),
            command=args.command,
            workflow=args.workflow,
            status=args.status,
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "entries": entries,
                "count": len(entries),
            }
        )
        _emit_telemetry("processed-command-list", ok=True, enabled=True, count=len(entries))
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        lowered = error_text.lower()
        is_missing_branch = (
            ("remote branch" in lowered and ("not found" in lowered or "does not exist" in lowered))
            or "could not find remote ref" in lowered
            or "couldn't find remote ref" in lowered
            or "remote ref does not exist" in lowered
            or "did not match any file(s) known to git" in lowered
        )
        if is_missing_branch:
            _print_json({"ok": True, "enabled": False, "entries": [], "count": 0, "warning": error_text})
            _emit_telemetry("processed-command-list", ok=True, enabled=False, count=0, warning="branch_unavailable")
            return 0
        print(f"AI_MEMORY_ERROR: {error_text}", file=sys.stderr)
        _print_json({"ok": False, "enabled": True, "entries": [], "count": 0, "error": error_text})
        _emit_telemetry("processed-command-list", ok=False, enabled=True, count=0, error="git_error", warning="git_error")
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_clarify_loop_guard(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    clarify_hash_input = _require_nonempty(args.clarify_hash, "clarify_hash").strip()
    lowered_clarify_hash_input = clarify_hash_input.lower()
    if len(lowered_clarify_hash_input) == 64 and all(ch in "0123456789abcdef" for ch in lowered_clarify_hash_input):
        clarify_hash = lowered_clarify_hash_input
    else:
        clarify_hash = compute_normalized_sha256(clarify_hash_input)
    max_cycles = _require_positive_int(args.max_cycles, "max_cycles")

    if not args.enabled:
        result = evaluate_clarify_loop_guard([], clarify_hash=clarify_hash, max_cycles=max_cycles)
        _print_json(
            {
                "ok": True,
                "enabled": False,
                "clarify_hash": clarify_hash,
                "result": result,
            }
        )
        _emit_telemetry("clarify-loop-guard", ok=True, enabled=False, blocked=bool(result.get("blocked")), cycle=_safe_int(result.get("cycle")), entries_considered=0)
        return 0

    repo_root = _resolve_repo_root(args.repo_root)
    branch_dir = None
    try:
        branch_dir = read_memory_root_from_branch(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
        )
        memory_root = _resolve_memory_root(branch_dir, args.memory_root)
        entries = list_processed_command_entries(
            memory_root,
            issue_number=_require_positive_int(args.issue_number, "issue_number"),
            command="answer",
            workflow="orchestrate_clarify_respond",
        )
        result = evaluate_clarify_loop_guard(
            entries,
            clarify_hash=clarify_hash,
            max_cycles=max_cycles,
            current_comment_id=_safe_int(args.current_comment_id),
        )
        _print_json(
            {
                "ok": True,
                "enabled": True,
                "clarify_hash": clarify_hash,
                "result": result,
                "entries_considered": len(entries),
            }
        )
        _emit_telemetry(
            "clarify-loop-guard",
            ok=True,
            enabled=True,
            blocked=bool(result.get("blocked")),
            cycle=_safe_int(result.get("cycle")),
            entries_considered=len(entries),
        )
        return 0
    except MemoryGitError as exc:
        error_text = _sanitize_git_error(str(exc))
        lowered = error_text.lower()
        is_missing_branch = (
            ("remote branch" in lowered and ("not found" in lowered or "does not exist" in lowered))
            or "could not find remote ref" in lowered
            or "couldn't find remote ref" in lowered
            or "remote ref does not exist" in lowered
            or "did not match any file(s) known to git" in lowered
        )
        if is_missing_branch:
            result = evaluate_clarify_loop_guard([], clarify_hash=clarify_hash, max_cycles=max_cycles)
            _print_json(
                {
                    "ok": True,
                    "enabled": False,
                    "clarify_hash": clarify_hash,
                    "result": result,
                    "warning": error_text,
                }
            )
            _emit_telemetry("clarify-loop-guard", ok=True, enabled=False, blocked=bool(result.get("blocked")), cycle=_safe_int(result.get("cycle")), entries_considered=0, warning="branch_unavailable")
            return 0
        print(f"AI_MEMORY_ERROR: {error_text}", file=sys.stderr)
        _print_json(
            {
                "ok": False,
                "enabled": True,
                "clarify_hash": clarify_hash,
                "result": {},
                "error": error_text,
            }
        )
        _emit_telemetry("clarify-loop-guard", ok=False, enabled=True, error="git_error", warning="git_error")
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_processed_command_claim(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json(
            {
                "ok": True,
                "enabled": False,
                "did_commit": False,
                "did_push": False,
                "commit_sha": None,
                "push_attempts": 0,
                "operation_result": {
                    "claimed": True,
                    "entry": None,
                },
            }
        )
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry, claimed = claim_processed_command(
            memory_root,
            issue_number=_require_positive_int(args.issue_number, "issue_number"),
            comment_id=_require_positive_int(args.comment_id, "comment_id"),
            command=_require_nonempty(args.command, "command"),
            workflow=_require_nonempty(args.workflow, "workflow"),
            actor=_require_nonempty(args.actor, "actor"),
            run_id=_require_nonempty(args.run_id, "run_id"),
            run_attempt=_require_positive_int(args.run_attempt, "run_attempt"),
            metadata=_json_or_empty(args.metadata_json),
        )
        return {
            "claimed": claimed,
            "entry": entry,
        }

    try:
        result = persist_memory_operation(
            repo_root,
            memory_branch=args.memory_branch,
            memory_root_relative=args.memory_root,
            push_retries=int(args.push_retries),
            commit_message=f"ai-memory: claim processed command [{args.command}]",
            operation=_op,
        )
        op_result = result.get("operation_result") or {}
        _print_json({"ok": True, **result})
        _emit_telemetry(
            "processed-command-claim",
            ok=True,
            command=args.command,
            claimed=op_result.get("claimed", False),
            did_push=result.get("did_push", False),
        )
        return 0
    except MemoryGitError as exc:
        # Concurrent claims on different runners can race on the same entry file.
        # If the entry now exists on the memory branch, treat this as a duplicate claim.
        branch_dir = None
        try:
            branch_dir = read_memory_root_from_branch(
                repo_root,
                memory_branch=args.memory_branch,
                memory_root_relative=args.memory_root,
            )
            memory_root = _resolve_memory_root(branch_dir, args.memory_root)
            entry = get_processed_command_entry(
                memory_root,
                issue_number=_require_positive_int(args.issue_number, "issue_number"),
                comment_id=_require_positive_int(args.comment_id, "comment_id"),
                command=_require_nonempty(args.command, "command"),
            )
            if entry is not None:
                print(f"AI_MEMORY_WARNING: {exc}", file=sys.stderr)
                _print_json(
                    {
                        "ok": True,
                        "did_commit": False,
                        "did_push": False,
                        "commit_sha": None,
                        "push_attempts": int(args.push_retries),
                        "operation_result": {
                            "claimed": False,
                            "entry": entry,
                        },
                        "warning": str(exc),
                    }
                )
                return 0
        except MemoryGitError as recovery_exc:
            print(f"AI_MEMORY_WARNING: recovery duplicate-check failed: {recovery_exc}", file=sys.stderr)
        finally:
            if branch_dir:
                shutil.rmtree(branch_dir, ignore_errors=True)
        raise


def cmd_processed_command_complete(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "entry": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        entry = complete_processed_command(
            memory_root,
            issue_number=_require_positive_int(args.issue_number, "issue_number"),
            comment_id=_require_positive_int(args.comment_id, "comment_id"),
            command=_require_nonempty(args.command, "command"),
            status=_require_nonempty(args.status, "status"),
            metadata=_json_or_empty(args.metadata_json),
        )
        return {"entry": entry}

    result = persist_memory_operation(
        repo_root,
        memory_branch=args.memory_branch,
        memory_root_relative=args.memory_root,
        push_retries=int(args.push_retries),
        commit_message=f"ai-memory: complete processed command [{args.command}]",
        operation=_op,
    )
    _print_json({"ok": True, **result})
    _emit_telemetry(
        "processed-command-complete",
        ok=True,
        command=args.command,
        status=args.status,
        did_push=result.get("did_push", False),
    )
    return 0


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--memory-branch", default=None)
    parser.add_argument("--memory-root", default=None)
    parser.add_argument("--push-retries", type=int, default=None)
    parser.add_argument("--enabled", action="store_true", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI memory workflow helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    retrieve = subparsers.add_parser("retrieve", help="Read role-scoped memory context")
    _add_shared_args(retrieve)
    retrieve.add_argument("--role", required=True)
    retrieve.add_argument("--issue-number", default=None)
    retrieve.add_argument("--pr-number", default=None)
    retrieve.add_argument("--output-file", default=None)
    retrieve.add_argument("--retrieval-profiles", default=None)
    retrieve.add_argument("--issue-title", default=None)
    retrieve.add_argument("--issue-body-file", default=None)
    retrieve.add_argument("--category", default=None)
    retrieve.add_argument("--scope-level", default=None)
    retrieve.add_argument("--max", default=None)
    retrieve.set_defaults(func=cmd_retrieve)

    event = subparsers.add_parser("record-run-event", help="Append run ledger event")
    _add_shared_args(event)
    event.add_argument("--run-id", required=True)
    event.add_argument("--workflow", required=True)
    event.add_argument("--event-type", required=True)
    event.add_argument("--status", required=True)
    event.add_argument("--message", required=True)
    event.add_argument("--issue-number", default=None)
    event.add_argument("--pr-number", default=None)
    event.add_argument("--actor", required=True)
    event.add_argument("--metadata-json", default="{}")
    event.set_defaults(func=cmd_record_run_event)

    candidate = subparsers.add_parser("record-candidate", help="Write candidate memory record")
    _add_shared_args(candidate)
    candidate.add_argument("--category", required=True)
    candidate.add_argument("--summary", required=True)
    candidate.add_argument("--details", required=True)
    candidate.add_argument("--confidence", default="0.70")
    candidate.add_argument("--workflow", required=True)
    candidate.add_argument("--run-id", required=True)
    candidate.add_argument("--run-attempt", default="1")
    candidate.add_argument("--actor", required=True)
    candidate.add_argument("--issue-number", default=None)
    candidate.add_argument("--pr-number", default=None)
    candidate.add_argument("--source-refs", default="")
    candidate.add_argument("--parent-ids", default="")
    candidate.add_argument("--supersedes", default=None)
    candidate.add_argument("--sensitive", action="store_true")
    candidate.add_argument("--scope-level", default=None)
    candidate.set_defaults(func=cmd_record_candidate)

    promote = subparsers.add_parser("promote", help="Promote candidate records")
    _add_shared_args(promote)
    promote.add_argument("--issue-number", default=None)
    promote.add_argument("--record-id", default=None)
    promote.add_argument("--run-id", required=True)
    promote.add_argument("--workflow", required=True)
    promote.add_argument("--actor", required=True)
    promote.add_argument("--pr-number", default=None)
    promote.set_defaults(func=cmd_promote)

    finalize = subparsers.add_parser("finalize-task", help="Write final task lineage state")
    _add_shared_args(finalize)
    finalize.add_argument("--issue-number", required=True)
    finalize.add_argument("--issue-url", required=True)
    finalize.add_argument("--final-state", required=True)
    finalize.add_argument("--workflow", required=True)
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--run-attempt", default="1")
    finalize.add_argument("--actor", required=True)
    finalize.add_argument("--pr-number", default=None)
    finalize.add_argument("--pr-url", default=None)
    finalize.add_argument("--memory-record-ids", default="")
    finalize.set_defaults(func=cmd_finalize_task)

    compact = subparsers.add_parser("compact", help="Archive monthly candidate and ledger data")
    _add_shared_args(compact)
    compact.add_argument("--month", default=None)
    compact.add_argument("--prune", default="false")
    compact.set_defaults(func=cmd_compact)

    actions_runs_cache = subparsers.add_parser("actions-runs-cache", help="Read/write actions runs cache")
    actions_runs_cache_subparsers = actions_runs_cache.add_subparsers(dest="actions_runs_cache_command", required=True)

    actions_runs_cache_get = actions_runs_cache_subparsers.add_parser("get", help="Read actions runs cache")
    _add_shared_args(actions_runs_cache_get)
    actions_runs_cache_get.add_argument("--repo", required=True)
    actions_runs_cache_get.set_defaults(func=cmd_actions_runs_cache_get)

    actions_runs_cache_put = actions_runs_cache_subparsers.add_parser("put", help="Write actions runs cache")
    _add_shared_args(actions_runs_cache_put)
    actions_runs_cache_put.add_argument("--repo", required=True)
    actions_runs_cache_put.add_argument("--runs-file", required=True)
    actions_runs_cache_put.add_argument("--etag", default="")
    actions_runs_cache_put.add_argument("--ttl-seconds", default="60")
    actions_runs_cache_put.add_argument("--fetched-at", default="")
    actions_runs_cache_put.set_defaults(func=cmd_actions_runs_cache_put)

    branch_rebuild_audit = subparsers.add_parser("branch-rebuild-audit", help="Read/write branch rebuild audit")
    branch_rebuild_audit_subparsers = branch_rebuild_audit.add_subparsers(
        dest="branch_rebuild_audit_command", required=True
    )

    branch_rebuild_audit_get = branch_rebuild_audit_subparsers.add_parser("get", help="Read branch rebuild audit")
    _add_shared_args(branch_rebuild_audit_get)
    branch_rebuild_audit_get.add_argument("--repo", required=True)
    branch_rebuild_audit_get.add_argument("--tracking-issue", required=True)
    branch_rebuild_audit_get.add_argument("--integration-branch", required=True)
    branch_rebuild_audit_get.set_defaults(func=cmd_branch_rebuild_audit_get)

    branch_rebuild_audit_put = branch_rebuild_audit_subparsers.add_parser("put", help="Write branch rebuild audit")
    _add_shared_args(branch_rebuild_audit_put)
    branch_rebuild_audit_put.add_argument("--repo", required=True)
    branch_rebuild_audit_put.add_argument("--tracking-issue", required=True)
    branch_rebuild_audit_put.add_argument("--integration-branch", required=True)
    branch_rebuild_audit_put.add_argument("--audit-file", required=True)
    branch_rebuild_audit_put.set_defaults(func=cmd_branch_rebuild_audit_put)

    validation_history = subparsers.add_parser("validation-history", help="Read/append validation history by integration SHA")
    validation_history_subparsers = validation_history.add_subparsers(
        dest="validation_history_command", required=True
    )

    validation_history_get = validation_history_subparsers.add_parser("get", help="Read validation history")
    _add_shared_args(validation_history_get)
    validation_history_get.add_argument("--repo", required=True)
    validation_history_get.add_argument("--integration-sha", required=True)
    validation_history_get.set_defaults(func=cmd_validation_history_get)

    validation_history_append = validation_history_subparsers.add_parser("append", help="Append validation history entry")
    _add_shared_args(validation_history_append)
    validation_history_append.add_argument("--repo", required=True)
    validation_history_append.add_argument("--integration-sha", required=True)
    validation_history_append.add_argument("--entry-file", required=True)
    validation_history_append.set_defaults(func=cmd_validation_history_append)

    validation_discovery = subparsers.add_parser(
        "validation-discovery", help="Read/append per-repository .ai/validate.yml discovery outcomes"
    )
    validation_discovery_subparsers = validation_discovery.add_subparsers(
        dest="validation_discovery_command", required=True
    )

    validation_discovery_get = validation_discovery_subparsers.add_parser(
        "get", help="Read validation discovery history for a consumer repository"
    )
    _add_shared_args(validation_discovery_get)
    validation_discovery_get.add_argument("--repo", required=True)
    validation_discovery_get.set_defaults(func=cmd_validation_discovery_get)

    validation_discovery_append = validation_discovery_subparsers.add_parser(
        "append", help="Append a validation discovery entry for a consumer repository"
    )
    _add_shared_args(validation_discovery_append)
    validation_discovery_append.add_argument("--repo", required=True)
    validation_discovery_append.add_argument("--entry-file", required=True)
    validation_discovery_append.set_defaults(func=cmd_validation_discovery_append)

    operator_bypass_audit = subparsers.add_parser("operator-bypass-audit", help="Read/append operator bypass audit entries")
    operator_bypass_audit_subparsers = operator_bypass_audit.add_subparsers(
        dest="operator_bypass_audit_command", required=True
    )

    operator_bypass_audit_get = operator_bypass_audit_subparsers.add_parser("get", help="Read operator bypass audit")
    _add_shared_args(operator_bypass_audit_get)
    operator_bypass_audit_get.add_argument("--repo", required=True)
    operator_bypass_audit_get.add_argument("--tracking-issue", required=True)
    operator_bypass_audit_get.add_argument("--integration-sha", required=True)
    operator_bypass_audit_get.set_defaults(func=cmd_operator_bypass_audit_get)

    operator_bypass_audit_append = operator_bypass_audit_subparsers.add_parser(
        "append", help="Append operator bypass audit entry"
    )
    _add_shared_args(operator_bypass_audit_append)
    operator_bypass_audit_append.add_argument("--repo", required=True)
    operator_bypass_audit_append.add_argument("--tracking-issue", required=True)
    operator_bypass_audit_append.add_argument("--integration-sha", required=True)
    operator_bypass_audit_append.add_argument("--entry-file", required=True)
    operator_bypass_audit_append.set_defaults(func=cmd_operator_bypass_audit_append)

    revalidate_events = subparsers.add_parser("revalidate-events", help="Read/append revalidate event entries")
    revalidate_events_subparsers = revalidate_events.add_subparsers(
        dest="revalidate_events_command", required=True
    )

    revalidate_events_get = revalidate_events_subparsers.add_parser("get", help="Read revalidate events")
    _add_shared_args(revalidate_events_get)
    revalidate_events_get.add_argument("--repo", required=True)
    revalidate_events_get.add_argument("--tracking-issue", required=True)
    revalidate_events_get.add_argument("--integration-sha", required=True)
    revalidate_events_get.set_defaults(func=cmd_revalidate_events_get)

    revalidate_events_append = revalidate_events_subparsers.add_parser("append", help="Append revalidate event entry")
    _add_shared_args(revalidate_events_append)
    revalidate_events_append.add_argument("--repo", required=True)
    revalidate_events_append.add_argument("--tracking-issue", required=True)
    revalidate_events_append.add_argument("--integration-sha", required=True)
    revalidate_events_append.add_argument("--entry-file", required=True)
    revalidate_events_append.set_defaults(func=cmd_revalidate_events_append)

    processed_check = subparsers.add_parser("processed-command-check", help="Check processed command entry")
    _add_shared_args(processed_check)
    processed_check.add_argument("--issue-number", required=True)
    processed_check.add_argument("--comment-id", required=True)
    processed_check.add_argument("--command", required=True)
    processed_check.set_defaults(func=cmd_processed_command_check)

    processed_list = subparsers.add_parser("processed-command-list", help="List processed command entries")
    _add_shared_args(processed_list)
    processed_list.add_argument("--issue-number", required=True)
    processed_list.add_argument("--command", default=None)
    processed_list.add_argument("--workflow", default=None)
    processed_list.add_argument("--status", default=None)
    processed_list.set_defaults(func=cmd_processed_command_list)

    loop_guard = subparsers.add_parser("clarify-loop-guard", help="Evaluate clarify loop guard from processed command history")
    _add_shared_args(loop_guard)
    loop_guard.add_argument("--issue-number", required=True)
    loop_guard.add_argument("--clarify-hash", required=True)
    loop_guard.add_argument("--max-cycles", required=True)
    loop_guard.add_argument("--current-comment-id", default=None)
    loop_guard.set_defaults(func=cmd_clarify_loop_guard)

    processed_claim = subparsers.add_parser("processed-command-claim", help="Claim processed command entry")
    _add_shared_args(processed_claim)
    processed_claim.add_argument("--issue-number", required=True)
    processed_claim.add_argument("--comment-id", required=True)
    processed_claim.add_argument("--command", required=True)
    processed_claim.add_argument("--workflow", required=True)
    processed_claim.add_argument("--actor", required=True)
    processed_claim.add_argument("--run-id", required=True)
    processed_claim.add_argument("--run-attempt", default="1")
    processed_claim.add_argument("--metadata-json", default="{}")
    processed_claim.set_defaults(func=cmd_processed_command_claim)

    processed_complete = subparsers.add_parser("processed-command-complete", help="Complete processed command entry")
    _add_shared_args(processed_complete)
    processed_complete.add_argument("--issue-number", required=True)
    processed_complete.add_argument("--comment-id", required=True)
    processed_complete.add_argument("--command", required=True)
    processed_complete.add_argument("--status", required=True)
    processed_complete.add_argument("--metadata-json", default="{}")
    processed_complete.set_defaults(func=cmd_processed_command_complete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except (MemoryValidationError, MemoryGitError, ValueError, json.JSONDecodeError) as exc:
        print(f"AI_MEMORY_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
