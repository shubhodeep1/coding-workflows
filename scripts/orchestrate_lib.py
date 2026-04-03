#!/usr/bin/env python3
"""Orchestrator library: DAG management, wave computation, issue tracking, and judge helpers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class OrchestrateError(ValueError):
	"""Raised when orchestrator data is invalid."""


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_decomposition(data: dict[str, Any]) -> dict[str, Any]:
	"""Validate decomposer output against orchestrate_decomposition.v1 rules."""
	if not isinstance(data, dict):
		raise OrchestrateError("Decomposition must be a JSON object")

	sv = data.get("schema_version")
	if sv != "orchestrate_decomposition.v1":
		raise OrchestrateError(f"Unsupported schema_version: {sv!r}")

	title = data.get("project_title", "")
	if not isinstance(title, str) or not title.strip():
		raise OrchestrateError("project_title is required")

	summary = data.get("project_summary", "")
	if not isinstance(summary, str) or not summary.strip():
		raise OrchestrateError("project_summary is required")

	issues = data.get("issues")
	if not isinstance(issues, list) or len(issues) < 1:
		raise OrchestrateError("issues must be a non-empty array")

	issue_ids: set[str] = set()
	for idx, issue in enumerate(issues):
		if not isinstance(issue, dict):
			raise OrchestrateError(f"issues[{idx}] must be an object")
		iid = issue.get("id", "")
		if not isinstance(iid, str) or not iid.strip():
			raise OrchestrateError(f"issues[{idx}].id is required")
		if iid in issue_ids:
			raise OrchestrateError(f"Duplicate issue id: {iid!r}")
		issue_ids.add(iid)

		for field in ("title", "body"):
			val = issue.get(field, "")
			if not isinstance(val, str) or not val.strip():
				raise OrchestrateError(f"issues[{idx}].{field} is required")

		priority = issue.get("priority")
		if not isinstance(priority, int) or priority < 1 or priority > 10:
			raise OrchestrateError(f"issues[{idx}].priority must be 1-10, got {priority!r}")

	edges = data.get("dependency_edges", [])
	if not isinstance(edges, list):
		raise OrchestrateError("dependency_edges must be an array")

	for idx, edge in enumerate(edges):
		if not isinstance(edge, dict):
			raise OrchestrateError(f"dependency_edges[{idx}] must be an object")
		for field in ("from", "to"):
			val = edge.get(field, "")
			if not isinstance(val, str) or val not in issue_ids:
				raise OrchestrateError(
					f"dependency_edges[{idx}].{field} references unknown issue id: {val!r}"
				)
		if edge["from"] == edge["to"]:
			raise OrchestrateError(f"dependency_edges[{idx}]: self-dependency on {edge['from']!r}")

	# Cycle detection
	_detect_cycles(issue_ids, edges)

	return data


def _detect_cycles(issue_ids: set[str], edges: list[dict[str, str]]) -> None:
	"""Detect cycles in the dependency graph using DFS."""
	adjacency: dict[str, list[str]] = {iid: [] for iid in issue_ids}
	for edge in edges:
		adjacency[edge["from"]].append(edge["to"])

	WHITE, GRAY, BLACK = 0, 1, 2
	color: dict[str, int] = {iid: WHITE for iid in issue_ids}

	def dfs(node: str) -> None:
		color[node] = GRAY
		for neighbor in adjacency[node]:
			if color[neighbor] == GRAY:
				raise OrchestrateError(f"Dependency cycle detected involving {node!r} -> {neighbor!r}")
			if color[neighbor] == WHITE:
				dfs(neighbor)
		color[node] = BLACK

	for iid in issue_ids:
		if color[iid] == WHITE:
			dfs(iid)


# ---------------------------------------------------------------------------
# Wave computation
# ---------------------------------------------------------------------------

def compute_waves(data: dict[str, Any]) -> list[list[dict[str, Any]]]:
	"""Compute execution waves from the dependency DAG.

	Returns a list of waves. Each wave is a list of issue objects that can
	run in parallel. Within each wave, issues are sorted by priority (ascending).
	"""
	issues_by_id: dict[str, dict[str, Any]] = {i["id"]: i for i in data["issues"]}
	edges = data.get("dependency_edges", [])

	# Build in-degree map
	in_degree: dict[str, int] = {iid: 0 for iid in issues_by_id}
	dependents: dict[str, list[str]] = {iid: [] for iid in issues_by_id}
	for edge in edges:
		in_degree[edge["to"]] += 1
		dependents[edge["from"]].append(edge["to"])

	waves: list[list[dict[str, Any]]] = []
	remaining = set(issues_by_id.keys())

	while remaining:
		# Current wave: all issues with in_degree == 0
		wave_ids = sorted(
			[iid for iid in remaining if in_degree[iid] == 0],
			key=lambda iid: issues_by_id[iid]["priority"],
		)
		if not wave_ids:
			# Should not happen after cycle detection, but guard anyway
			raise OrchestrateError(f"Cannot compute next wave; remaining issues have unmet deps: {remaining}")

		waves.append([issues_by_id[iid] for iid in wave_ids])

		for iid in wave_ids:
			remaining.discard(iid)
			for dep in dependents[iid]:
				in_degree[dep] -= 1

	return waves


# ---------------------------------------------------------------------------
# State tracking (stored as JSON comment on the tracking issue)
# ---------------------------------------------------------------------------

def build_tracking_state(
	data: dict[str, Any],
	waves: list[list[dict[str, Any]]],
	issue_number_map: dict[str, int],
) -> dict[str, Any]:
	"""Build the orchestrator tracking state object.

	Args:
		data: Validated decomposition.
		waves: Computed wave list.
		issue_number_map: Map of local issue id -> GitHub issue number.
			Only Wave 1 issues need to be present; later waves are
			created on demand by the poller (deferred creation).

	Returns:
		Tracking state dict suitable for JSON serialisation.
	"""
	wave_list = []
	for wave_idx, wave in enumerate(waves):
		wave_issues = []
		for issue in wave:
			gh_num = issue_number_map.get(issue["id"])
			wave_issues.append({
				"id": issue["id"],
				"github_issue": gh_num,
				"status": "pending" if gh_num is not None else "not_created",
			})
		wave_list.append({
			"wave": wave_idx + 1,
			"issues": wave_issues,
		})

	# Store full issue definitions for deferred creation by the poller.
	# Only issues NOT in issue_number_map need to be stored.
	pending_issue_defs: dict[str, dict[str, Any]] = {}
	for issue in data["issues"]:
		if issue["id"] not in issue_number_map:
			pending_issue_defs[issue["id"]] = {
				"title": issue["title"],
				"body": issue["body"],
				"priority": issue["priority"],
			}

	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": data["project_title"],
		"total_issues": len(data["issues"]),
		"total_waves": len(waves),
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": wave_list,
		"dependency_edges": data.get("dependency_edges", []),
		"issue_number_map": issue_number_map,
		"pending_issue_defs": pending_issue_defs,
	}


def build_tracking_issue_body(data: dict[str, Any], waves: list[list[dict[str, Any]]]) -> str:
	"""Build the markdown body for the project tracking issue."""
	lines: list[str] = []
	lines.append(f"## Project: {data['project_title']}")
	lines.append("")
	lines.append(data["project_summary"])
	lines.append("")
	lines.append("---")
	lines.append("")
	lines.append(f"**Total issues:** {len(data['issues'])} | **Waves:** {len(waves)}")
	lines.append("")

	for wave_idx, wave in enumerate(waves):
		lines.append(f"### Wave {wave_idx + 1}")
		lines.append("")
		for issue in wave:
			lines.append(f"- [ ] **{issue['id']}**: {issue['title']} (priority {issue['priority']})")
		lines.append("")

	if data.get("dependency_edges"):
		lines.append("### Dependencies")
		lines.append("")
		for edge in data["dependency_edges"]:
			lines.append(f"- `{edge['from']}` -> `{edge['to']}`")
		lines.append("")

	lines.append("---")
	lines.append("*This issue is managed by the AI orchestrator. Do not edit manually.*")
	lines.append("`ai:orchestrator-tracking`")
	return "\n".join(lines)


def format_wave_status_comment(state: dict[str, Any], wave_idx: int) -> str:
	"""Format a status comment for a completed wave."""
	wave = state["waves"][wave_idx]
	lines: list[str] = []
	lines.append(f"## Wave {wave['wave']} Status")
	lines.append("")
	for issue in wave["issues"]:
		gh_num = issue.get("github_issue", "?")
		status = issue.get("status", "unknown")
		marker = "x" if status == "merged" else " "
		lines.append(f"- [{marker}] #{gh_num} — `{issue['id']}` — {status}")
	lines.append("")
	return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _print_json(payload: dict[str, Any] | list[Any]) -> None:
	print(json.dumps(payload, ensure_ascii=True, sort_keys=False, indent=2))


def cmd_validate(args: argparse.Namespace) -> int:
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	validate_decomposition(data)
	_print_json({"ok": True, "issues": len(data["issues"]), "edges": len(data.get("dependency_edges", []))})
	return 0


def cmd_compute_waves(args: argparse.Namespace) -> int:
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	validate_decomposition(data)
	waves = compute_waves(data)
	output = []
	for wave_idx, wave in enumerate(waves):
		output.append({
			"wave": wave_idx + 1,
			"issues": [{"id": i["id"], "title": i["title"], "priority": i["priority"]} for i in wave],
		})
	_print_json({"ok": True, "total_waves": len(waves), "waves": output})
	return 0


def cmd_build_tracking_body(args: argparse.Namespace) -> int:
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	validate_decomposition(data)
	waves = compute_waves(data)
	body = build_tracking_issue_body(data, waves)
	print(body)
	return 0


def cmd_next_wave(args: argparse.Namespace) -> int:
	"""Given the current tracking state, output the next wave's issue numbers to dispatch."""
	path = Path(args.state_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		state = json.load(f)

	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])

	if current_wave_idx >= len(waves):
		_print_json({"ok": True, "action": "complete", "issues": []})
		return 0

	wave = waves[current_wave_idx]
	pending_issues = [
		i for i in wave["issues"]
		if i.get("status") in ("pending", None)
	]

	_print_json({
		"ok": True,
		"action": "dispatch",
		"wave": current_wave_idx + 1,
		"issues": [
			{"id": i["id"], "github_issue": i.get("github_issue")}
			for i in pending_issues
		],
	})
	return 0


def cmd_check_wave_status(args: argparse.Namespace) -> int:
	"""Check if all issues in the current wave are merged/closed."""
	path = Path(args.state_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		state = json.load(f)

	labels_json = args.labels_json
	if not labels_json:
		_print_json({"ok": False, "error": "labels_json is required"})
		return 1

	issue_labels: dict[str, list[str]] = json.loads(labels_json)
	if not isinstance(issue_labels, dict) or any(not isinstance(v, list) for v in issue_labels.values()):
		_print_json({"ok": False, "error": "labels_json must be an object mapping issue numbers to label arrays"})
		return 1

	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])

	if current_wave_idx >= len(waves):
		_print_json({"ok": True, "wave_complete": True, "project_complete": True})
		return 0

	wave = waves[current_wave_idx]
	all_merged = True
	any_failed = False
	any_review_blocked = False
	statuses: list[dict[str, Any]] = []

	for issue in wave["issues"]:
		gh_num = str(issue.get("github_issue", ""))
		labels = issue_labels.get(gh_num, [])

		if "ai:merged" in labels:
			status = "merged"
		elif "ai:closed" in labels:
			status = "closed"
			any_failed = True
		elif "ai:implementation-failed" in labels:
			status = "implementation-failed"
			any_failed = True
			all_merged = False
		elif "ai:review-blocked" in labels:
			status = "review-blocked"
			any_review_blocked = True
			all_merged = False
		elif "ai:ready-to-merge" in labels:
			status = "ready-to-merge"
			all_merged = False
		elif "ai:done" in labels:
			status = "done"
			all_merged = False
		else:
			status = "in_progress"
			all_merged = False

		statuses.append({"id": issue["id"], "github_issue": gh_num, "status": status})

	project_complete = all_merged and (current_wave_idx + 1 >= len(waves))

	_print_json({
		"ok": True,
		"wave": current_wave_idx + 1,
		"wave_complete": all_merged,
		"any_failed": any_failed,
		"any_review_blocked": any_review_blocked,
		"project_complete": project_complete,
		"issues": statuses,
	})
	return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Orchestrator helper utilities")
	subparsers = parser.add_subparsers(dest="command", required=True)

	p_validate = subparsers.add_parser("validate", help="Validate decomposition JSON")
	p_validate.add_argument("--input-file", required=True)
	p_validate.set_defaults(func=cmd_validate)

	p_waves = subparsers.add_parser("compute-waves", help="Compute execution waves from decomposition")
	p_waves.add_argument("--input-file", required=True)
	p_waves.set_defaults(func=cmd_compute_waves)

	p_body = subparsers.add_parser("build-tracking-body", help="Build tracking issue markdown body")
	p_body.add_argument("--input-file", required=True)
	p_body.set_defaults(func=cmd_build_tracking_body)

	p_next = subparsers.add_parser("next-wave", help="Get next wave to dispatch")
	p_next.add_argument("--state-file", required=True)
	p_next.set_defaults(func=cmd_next_wave)

	p_check = subparsers.add_parser("check-wave-status", help="Check current wave completion")
	p_check.add_argument("--state-file", required=True)
	p_check.add_argument("--labels-json", required=True, help='JSON: {"issue_num": ["label1", ...]}')
	p_check.set_defaults(func=cmd_check_wave_status)

	return parser


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)

	try:
		return int(args.func(args))
	except (OrchestrateError, ValueError, json.JSONDecodeError) as exc:
		print(f"ORCHESTRATE_ERROR: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
