#!/usr/bin/env python3
"""Orchestrator library: DAG management, wave computation, issue tracking, and judge helpers."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
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
	if sv not in (None, "orchestrate_decomposition.v1"):
		raise OrchestrateError(f"Unsupported schema_version: {sv!r}")

	data = dict(data)
	data["schema_version"] = "orchestrate_decomposition.v1"

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

		# files_touched is optional. When present, it must be an array of
		# non-empty repository-relative path strings with at most 50 entries.
		# Duplicates within a single issue are dropped silently (the partition
		# guard treats them as a set).
		ft_raw = issue.get("files_touched", None)
		if ft_raw is None:
			# Normalize missing key to empty list so downstream code can treat
			# it uniformly without re-checking for absence.
			issue["files_touched"] = []
		else:
			if not isinstance(ft_raw, list):
				raise OrchestrateError(f"issues[{idx}].files_touched must be an array if present")
			if len(ft_raw) > 50:
				raise OrchestrateError(
					f"issues[{idx}].files_touched has {len(ft_raw)} entries (max 50)"
				)
			cleaned: list[str] = []
			seen: set[str] = set()
			for fidx, raw_path in enumerate(ft_raw):
				if not isinstance(raw_path, str):
					raise OrchestrateError(
						f"issues[{idx}].files_touched[{fidx}] must be a string"
					)
				p = raw_path.strip()
				if not p:
					raise OrchestrateError(
						f"issues[{idx}].files_touched[{fidx}] must not be empty"
					)
				if len(p) > 512:
					raise OrchestrateError(
						f"issues[{idx}].files_touched[{fidx}] exceeds 512 chars"
					)
				# Normalize to forward slashes and strip leading "./".
				p = p.replace("\\", "/")
				while p.startswith("./"):
					p = p[2:]
				if p in seen:
					continue
				seen.add(p)
				cleaned.append(p)
			issue["files_touched"] = cleaned

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
# Hot-file registry loader
# ---------------------------------------------------------------------------

DEFAULT_HOT_FILES_PATH = ".github/ai/hot_files.json"


def load_hot_files(path: str | Path | None = None) -> set[str]:
	"""Load the hot-file registry used by the partition guard.

	Consumer repositories opt into hot-file partitioning by committing a JSON
	file at ``.github/ai/hot_files.json`` with the shape:

		{"hot_files": ["path/one", "path/two", ...]}

	If the file is absent, malformed, or unreadable, the hot-file set is
	empty and the partition guard falls back to straight pairwise overlap
	detection without the multi-sibling-per-hot-file rule. This keeps new
	consumer repos working out of the box with no config.
	"""
	target = Path(path) if path else Path(DEFAULT_HOT_FILES_PATH)
	try:
		with target.open("r", encoding="utf-8") as f:
			data = json.load(f)
	except (FileNotFoundError, PermissionError, OSError):
		return set()
	except json.JSONDecodeError:
		return set()

	raw_list = data.get("hot_files") if isinstance(data, dict) else None
	if not isinstance(raw_list, list):
		return set()

	cleaned: set[str] = set()
	for item in raw_list:
		if not isinstance(item, str):
			continue
		p = item.strip().replace("\\", "/")
		while p.startswith("./"):
			p = p[2:]
		if p:
			cleaned.add(p)
	return cleaned


# ---------------------------------------------------------------------------
# Effective hot-file resolution (committed seed + learned telemetry)
# ---------------------------------------------------------------------------

DEFAULT_TELEMETRY_WINDOW_DAYS = 90
DEFAULT_TELEMETRY_MIN_EVENTS = 3
DEFAULT_TELEMETRY_MIN_PROJECTS = 2


def compute_effective_hot_files(
	committed_hot_files: set[str],
	telemetry_jsonl_path: str | Path | None = None,
	window_days: int = DEFAULT_TELEMETRY_WINDOW_DAYS,
	min_events: int = DEFAULT_TELEMETRY_MIN_EVENTS,
	min_distinct_projects: int = DEFAULT_TELEMETRY_MIN_PROJECTS,
	now_ts: int | None = None,
) -> tuple[set[str], dict[str, Any]]:
	"""Compose the *effective* hot-file set from two sources:

	1. **Committed seed** — optional per-consumer-repo JSON at
	   ``.github/ai/hot_files.json`` (already loaded via
	   :func:`load_hot_files`). This is a human override, NOT required.
	2. **Learned telemetry** — an append-only JSONL at
	   ``ai-memory/orchestrator/merge_conflicts.jsonl`` on the
	   ``ai-memory`` branch, written by the poller every time
	   :func:`probe_sibling_merge_conflicts` in
	   ``orchestrate_poll_process.sh`` detects a real byte-level
	   conflict. Records have the shape::

	       {"ts": <epoch>, "project": "<tracking_issue>", "pr_a": "<num>",
	        "pr_b": "<num>", "paths": ["path/a", "path/b", ...]}

	Promotion rule (Option D from the design thread): a path is
	"learned hot" when it appears in at least ``min_events`` distinct
	conflict events across at least ``min_distinct_projects`` distinct
	orchestrator projects within the last ``window_days``. The window
	naturally handles stale demotion — files with zero recent events
	drop out of the learned set on the next run without any persistent
	state needed.

	Returns ``(effective_set, audit)`` where ``audit`` is a
	JSON-serialisable dict so the orchestrate.yml partition-guard step
	can log which files were promoted and why.
	"""
	now_ts = now_ts if now_ts is not None else int(time.time())
	audit: dict[str, Any] = {
		"committed_seed_count": len(committed_hot_files),
		"telemetry_events_total": 0,
		"telemetry_events_in_window": 0,
		"window_days": window_days,
		"min_events": min_events,
		"min_distinct_projects": min_distinct_projects,
		"learned_count": 0,
		"learned_files": [],
		"telemetry_source": None,
	}
	effective = set(committed_hot_files)

	if not telemetry_jsonl_path:
		return effective, audit

	p = Path(telemetry_jsonl_path)
	audit["telemetry_source"] = str(p)
	try:
		if not p.exists() or p.stat().st_size == 0:
			return effective, audit
	except OSError:
		return effective, audit

	window_start = now_ts - (window_days * 86400)

	# path -> {"events": int, "projects": set[str]}
	tallies: dict[str, dict[str, Any]] = {}
	try:
		with p.open("r", encoding="utf-8") as f:
			for raw_line in f:
				line = raw_line.strip()
				if not line:
					continue
				try:
					rec = json.loads(line)
				except json.JSONDecodeError:
					continue
				if not isinstance(rec, dict):
					continue
				audit["telemetry_events_total"] += 1
				ts = rec.get("ts")
				try:
					ts_int = int(ts)
				except (TypeError, ValueError):
					continue
				if ts_int < window_start:
					continue
				audit["telemetry_events_in_window"] += 1
				project = str(rec.get("project", "") or "").strip()
				raw_paths = rec.get("paths")
				if not isinstance(raw_paths, list):
					continue
				for raw_path in raw_paths:
					if not isinstance(raw_path, str):
						continue
					path = raw_path.strip().replace("\\", "/")
					while path.startswith("./"):
						path = path[2:]
					if not path:
						continue
					entry = tallies.setdefault(path, {"events": 0, "projects": set()})
					entry["events"] += 1
					if project:
						entry["projects"].add(project)
	except OSError:
		return effective, audit

	learned_records: list[dict[str, Any]] = []
	for path, t in tallies.items():
		if t["events"] >= min_events and len(t["projects"]) >= min_distinct_projects:
			effective.add(path)
			learned_records.append({
				"path": path,
				"events": t["events"],
				"distinct_projects": len(t["projects"]),
			})

	# Sort learned records for stable audit logs
	learned_records.sort(key=lambda r: (-r["events"], r["path"]))
	audit["learned_count"] = len(learned_records)
	audit["learned_files"] = learned_records
	return effective, audit


# ---------------------------------------------------------------------------
# Partition guard: detect and auto-serialize sibling file-touch overlaps
# ---------------------------------------------------------------------------

def _parallel_groups_from_edges(
	issues_by_id: dict[str, dict[str, Any]],
	edges: list[dict[str, str]],
) -> list[list[str]]:
	"""Return the topological wave grouping (list of lists of issue IDs).

	This is the pure-ordering helper used by the partition guard. It mirrors
	the logic of :func:`compute_waves` but returns IDs only and skips issue
	object materialisation so we can run it repeatedly during auto-serialize
	without mutating state.
	"""
	in_degree: dict[str, int] = {iid: 0 for iid in issues_by_id}
	dependents: dict[str, list[str]] = {iid: [] for iid in issues_by_id}
	for edge in edges:
		if edge["to"] in in_degree:
			in_degree[edge["to"]] += 1
		if edge["from"] in dependents:
			dependents[edge["from"]].append(edge["to"])

	remaining = set(issues_by_id.keys())
	groups: list[list[str]] = []
	while remaining:
		ready = sorted(
			[iid for iid in remaining if in_degree[iid] == 0],
			key=lambda iid: issues_by_id[iid].get("priority", 10),
		)
		if not ready:
			raise OrchestrateError(
				f"Cannot compute wave grouping; remaining issues have unmet deps: {remaining}"
			)
		groups.append(ready)
		for iid in ready:
			remaining.discard(iid)
			for dep in dependents[iid]:
				if dep in in_degree:
					in_degree[dep] -= 1
	return groups


def validate_wave_file_partition(
	wave_ids: list[str],
	issues_by_id: dict[str, dict[str, Any]],
	hot_files: set[str] | None = None,
) -> list[dict[str, Any]]:
	"""Detect sibling file-touch overlaps within a single wave.

	Returns a list of overlap records, each:

		{
			"type": "pair" | "hot_file",
			"issue_a": <id>,
			"issue_b": <id>,
			"files": [<overlapping paths>],
		}

	- ``pair`` overlaps: any two siblings that share at least one
	  ``files_touched`` entry. The caller should serialize them.
	- ``hot_file`` overlaps: any two siblings that both touch the same hot
	  file (even if it's their ONLY overlap). Deliberately reported
	  separately from pair overlaps so the caller can treat them with a
	  different policy if desired; at present they are serialized the same
	  way.

	Issues whose ``files_touched`` list is empty are never flagged — there is
	nothing to compare. The byte-level pre-merge probe in the poller handles
	unknown-scope issues at merge time instead.
	"""
	hot = hot_files or set()
	overlaps: list[dict[str, Any]] = []

	# Build {issue_id: set(paths)} once.
	files_for: dict[str, set[str]] = {}
	for iid in wave_ids:
		raw = issues_by_id.get(iid, {}).get("files_touched", []) or []
		files_for[iid] = set(raw)

	seen_pairs: set[tuple[str, str]] = set()
	for i, iid_a in enumerate(wave_ids):
		fa = files_for[iid_a]
		if not fa:
			continue
		for iid_b in wave_ids[i + 1:]:
			fb = files_for[iid_b]
			if not fb:
				continue
			common = sorted(fa & fb)
			if not common:
				continue
			pair_key = (iid_a, iid_b)
			if pair_key in seen_pairs:
				continue
			seen_pairs.add(pair_key)

			hot_common = [p for p in common if p in hot]
			if hot_common:
				overlaps.append({
					"type": "hot_file",
					"issue_a": iid_a,
					"issue_b": iid_b,
					"files": hot_common,
				})
			# Also report the full pair overlap (may be the same files, but
			# a non-hot-file overlap is equally fatal). We emit only one
			# record per pair, choosing pair if there are non-hot overlaps
			# and hot_file otherwise.
			non_hot = [p for p in common if p not in hot]
			if non_hot:
				overlaps.append({
					"type": "pair",
					"issue_a": iid_a,
					"issue_b": iid_b,
					"files": non_hot,
				})
	return overlaps


def auto_serialize_file_overlaps(
	data: dict[str, Any],
	hot_files: set[str] | None = None,
	max_rounds: int = 50,
) -> list[dict[str, Any]]:
	"""Inject synthetic dependency_edges that resolve sibling file overlaps.

	Mutates ``data["dependency_edges"]`` in place, appending one edge per
	resolved overlap, and records a machine-readable audit trail in
	``data["partition_serializations"]`` so the orchestrate.yml step can log
	the rewrites to the tracking issue.

	The injected edge orders the lower-priority (higher numeric priority)
	sibling AFTER the higher-priority one. On priority tie, the issue with
	the lexicographically smaller ID wins first-wave placement.

	If adding an edge would introduce a cycle, the function raises
	OrchestrateError — the decomposition is fundamentally inconsistent and a
	human must re-plan.

	``max_rounds`` caps the auto-serialize loop to avoid pathological cases
	where every round keeps producing new overlaps. In practice one or two
	rounds suffice because each edge strictly reduces the set of parallel
	siblings that can still collide.

	Returns the audit trail (list of serialization records).
	"""
	hot = hot_files or set()
	issues_by_id: dict[str, dict[str, Any]] = {i["id"]: i for i in data["issues"]}
	edges: list[dict[str, str]] = list(data.get("dependency_edges", []) or [])
	serializations: list[dict[str, Any]] = []

	def _edge_exists(frm: str, to: str) -> bool:
		for e in edges:
			if e.get("from") == frm and e.get("to") == to:
				return True
		return False

	def _pick_winner(iid_a: str, iid_b: str) -> tuple[str, str]:
		"""Return (winner, loser) — winner runs first, loser depends on winner."""
		pa = issues_by_id[iid_a].get("priority", 10)
		pb = issues_by_id[iid_b].get("priority", 10)
		if pa < pb:
			return iid_a, iid_b
		if pb < pa:
			return iid_b, iid_a
		# Priority tie: stable by lexicographic id
		return (iid_a, iid_b) if iid_a < iid_b else (iid_b, iid_a)

	for round_idx in range(max_rounds):
		groups = _parallel_groups_from_edges(issues_by_id, edges)
		any_new = False
		for group_idx, wave_ids in enumerate(groups):
			overlaps = validate_wave_file_partition(wave_ids, issues_by_id, hot)
			if not overlaps:
				continue
			for record in overlaps:
				winner, loser = _pick_winner(record["issue_a"], record["issue_b"])
				if _edge_exists(winner, loser):
					continue  # already serialized by a previous round
				# Prevent cycles: test with the candidate edge applied.
				test_edges = edges + [{"from": winner, "to": loser}]
				try:
					_detect_cycles(set(issues_by_id.keys()), test_edges)
				except OrchestrateError as exc:
					raise OrchestrateError(
						"auto_serialize_file_overlaps: injecting dependency "
						f"{winner!r} -> {loser!r} to resolve overlap "
						f"{record['files']!r} would create a cycle: {exc}. "
						"The decomposition is inconsistent — re-plan required."
					)
				edges.append({"from": winner, "to": loser})
				serializations.append({
					"round": round_idx + 1,
					"wave_index": group_idx,
					"overlap_type": record["type"],
					"winner": winner,
					"loser": loser,
					"files": record["files"],
				})
				any_new = True
		if not any_new:
			break
	else:
		# max_rounds exhausted without stabilising
		raise OrchestrateError(
			f"auto_serialize_file_overlaps did not converge within {max_rounds} rounds; "
			"decomposition has structurally tangled file ownership — re-plan required."
		)

	data["dependency_edges"] = edges
	data["partition_serializations"] = serializations
	return serializations


# ---------------------------------------------------------------------------
# Wave computation
# ---------------------------------------------------------------------------

def compute_waves(
	data: dict[str, Any],
	hot_files: set[str] | None = None,
	auto_serialize: bool = True,
) -> list[list[dict[str, Any]]]:
	"""Compute execution waves from the dependency DAG.

	Returns a list of waves. Each wave is a list of issue objects that can
	run in parallel. Within each wave, issues are sorted by priority (ascending).

	When ``auto_serialize`` is true (default), any pair of siblings in the
	same wave that share ``files_touched`` entries is resolved by injecting a
	synthetic dependency edge, pushing the lower-priority sibling into a
	later wave. The audit trail is recorded on ``data["partition_serializations"]``.

	``hot_files`` is an optional set of repository-relative paths that are
	considered "hot" — they are flagged separately from regular pair overlaps
	so the orchestrate.yml log step can highlight them, but they are
	serialized identically. When ``None``, the function calls
	:func:`load_hot_files` with the default registry path.
	"""
	if auto_serialize:
		effective_hot = hot_files if hot_files is not None else load_hot_files()
		auto_serialize_file_overlaps(data, effective_hot)

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
	integration_branch: str = "",
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
	now_ts = int(time.time())

	wave_list = []
	for wave_idx, wave in enumerate(waves):
		wave_issues = []
		for issue in wave:
			gh_num = issue_number_map.get(issue["id"])
			entry: dict[str, Any] = {
				"id": issue["id"],
				"github_issue": gh_num,
				"status": "pending" if gh_num is not None else "not_created",
			}
			# Persist the file-partition manifest onto each wave entry so the
			# poller-side merge-tree probe can consult it without re-parsing
			# issue bodies. Empty list is persisted explicitly to distinguish
			# "known empty / unknown scope" from "missing".
			entry["files_touched"] = list(issue.get("files_touched", []) or [])
			# Seed stall-tracking fields for issues that already exist
			if gh_num is not None:
				entry["last_seen_phase"] = ""
				entry["status_since_ts"] = now_ts
				entry["stall_recovery_count"] = 0
			wave_issues.append(entry)
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
				"files_touched": list(issue.get("files_touched", []) or []),
			}

	# Snapshot the tracking issue body at project creation time. The
	# judge prompt reads this snapshot instead of re-fetching the live
	# tracking issue body on every poll tick so (a) the body is
	# guaranteed byte-stable for provider-side prompt-prefix caching,
	# (b) one GH API call per judge tick is eliminated. The tracking
	# body is immutable by contract ("Do not edit manually." — see
	# build_tracking_issue_body below); validate and clarify-respond
	# continue to read the live body for their own narrower purposes.
	project_body_snapshot = build_tracking_issue_body(
		data, waves, integration_branch=integration_branch
	)

	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": data["project_title"],
		"project_body_snapshot": project_body_snapshot,
		"total_issues": len(data["issues"]),
		"total_waves": len(waves),
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": wave_list,
		"dependency_edges": data.get("dependency_edges", []),
		"issue_number_map": issue_number_map,
		"pending_issue_defs": pending_issue_defs,
		"integration_branch": integration_branch,
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
		# Self-healing telemetry for main <-> integration branch divergence.
		# Populated at runtime by the poller; initialised here so that state
		# schema consumers can rely on their presence without migration logic.
		"integration_sync_status": "clean",
		"integration_sync_last_error": "",
		"integration_conflict_dispatch_count": 0,
		"integration_conflict_dispatch_ts": 0,
		"integration_conflict_unresolved_ticks": 0,
	}


def build_tracking_issue_body(
	data: dict[str, Any],
	waves: list[list[dict[str, Any]]],
	integration_branch: str = "",
) -> str:
	"""Build the markdown body for the project tracking issue."""
	lines: list[str] = []
	lines.append(f"## Project: {data['project_title']}")
	lines.append("")
	lines.append(data["project_summary"])
	lines.append("")
	lines.append("---")
	lines.append("")
	lines.append(f"**Total issues:** {len(data['issues'])} | **Waves:** {len(waves)}")
	if integration_branch:
		lines.append(f"**Integration branch:** `{integration_branch}`")
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
# Stall detection and self-healing
# ---------------------------------------------------------------------------

# Label priority order — first match wins when determining the current phase.
PHASE_LABELS_PRIORITY: list[str] = [
	"ai:merged",
	"ai:closed",
	"ai:needs-human",
	"ai:ready-to-merge",
	"ai:review-blocked",
	"ai:implementation-failed",
	"ai:validated",
	"ai:validation-failed",
	"ai:validation-fixing",
	"ai:validation-recovery",
	"ai:validating",
	"ai:done",
	"ai:implementing",
	"ai:awaiting-approval",
	"ai:planning",
	"ai:clarification",
]

TERMINAL_PHASES: set[str] = {"ai:merged", "ai:closed", "ai:validated", "ai:validation-failed"}
TERMINAL_WAVE_STATUSES: set[str] = {"merged", "closed", "skipped", "not_created"}

# Phases already handled by dedicated logic in the poller — stall detector
# should not double-act on these.
DEDICATED_HANDLER_PHASES: set[str] = {"ai:needs-human", "ai:review-blocked", "ai:implementation-failed", "ai:validating", "ai:validation-fixing"}

# Escalating recovery actions per detected phase.
# The poller indexes into this list using the per-issue stall_recovery_count.
# If recovery_count exceeds the list length, the last entry is repeated.
# After MAX_STALL_RECOVERIES_PER_ISSUE total attempts the poller skips the
# issue (adds ai:closed) so the wave can advance and the judge handles it.
# Per-phase stall thresholds (minutes).  Phases not listed here fall back to
# the global --threshold-minutes value passed on the CLI.
DEFAULT_PHASE_STALL_THRESHOLDS: dict[str, int] = {
	"no_labels": 60,
	"ai:clarification": 60,
	"ai:planning": 60,
	"ai:awaiting-approval": 60,
	"ai:implementing": 120,
	"ai:done": 120,
	"ai:ready-to-merge": 60,
}

RUN_STALL_JUDGE_ACTION = "run_stall_judge"

STALL_RECOVERY_ACTIONS: dict[str, list[str]] = {
	"no_labels": [
		"retrigger_pipeline",
		"retrigger_pipeline",
		"escalate_human",
	],
	"ai:clarification": [
		"auto_respond_clarify",
		"auto_respond_clarify",
		"escalate_human",
	],
	"ai:planning": [
		"retrigger_plan",
		"retrigger_plan",
		"escalate_human",
	],
	"ai:awaiting-approval": [
		"auto_approve",
		"auto_approve",
		"escalate_human",
	],
	"ai:implementing": [
		"retrigger_implement",
		"retrigger_implement",
		"escalate_human",
	],
	"ai:done": [
		"retrigger_review",
		"retrigger_review",
		"escalate_human",
	],
	"ai:ready-to-merge": [
		"attempt_merge",
		"attempt_merge",
		"escalate_human",
	],
}


def determine_phase(labels: list[str]) -> str:
	"""Determine the current pipeline phase from issue labels.

	Returns the highest-priority matching label or ``"no_labels"`` when no
	AI pipeline label is present.
	"""
	for phase in PHASE_LABELS_PRIORITY:
		if phase in labels:
			return phase
	return "no_labels"


def reconcile_wave_issue_status(
	issue: dict[str, Any],
	labels: list[str],
	issue_state: str | None = None,
	pr_state: str | None = None,
	pr_merged: bool | None = None,
) -> tuple[str, str]:
	"""Reconcile a wave issue status from labels + GitHub truth signals.

	Precedence:
	1) Stored terminal state never regresses.
	2) Linked PR merged signal.
	3) Explicit terminal labels.
	4) Issue closed/open state.
	5) Non-terminal phase labels.
	6) Default in_progress.

	Returns:
		(status, source) where:
		- status is the reconciled wave issue status string
		- source identifies which signal decided the status
	"""
	stored_status = str(issue.get("status", "")).strip()
	if stored_status in TERMINAL_WAVE_STATUSES:
		return stored_status, "stored_terminal"

	raw_gh_num = issue.get("github_issue")
	if raw_gh_num is None:
		return "not_created", "no_github_issue"

	if pr_merged is True:
		return "merged", "linked_pr_merged"
	if "ai:merged" in labels:
		return "merged", "label_ai_merged"
	if issue_state == "closed":
		return "closed", "issue_closed"
	if "ai:closed" in labels:
		return "closed", "label_ai_closed"
	if "ai:implementation-failed" in labels:
		return "implementation-failed", "label_ai_implementation_failed"
	if "ai:review-blocked" in labels:
		return "review-blocked", "label_ai_review_blocked"
	if "ai:ready-to-merge" in labels:
		return "ready-to-merge", "label_ai_ready_to_merge"
	if "ai:done" in labels:
		return "done", "label_ai_done"
	return "in_progress", "default_in_progress"


def detect_stalls(
	state: dict[str, Any],
	issue_labels: dict[str, list[str]],
	threshold_minutes: int,
	now_ts: int,
	max_recoveries: int = 5,
	phase_thresholds: dict[str, int] | None = None,
	stall_judge_trigger_count: int = 2,
	enable_stall_judge: bool = True,
) -> list[dict[str, Any]]:
	"""Detect stalled issues in the current wave.

	An issue is stalled when its phase has not changed for longer than
	the phase-specific threshold (or the fallback *threshold_minutes*)
	and the phase is non-terminal and not already handled by a dedicated
	poller handler (review-blocked, impl-failed).

	*phase_thresholds* maps phase labels to per-phase thresholds in
	minutes.  Phases not present in the dict fall back to
	*threshold_minutes*.

	When *enable_stall_judge* is true and *stall_judge_trigger_count* is
	reached (but still below *max_recoveries*), recovery action is
	overridden to RUN_STALL_JUDGE_ACTION for non-dedicated phases.

	Returns a list of dicts, each containing:
		id, github_issue, phase, recovery_action,
		stall_duration_minutes, stall_recovery_count
	"""
	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])
	if current_wave_idx >= len(waves):
		return []

	effective_thresholds = dict(DEFAULT_PHASE_STALL_THRESHOLDS)
	if phase_thresholds:
		effective_thresholds.update(phase_thresholds)

	wave = waves[current_wave_idx]
	stalled: list[dict[str, Any]] = []

	for issue in wave["issues"]:
		gh_num = issue.get("github_issue")
		if not gh_num:
			continue

		cur_status = issue.get("status", "pending")
		if cur_status in ("merged", "closed", "skipped", "not_created"):
			continue

		labels = issue_labels.get(str(gh_num), [])
		if "ai:needs-human" in labels:
			continue
		phase = determine_phase(labels)

		if phase in TERMINAL_PHASES:
			continue
		if phase in DEDICATED_HANDLER_PHASES:
			continue

		status_since = issue.get("status_since_ts", 0)
		if status_since <= 0:
			# First observation — will be initialised this cycle
			continue

		phase_threshold = effective_thresholds.get(phase, threshold_minutes)
		threshold_secs = phase_threshold * 60

		elapsed = now_ts - status_since
		if elapsed < threshold_secs:
			continue

		recovery_count = issue.get("stall_recovery_count", 0)

		# Determine recovery action
		if recovery_count >= max_recoveries:
			action = "skip"
		elif enable_stall_judge and stall_judge_trigger_count >= 1 and recovery_count >= stall_judge_trigger_count:
			action = RUN_STALL_JUDGE_ACTION
		else:
			actions = STALL_RECOVERY_ACTIONS.get(phase, ["retrigger_pipeline"])
			action_idx = min(recovery_count, len(actions) - 1)
			action = actions[action_idx]

		stalled.append({
			"id": issue["id"],
			"github_issue": gh_num,
			"phase": phase,
			"recovery_action": action,
			"stall_duration_minutes": int(elapsed / 60),
			"stall_recovery_count": recovery_count,
		})

	return stalled


def update_issue_timestamps(
	state: dict[str, Any],
	issue_labels: dict[str, list[str]],
	now_ts: int,
) -> dict[str, Any]:
	"""Update status-tracking timestamps for every issue in the current wave.

	When an issue's detected phase differs from its ``last_seen_phase`` the
	``status_since_ts`` is reset to *now_ts* and the per-issue
	``stall_recovery_count`` is zeroed (phase advanced, so the issue is no
	longer stalled).

	Mutates *state* in-place and returns it for convenience.
	"""
	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])
	if current_wave_idx >= len(waves):
		return state

	wave = waves[current_wave_idx]
	for issue in wave["issues"]:
		gh_num = issue.get("github_issue")
		if not gh_num:
			continue

		labels = issue_labels.get(str(gh_num), [])
		phase = determine_phase(labels)

		last_phase = issue.get("last_seen_phase", "")
		if phase != last_phase:
			issue["last_seen_phase"] = phase
			issue["status_since_ts"] = now_ts
			if last_phase:
				# Phase genuinely advanced — reset stall counter
				issue["stall_recovery_count"] = 0
		elif "status_since_ts" not in issue:
			# First observation — seed the timestamp
			issue["status_since_ts"] = now_ts

	return state


def increment_stall_recovery(
	state: dict[str, Any],
	issue_id: str,
) -> dict[str, Any]:
	"""Increment the stall recovery counter for *issue_id* in the current wave.

	Also resets ``status_since_ts`` to now so the threshold restarts.
	Mutates *state* in-place.
	"""
	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])
	if current_wave_idx >= len(waves):
		return state

	now_ts = int(time.time())
	for issue in waves[current_wave_idx]["issues"]:
		if issue.get("id") == issue_id:
			issue["stall_recovery_count"] = issue.get("stall_recovery_count", 0) + 1
			issue["status_since_ts"] = now_ts
			break

	return state


def increment_impl_noop_count(
	state: dict[str, Any],
	issue_id: str,
) -> dict[str, Any]:
	"""Increment the implementation no-op counter for *issue_id*.

	Tracks consecutive cycles where implementation produced no repository
	changes (ai:implementation-failed or stall-recovery retriggers that
	result in no-ops).  Used to cap re-issue loops when the code already
	exists on the default branch.

	Mutates *state* in-place.
	"""
	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])
	if current_wave_idx >= len(waves):
		return state

	for issue in waves[current_wave_idx]["issues"]:
		if issue.get("id") == issue_id:
			try:
				current_value = int(issue.get("impl_noop_count", 0))
			except (TypeError, ValueError):
				current_value = 0
			issue["impl_noop_count"] = current_value + 1
			break

	return state


def get_impl_noop_count(
	state: dict[str, Any],
	issue_id: str,
) -> int:
	"""Return the current implementation no-op counter for *issue_id*."""
	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])
	if current_wave_idx >= len(waves):
		return 0

	for issue in waves[current_wave_idx]["issues"]:
		if issue.get("id") == issue_id:
			try:
				return int(issue.get("impl_noop_count", 0))
			except (TypeError, ValueError):
				return 0

	return 0


# ---------------------------------------------------------------------------
# State reconstruction (recovery from missing initial state)
# ---------------------------------------------------------------------------

def parse_tracking_body(body: str) -> dict[str, Any]:
	"""Parse a tracking issue markdown body to extract wave structure.

	Returns a dict with keys:
		project_title, waves (list of lists of {id, title, priority}),
		dependency_edges (list of {from, to}).
	"""
	result: dict[str, Any] = {
		"project_title": "",
		"waves": [],
		"dependency_edges": [],
		"integration_branch": "",
	}

	title_match = re.search(r"^## Project:\s*(.+)$", body, re.MULTILINE)
	if title_match:
		result["project_title"] = title_match.group(1).strip()

	integration_match = re.search(r"^\*\*Integration branch:\*\*\s*`?([^`\n]+)`?$", body, re.MULTILINE)
	if integration_match:
		result["integration_branch"] = integration_match.group(1).strip()

	# Split on wave headers and parse each section
	wave_sections = re.split(r"### Wave \d+", body)
	for section in wave_sections[1:]:  # skip preamble before Wave 1
		issues: list[dict[str, Any]] = []
		for match in re.finditer(
			r"-\s*\[[ x]\]\s*\*\*([^*]+)\*\*:\s*(.+?)\s*\(priority\s+(\d+)\)",
			section,
		):
			issues.append({
				"id": match.group(1).strip(),
				"title": match.group(2).strip(),
				"priority": int(match.group(3)),
			})
		if issues:
			result["waves"].append(issues)

	# Parse dependency edges from the ### Dependencies section
	dep_parts = body.split("### Dependencies")
	if len(dep_parts) > 1:
		# Only parse until the next --- or ### to avoid false matches
		dep_section = re.split(r"\n---|\n###", dep_parts[1])[0]
		for match in re.finditer(r"`([^`]+)`\s*->\s*`([^`]+)`", dep_section):
			result["dependency_edges"].append({
				"from": match.group(1).strip(),
				"to": match.group(2).strip(),
			})

	return result


def rebuild_tracking_state(
	body: str,
	issue_number_map: dict[str, int],
	tracking_issue: int,
) -> dict[str, Any]:
	"""Rebuild orchestrator state from a tracking issue body and discovered issues.

	Used when the orchestrate.yml workflow created issues but failed before
	posting the initial state comment.  The poller calls this to recover
	automatically instead of leaving the project stuck.

	Args:
		body: Markdown body of the tracking issue.
		issue_number_map: Map of local_id -> GitHub issue number, built by
			searching for child issues that reference the tracking issue.
		tracking_issue: The tracking issue number.

	Returns:
		Reconstructed state dict suitable for JSON serialisation.
	"""
	parsed = parse_tracking_body(body)
	now_ts = int(time.time())

	all_ids: set[str] = set()
	wave_list: list[dict[str, Any]] = []
	for wave_idx, wave_issues in enumerate(parsed["waves"]):
		entries: list[dict[str, Any]] = []
		for issue in wave_issues:
			all_ids.add(issue["id"])
			gh_num = issue_number_map.get(issue["id"])
			entry: dict[str, Any] = {
				"id": issue["id"],
				"github_issue": gh_num,
				"status": "pending" if gh_num is not None else "not_created",
			}
			if gh_num is not None:
				entry["last_seen_phase"] = ""
				entry["status_since_ts"] = now_ts
				entry["stall_recovery_count"] = 0
			entries.append(entry)
		wave_list.append({
			"wave": wave_idx + 1,
			"issues": entries,
		})

	total_issues = sum(len(w) for w in parsed["waves"])

	# Build pending_issue_defs for issues not yet created.
	# We reconstruct a minimal body from the title since the original
	# decomposition body is unavailable.  The clarify/plan phases will
	# fill in details from the repo context.
	pending_issue_defs: dict[str, dict[str, Any]] = {}
	for wave_issues in parsed["waves"]:
		for issue in wave_issues:
			if issue["id"] not in issue_number_map:
				pending_issue_defs[issue["id"]] = {
					"title": issue["title"],
					"body": (
						f"Implement: {issue['title']}\n\n"
						f"This issue is part of the project tracked in #{tracking_issue}.\n"
						f"See the tracking issue for full project context and dependency information."
					),
					"priority": issue["priority"],
				}

	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": parsed["project_title"],
		# In the recovery path the body we were given *is* the current
		# tracking body, so snapshot it directly for subsequent judge
		# ticks (same semantics as build_tracking_state above).
		"project_body_snapshot": body,
		"total_issues": total_issues,
		"total_waves": len(parsed["waves"]),
		"current_wave": 1,
		"judge_cycle": 0,
		"judge_stall_cycles": 0,
		"recovery_count": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": wave_list,
		"dependency_edges": parsed["dependency_edges"],
		"issue_number_map": {k: v for k, v in issue_number_map.items()},
		"pending_issue_defs": pending_issue_defs,
		"integration_branch": parsed.get("integration_branch", ""),
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
		"integration_sync_status": "clean",
		"integration_sync_last_error": "",
		"integration_conflict_dispatch_count": 0,
		"integration_conflict_dispatch_ts": 0,
		"integration_conflict_unresolved_ticks": 0,
		"tracking_issue": tracking_issue,
		"state_rebuilt": True,
	}


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _print_json(payload: dict[str, Any] | list[Any]) -> None:
	print(json.dumps(payload, ensure_ascii=True, sort_keys=False, indent=2))


def cmd_validate(args: argparse.Namespace) -> int:
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	data = validate_decomposition(data)
	_print_json({"ok": True, "issues": len(data["issues"]), "edges": len(data.get("dependency_edges", []))})
	return 0


def _resolve_hot_files_from_args(args: argparse.Namespace) -> tuple[set[str], dict[str, Any]]:
	"""Shared helper for CLI commands that need the effective hot-file set.

	Composes the committed seed (optional) with the learned-telemetry
	compute from the ai-memory JSONL (optional). Either source may be
	missing — both missing means an empty effective set, which is a
	valid state: the planner guard still detects pairwise file-touch
	overlaps and the poller probe still catches byte-level conflicts.
	"""
	committed = load_hot_files(getattr(args, "hot_files_path", None) or None)
	telemetry_path = getattr(args, "conflict_telemetry_jsonl", None) or None
	window_days = int(getattr(args, "telemetry_window_days", DEFAULT_TELEMETRY_WINDOW_DAYS) or DEFAULT_TELEMETRY_WINDOW_DAYS)
	min_events = int(getattr(args, "telemetry_min_events", DEFAULT_TELEMETRY_MIN_EVENTS) or DEFAULT_TELEMETRY_MIN_EVENTS)
	min_projects = int(getattr(args, "telemetry_min_projects", DEFAULT_TELEMETRY_MIN_PROJECTS) or DEFAULT_TELEMETRY_MIN_PROJECTS)
	effective, audit = compute_effective_hot_files(
		committed_hot_files=committed,
		telemetry_jsonl_path=telemetry_path,
		window_days=window_days,
		min_events=min_events,
		min_distinct_projects=min_projects,
	)
	return effective, audit


def cmd_compute_waves(args: argparse.Namespace) -> int:
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	data = validate_decomposition(data)
	hot_files, hot_files_audit = _resolve_hot_files_from_args(args)
	waves = compute_waves(data, hot_files=hot_files)
	output = []
	for wave_idx, wave in enumerate(waves):
		output.append({
			"wave": wave_idx + 1,
			"issues": [
				{
					"id": i["id"],
					"title": i["title"],
					"priority": i["priority"],
					"files_touched": list(i.get("files_touched", []) or []),
				}
				for i in wave
			],
		})
	_print_json({
		"ok": True,
		"total_waves": len(waves),
		"waves": output,
		"hot_files": sorted(hot_files),
		"hot_files_audit": hot_files_audit,
		"partition_serializations": data.get("partition_serializations", []),
	})
	# Also write the (possibly serialized) decomposition back to disk so
	# downstream steps (tracking issue creation, Wave 1 dispatch) consume
	# the post-serialization edge set, not the raw decomposer output.
	if getattr(args, "write_back", False):
		with path.open("w", encoding="utf-8") as f:
			json.dump(data, f, ensure_ascii=True, indent=2)
	return 0


def cmd_check_partition(args: argparse.Namespace) -> int:
	"""Probe a decomposition for file-partition overlaps without mutating it.

	Emits a machine-readable report of overlaps detected in each wave and a
	dry-run audit trail of edges that ``auto_serialize_file_overlaps`` would
	inject. Used by the orchestrate.yml partition-guard step to log the
	rewrite plan to the tracking issue before the destructive compute-waves
	call runs with ``--write-back``.
	"""
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	data = validate_decomposition(data)
	hot_files, hot_files_audit = _resolve_hot_files_from_args(args)

	# Dry-run pass: compute waves WITHOUT auto-serialize to see what the raw
	# decomposer produced, and then report overlaps per wave.
	raw_waves = compute_waves(dict(data, dependency_edges=list(data.get("dependency_edges", []))), hot_files=hot_files, auto_serialize=False)
	issues_by_id = {i["id"]: i for i in data["issues"]}

	wave_reports: list[dict[str, Any]] = []
	total_overlaps = 0
	for wave_idx, wave in enumerate(raw_waves):
		ids = [i["id"] for i in wave]
		overlaps = validate_wave_file_partition(ids, issues_by_id, hot_files)
		total_overlaps += len(overlaps)
		wave_reports.append({
			"wave": wave_idx + 1,
			"issue_count": len(ids),
			"overlap_count": len(overlaps),
			"overlaps": overlaps,
		})

	# Dry-run serialize against a clone so we can report the planned rewrites.
	clone = json.loads(json.dumps(data))
	try:
		planned = auto_serialize_file_overlaps(clone, hot_files)
	except OrchestrateError as exc:
		_print_json({
			"ok": False,
			"error": str(exc),
			"total_overlaps": total_overlaps,
			"wave_reports": wave_reports,
			"hot_files_audit": hot_files_audit,
		})
		return 3

	_print_json({
		"ok": True,
		"hot_files": sorted(hot_files),
		"hot_files_audit": hot_files_audit,
		"total_overlaps": total_overlaps,
		"wave_reports": wave_reports,
		"planned_serializations": planned,
	})
	return 0


def cmd_build_tracking_body(args: argparse.Namespace) -> int:
	path = Path(args.input_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	data = validate_decomposition(data)
	waves = compute_waves(data)
	body = build_tracking_issue_body(data, waves, integration_branch=(args.integration_branch or ""))
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

	issue_states: dict[str, str] = {}
	if getattr(args, "issue_states_json", None):
		issue_states_raw = json.loads(args.issue_states_json)
		if not isinstance(issue_states_raw, dict):
			_print_json({"ok": False, "error": "issue_states_json must be an object mapping issue numbers to issue states"})
			return 1
		for key, value in issue_states_raw.items():
			if value in ("open", "closed"):
				issue_states[str(key)] = str(value)

	pr_states: dict[str, dict[str, Any]] = {}
	if getattr(args, "pr_states_json", None):
		pr_states_raw = json.loads(args.pr_states_json)
		if not isinstance(pr_states_raw, dict):
			_print_json({"ok": False, "error": "pr_states_json must be an object mapping issue numbers to linked PR state objects"})
			return 1
		for key, value in pr_states_raw.items():
			if isinstance(value, dict):
				pr_states[str(key)] = value

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

	any_not_created = False

	for issue in wave["issues"]:
		raw_gh_num = issue.get("github_issue")  # int or None (JSON null)

		if raw_gh_num is None:
			status = "not_created"
			source = "no_github_issue"
			any_not_created = True
			all_merged = False
			statuses.append({"id": issue["id"], "github_issue": raw_gh_num, "status": status, "decision_source": source})
			continue

		gh_num_str = str(raw_gh_num)
		labels = issue_labels.get(gh_num_str, [])
		issue_state = issue_states.get(gh_num_str)
		pr_entry = pr_states.get(gh_num_str, {})
		pr_state = pr_entry.get("state") if isinstance(pr_entry, dict) else None
		pr_merged = pr_entry.get("merged") if isinstance(pr_entry, dict) else None
		if isinstance(pr_merged, str):
			pr_merged = pr_merged.lower() == "true"
		elif not isinstance(pr_merged, bool):
			pr_merged = None

		status, source = reconcile_wave_issue_status(
			issue=issue,
			labels=labels,
			issue_state=issue_state if issue_state in ("open", "closed") else None,
			pr_state=pr_state if pr_state in ("open", "closed") else None,
			pr_merged=pr_merged,
		)
		if status == "review-blocked":
			any_review_blocked = True
		if status in ("closed", "implementation-failed"):
			any_failed = True
		if status not in ("merged", "closed", "skipped"):
			all_merged = False
		if status == "not_created":
			any_not_created = True

		statuses.append({
			"id": issue["id"],
			"github_issue": raw_gh_num,
			"status": status,
			"decision_source": source,
		})

	project_complete = all_merged and (current_wave_idx + 1 >= len(waves))

	_print_json({
		"ok": True,
		"wave": current_wave_idx + 1,
		"wave_complete": all_merged,
		"any_failed": any_failed,
		"any_review_blocked": any_review_blocked,
		"any_not_created": any_not_created,
		"project_complete": project_complete,
		"issues": statuses,
	})
	return 0


def cmd_rebuild_state(args: argparse.Namespace) -> int:
	"""Rebuild state from tracking issue body + discovered issue map."""
	body_path = Path(args.body_file).resolve()
	with body_path.open("r", encoding="utf-8") as f:
		body = f.read()

	issue_map_raw: dict[str, Any] = json.loads(args.issue_map_json)
	issue_map = {k: int(v) for k, v in issue_map_raw.items()}
	tracking_issue = int(args.tracking_issue)

	state = rebuild_tracking_state(body, issue_map, tracking_issue)
	_print_json(state)
	return 0


def cmd_check_stalls(args: argparse.Namespace) -> int:
	"""Detect stalled issues and return recommended recovery actions."""
	path = Path(args.state_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		state = json.load(f)

	issue_labels: dict[str, list[str]] = json.loads(args.labels_json)
	now_ts = int(args.now_ts) if args.now_ts else int(time.time())
	threshold = int(args.threshold_minutes)
	max_recoveries = int(args.max_recoveries)
	stall_judge_trigger_count = int(getattr(args, "stall_judge_trigger_count", 2))
	if stall_judge_trigger_count < 1:
		raise OrchestrateError(f"stall_judge_trigger_count must be a positive integer, got {stall_judge_trigger_count!r}")
	enable_stall_judge_raw = getattr(args, "enable_stall_judge", "true")
	if isinstance(enable_stall_judge_raw, bool):
		enable_stall_judge = enable_stall_judge_raw
	else:
		enable_stall_judge = str(enable_stall_judge_raw).lower() == "true"

	phase_thresholds: dict[str, int] | None = None
	if args.phase_thresholds_json:
		phase_thresholds = {
			k: int(v) for k, v in json.loads(args.phase_thresholds_json).items()
		}

	stalls = detect_stalls(
		state, issue_labels, threshold, now_ts, max_recoveries,
		phase_thresholds=phase_thresholds,
		stall_judge_trigger_count=stall_judge_trigger_count,
		enable_stall_judge=enable_stall_judge,
	)
	_print_json({"ok": True, "stalls": stalls, "count": len(stalls)})
	return 0


def cmd_update_timestamps(args: argparse.Namespace) -> int:
	"""Update issue phase timestamps in the state file (mutates file)."""
	path = Path(args.state_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		state = json.load(f)

	issue_labels: dict[str, list[str]] = json.loads(args.labels_json)
	now_ts = int(args.now_ts) if args.now_ts else int(time.time())

	state = update_issue_timestamps(state, issue_labels, now_ts)

	with path.open("w", encoding="utf-8") as f:
		json.dump(state, f, indent=2)

	_print_json({"ok": True})
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

	def _add_hot_file_args(sp: argparse.ArgumentParser) -> None:
		sp.add_argument("--hot-files-path", default=None, help="Path to committed hot_files.json seed (optional; default: .github/ai/hot_files.json in CWD; absent file => empty seed). Consumer repos do NOT need to create this file; the effective hot-file set is learned from telemetry if --conflict-telemetry-jsonl is supplied.")
		sp.add_argument("--conflict-telemetry-jsonl", default=None, help="Path to merge_conflicts.jsonl extracted from the ai-memory branch (one JSON record per line with fields ts, project, pr_a, pr_b, paths). When provided, paths meeting --telemetry-min-events and --telemetry-min-projects thresholds within --telemetry-window-days are promoted to the effective hot-file set automatically. Absent file or empty JSONL degrades cleanly to the committed seed (which may also be empty).")
		sp.add_argument("--telemetry-window-days", type=int, default=DEFAULT_TELEMETRY_WINDOW_DAYS, help=f"Lookback window for telemetry-learned hot files in days (default: {DEFAULT_TELEMETRY_WINDOW_DAYS}). Events older than this drop out automatically, providing implicit demotion without persistent state.")
		sp.add_argument("--telemetry-min-events", type=int, default=DEFAULT_TELEMETRY_MIN_EVENTS, help=f"Minimum distinct conflict events within the window before a path is promoted (default: {DEFAULT_TELEMETRY_MIN_EVENTS}).")
		sp.add_argument("--telemetry-min-projects", type=int, default=DEFAULT_TELEMETRY_MIN_PROJECTS, help=f"Minimum distinct orchestrator projects within the window before a path is promoted (default: {DEFAULT_TELEMETRY_MIN_PROJECTS}). Prevents a single runaway project from skewing the learned set.")

	p_waves = subparsers.add_parser("compute-waves", help="Compute execution waves from decomposition")
	p_waves.add_argument("--input-file", required=True)
	_add_hot_file_args(p_waves)
	p_waves.add_argument("--write-back", action="store_true", help="Write the serialized decomposition (with injected dependency_edges) back to --input-file")
	p_waves.set_defaults(func=cmd_compute_waves)

	p_partition = subparsers.add_parser("check-partition", help="Dry-run partition check for sibling file-touch overlaps")
	p_partition.add_argument("--input-file", required=True)
	_add_hot_file_args(p_partition)
	p_partition.set_defaults(func=cmd_check_partition)

	p_body = subparsers.add_parser("build-tracking-body", help="Build tracking issue markdown body")
	p_body.add_argument("--input-file", required=True)
	p_body.add_argument("--integration-branch", default="", help="Optional integration branch name")
	p_body.set_defaults(func=cmd_build_tracking_body)

	p_next = subparsers.add_parser("next-wave", help="Get next wave to dispatch")
	p_next.add_argument("--state-file", required=True)
	p_next.set_defaults(func=cmd_next_wave)

	p_check = subparsers.add_parser("check-wave-status", help="Check current wave completion")
	p_check.add_argument("--state-file", required=True)
	p_check.add_argument("--labels-json", required=True, help='JSON: {"issue_num": ["label1", ...]}')
	p_check.add_argument("--issue-states-json", default=None, help='Optional JSON: {"issue_num": "open|closed", ...}')
	p_check.add_argument("--pr-states-json", default=None, help='Optional JSON: {"issue_num": {"state":"open|closed","merged":bool}, ...}')
	p_check.set_defaults(func=cmd_check_wave_status)

	p_rebuild = subparsers.add_parser("rebuild-state", help="Rebuild state from tracking body + issue map")
	p_rebuild.add_argument("--body-file", required=True, help="Path to tracking issue body text file")
	p_rebuild.add_argument("--issue-map-json", required=True, help='JSON: {"local_id": github_number, ...}')
	p_rebuild.add_argument("--tracking-issue", required=True, help="Tracking issue number")
	p_rebuild.set_defaults(func=cmd_rebuild_state)

	p_stalls = subparsers.add_parser("check-stalls", help="Detect stalled issues in current wave")
	p_stalls.add_argument("--state-file", required=True)
	p_stalls.add_argument("--labels-json", required=True, help='JSON: {"issue_num": ["label1", ...]}')
	p_stalls.add_argument("--threshold-minutes", required=True, help="Fallback stall threshold in minutes (used when a phase has no specific override)")
	p_stalls.add_argument("--phase-thresholds-json", default=None, help='Optional JSON: {"ai:clarification": 60, "ai:implementing": 120, ...}. Per-phase overrides.')
	p_stalls.add_argument("--max-recoveries", default="5", help="Max recovery attempts per issue")
	p_stalls.add_argument("--stall-judge-trigger-count", default="2", help="Recovery-count threshold to switch stall recovery to run_stall_judge")
	p_stalls.add_argument("--enable-stall-judge", default="true", choices=("true", "false"), help="Enable/disable stall judge escalation action")
	p_stalls.add_argument("--now-ts", default=None, help="Current epoch seconds (default: now)")
	p_stalls.set_defaults(func=cmd_check_stalls)

	p_ts = subparsers.add_parser("update-timestamps", help="Update issue phase timestamps in state")
	p_ts.add_argument("--state-file", required=True)
	p_ts.add_argument("--labels-json", required=True, help='JSON: {"issue_num": ["label1", ...]}')
	p_ts.add_argument("--now-ts", default=None, help="Current epoch seconds (default: now)")
	p_ts.set_defaults(func=cmd_update_timestamps)

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
