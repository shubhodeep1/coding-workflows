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
    compact_memory,
    finalize_task_lineage,
    parse_bool,
    persist_memory_operation,
    promote_candidates,
    read_memory_root_from_branch,
    record_candidate,
    record_run_event,
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
    return memory_dir / memory_root_relative


def _read_env_defaults(args: argparse.Namespace) -> argparse.Namespace:
    if not getattr(args, "memory_branch", None):
        args.memory_branch = os.getenv("AI_MEMORY_BRANCH", "ai-memory")
    if not getattr(args, "memory_root", None):
        args.memory_root = os.getenv("AI_MEMORY_ROOT", ".github/ai-memory")
    if not getattr(args, "push_retries", None):
        args.push_retries = int(os.getenv("AI_MEMORY_PUSH_RETRIES", "5"))
    if not getattr(args, "enabled", None):
        args.enabled = parse_bool(os.getenv("AI_MEMORY_ENABLED", "true"), default=True)
    if not getattr(args, "retrieval_profiles", None):
        args.retrieval_profiles = os.getenv(
            "AI_MEMORY_RETRIEVAL_PROFILES", ".github/ai-memory/config/retrieval_profiles.v1.json"
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

        result = retrieve_memory_context(
            memory_root,
            profiles_path,
            role=_require_nonempty(args.role, "role"),
            issue_number=_safe_int(args.issue_number),
            pr_number=_safe_int(args.pr_number),
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
