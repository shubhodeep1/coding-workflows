#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
	sys.path.insert(0, str(SCRIPT_DIR))

from orchestrate_lib import is_terminal_wave_issue  # noqa: E402


def _load_state(path: Path) -> dict[str, Any]:
	with path.open("r", encoding="utf-8") as handle:
		payload = json.load(handle)
	if not isinstance(payload, dict):
		raise ValueError(f"state file {path} did not contain a JSON object")
	return payload


def _parse_optional_json_object(raw: str | None, *, field_name: str) -> dict[str, Any]:
	if not raw:
		return {}
	parsed = json.loads(raw)
	if not isinstance(parsed, dict):
		raise ValueError(f"{field_name} must decode to a JSON object")
	return parsed


def _deferred_result(
	*,
	local_id: str,
	reason: str,
	metadata_present: bool,
	blockers: list[dict[str, Any]] | None = None,
	detail: str | None = None,
) -> dict[str, Any]:
	result = {
		"local_id": local_id,
		"eligible": False,
		"signal": "dispatch_deferred_blocker",
		"reason": reason,
		"metadata_present": metadata_present,
		"blocker_count": len(blockers or []),
		"blockers": blockers or [],
	}
	if detail:
		result["detail"] = detail
	return result


def _wave_entries_by_local_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
	entries: dict[str, dict[str, Any]] = {}
	for wave in state.get("waves", []) or []:
		if not isinstance(wave, dict):
			continue
		for issue in wave.get("issues", []) or []:
			if not isinstance(issue, dict):
				continue
			local_id = issue.get("id")
			if isinstance(local_id, str) and local_id:
				entries[local_id] = issue
	return entries


def _incoming_blocker_local_ids(state: dict[str, Any], local_id: str) -> tuple[str, list[str]]:
	if "dependency_edges" not in state:
		return "missing", []
	edges = state.get("dependency_edges")
	if not isinstance(edges, list):
		return "invalid", []
	if not edges:
		return "present", []

	blockers: list[str] = []
	seen: set[str] = set()
	for edge in edges:
		if not isinstance(edge, dict):
			continue
		from_id = edge.get("from")
		to_id = edge.get("to")
		if to_id != local_id or not isinstance(from_id, str) or not from_id:
			continue
		if from_id in seen:
			continue
		seen.add(from_id)
		blockers.append(from_id)
	return "present", blockers


def _issue_number_map_entry(state: dict[str, Any], local_id: str) -> int | None:
	raw_issue_number_map = state.get("issue_number_map")
	if not isinstance(raw_issue_number_map, dict):
		return None

	raw_number = raw_issue_number_map.get(local_id)
	if isinstance(raw_number, bool):
		return None
	if isinstance(raw_number, int):
		return raw_number if raw_number > 0 else None
	if isinstance(raw_number, str) and raw_number.isdigit():
		number = int(raw_number)
		return number if number > 0 else None
	return None


def _candidate_entry(candidate_details: dict[str, Any], github_issue: Any) -> dict[str, Any]:
	if github_issue is None:
		return {}
	key = str(github_issue)
	entry = candidate_details.get(key, {})
	if isinstance(entry, dict):
		return entry
	return {}


def _candidate_labels(candidate: dict[str, Any]) -> list[str]:
	raw = candidate.get("labels", [])
	if not isinstance(raw, list):
		return []
	labels: list[str] = []
	for value in raw:
		if isinstance(value, str) and value:
			labels.append(value)
	return labels


def _candidate_issue_state(candidate: dict[str, Any]) -> str | None:
	raw_state = candidate.get("state")
	if not isinstance(raw_state, str):
		return None
	state = raw_state.strip().lower()
	if state in {"open", "closed"}:
		return state
	return None


def _candidate_pr_truth(candidate: dict[str, Any]) -> tuple[str | None, bool | None]:
	raw_pr = candidate.get("linked_pr")
	if not isinstance(raw_pr, dict):
		return None, None

	raw_state = raw_pr.get("state")
	pr_state: str | None = None
	if isinstance(raw_state, str):
		normalized = raw_state.strip().lower()
		if normalized in {"open", "closed", "merged"}:
			pr_state = "closed" if normalized == "merged" else normalized

	raw_merged = raw_pr.get("merged")
	pr_merged = raw_merged if isinstance(raw_merged, bool) else None
	return pr_state, pr_merged


def evaluate_blocker_eligibility(
	state: dict[str, Any],
	*,
	local_id: str,
	candidate_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
	candidate_details = candidate_details or {}
	entries_by_local_id = _wave_entries_by_local_id(state)
	metadata_status, blocker_local_ids = _incoming_blocker_local_ids(state, local_id)

	if metadata_status == "missing":
		return {
			"local_id": local_id,
			"eligible": True,
			"signal": "dispatch_eligible",
			"reason": "no_dependency_metadata",
			"metadata_present": False,
			"blocker_count": 0,
			"blockers": [],
		}

	if metadata_status == "invalid":
		return _deferred_result(
			local_id=local_id,
			reason="invalid_dependency_metadata",
			metadata_present=True,
		)

	if not blocker_local_ids:
		return {
			"local_id": local_id,
			"eligible": True,
			"signal": "dispatch_eligible",
			"reason": "no_incoming_blockers",
			"metadata_present": True,
			"blocker_count": 0,
			"blockers": [],
		}

	blockers: list[dict[str, Any]] = []
	missing_blocker_entry = False
	blocked_by_nonterminal = False

	for blocker_local_id in blocker_local_ids:
		entry = entries_by_local_id.get(blocker_local_id)
		if entry is None:
			mapped_github_issue = _issue_number_map_entry(state, blocker_local_id)
			if mapped_github_issue is None:
				missing_blocker_entry = True
				blockers.append(
					{
						"local_id": blocker_local_id,
						"github_issue": None,
						"terminal": False,
						"status": "missing",
						"source": "missing_wave_entry",
					}
				)
				continue
			entry = {"id": blocker_local_id, "github_issue": mapped_github_issue}

		github_issue = entry.get("github_issue")
		candidate = _candidate_entry(candidate_details, github_issue)
		labels = _candidate_labels(candidate)
		issue_state = _candidate_issue_state(candidate)
		pr_state, pr_merged = _candidate_pr_truth(candidate)
		terminal, status, source = is_terminal_wave_issue(
			entry,
			labels,
			issue_state=issue_state,
			pr_state=pr_state,
			pr_merged=pr_merged,
		)
		if not terminal:
			blocked_by_nonterminal = True

		blockers.append(
			{
				"local_id": blocker_local_id,
				"github_issue": github_issue if isinstance(github_issue, int) else None,
				"terminal": terminal,
				"status": status,
				"source": source,
			}
		)

	if missing_blocker_entry:
		reason = "unresolved_blocker_mapping"
		eligible = False
	elif blocked_by_nonterminal:
		reason = "blocked_by_dependency"
		eligible = False
	else:
		reason = "all_blockers_terminal"
		eligible = True

	if not eligible:
		return _deferred_result(
			local_id=local_id,
			reason=reason,
			metadata_present=True,
			blockers=blockers,
		)

	return {
		"local_id": local_id,
		"eligible": True,
		"signal": "dispatch_eligible",
		"reason": reason,
		"metadata_present": True,
		"blocker_count": len(blockers),
		"blockers": blockers,
	}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Evaluate orchestrator runtime blocker eligibility")
	parser.add_argument("--state-file", required=True)
	parser.add_argument("--local-id", required=True)
	parser.add_argument("--candidate-details-json", default="")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	try:
		state = _load_state(Path(args.state_file))
	except (OSError, ValueError, json.JSONDecodeError) as exc:
		print(
			json.dumps(
				_deferred_result(
					local_id=args.local_id,
					reason="state_load_failed",
					metadata_present=False,
					detail=f"{type(exc).__name__}: {exc}",
				),
				separators=(",", ":"),
				sort_keys=True,
			)
		)
		return 0
	try:
		candidate_details = _parse_optional_json_object(
			args.candidate_details_json,
			field_name="--candidate-details-json",
		)
	except (OSError, ValueError, json.JSONDecodeError) as exc:
		print(
			json.dumps(
				_deferred_result(
					local_id=args.local_id,
					reason="candidate_details_invalid",
					metadata_present=False,
					detail=f"{type(exc).__name__}: {exc}",
				),
				separators=(",", ":"),
				sort_keys=True,
			)
		)
		return 0
	result = evaluate_blocker_eligibility(
		state,
		local_id=args.local_id,
		candidate_details=candidate_details,
	)
	print(json.dumps(result, separators=(",", ":"), sort_keys=True))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
