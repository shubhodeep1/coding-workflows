#!/usr/bin/env python3
"""CLI for AI memory operations used by GitHub workflows."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_memory_lib import (
    MemoryGitError,
    MemoryValidationError,
    claim_processed_command,
    compact_memory,
    compute_normalized_sha256,
    complete_processed_command,
    evaluate_clarify_loop_guard,
    finalize_task_lineage,
    get_processed_command_entry,
    list_processed_command_entries,
    parse_bool,
    persist_memory_operation,
    promote_candidates,
    read_memory_root_from_branch,
    record_candidate,
    record_run_event,
    resolve_memory_root_dir,
    retrieve_memory_context,
    summarize_candidate_for_event,
)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


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
    parsed = _safe_int(value)
    if parsed is None or parsed < 1:
        raise MemoryValidationError(f"{field_name} must be a positive integer")
    return parsed


def _read_env_defaults(args: argparse.Namespace) -> argparse.Namespace:
    if not getattr(args, "memory_branch", None):
        args.memory_branch = os.getenv("AI_MEMORY_BRANCH", "ai-memory")
    if not getattr(args, "memory_root", None):
        args.memory_root = os.getenv("AI_MEMORY_ROOT", "ai-memory")
    if not getattr(args, "push_retries", None):
        args.push_retries = int(os.getenv("AI_MEMORY_PUSH_RETRIES", "5"))
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
            print(f"AI_MEMORY_WARNING: {exc}", file=sys.stderr)
            context = "AI MEMORY CONTEXT\nstatus: unavailable\n"
            if args.output_file:
                Path(args.output_file).write_text(context, encoding="utf-8")
            else:
                print(context, end="")
            _print_json({"ok": True, "enabled": False, "records_selected": 0, "estimated_tokens": 0, "warning": str(exc)})
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
            }
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
    return 0


def _json_or_empty(payload: str | None) -> dict[str, Any]:
    if payload is None or payload.strip() == "":
        return {}
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise MemoryValidationError("metadata JSON must be an object")
    return parsed


def cmd_record_candidate(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "record_id": None})
        return 0

    repo_root = _resolve_repo_root(args.repo_root)

    def _op(clone_dir: Path) -> dict[str, Any]:
        memory_root = _resolve_memory_root(clone_dir, args.memory_root)
        record = record_candidate(
            memory_root,
            category=_require_nonempty(args.category, "category"),
            summary=_require_nonempty(args.summary, "summary"),
            details=_require_nonempty(args.details, "details"),
            confidence=_safe_float(args.confidence, default=0.7),
            workflow=_require_nonempty(args.workflow, "workflow"),
            run_id=args.run_id,
            run_attempt=_safe_int(args.run_attempt),
            actor=_require_nonempty(args.actor, "actor"),
            issue_number=_safe_int(args.issue_number),
            pr_number=_safe_int(args.pr_number),
            source_refs=_split_csv(args.source_refs),
            parent_ids=_split_csv(args.parent_ids),
            supersedes=args.supersedes,
            sensitive=True if args.sensitive is True else None,
        )
        record_run_event(
            memory_root,
            run_id=_require_nonempty(args.run_id or "run-unknown", "run_id"),
            workflow=_require_nonempty(args.workflow, "workflow"),
            event_type="candidate_written",
            status="ok",
            message=f"Candidate {record['record_id']} written: {summarize_candidate_for_event(record)}",
            issue_number=_safe_int(args.issue_number),
            pr_number=_safe_int(args.pr_number),
            actor=_require_nonempty(args.actor, "actor"),
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
    _print_json({"ok": True, **result})
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
    _print_json({"ok": True, **result})
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
    _print_json({"ok": True, **result})
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
    _print_json({"ok": True, **result})
    return 0


def cmd_processed_command_check(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "exists": False, "entry": None})
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
            _print_json({"ok": True, "enabled": False, "exists": False, "entry": None, "warning": error_text})
            return 0
        print(f"AI_MEMORY_ERROR: {exc}", file=sys.stderr)
        _print_json({"ok": False, "enabled": True, "exists": False, "entry": None, "error": error_text})
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_processed_command_list(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    if not args.enabled:
        _print_json({"ok": True, "enabled": False, "entries": [], "count": 0})
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
            _print_json({"ok": True, "enabled": False, "entries": [], "count": 0, "warning": error_text})
            return 0
        print(f"AI_MEMORY_ERROR: {exc}", file=sys.stderr)
        _print_json({"ok": False, "enabled": True, "entries": [], "count": 0, "error": error_text})
        return 2
    finally:
        if branch_dir:
            shutil.rmtree(branch_dir, ignore_errors=True)


def cmd_clarify_loop_guard(args: argparse.Namespace) -> int:
    args = _read_env_defaults(args)
    clarify_hash = compute_normalized_sha256(_require_nonempty(args.clarify_hash, "clarify_hash"))
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
            return 0
        print(f"AI_MEMORY_ERROR: {exc}", file=sys.stderr)
        _print_json(
            {
                "ok": False,
                "enabled": True,
                "clarify_hash": clarify_hash,
                "result": {},
                "error": error_text,
            }
        )
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
        _print_json({"ok": True, **result})
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
