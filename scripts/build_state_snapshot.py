#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIR))


from ai_memory_lib import (  # noqa: E402
	MemoryGitError,
	MemoryValidationError,
	load_run_ledger_entries,
	read_memory_root_from_branch,
	resolve_memory_root_dir,
	utc_now_iso,
	validate_state_snapshot_payload,
)
from blocker_check import evaluate_blocker_eligibility  # noqa: E402
from orchestrate_lib import determine_phase  # noqa: E402


STATE_SNAPSHOT_SCHEMA_VERSION = "state_snapshot.v1"
_ACTIVE_RUN_BRANCH_RE = re.compile(r"(?:^|/)(?:ai/(?:issue-)?|ai-(?:implement-)?)(?P<num>[0-9]+)(?:$|[^0-9])")
_ISSUE_COUNT_KEYS = (
	"merged",
	"closed",
	"skipped",
	"in_progress",
	"review_blocked",
	"ready_to_merge",
	"done",
	"implementation_failed",
	"not_created",
)
_STATUS_TO_COUNT_KEY = {
	"merged": "merged",
	"closed": "closed",
	"skipped": "skipped",
	"in_progress": "in_progress",
	"review-blocked": "review_blocked",
	"ready-to-merge": "ready_to_merge",
	"done": "done",
	"implementation-failed": "implementation_failed",
	"not_created": "not_created",
}


def _load_json_file(path: Path | None, default: Any) -> Any:
	if path is None or not path.exists():
		return default
	with path.open("r", encoding="utf-8") as handle:
		return json.load(handle)


def _empty_issue_counts() -> dict[str, int]:
	return {key: 0 for key in _ISSUE_COUNT_KEYS}


def _as_positive_int(value: Any) -> int | None:
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value if value > 0 else None
	if isinstance(value, str) and value.isdigit():
		parsed = int(value)
		return parsed if parsed > 0 else None
	return None


def _as_non_negative_int(value: Any) -> int | None:
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		return value if value >= 0 else None
	if isinstance(value, str) and value.isdigit():
		return int(value)
	return None


def _normalize_labels(raw: Any) -> list[str]:
	if not isinstance(raw, list):
		return []
	labels: list[str] = []
	for item in raw:
		if isinstance(item, str) and item:
			labels.append(item)
	return labels


def _normalize_tokens(raw: Any) -> dict[str, int] | None:
	if not isinstance(raw, dict):
		return None
	result: dict[str, int] = {}
	for key in ("input", "output", "total"):
		value = _as_non_negative_int(raw.get(key))
		if value is not None:
			result[key] = value
	if "total" not in result and "input" in result and "output" in result:
		result["total"] = result["input"] + result["output"]
	return result or None


def _normalize_issue_counts(wave_status: dict[str, Any]) -> dict[str, int]:
	counts = _empty_issue_counts()
	for issue in wave_status.get("issues") or []:
		if not isinstance(issue, dict):
			continue
		status = str(issue.get("status") or "").strip()
		mapped_key = _STATUS_TO_COUNT_KEY.get(status)
		if mapped_key is None:
			continue
		counts[mapped_key] += 1
	return counts


def _tracker_status(state: dict[str, Any], wave_status: dict[str, Any]) -> str:
	state_status = str(state.get("status") or "").strip().lower()
	if state_status == "validated":
		return "validated"
	if state_status == "failed" or bool(wave_status.get("any_failed")):
		return "failed"
	if bool(wave_status.get("project_complete")):
		return "ready"
	if bool(wave_status.get("wave_complete")):
		return "waiting"
	return "in-progress"


def _placeholder_tracking_summary(raw: dict[str, Any]) -> dict[str, Any]:
	number = _as_positive_int(raw.get("number"))
	if number is None:
		raise ValueError(f"tracking issue payload missing numeric number: {raw!r}")
	title = str(raw.get("title") or f"Tracking issue #{number}").strip()
	if not title:
		title = f"Tracking issue #{number}"
	return {
		"number": number,
		"title": title,
		"status": "in-progress",
		"current_wave": 0,
		"total_waves": 0,
		"wave_complete": False,
		"project_complete": False,
		"issue_counts": _empty_issue_counts(),
	}


def _build_tracking_summary(tracker: dict[str, Any]) -> dict[str, Any]:
	number = _as_positive_int(tracker.get("number"))
	if number is None:
		raise ValueError(f"tracker export missing numeric number: {tracker!r}")

	state = tracker.get("state") if isinstance(tracker.get("state"), dict) else {}
	wave_status = tracker.get("wave_status") if isinstance(tracker.get("wave_status"), dict) else {}
	title = str(tracker.get("title") or f"Tracking issue #{number}").strip()
	if not title:
		title = f"Tracking issue #{number}"

	current_wave = _as_non_negative_int(wave_status.get("wave"))
	if current_wave is None:
		current_wave = _as_non_negative_int(state.get("current_wave"))
	if current_wave is None:
		current_wave = 0

	total_waves = _as_non_negative_int(state.get("total_waves"))
	if total_waves is None:
		total_waves = len(state.get("waves") or []) if isinstance(state.get("waves"), list) else 0

	summary = {
		"number": number,
		"title": title,
		"status": _tracker_status(state, wave_status),
		"current_wave": current_wave,
		"total_waves": total_waves,
		"wave_complete": bool(wave_status.get("wave_complete")),
		"project_complete": bool(wave_status.get("project_complete")),
		"issue_counts": _normalize_issue_counts(wave_status),
	}
	integration_ahead_by = str(wave_status.get("integration_ahead_by") or "").strip()
	if integration_ahead_by or "integration_ahead_by" in wave_status:
		summary["integration_ahead_by"] = integration_ahead_by
	return summary


def _extract_issue_number_from_head_branch(head_branch: Any) -> int | None:
	if not isinstance(head_branch, str) or not head_branch:
		return None
	match = _ACTIVE_RUN_BRANCH_RE.search(head_branch)
	if match is None:
		return None
	return _as_positive_int(match.group("num"))


def _load_tracker_exports(trackers_dir: Path | None) -> list[dict[str, Any]]:
	if trackers_dir is None or not trackers_dir.exists():
		return []

	trackers: list[dict[str, Any]] = []
	for path in sorted(trackers_dir.glob("*.json")):
		try:
			payload = _load_json_file(path, {})
		except (OSError, ValueError, json.JSONDecodeError):
			continue
		if isinstance(payload, dict):
			trackers.append(payload)

	trackers.sort(key=lambda item: (_as_positive_int(item.get("number")) or 0, str(item.get("title") or "")))
	return trackers


def _resolve_memory_roots(
	*,
	repo_root: Path,
	memory_root_dir: Path | None,
	memory_branch: str,
	memory_root_relative: str,
) -> tuple[Path, Path | None, Path | None]:
	schema_root = memory_root_dir.resolve() if memory_root_dir is not None else resolve_memory_root_dir(repo_root, memory_root_relative)
	if memory_root_dir is not None:
		return schema_root, schema_root, None

	branch_dir: Path | None = None
	try:
		branch_dir = read_memory_root_from_branch(
			repo_root,
			memory_branch=memory_branch,
			memory_root_relative=memory_root_relative,
		)
		return schema_root, resolve_memory_root_dir(branch_dir, memory_root_relative), branch_dir
	except MemoryGitError:
		return schema_root, None, None


def _latest_ledger_enrichment(
	*,
	memory_root: Path | None,
	run_id: int,
	ledger_enabled: bool,
	cache: dict[int, tuple[str | None, dict[str, int] | None]],
) -> tuple[str | None, dict[str, int] | None]:
	if not ledger_enabled or memory_root is None:
		return None, None
	if run_id in cache:
		return cache[run_id]

	latest_substate: str | None = None
	latest_tokens: dict[str, int] | None = None
	try:
		entries = load_run_ledger_entries(memory_root, str(run_id))
	except (MemoryValidationError, OSError, ValueError, json.JSONDecodeError):
		cache[run_id] = (None, None)
		return cache[run_id]

	for entry in entries:
		if not isinstance(entry, dict):
			continue
		metadata = entry.get("metadata")
		if not isinstance(metadata, dict):
			continue
		run_substate = metadata.get("run_substate")
		if isinstance(run_substate, str) and run_substate:
			latest_substate = run_substate
		normalized_tokens = _normalize_tokens(metadata.get("tokens"))
		if normalized_tokens is not None:
			latest_tokens = normalized_tokens

	cache[run_id] = (latest_substate, latest_tokens)
	return cache[run_id]


def _issue_contexts(trackers: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
	contexts: dict[int, dict[str, Any]] = {}
	for tracker in trackers:
		tracker_number = _as_positive_int(tracker.get("number"))
		if tracker_number is None:
			continue
		wave_status = tracker.get("wave_status") if isinstance(tracker.get("wave_status"), dict) else {}
		labels_json = tracker.get("labels_json") if isinstance(tracker.get("labels_json"), dict) else {}
		wave_number = _as_positive_int(wave_status.get("wave")) or _as_positive_int((tracker.get("state") or {}).get("current_wave")) or 1
		ledger_enabled = bool(tracker.get("ledger_substates_enabled", True))

		for issue in wave_status.get("issues") or []:
			if not isinstance(issue, dict):
				continue
			github_issue = _as_positive_int(issue.get("github_issue"))
			if github_issue is None:
				continue
			labels = _normalize_labels(labels_json.get(str(github_issue)))
			contexts[github_issue] = {
				"tracking_issue": tracker_number,
				"wave": wave_number,
				"local_id": str(issue.get("id") or f"issue-{github_issue}"),
				"issue_status": str(issue.get("status") or "in_progress"),
				"phase": determine_phase(labels),
				"ledger_substates_enabled": ledger_enabled,
			}
	return contexts


def _build_running_entries(
	*,
	trackers: list[dict[str, Any]],
	actions_runs_payload: dict[str, Any],
	memory_root: Path | None,
) -> list[dict[str, Any]]:
	contexts = _issue_contexts(trackers)
	ledger_cache: dict[int, tuple[str | None, dict[str, int] | None]] = {}
	running: list[dict[str, Any]] = []

	workflow_runs = actions_runs_payload.get("workflow_runs")
	if not isinstance(workflow_runs, list):
		workflow_runs = []

	for run in workflow_runs:
		if not isinstance(run, dict):
			continue
		if str(run.get("status") or "") != "in_progress":
			continue
		github_issue = _extract_issue_number_from_head_branch(run.get("head_branch"))
		if github_issue is None:
			continue
		context = contexts.get(github_issue)
		if context is None:
			continue
		run_id = _as_positive_int(run.get("id"))
		if run_id is None:
			continue

		entry = {
			"tracking_issue": context["tracking_issue"],
			"wave": context["wave"],
			"local_id": context["local_id"],
			"github_issue": github_issue,
			"run_id": run_id,
			"workflow": str(run.get("name") or "unknown"),
			"run_status": "in_progress",
			"issue_status": context["issue_status"],
			"phase": str(context["phase"] or "no_labels"),
		}

		head_branch = str(run.get("head_branch") or "").strip()
		if head_branch:
			entry["head_branch"] = head_branch
		html_url = str(run.get("html_url") or "").strip()
		if html_url:
			entry["html_url"] = html_url
		started_at = str(run.get("run_started_at") or run.get("created_at") or "").strip()
		if started_at:
			entry["started_at"] = started_at

		latest_substate, latest_tokens = _latest_ledger_enrichment(
			memory_root=memory_root,
			run_id=run_id,
			ledger_enabled=bool(context["ledger_substates_enabled"]),
			cache=ledger_cache,
		)
		if latest_substate:
			entry["substate"] = latest_substate
		if latest_tokens is not None:
			entry["tokens"] = latest_tokens

		running.append(entry)

	running.sort(key=lambda item: (item["tracking_issue"], item["github_issue"], item["run_id"]))
	return running


def _build_deferred_entries(tracker: dict[str, Any]) -> list[dict[str, Any]]:
	state = tracker.get("state") if isinstance(tracker.get("state"), dict) else {}
	if not bool(tracker.get("runtime_blocker_check_enabled", False)):
		return []

	tracking_issue = _as_positive_int(tracker.get("number"))
	if tracking_issue is None:
		return []

	waves = state.get("waves")
	if not isinstance(waves, list) or not waves:
		return []
	current_wave = _as_positive_int(state.get("current_wave")) or 1
	wave_index = max(current_wave - 1, 0)
	if wave_index >= len(waves):
		return []
	wave = waves[wave_index]
	if not isinstance(wave, dict):
		return []

	candidate_details = tracker.get("candidate_details_json") if isinstance(tracker.get("candidate_details_json"), dict) else {}
	deferred: list[dict[str, Any]] = []
	for issue in wave.get("issues") or []:
		if not isinstance(issue, dict):
			continue
		if issue.get("github_issue") is not None:
			continue
		local_id = str(issue.get("id") or "").strip()
		if not local_id:
			continue
		result = evaluate_blocker_eligibility(state, local_id=local_id, candidate_details=candidate_details)
		if result.get("eligible") is not False:
			continue

		entry = {
			"tracking_issue": tracking_issue,
			"wave": current_wave,
			"local_id": local_id,
			"reason": str(result.get("reason") or "blocked_by_dependency"),
			"metadata_present": bool(result.get("metadata_present", False)),
			"blocker_count": 0,
			"blockers": [],
		}

		detail = result.get("detail")
		if isinstance(detail, str) and detail:
			entry["detail"] = detail

		blockers: list[dict[str, Any]] = []
		for blocker in result.get("blockers") or []:
			if not isinstance(blocker, dict):
				continue
			blocker_entry = {
				"local_id": str(blocker.get("local_id") or "unknown"),
				"github_issue": _as_positive_int(blocker.get("github_issue")),
				"terminal": bool(blocker.get("terminal", False)),
				"status": str(blocker.get("status") or "unknown"),
				"source": str(blocker.get("source") or "unknown"),
			}
			if blocker.get("github_issue") is None:
				blocker_entry["github_issue"] = None
			blockers.append(blocker_entry)

		entry["blockers"] = blockers
		entry["blocker_count"] = len(blockers)
		deferred.append(entry)

	return deferred


def build_state_snapshot(
	*,
	repo_root: Path,
	tracker_exports: list[dict[str, Any]],
	tracking_issues_payload: list[dict[str, Any]],
	actions_runs_payload: dict[str, Any],
	tick_at: str,
	schema_memory_root: Path,
	ledger_memory_root: Path | None,
) -> dict[str, Any]:
	tracking_summaries: list[dict[str, Any]] = []
	seen_tracking_numbers: set[int] = set()

	for tracker in tracker_exports:
		summary = _build_tracking_summary(tracker)
		tracking_summaries.append(summary)
		seen_tracking_numbers.add(summary["number"])

	for raw in tracking_issues_payload:
		if not isinstance(raw, dict):
			continue
		number = _as_positive_int(raw.get("number"))
		if number is None or number in seen_tracking_numbers:
			continue
		tracking_summaries.append(_placeholder_tracking_summary(raw))
		seen_tracking_numbers.add(number)

	tracking_summaries.sort(key=lambda item: item["number"])

	running = _build_running_entries(
		trackers=tracker_exports,
		actions_runs_payload=actions_runs_payload,
		memory_root=ledger_memory_root,
	)

	deferred: list[dict[str, Any]] = []
	for tracker in tracker_exports:
		deferred.extend(_build_deferred_entries(tracker))
	deferred.sort(key=lambda item: (item["tracking_issue"], item["wave"], item["local_id"]))

	tokens = {"input": 0, "output": 0, "total": 0}
	for entry in running:
		normalized_tokens = _normalize_tokens(entry.get("tokens"))
		if normalized_tokens is None:
			continue
		for key in ("input", "output", "total"):
			tokens[key] += normalized_tokens.get(key, 0)

	snapshot = {
		"schema_version": STATE_SNAPSHOT_SCHEMA_VERSION,
		"tick_at": tick_at,
		"tracking_issues": tracking_summaries,
		"running": running,
		"deferred": deferred,
		"totals": {
			"tracking_issues": len(tracking_summaries),
			"running": len(running),
			"deferred": len(deferred),
			"tokens": tokens,
		},
	}
	validate_state_snapshot_payload(snapshot, schema_memory_root)
	return snapshot


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Build the orchestrator per-tick state snapshot")
	parser.add_argument("--repo-root", default=".")
	parser.add_argument("--tracking-issues-file", default="")
	parser.add_argument("--trackers-dir", default="")
	parser.add_argument("--actions-runs-file", default="")
	parser.add_argument("--output-file", default="")
	parser.add_argument("--tick-at", default="")
	parser.add_argument("--memory-branch", default="ai-memory")
	parser.add_argument("--memory-root-relative", default="ai-memory")
	parser.add_argument("--memory-root-dir", default="")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	repo_root = Path(args.repo_root).resolve()
	tracking_issues_file = Path(args.tracking_issues_file).resolve() if args.tracking_issues_file else None
	trackers_dir = Path(args.trackers_dir).resolve() if args.trackers_dir else None
	actions_runs_file = Path(args.actions_runs_file).resolve() if args.actions_runs_file else None
	memory_root_dir = Path(args.memory_root_dir).resolve() if args.memory_root_dir else None

	tracking_issues_payload = _load_json_file(tracking_issues_file, [])
	if not isinstance(tracking_issues_payload, list):
		tracking_issues_payload = []
	actions_runs_payload = _load_json_file(actions_runs_file, {"workflow_runs": []})
	if not isinstance(actions_runs_payload, dict):
		actions_runs_payload = {"workflow_runs": []}
	tracker_exports = _load_tracker_exports(trackers_dir)
	tick_at = args.tick_at or utc_now_iso()

	schema_memory_root: Path
	ledger_memory_root: Path | None
	cleanup_dir: Path | None
	schema_memory_root, ledger_memory_root, cleanup_dir = _resolve_memory_roots(
		repo_root=repo_root,
		memory_root_dir=memory_root_dir,
		memory_branch=args.memory_branch,
		memory_root_relative=args.memory_root_relative,
	)
	try:
		snapshot = build_state_snapshot(
			repo_root=repo_root,
			tracker_exports=tracker_exports,
			tracking_issues_payload=tracking_issues_payload,
			actions_runs_payload=actions_runs_payload,
			tick_at=tick_at,
			schema_memory_root=schema_memory_root,
			ledger_memory_root=ledger_memory_root,
		)
		serialized = json.dumps(snapshot, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
		if args.output_file:
			output_path = Path(args.output_file).resolve()
			output_path.parent.mkdir(parents=True, exist_ok=True)
			output_path.write_text(serialized, encoding="utf-8")
		else:
			print(serialized, end="")
		return 0
	finally:
		if cleanup_dir is not None:
			shutil.rmtree(cleanup_dir, ignore_errors=True)


if __name__ == "__main__":
	raise SystemExit(main())
