#!/usr/bin/env python3
"""Orchestrator library: DAG management, wave computation, issue tracking, and judge helpers."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
	import yaml
except Exception:  # pragma: no cover - fail-open dependency guard
	yaml = None


class OrchestrateError(ValueError):
	"""Raised when orchestrator data is invalid."""


class IntegrationBranchMissingError(OrchestrateError):
	"""Raised when integration metadata points to a missing branch ref."""


class ReconstructionUnsafeError(OrchestrateError):
	"""Raised when rebuilding orchestrator state from the tracking body would
	rewind or duplicate already-completed work.

	Reconstruction resets ``current_wave`` to 1 and re-creates GitHub issues
	for any local id missing from the discovered issue map.  When the tracking
	body marks a sub-issue complete (``[x]``) but that issue cannot be mapped
	to its existing GitHub issue, a from-scratch rebuild would spawn a
	duplicate of finished work (the project #3627 failure mode), so the rebuild
	must be refused and retried on a later poll cycle instead."""


INTEGRATION_BRANCH_LINE_RE = re.compile(
	r"^\s*(?:-\s*)?(?:\*\*Integration branch:\*\*|Integration branch:)\s*`?\s*([^`\n]+?)\s*`?\s*$",
	re.MULTILINE,
)
TRACKING_ISSUE_LINE_RE = re.compile(
	r"^\s*(?:-\s*)?(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)\s*$",
	re.MULTILINE,
)
TRACKING_BODY_ISSUE_LINE_RE = re.compile(
	r"^(?P<prefix>\s*-\s*)\[(?P<checkbox>[ xX])\](?P<suffix>\s*\*\*(?P<id>[^*\n]+)\*\*:.*)$"
)
TRACKING_BODY_WAVE_HEADING_RE = re.compile(r"^### Wave (?P<wave>\d+)\s*$")
TRACKING_BODY_CHECKED_STATUSES: set[str] = {"merged", "closed", "skipped"}


def tracking_body_sync_hash(body: str) -> str:
	"""Return the stable hash used to cache live tracking-body sync state."""
	return hashlib.sha256(body.encode("utf-8")).hexdigest()


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

DEFAULT_CONCURRENCY_CAPS_PATH = ".github/ai/concurrency_caps.yml"
SUPPORTED_CONCURRENCY_CAP_STATES: tuple[str, ...] = (
	"ai:clarification",
	"ai:planning",
	"ai:implementing",
	"ai:validating",
	"ai:review-blocked",
)
_SUPPORTED_CONCURRENCY_CAP_STATES_SET: frozenset[str] = frozenset(SUPPORTED_CONCURRENCY_CAP_STATES)
_WORKFLOW_RUN_STATE_BY_PATH: dict[str, str] = {
	"clarify.yml": "ai:clarification",
	"internal-clarify.yml": "ai:clarification",
	"plan.yml": "ai:planning",
	"internal-plan.yml": "ai:planning",
	"implement.yml": "ai:implementing",
	"internal-implement.yml": "ai:implementing",
	"validate.yml": "ai:validating",
	"internal-validate.yml": "ai:validating",
	"review_rb_judge_dispatch.yml": "ai:review-blocked",
}
_WORKFLOW_RUN_STATE_NAME_PREFIXES: tuple[tuple[str, str], ...] = (
	("internal: ai clarify", "ai:clarification"),
	("ai clarify", "ai:clarification"),
	("internal: ai plan", "ai:planning"),
	("ai plan", "ai:planning"),
	("internal: ai implement", "ai:implementing"),
	("ai implement", "ai:implementing"),
	("internal: ai validate", "ai:validating"),
	("ai validate", "ai:validating"),
	("internal: review-blocked judge dispatch", "ai:review-blocked"),
	("ai review-blocked judge dispatch", "ai:review-blocked"),
)


def _disabled_concurrency_caps(
	target: Path,
	*,
	status: str,
	error: str = "",
) -> dict[str, Any]:
	return {
		"enabled": False,
		"status": status,
		"error": error,
		"source_path": str(target),
		"global_max_concurrent": None,
		"max_concurrent_by_state": {},
		"supported_states": list(SUPPORTED_CONCURRENCY_CAP_STATES),
	}


def _normalize_concurrency_cap_state(raw_state: Any) -> str | None:
	if not isinstance(raw_state, str):
		return None
	normalized = raw_state.strip().lower()
	if normalized in _SUPPORTED_CONCURRENCY_CAP_STATES_SET:
		return normalized
	return None


def _normalize_concurrency_cap_value(raw_value: Any, *, field_name: str) -> int | None:
	if raw_value is None:
		return None
	if isinstance(raw_value, bool):
		raise OrchestrateError(f"{field_name} must be an integer")
	if isinstance(raw_value, float):
		if not raw_value.is_integer():
			raise OrchestrateError(f"{field_name} must be an integer")
		raw_value = int(raw_value)
	if not isinstance(raw_value, int):
		raise OrchestrateError(f"{field_name} must be an integer")
	if raw_value < -1:
		raise OrchestrateError(f"{field_name} must be >= -1")
	if raw_value == -1:
		return None
	return raw_value


def _parse_minimal_concurrency_cap_scalar(
	raw_value: str,
	*,
	field_name: str,
	path: Path,
	line_number: int,
) -> int | float | None:
	if raw_value in {"", "null", "~"}:
		return None
	if re.fullmatch(r"-?[0-9]+", raw_value):
		return int(raw_value)
	if re.fullmatch(r"-?[0-9]+\.0+", raw_value):
		return float(raw_value)
	raise OrchestrateError(
		f"Unsupported scalar for {field_name} at {path}:{line_number}; install PyYAML for richer caps syntax"
	)


def _parse_minimal_concurrency_caps_yaml(text: str, path: Path) -> dict[str, Any]:
	stripped = text.strip()
	if not stripped:
		return {}
	if stripped.startswith("{"):
		try:
			loaded = json.loads(stripped)
		except json.JSONDecodeError as exc:
			raise OrchestrateError(
				f"Invalid JSON-formatted caps file '{path}' at line {exc.lineno}, column {exc.colno}: {exc.msg}"
			) from exc
		if not isinstance(loaded, dict):
			raise OrchestrateError("caps file must be a YAML mapping")
		return loaded

	data: dict[str, Any] = {}
	lines = text.splitlines()
	index = 0
	while index < len(lines):
		raw_line = lines[index]
		line_number = index + 1
		stripped_line = raw_line.strip()
		if not stripped_line or stripped_line.startswith("#"):
			index += 1
			continue
		if raw_line.startswith((" ", "\t")):
			raise OrchestrateError(
				f"Unsupported indentation at {path}:{line_number}; install PyYAML for richer caps syntax"
			)
		key, separator, remainder = raw_line.partition(":")
		if separator != ":":
			raise OrchestrateError(f"Invalid caps line at {path}:{line_number}: {raw_line!r}")
		key = key.strip()
		remainder = remainder.strip()
		if key == "global_max_concurrent":
			data[key] = _parse_minimal_concurrency_cap_scalar(
				remainder,
				field_name=key,
				path=path,
				line_number=line_number,
			)
			index += 1
			continue
		if key != "max_concurrent_by_state":
			raise OrchestrateError(
				f"Unknown concurrency-caps key '{key}' in '{path}'; supported keys: ['global_max_concurrent', 'max_concurrent_by_state']"
			)
		if remainder:
			if remainder == "{}":
				data[key] = {}
				index += 1
				continue
			raise OrchestrateError(
				f"Unsupported inline mapping syntax for {key} at {path}:{line_number}; install PyYAML for richer caps syntax"
			)

		mapping: dict[str, int | float | None] = {}
		index += 1
		while index < len(lines):
			child_raw = lines[index]
			child_line_number = index + 1
			child_stripped = child_raw.strip()
			if not child_stripped or child_stripped.startswith("#"):
				index += 1
				continue
			if not child_raw.startswith("  "):
				break
			if child_raw.startswith("   ") or child_raw.startswith("  \t"):
				raise OrchestrateError(
					f"Unsupported indentation at {path}:{child_line_number}; install PyYAML for richer caps syntax"
				)
			child_payload = child_raw[2:]
			child_key, child_separator, child_remainder = child_payload.rpartition(":")
			if child_separator != ":":
				raise OrchestrateError(
					f"Expected key/value mapping under max_concurrent_by_state at {path}:{child_line_number}"
				)
			child_key = child_key.strip()
			mapping[child_key] = _parse_minimal_concurrency_cap_scalar(
				child_remainder.strip(),
				field_name=f"max_concurrent_by_state.{child_key}",
				path=path,
				line_number=child_line_number,
			)
			index += 1
		data[key] = mapping

	return data


def load_concurrency_caps(path: str | Path | None = None) -> dict[str, Any]:
	"""Load the optional per-state workflow concurrency caps.

	The config is fail-open by contract: missing, empty, malformed, unreadable,
	or semantically invalid files disable caps instead of raising.
	"""
	target = Path(path) if path else Path(DEFAULT_CONCURRENCY_CAPS_PATH)
	try:
		raw_text = target.read_text(encoding="utf-8")
	except FileNotFoundError:
		return _disabled_concurrency_caps(target, status="missing")
	except (PermissionError, OSError) as exc:
		return _disabled_concurrency_caps(target, status="unreadable", error=str(exc))

	if not raw_text.strip():
		return _disabled_concurrency_caps(target, status="empty")

	try:
		if yaml is None:
			data = _parse_minimal_concurrency_caps_yaml(raw_text, target)
		else:
			data = yaml.safe_load(raw_text)
	except OrchestrateError as exc:
		return _disabled_concurrency_caps(target, status="yaml_unavailable", error=str(exc))
	except Exception as exc:
		return _disabled_concurrency_caps(target, status="malformed", error=str(exc))

	if data is None:
		return _disabled_concurrency_caps(target, status="empty")
	if not isinstance(data, dict):
		return _disabled_concurrency_caps(target, status="invalid", error="caps file must be a YAML mapping")

	try:
		global_cap = _normalize_concurrency_cap_value(
			data.get("global_max_concurrent", None),
			field_name="global_max_concurrent",
		)
		raw_state_caps = data.get("max_concurrent_by_state", {})
		if raw_state_caps is None:
			raw_state_caps = {}
		if not isinstance(raw_state_caps, dict):
			raise OrchestrateError("max_concurrent_by_state must be a mapping")

		normalized_state_caps: dict[str, int] = {}
		for raw_state, raw_limit in raw_state_caps.items():
			state = _normalize_concurrency_cap_state(raw_state)
			if state is None:
				raise OrchestrateError(f"unsupported concurrency-cap state: {raw_state!r}")
			limit = _normalize_concurrency_cap_value(raw_limit, field_name=f"max_concurrent_by_state.{state}")
			if limit is not None:
				normalized_state_caps[state] = limit
	except OrchestrateError as exc:
		return _disabled_concurrency_caps(target, status="invalid", error=str(exc))

	enabled = global_cap is not None or bool(normalized_state_caps)
	status = "enabled" if enabled else "disabled"
	return {
		"enabled": enabled,
		"status": status,
		"error": "",
		"source_path": str(target),
		"global_max_concurrent": global_cap,
		"max_concurrent_by_state": normalized_state_caps,
		"supported_states": list(SUPPORTED_CONCURRENCY_CAP_STATES),
	}


def classify_workflow_run_target_state(run: dict[str, Any]) -> str | None:
	"""Map a workflow run payload to the orchestrator phase it directly drives."""
	if not isinstance(run, dict):
		return None

	workflow_path = str(run.get("path") or "").strip().replace("\\", "/")
	while workflow_path.startswith("./"):
		workflow_path = workflow_path[2:]
	for suffix, state in _WORKFLOW_RUN_STATE_BY_PATH.items():
		if workflow_path == suffix or workflow_path.endswith(f"/{suffix}"):
			return state

	workflow_name = " ".join(str(run.get("name") or "").strip().casefold().split())
	for prefix, state in _WORKFLOW_RUN_STATE_NAME_PREFIXES:
		if workflow_name.startswith(prefix):
			return state
	return None


def _workflow_run_counts_toward_concurrency(
	run: dict[str, Any],
	state: str,
	*,
	now_ts: int,
	threshold_minutes: int,
	implementing_threshold_minutes: int | None,
) -> bool:
	status = str(run.get("status") or "").strip().lower()
	if status not in {"in_progress", "queued"}:
		return False

	start_epoch = _parse_iso8601_to_epoch(run.get("run_started_at"))
	if start_epoch is None:
		start_epoch = _parse_iso8601_to_epoch(run.get("created_at"))
	if start_epoch is None:
		return False

	effective_minutes = threshold_minutes
	if state == "ai:implementing" and implementing_threshold_minutes is not None:
		effective_minutes = implementing_threshold_minutes
	if effective_minutes < 1:
		effective_minutes = 120

	return (now_ts - start_epoch) < (effective_minutes * 60)


def build_concurrency_snapshot(
	actions_runs_payload: Any,
	*,
	caps: dict[str, Any] | None = None,
	caps_path: str | Path | None = None,
	threshold_minutes: int = 120,
	implementing_threshold_minutes: int | None = None,
	now_ts: int | None = None,
) -> dict[str, Any]:
	"""Build one cycle-local count map from the shared Actions runs snapshot."""
	caps_doc = dict(caps) if caps is not None else load_concurrency_caps(caps_path)
	counts = {state: 0 for state in SUPPORTED_CONCURRENCY_CAP_STATES}
	global_running = 0

	runs = actions_runs_payload.get("workflow_runs") if isinstance(actions_runs_payload, dict) else None
	if not isinstance(runs, list):
		runs = []

	try:
		effective_now_ts = int(now_ts) if now_ts is not None else int(time.time())
	except (TypeError, ValueError):
		effective_now_ts = int(time.time())
	if not isinstance(threshold_minutes, int) or threshold_minutes < 1:
		threshold_minutes = 120
	if implementing_threshold_minutes is not None and (
		not isinstance(implementing_threshold_minutes, int) or implementing_threshold_minutes < 1
	):
		implementing_threshold_minutes = None

	for run in runs:
		if not isinstance(run, dict):
			continue
		state = classify_workflow_run_target_state(run)
		if state is None:
			continue
		if not _workflow_run_counts_toward_concurrency(
			run,
			state,
			now_ts=effective_now_ts,
			threshold_minutes=threshold_minutes,
			implementing_threshold_minutes=implementing_threshold_minutes,
		):
			continue
		counts[state] += 1
		global_running += 1

	return {
		**caps_doc,
		"running_by_state": counts,
		"global_running": global_running,
	}


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
			key=lambda iid: (issues_by_id[iid].get("priority", 10), iid),
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
			# Also report the non-hot overlap for the same pair so callers
			# can distinguish "hot-file-only" overlap from mixed overlap.
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
	# body *snapshot* is immutable by contract even though the live
	# GitHub issue body may later be re-rendered from state to reconcile
	# checkbox rows. Validate and clarify-respond continue to read the
	# live body for their own narrower purposes.
	project_body_snapshot = build_tracking_issue_body(
		data, waves, integration_branch=integration_branch
	)

	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": data["project_title"],
		"project_body_snapshot": project_body_snapshot,
		"tracking_body_sync_hash": tracking_body_sync_hash(project_body_snapshot),
		"tracking_body_last_readiness_refresh_hash": tracking_body_sync_hash(project_body_snapshot),
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


def render_tracking_issue_body_from_state(
	state: dict[str, Any],
	template_body: str | None = None,
) -> str:
	"""Render the live tracking issue body from orchestrator state.

	The cached ``project_body_snapshot`` remains the canonical immutable
	template for judge prompt caching. The live GitHub issue body may be
	re-rendered from state so completed sub-issue rows flip from ``[ ]`` to
	``[x]`` without losing the original summary/dependencies/footer text.
	"""
	body_template = template_body if template_body is not None else str(state.get("project_body_snapshot", "") or "")
	if not body_template.strip():
		raise OrchestrateError("Tracking issue body template is required to render live body state")

	issue_status_by_id: dict[str, str] = {}
	issues_by_wave: dict[int, list[dict[str, Any]]] = {}
	issue_id_order: list[str] = []
	for wave in state.get("waves", []) or []:
		if not isinstance(wave, dict):
			continue
		wave_num = wave.get("wave")
		if not isinstance(wave_num, int):
			continue
		wave_issues: list[dict[str, Any]] = []
		for issue in wave.get("issues", []) or []:
			if not isinstance(issue, dict):
				continue
			issue_id = str(issue.get("id", "") or "").strip()
			if not issue_id:
				continue
			payload = {
				"id": issue_id,
				"status": str(issue.get("status", "") or ""),
				"github_issue": issue.get("github_issue"),
				"wave": wave_num,
			}
			issue_status_by_id[issue_id] = payload["status"]
			issue_id_order.append(issue_id)
			wave_issues.append(payload)
		issues_by_wave[wave_num] = wave_issues

	if not issue_status_by_id:
		raise OrchestrateError("Tracking state contains no issue rows to reconcile")

	def _synthesized_issue_line(issue: dict[str, Any], prefix: str) -> str:
		checkbox = "x" if issue["status"] in TRACKING_BODY_CHECKED_STATUSES else " "
		gh_num = issue.get("github_issue")
		if isinstance(gh_num, int) or (isinstance(gh_num, str) and gh_num.isdigit()):
			suffix = f" **{issue['id']}**: #{gh_num}"
		else:
			suffix = f" **{issue['id']}**: pending creation"
		return f"{prefix}[{checkbox}]{suffix}"

	lines = body_template.splitlines()
	section_starts = [idx for idx, line in enumerate(lines) if line.startswith("### ")]
	section_end_by_start = {
		start: (section_starts[pos + 1] if pos + 1 < len(section_starts) else len(lines))
		for pos, start in enumerate(section_starts)
	}
	wave_sections: list[tuple[int, int]] = []
	for idx, line in enumerate(lines):
		match = TRACKING_BODY_WAVE_HEADING_RE.match(line)
		if match is None:
			continue
		wave_sections.append((idx, int(match.group("wave"))))

	rendered_lines: list[str] = []
	matched_ids: set[str] = set()
	duplicate_ids: set[str] = set()
	saw_issue_row = False
	seen_wave_numbers: set[int] = set()
	cursor = 0
	for start_idx, wave_num in wave_sections:
		end_idx = section_end_by_start.get(start_idx, len(lines))
		rendered_lines.extend(lines[cursor:start_idx])
		block_lines = lines[start_idx:end_idx]
		block_output: list[str] = []
		expected_issues = issues_by_wave.get(wave_num, [])
		expected_ids = {issue["id"] for issue in expected_issues}
		matched_in_wave: set[str] = set()
		insertion_prefix = "- "
		last_issue_output_idx: int | None = None
		seen_wave_numbers.add(wave_num)

		for line in block_lines:
			match = TRACKING_BODY_ISSUE_LINE_RE.match(line)
			if match is None:
				block_output.append(line)
				continue

			saw_issue_row = True
			issue_id = match.group("id").strip()
			if expected_ids:
				insertion_prefix = match.group("prefix") or insertion_prefix
			if issue_id not in expected_ids:
				block_output.append(line)
				last_issue_output_idx = len(block_output) - 1
				continue

			if issue_id in matched_ids:
				duplicate_ids.add(issue_id)
			matched_ids.add(issue_id)
			matched_in_wave.add(issue_id)

			checkbox = "x" if issue_status_by_id[issue_id] in TRACKING_BODY_CHECKED_STATUSES else " "
			block_output.append(f"{match.group('prefix')}[{checkbox}]{match.group('suffix')}")
			last_issue_output_idx = len(block_output) - 1

		missing_in_wave = [issue for issue in expected_issues if issue["id"] not in matched_in_wave]
		if missing_in_wave:
			if last_issue_output_idx is not None:
				insert_at = last_issue_output_idx + 1
			else:
				insert_at = 1
				while insert_at < len(block_output) and block_output[insert_at].strip() == "":
					insert_at += 1
			inserted_lines = [_synthesized_issue_line(issue, insertion_prefix) for issue in missing_in_wave]
			block_output[insert_at:insert_at] = inserted_lines
			matched_ids.update(issue["id"] for issue in missing_in_wave)

		rendered_lines.extend(block_output)
		cursor = end_idx

	rendered_lines.extend(lines[cursor:])

	if duplicate_ids:
		dups = ", ".join(sorted(duplicate_ids))
		raise OrchestrateError(f"Tracking issue body template repeats local issue id(s): {dups}")

	missing_wave_numbers = sorted(wave_num for wave_num, issues in issues_by_wave.items() if issues and wave_num not in seen_wave_numbers)
	if missing_wave_numbers:
		preview = ", ".join(str(wave_num) for wave_num in missing_wave_numbers[:10])
		raise OrchestrateError(
			"Tracking issue body template is missing wave heading(s): "
			f"{preview}"
		)

	missing_ids = [issue_id for issue_id in issue_id_order if issue_id not in matched_ids]
	if missing_ids:
		preview = ", ".join(missing_ids[:10])
		if len(missing_ids) > 10:
			preview += f", ... +{len(missing_ids) - 10} more"
		if saw_issue_row or wave_sections:
			raise OrchestrateError(
				"Tracking issue body template is missing local issue id(s): "
				f"{preview}"
			)
		raise OrchestrateError(
			"Tracking issue body template does not contain parseable sub-issue rows; "
			f"missing ids: {preview}"
		)

	rendered = "\n".join(rendered_lines)
	if body_template.endswith("\n"):
		rendered += "\n"
	return rendered


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
	"ai:blocked",
	"ai:clarify-failed",
	"ai:clarify-respond-failed",
	"ai:plan-failed",
	"ai:implement-diagnose-failed",
	"ai:review-autofix-failed",
	"ai:validate-failed",
	"ai:integration-judge-failed",
	"ai:log-analysis-failed",
	"ai:memory-maintenance-failed",
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

TERMINAL_PHASES: set[str] = {
	"ai:merged",
	"ai:closed",
	"ai:validated",
	"ai:validation-failed",
	"ai:clarify-failed",
	"ai:clarify-respond-failed",
	"ai:plan-failed",
	"ai:implement-diagnose-failed",
	"ai:review-autofix-failed",
	"ai:validate-failed",
	"ai:integration-judge-failed",
	"ai:log-analysis-failed",
	"ai:memory-maintenance-failed",
}
VALIDATION_DISPATCH_BLOCKING_TERMINAL_PHASES: set[str] = TERMINAL_PHASES - {"ai:closed"}
VALIDATION_DISPATCH_BLOCKING_FAILURE_LABELS: set[str] = (
	VALIDATION_DISPATCH_BLOCKING_TERMINAL_PHASES - {"ai:merged", "ai:validated"}
) | {"ai:implementation-failed"}
TERMINAL_WAVE_STATUSES: set[str] = {"merged", "closed", "skipped", "not_created"}
BLOCKER_TERMINAL_WAVE_STATUSES: set[str] = {"merged", "closed", "skipped", "not_created"}

# Phases already handled by dedicated logic in the poller — stall detector
# should not double-act on these.
#
# ai:review-blocked was previously in this set, but the review/autofix
# workflow's dedicated handler (review_rb_judge.sh) only runs inline at
# the end of a review_autofix.yml run — it has no standalone trigger.
# When the dedicated handler goes silent (e.g. the empty-editor failure
# mode that stamps ai:review-blocked on linked issues without ever
# running the judge), the phase has no autonomous escape.  The stall
# detector therefore now covers ai:review-blocked too: its ladder
# dispatches review_rb_judge_dispatch.yml (which runs review_autofix.yml
# with force_rb_judge=true) past the per-phase stall threshold.  See
# STALL_RECOVERY_ACTIONS["ai:review-blocked"] below.
DEDICATED_HANDLER_PHASES: set[str] = {"ai:needs-human", "ai:blocked", "ai:implementation-failed", "ai:validating", "ai:validation-fixing"}

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
	# ai:review-blocked: matches ai:done (120 min) so an in-flight
	# review_autofix run that legitimately takes a long time to reach
	# the inline rb_judge step does not get double-dispatched by the
	# stall loop's dispatch_rb_judge action.
	"ai:review-blocked": 120,
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
	"ai:validating": [
		"retrigger_validate",
		"retrigger_validate",
		"escalate_human",
	],
	# ai:review-blocked recovery: the dispatch_rb_judge action triggers
	# review_rb_judge_dispatch.yml via workflow_dispatch, which runs
	# review_autofix.yml with force_rb_judge=true — i.e. the dedicated
	# review-blocked judge (scripts/review_rb_judge.sh) decides the next
	# step (merge, fix, or close_and_reissue).  The two dispatch_rb_judge
	# rungs mirror the two-retry allowance used elsewhere; escalate_human
	# is the terminal fallback when ENABLE_STALL_HUMAN_TERMINALIZATION=true.
	"ai:review-blocked": [
		"dispatch_rb_judge",
		"dispatch_rb_judge",
		"escalate_human",
	],
}

VALID_STALL_RECOVERY_ACTIONS: set[str] = {
	action
	for ladder in STALL_RECOVERY_ACTIONS.values()
	for action in ladder
}
VALID_STALL_RECOVERY_ACTIONS.update({
	"close_and_reissue",
	"resolve_merge_conflict",
	"retrigger_validate",
})

STALL_RECOVERY_ACTION_PRIORITY: dict[str, int] = {
	"retrigger_pipeline": 10,
	"auto_respond_clarify": 20,
	"retrigger_plan": 30,
	"auto_approve": 40,
	"retrigger_implement": 50,
	"retrigger_review": 60,
	"resolve_merge_conflict": 65,
	"dispatch_rb_judge": 68,
	"retrigger_validate": 70,
	"attempt_merge": 80,
	"close_and_reissue": 90,
	"escalate_human": 100,
}

PHASE_FAILURE_MARKER_RE = re.compile(
	r"<!--\s*AI_PHASE_FAILURE_V1\s*\n(.*?)\nAI_PHASE_FAILURE_V1\s*-->",
	re.DOTALL,
)

PHASE_FAILURE_PHASE_TO_PHASE_LABEL: dict[str, str] = {
	"clarify": "ai:clarification",
	"clarify-respond": "ai:clarification",
	"plan": "ai:planning",
	"implement": "ai:implementing",
	"review-autofix": "ai:done",
	"validate": "ai:validating",
	"integration-judge": "ai:ready-to-merge",
	"log-analysis": "ai:validation-fixing",
	"memory-maintenance": "ai:validation-fixing",
}

PHASE_FAILURE_PHASE_TO_FAILURE_LABEL: dict[str, str] = {
	"clarify": "ai:clarify-failed",
	"clarify-respond": "ai:clarify-respond-failed",
	"plan": "ai:plan-failed",
	"implement": "ai:implement-diagnose-failed",
	"review-autofix": "ai:review-autofix-failed",
	"validate": "ai:validate-failed",
	"integration-judge": "ai:integration-judge-failed",
	"log-analysis": "ai:log-analysis-failed",
	"memory-maintenance": "ai:memory-maintenance-failed",
}


def _stable_int(value: Any, default: int = 0) -> int:
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


def parse_phase_failure_markers(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Extract and normalize every AI_PHASE_FAILURE_V1 marker from comments."""
	markers: list[dict[str, Any]] = []
	for comment in comments:
		if not isinstance(comment, dict):
			continue
		body = comment.get("body")
		if not isinstance(body, str) or "AI_PHASE_FAILURE_V1" not in body:
			continue
		created_at_raw = comment.get("created_at")
		if not isinstance(created_at_raw, str) or not created_at_raw.strip():
			created_at_raw = comment.get("createdAt")
		created_at = created_at_raw.strip() if isinstance(created_at_raw, str) else ""
		comment_id = _stable_int(comment.get("id"), 0)
		for match in PHASE_FAILURE_MARKER_RE.finditer(body):
			raw_payload = (match.group(1) or "").strip()
			if not raw_payload:
				continue
			try:
				payload = json.loads(raw_payload)
			except ValueError:
				continue
			if not isinstance(payload, dict):
				continue
			recommended_action_raw = payload.get("recommended_resume_action", "")
			phase_raw = payload.get("phase", "")
			run_id_raw = payload.get("workflow_run_id", "")
			ts_raw = payload.get("timestamp", "")
			recommended_action = recommended_action_raw.strip() if isinstance(recommended_action_raw, str) else ""
			phase = phase_raw.strip() if isinstance(phase_raw, str) else ""
			run_id = run_id_raw.strip() if isinstance(run_id_raw, str) else ""
			marker_ts = ts_raw.strip() if isinstance(ts_raw, str) else ""
			markers.append(
				{
					"comment_id": comment_id,
					"created_at": created_at,
					"timestamp": marker_ts or created_at,
					"phase": phase,
					"recommended_resume_action": recommended_action,
					"workflow_run_id": run_id,
					"workflow_run_url": payload.get("workflow_run_url"),
					"payload": payload,
				}
			)
	markers.sort(
		key=lambda item: (
			str(item.get("timestamp", "") or item.get("created_at", "") or ""),
			_stable_int(item.get("comment_id"), 0),
		),
		reverse=True,
	)
	return markers


def _phase_failure_markers_to_evidence(markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
	evidence: list[dict[str, Any]] = []
	for marker in markers:
		action = marker.get("recommended_resume_action")
		if not isinstance(action, str) or action not in STALL_RECOVERY_ACTION_PRIORITY:
			continue
		evidence.append(
			{
				"action": action,
				"source": "phase_failure_marker",
				"phase": marker.get("phase", ""),
				"workflow_run_id": marker.get("workflow_run_id", ""),
				"timestamp": marker.get("timestamp", "") or marker.get("created_at", ""),
				"comment_id": _stable_int(marker.get("comment_id"), 0),
				"marker": marker,
			}
		)
	return evidence


def _linked_pr_evidence(linked_pr: dict[str, Any] | None) -> dict[str, Any] | None:
	if not isinstance(linked_pr, dict):
		return None
	if bool(linked_pr.get("merged")):
		return {
			"action": "attempt_merge",
			"source": "linked_pr_state",
			"timestamp": str(linked_pr.get("headPushedAt") or ""),
			"comment_id": 0,
		}
	state_raw = linked_pr.get("state")
	state = state_raw.strip().lower() if isinstance(state_raw, str) else ""
	if state == "open":
		return {
			"action": "retrigger_review",
			"source": "linked_pr_state",
			"timestamp": str(linked_pr.get("headPushedAt") or ""),
			"comment_id": 0,
		}
	return None


def _comment_is_newer_than_marker(comment: dict[str, Any], marker: dict[str, Any]) -> bool:
	comment_created_raw = comment.get("created_at")
	if not isinstance(comment_created_raw, str) or not comment_created_raw.strip():
		comment_created_raw = comment.get("createdAt")
	comment_created = comment_created_raw.strip() if isinstance(comment_created_raw, str) else ""
	comment_id = _stable_int(comment.get("id"), 0)
	marker_ts = str(marker.get("timestamp", "") or marker.get("created_at", "") or "")
	marker_id = _stable_int(marker.get("comment_id"), 0)
	return (comment_created, comment_id) > (marker_ts, marker_id)


def evaluate_phase_failure_resume(
	*,
	candidate_action: str,
	current_phase: str,
	labels: list[str],
	comments: list[dict[str, Any]],
	linked_pr: dict[str, Any] | None,
	processed_workflow_run_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
	"""Evaluate whether a phase-failure marker can safely resume a retrigger action."""
	markers = parse_phase_failure_markers(comments)
	if not markers:
		return {
			"marker_found": False,
			"ok": True,
			"reason": "no_phase_failure_marker",
			"selected_marker": None,
			"selected_evidence_source": "none",
			"selected_action": "",
			"discarded_markers": [],
		}

	evidence = _phase_failure_markers_to_evidence(markers)
	pr_evidence = _linked_pr_evidence(linked_pr)
	if pr_evidence is not None:
		evidence.append(pr_evidence)

	selection = choose_most_advanced_conclusive_evidence(evidence)
	selected = selection.get("selected")
	discarded_raw = selection.get("discarded", [])
	discarded_markers: list[dict[str, Any]] = []
	for item in discarded_raw:
		if isinstance(item, dict) and item.get("source") == "phase_failure_marker":
			marker = item.get("marker")
			if isinstance(marker, dict):
				discarded_markers.append(marker)
	selected_source = "none"
	selected_action = ""
	if isinstance(selected, dict):
		selected_source = str(selected.get("source", "") or "none")
		action_raw = selected.get("action", "")
		if isinstance(action_raw, str):
			selected_action = action_raw

	if not isinstance(selected, dict) or selected_source != "phase_failure_marker":
		return {
			"marker_found": True,
			"ok": False,
			"reason": "newer_conclusive_non_marker_evidence",
			"selected_marker": None,
			"selected_evidence_source": selected_source,
			"selected_action": selected_action,
			"discarded_markers": discarded_markers,
		}

	selected_marker = selected.get("marker")
	if not isinstance(selected_marker, dict):
		return {
			"marker_found": True,
			"ok": False,
			"reason": "selected_marker_unavailable",
			"selected_marker": None,
			"selected_evidence_source": selected_source,
			"selected_action": selected_action,
			"discarded_markers": discarded_markers,
		}

	selected_run_id_raw = selected_marker.get("workflow_run_id", "")
	selected_run_id = selected_run_id_raw.strip() if isinstance(selected_run_id_raw, str) else ""
	if selected_run_id:
		processed = {item for item in (processed_workflow_run_ids or []) if isinstance(item, str)}
		if selected_run_id in processed:
			return {
				"marker_found": True,
				"ok": False,
				"reason": "workflow_run_id_already_processed",
				"selected_marker": selected_marker,
				"selected_evidence_source": selected_source,
				"selected_action": selected_action,
				"discarded_markers": discarded_markers,
			}

	marker_action_raw = selected_marker.get("recommended_resume_action", "")
	marker_action = marker_action_raw.strip() if isinstance(marker_action_raw, str) else ""
	if marker_action and marker_action != candidate_action:
		return {
			"marker_found": True,
			"ok": False,
			"reason": "recommended_action_mismatch",
			"selected_marker": selected_marker,
			"selected_evidence_source": selected_source,
			"selected_action": marker_action,
			"discarded_markers": discarded_markers,
		}

	marker_phase_raw = selected_marker.get("phase", "")
	marker_phase = marker_phase_raw.strip() if isinstance(marker_phase_raw, str) else ""
	expected_phase_label = PHASE_FAILURE_PHASE_TO_PHASE_LABEL.get(marker_phase)
	expected_failure_label = PHASE_FAILURE_PHASE_TO_FAILURE_LABEL.get(marker_phase)
	if expected_phase_label:
		label_set = {label for label in labels if isinstance(label, str)}
		if expected_phase_label not in label_set and (not expected_failure_label or expected_failure_label not in label_set):
			return {
				"marker_found": True,
				"ok": False,
				"reason": "label_phase_mismatch",
				"selected_marker": selected_marker,
				"selected_evidence_source": selected_source,
				"selected_action": marker_action,
				"discarded_markers": discarded_markers,
			}

	if candidate_action == "auto_respond_clarify":
		for comment in comments:
			if not isinstance(comment, dict) or not _comment_is_newer_than_marker(comment, selected_marker):
				continue
			body = comment.get("body", "")
			if isinstance(body, str) and re.search(r"^\s*/answer(\s|$)", body, flags=re.MULTILINE):
				return {
					"marker_found": True,
					"ok": False,
					"reason": "clarify_answers_present",
					"selected_marker": selected_marker,
					"selected_evidence_source": selected_source,
					"selected_action": marker_action,
					"discarded_markers": discarded_markers,
				}

	if candidate_action == "retrigger_plan":
		for comment in comments:
			if not isinstance(comment, dict) or not _comment_is_newer_than_marker(comment, selected_marker):
				continue
			body = comment.get("body", "")
			if not isinstance(body, str):
				continue
			if re.search(r"^\s*/approved(\s|$)", body, flags=re.MULTILINE) or "Implementation Plan" in body:
				return {
					"marker_found": True,
					"ok": False,
					"reason": "plan_already_approved",
					"selected_marker": selected_marker,
					"selected_evidence_source": selected_source,
					"selected_action": marker_action,
					"discarded_markers": discarded_markers,
				}

	if candidate_action == "retrigger_implement":
		if isinstance(linked_pr, dict) and _stable_int(linked_pr.get("number"), 0) > 0:
			return {
				"marker_found": True,
				"ok": False,
				"reason": "linked_pr_already_exists",
				"selected_marker": selected_marker,
				"selected_evidence_source": selected_source,
				"selected_action": marker_action,
				"discarded_markers": discarded_markers,
			}

	if candidate_action == "retrigger_review":
		state_raw = (linked_pr or {}).get("state") if isinstance(linked_pr, dict) else None
		state = state_raw.strip().lower() if isinstance(state_raw, str) else ""
		if state != "open":
			return {
				"marker_found": True,
				"ok": False,
				"reason": "linked_pr_not_open",
				"selected_marker": selected_marker,
				"selected_evidence_source": selected_source,
				"selected_action": marker_action,
				"discarded_markers": discarded_markers,
			}

	if candidate_action == "retrigger_validate":
		for comment in comments:
			if not isinstance(comment, dict) or not _comment_is_newer_than_marker(comment, selected_marker):
				continue
			body = comment.get("body", "")
			if not isinstance(body, str):
				continue
			if "## 🧪 Runtime validation dispatched" in body or "## ✅ Runtime validation complete" in body:
				return {
					"marker_found": True,
					"ok": False,
					"reason": "validation_already_dispatched",
					"selected_marker": selected_marker,
					"selected_evidence_source": selected_source,
					"selected_action": marker_action,
					"discarded_markers": discarded_markers,
				}

	return {
		"marker_found": True,
		"ok": True,
		"reason": "actionable_phase_failure_marker",
		"selected_marker": selected_marker,
		"selected_evidence_source": selected_source,
		"selected_action": marker_action,
		"discarded_markers": discarded_markers,
	}


def resolve_label_repair_evidence(
	*,
	labels: list[str],
	comments: list[dict[str, Any]],
	linked_pr: dict[str, Any] | None,
) -> dict[str, Any]:
	"""Resolve authoritative label evidence and stale-marker audit details."""
	markers = parse_phase_failure_markers(comments)
	evidence = _phase_failure_markers_to_evidence(markers)
	pr_evidence = _linked_pr_evidence(linked_pr)
	if pr_evidence is not None:
		evidence.append(pr_evidence)
	current_phase = determine_phase(labels)
	if current_phase != "no_labels":
		phase_ladder = STALL_RECOVERY_ACTIONS.get(current_phase, [])
		if current_phase in TERMINAL_PHASES:
			phase_action = "escalate_human"
		elif phase_ladder:
			phase_action = phase_ladder[0]
		else:
			phase_action = "retrigger_pipeline"
		evidence.append(
			{
				"action": phase_action,
				"source": "current_labels",
				"phase": current_phase,
				"timestamp": "",
				"comment_id": 0,
			}
		)

	selection = choose_most_advanced_conclusive_evidence(evidence)
	selected = selection.get("selected")
	discarded_raw = selection.get("discarded", [])
	discarded_markers: list[dict[str, Any]] = []
	for item in discarded_raw:
		if isinstance(item, dict) and item.get("source") == "phase_failure_marker":
			marker = item.get("marker")
			if isinstance(marker, dict):
				discarded_markers.append(marker)

	authoritative_phase = current_phase if current_phase != "no_labels" else ""
	selected_source = "none"
	selected_action = ""
	if isinstance(selected, dict):
		selected_source = str(selected.get("source", "") or "none")
		action_raw = selected.get("action", "")
		if isinstance(action_raw, str):
			selected_action = action_raw
		if selected_source == "linked_pr_state":
			if bool((linked_pr or {}).get("merged")):
				authoritative_phase = "ai:merged"
			else:
				authoritative_phase = "ai:done" if current_phase in {"ai:done", "ai:ready-to-merge"} else "ai:implementing"
		elif selected_source == "phase_failure_marker":
			marker = selected.get("marker")
			if isinstance(marker, dict):
				marker_phase = marker.get("phase", "")
				if isinstance(marker_phase, str):
					authoritative_phase = PHASE_FAILURE_PHASE_TO_PHASE_LABEL.get(marker_phase, authoritative_phase)
		elif selected_source == "current_labels":
			phase_raw = selected.get("phase", "")
			if isinstance(phase_raw, str) and phase_raw:
				authoritative_phase = phase_raw
		if selected_source == "phase_failure_marker" and current_phase in TERMINAL_PHASES:
			authoritative_phase = current_phase

	return {
		"authoritative_phase": authoritative_phase,
		"selected_evidence_source": selected_source,
		"selected_action": selected_action,
		"discarded_markers": discarded_markers,
	}


def choose_most_advanced_conclusive_evidence(
	evidence_items: list[dict[str, Any]],
) -> dict[str, Any]:
	"""Select the most advanced conclusive evidence item and list stale ones.

	Evidence items should include at least an ``action`` key and may optionally
	include ``timestamp`` and ``comment_id`` for deterministic tie-breaking.
	Items with unknown actions or ``conclusive == False`` are treated as stale.
	"""
	candidates: list[dict[str, Any]] = []
	discarded: list[dict[str, Any]] = []

	for raw_item in evidence_items:
		if not isinstance(raw_item, dict):
			continue
		item = dict(raw_item)
		action = item.get("action")
		conclusive = bool(item.get("conclusive", True))
		if not isinstance(action, str) or action not in STALL_RECOVERY_ACTION_PRIORITY or not conclusive:
			discarded.append(item)
			continue
		candidates.append(item)

	def _sort_key(item: dict[str, Any]) -> tuple[int, str, int]:
		action = str(item.get("action", ""))
		rank = STALL_RECOVERY_ACTION_PRIORITY.get(action, -1)
		ts = str(item.get("timestamp", "") or "")
		raw_comment_id = item.get("comment_id", 0)
		try:
			comment_id = int(raw_comment_id)
		except (TypeError, ValueError):
			comment_id = 0
		return rank, ts, comment_id

	selected: dict[str, Any] | None = None
	if candidates:
		selected = max(candidates, key=_sort_key)
		selected_id = id(selected)
		for item in candidates:
			if id(item) != selected_id:
				discarded.append(item)

	discarded_sorted = sorted(
		discarded,
		key=lambda item: (
			str(item.get("timestamp", "") or ""),
			_stable_int(item.get("comment_id"), 0),
		),
		reverse=True,
	)

	return {
		"selected": selected,
		"discarded": discarded_sorted,
	}


def _nearest_non_human_stall_action(actions: list[str], start_idx: int) -> str | None:
	"""Return the nearest prior non-human action, if any."""
	if not actions:
		return None
	start = min(start_idx, len(actions) - 1)
	for idx in range(start, -1, -1):
		action = actions[idx]
		if isinstance(action, str) and action and action != "escalate_human":
			return action
	return None


def _phase_specific_max_recoveries(
	phase: str,
	max_recoveries: int,
	max_recoveries_by_phase: dict[str, int] | None,
) -> int:
	"""Return the effective recovery cap for ``phase``.

	Per-phase caps allow operators to keep the global cap (default 5) tight
	for cheap recovery actions (auto_respond_clarify, retrigger_plan) while
	exempting expensive phases — e.g. ``ai:done``, where each "recovery" is
	a fresh review-autofix run that itself takes ≥10 min, so a uniform 5×
	cap can hard-close a PR after ~25 minutes of cron ticks even when the
	autofix loop is making real progress.
	"""
	if not isinstance(max_recoveries_by_phase, dict):
		return max_recoveries
	override = max_recoveries_by_phase.get(phase)
	if not isinstance(override, int) or override < 1:
		return max_recoveries
	return override


def resolve_stall_recovery_action(
	phase: str,
	recovery_count: int,
	max_recoveries: int = 5,
	enable_stall_human_terminalization: bool = False,
	actions_by_phase: dict[str, list[str]] | None = None,
	fallback_action: str = "retrigger_pipeline",
	max_recoveries_by_phase: dict[str, int] | None = None,
	phase_attempts_count: int = 0,
) -> str:
	"""Resolve the declarative stall recovery action for a phase/recovery count.

	``phase_attempts_count`` is a sibling counter to ``recovery_count`` that
	survives phase oscillation (see :func:`update_issue_timestamps`, which
	zeroes ``stall_recovery_count`` on phase change but leaves
	``phase_attempts`` untouched).  When an autofix run flaps a label
	transiently — e.g. ai:review-blocked -> ai:done -> ai:review-blocked —
	the per-recovery counter restarts at 0 each time, so the ladder would
	otherwise run forever.  Capping on either counter bounds the lifetime
	work done for one phase.
	"""
	effective_max = _phase_specific_max_recoveries(
		phase, max_recoveries, max_recoveries_by_phase
	)
	if recovery_count >= effective_max:
		return "skip"
	if phase_attempts_count >= effective_max:
		return "skip"

	recovery_idx = max(recovery_count, 0)
	ladders = actions_by_phase if actions_by_phase is not None else STALL_RECOVERY_ACTIONS
	actions = ladders.get(phase, [fallback_action])
	if not isinstance(actions, list) or not actions:
		return fallback_action

	action_idx = min(recovery_idx, len(actions) - 1)
	action = actions[action_idx]
	if not isinstance(action, str) or not action:
		return fallback_action

	if action == "escalate_human" and not enable_stall_human_terminalization:
		prior_non_human = _nearest_non_human_stall_action(actions, action_idx - 1)
		if prior_non_human:
			return prior_non_human
		return fallback_action
	return action


def resolve_effective_stall_recovery_action(
	phase: str,
	recovery_count: int,
	candidate_action: str | None,
	max_recoveries: int = 5,
	enable_stall_human_terminalization: bool = False,
	max_recoveries_by_phase: dict[str, int] | None = None,
	phase_attempts_count: int = 0,
) -> str:
	"""Normalize a candidate action (e.g. stall judge output) into a safe action."""
	fallback_action = resolve_stall_recovery_action(
		phase,
		recovery_count,
		max_recoveries=max_recoveries,
		enable_stall_human_terminalization=enable_stall_human_terminalization,
		max_recoveries_by_phase=max_recoveries_by_phase,
		phase_attempts_count=phase_attempts_count,
	)

	if not isinstance(candidate_action, str) or not candidate_action:
		return fallback_action
	if candidate_action not in VALID_STALL_RECOVERY_ACTIONS:
		return fallback_action
	if candidate_action == "escalate_human" and not enable_stall_human_terminalization:
		return fallback_action
	return candidate_action


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
	if determine_phase(labels) in TERMINAL_PHASES:
		return "closed", "label_terminal_phase"
	return "in_progress", "default_in_progress"


def is_terminal_wave_issue_status(status: str | None) -> bool:
	"""Return whether a wave issue status is terminal for dependency gating."""
	return str(status or "").strip() in BLOCKER_TERMINAL_WAVE_STATUSES


def is_terminal_wave_issue(
	issue: dict[str, Any],
	labels: list[str],
	issue_state: str | None = None,
	pr_state: str | None = None,
	pr_merged: bool | None = None,
) -> tuple[bool, str, str]:
	"""Return blocker-terminality using the shared reconciliation model.

	The returned tuple is ``(terminal, status, source)`` where ``status`` and
	``source`` come from :func:`reconcile_wave_issue_status`.
	"""
	status, source = reconcile_wave_issue_status(
		issue,
		labels,
		issue_state=issue_state,
		pr_state=pr_state,
		pr_merged=pr_merged,
	)
	terminal = is_terminal_wave_issue_status(status)
	if status == "not_created" and source == "no_github_issue":
		terminal = False
	return terminal, status, source


def _task_state_files_enabled() -> bool:
	return os.environ.get("ORCH_TASK_FILES_ENABLED", "").strip().lower() == "true"


def _log_task_state_write_fail(issue_id: Any, reason: str) -> None:
	issue_token = str(issue_id or "unknown")
	reason_token = " ".join(str(reason).split()) or "unknown"
	print(f"TASK_STATE_WRITE_FAIL {issue_token} {reason_token}", file=sys.stderr)


def _maybe_unblock_task_state_dependents(wave_id: Any, issue: dict[str, Any]) -> None:
	if not _task_state_files_enabled():
		return
	resolved_status = issue.get("_task_state_resolved_status")
	if str(resolved_status or "").strip() not in TRACKING_BODY_CHECKED_STATUSES:
		return

	completed_issue_id = issue.get("id")
	if completed_issue_id in (None, ""):
		return

	try:
		import task_state as task_state_module
	except Exception as exc:
		_log_task_state_write_fail(completed_issue_id, f"import_failed:{exc}")
		return

	try:
		unblock_dependents = task_state_module.unblock_dependents
		if "completed_issue_payload" in inspect.signature(unblock_dependents).parameters:
			unblock_dependents(
				wave_id,
				completed_issue_id,
				completed_issue_payload=issue,
			)
		else:
			unblock_dependents(wave_id, completed_issue_id)
	except Exception as exc:
		_log_task_state_write_fail(completed_issue_id, f"unblock_failed:{exc}")


# Phases whose stall clock is re-anchored to
# `max(status_since_ts, headPushedAt_epoch)` when a linked-PR push
# timestamp is available.  Currently only `ai:done` per Q2=A on the
# review-consumer-repo-issue-7IQNj investigation: during a multi-cycle
# review_autofix loop the phase stays `ai:done` while commits/reviewer
# runs land every 35-45 min, so `status_since_ts`-only elapsed grows
# monotonically past the 120-min threshold and the detector fires every
# cycle.  Other phases keep the legacy `status_since_ts`-only anchor.
_PHASES_WITH_PUSH_REANCHOR: frozenset[str] = frozenset({"ai:done"})


def _parse_iso8601_to_epoch(iso_str: Any) -> int | None:
	"""Parse an ISO 8601 timestamp into Unix epoch seconds.

	Returns None on empty input, wrong type, or any parse error.
	Tolerates the trailing 'Z' UTC suffix and microseconds (both are
	emitted by GitHub's GraphQL `pushedDate` / `committedDate` fields).
	Naive datetimes are interpreted as UTC, matching GitHub's contract.
	"""
	if not isinstance(iso_str, str) or not iso_str:
		return None
	try:
		s = iso_str
		if s.endswith("Z"):
			s = s[:-1] + "+00:00"
		dt = datetime.fromisoformat(s)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
		return int(dt.timestamp())
	except (ValueError, TypeError, OverflowError, OSError):
		return None


def detect_stalls(
	state: dict[str, Any],
	issue_labels: dict[str, list[str]],
	threshold_minutes: int,
	now_ts: int,
	max_recoveries: int = 5,
	phase_thresholds: dict[str, int] | None = None,
	stall_judge_trigger_count: int = 2,
	enable_stall_judge: bool = True,
	enable_stall_human_terminalization: bool = False,
	max_recoveries_by_phase: dict[str, int] | None = None,
	head_pushed_at: dict[str, str] | None = None,
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

	*head_pushed_at* maps stringified GitHub issue numbers to the linked
	PR's most recent head push timestamp (ISO 8601).  For phases listed
	in `_PHASES_WITH_PUSH_REANCHOR` the stall clock is re-anchored to
	`max(status_since_ts, head_pushed_at_epoch)` so an in-flight
	review/autofix loop (which leaves the phase label unchanged across
	cycles) is not repeatedly flagged as stalled.  Missing, null, or
	unparseable timestamps fall back to the legacy `status_since_ts`
	anchor (fail-open).  Future-dated timestamps are clamped at
	*now_ts*, so a skewed future timestamp is treated as fresh until
	wall-clock time reaches that timestamp, after which stall detection
	resumes.

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

		effective_anchor = status_since
		if head_pushed_at and phase in _PHASES_WITH_PUSH_REANCHOR:
			raw_pushed = head_pushed_at.get(str(gh_num))
			pushed_epoch = _parse_iso8601_to_epoch(raw_pushed)
			if pushed_epoch is not None:
				pushed_epoch = min(pushed_epoch, now_ts)
				effective_anchor = max(status_since, pushed_epoch)

		elapsed = now_ts - effective_anchor
		if elapsed < threshold_secs:
			continue

		raw_recovery_count = issue.get("stall_recovery_count", 0)
		try:
			recovery_count = int(raw_recovery_count or 0)
		except (TypeError, ValueError):
			recovery_count = 0
		recovery_count = max(0, recovery_count)

		raw_phase_attempts = issue.get("phase_attempts", {})
		if isinstance(raw_phase_attempts, dict):
			try:
				phase_attempts_count = int(raw_phase_attempts.get(phase, 0) or 0)
			except (TypeError, ValueError):
				phase_attempts_count = 0
		else:
			phase_attempts_count = 0
		phase_attempts_count = max(0, phase_attempts_count)
		effective_max = _phase_specific_max_recoveries(
			phase, max_recoveries, max_recoveries_by_phase
		)

		# Determine recovery action
		if recovery_count >= effective_max:
			action = "skip"
		elif phase_attempts_count >= effective_max:
			action = "skip"
		elif enable_stall_judge and stall_judge_trigger_count >= 1 and recovery_count >= stall_judge_trigger_count:
			action = RUN_STALL_JUDGE_ACTION
		else:
			action = resolve_stall_recovery_action(
				phase,
				recovery_count,
				max_recoveries=max_recoveries,
				enable_stall_human_terminalization=enable_stall_human_terminalization,
				max_recoveries_by_phase=max_recoveries_by_phase,
				phase_attempts_count=phase_attempts_count,
			)

		stalled.append({
			"id": issue["id"],
			"github_issue": gh_num,
			"phase": phase,
			"recovery_action": action,
			"stall_duration_minutes": int(elapsed / 60),
			"stall_recovery_count": recovery_count,
			"phase_attempts_count": phase_attempts_count,
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
	phase: str | None = None,
) -> dict[str, Any]:
	"""Increment the stall recovery counter for *issue_id* in the current wave.

	Also resets ``status_since_ts`` to now so the threshold restarts.
	When *phase* is supplied, additionally bumps a phase-scoped lifetime
	counter at ``issue["phase_attempts"][phase]``.  Unlike
	``stall_recovery_count``, which :func:`update_issue_timestamps` zeroes
	on phase change, ``phase_attempts`` is never reset by phase oscillation,
	so it caps re-issue loops where an autofix run transiently flips a
	label (e.g. ai:review-blocked -> ai:done -> ai:review-blocked).
	Mutates *state* in-place.
	"""
	current_wave_idx = state.get("current_wave", 1) - 1
	waves = state.get("waves", [])
	if current_wave_idx >= len(waves):
		return state

	now_ts = int(time.time())
	for issue in waves[current_wave_idx]["issues"]:
		if issue.get("id") == issue_id:
			raw_recovery_count = issue.get("stall_recovery_count", 0)
			try:
				recovery_count = int(raw_recovery_count or 0)
			except (TypeError, ValueError):
				print(f"::warning::Malformed stall_recovery_count for issue {issue.get('id')}; resetting to 0", file=sys.stderr)
				recovery_count = 0
			issue["stall_recovery_count"] = max(0, recovery_count) + 1
			issue["status_since_ts"] = now_ts
			if phase:
				raw_phase_attempts = issue.get("phase_attempts")
				if not isinstance(raw_phase_attempts, dict):
					raw_phase_attempts = {}
					issue["phase_attempts"] = raw_phase_attempts
				try:
					prev_phase_count = int(raw_phase_attempts.get(phase, 0) or 0)
				except (TypeError, ValueError):
					print(f"::warning::Malformed phase_attempts[{phase}] for issue {issue.get('id')}; resetting to 0", file=sys.stderr)
					prev_phase_count = 0
				raw_phase_attempts[phase] = max(0, prev_phase_count) + 1
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

def extract_integration_branch(body: str) -> str:
	"""Extract integration-branch metadata from markdown body text."""
	if not body:
		return ""
	match = INTEGRATION_BRANCH_LINE_RE.search(body)
	if not match:
		return ""
	return match.group(1).strip()


def extract_tracking_issue_number(body: str) -> int | None:
	"""Extract the first tracking-issue number from markdown body text."""
	if not body:
		return None
	match = TRACKING_ISSUE_LINE_RE.search(body)
	if not match:
		return None
	return int(match.group(1))


def _gh_api_json(endpoint: str) -> dict[str, Any]:
	env = os.environ.copy()
	token = env.get("GH_TOKEN", "")
	if token and not env.get("GITHUB_TOKEN"):
		env["GITHUB_TOKEN"] = token

	try:
		proc = subprocess.run(
			["gh", "api", endpoint],
			check=False,
			capture_output=True,
			text=True,
			env=env,
		)
	except OSError as exc:
		raise OrchestrateError(f"gh api failed for {endpoint}: {exc}") from exc
	if proc.returncode != 0:
		error_text = (proc.stderr or proc.stdout).strip()
		raise OrchestrateError(f"gh api failed for {endpoint}: {error_text}")

	try:
		payload = json.loads(proc.stdout or "{}")
	except json.JSONDecodeError as exc:
		raise OrchestrateError(f"Invalid JSON from gh api ({endpoint}): {exc}") from exc
	if not isinstance(payload, dict):
		raise OrchestrateError(f"Expected JSON object from gh api ({endpoint})")
	return payload


def _gh_ref_exists(repo: str, ref_name: str) -> bool:
	encoded_ref = quote(ref_name, safe="")
	endpoint = f"repos/{repo}/git/ref/heads/{encoded_ref}"
	env = os.environ.copy()
	token = env.get("GH_TOKEN", "")
	if token and not env.get("GITHUB_TOKEN"):
		env["GITHUB_TOKEN"] = token

	try:
		proc = subprocess.run(
			["gh", "api", endpoint],
			check=False,
			capture_output=True,
			text=True,
			env=env,
		)
	except OSError as exc:
		raise OrchestrateError(f"gh api failed while checking branch ref {ref_name!r}: {exc}") from exc
	if proc.returncode == 0:
		return True

	error_text = (proc.stderr or proc.stdout).strip()
	if "404" in error_text or "Not Found" in error_text:
		return False

	raise OrchestrateError(f"gh api failed while checking branch ref {ref_name!r}: {error_text}")


def resolve_integration_ref(repo: str, issue: int) -> str:
	"""Resolve integration branch from child issue, falling back to tracking issue."""
	repo_name = (repo or "").strip()
	if not repo_name:
		raise OrchestrateError("REPO (or GITHUB_REPOSITORY) is required")

	child_payload = _gh_api_json(f"repos/{repo_name}/issues/{issue}")
	child_body = str(child_payload.get("body", "") or "")
	child_branch = extract_integration_branch(child_body)
	if child_branch:
		if not _gh_ref_exists(repo_name, child_branch):
			raise IntegrationBranchMissingError(
				f"Integration branch {child_branch!r} declared in child issue #{issue} does not exist"
			)
		return child_branch

	tracking_issue = extract_tracking_issue_number(child_body)
	if tracking_issue is None:
		return ""

	tracking_payload = _gh_api_json(f"repos/{repo_name}/issues/{tracking_issue}")
	tracking_body = str(tracking_payload.get("body", "") or "")
	tracking_branch = extract_integration_branch(tracking_body)
	if not tracking_branch:
		return ""
	if not _gh_ref_exists(repo_name, tracking_branch):
		raise IntegrationBranchMissingError(
			f"Integration branch {tracking_branch!r} declared in tracking issue #{tracking_issue} does not exist"
		)
	return tracking_branch

def parse_tracking_body(body: str) -> dict[str, Any]:
	"""Parse a tracking issue markdown body to extract wave structure.

	Returns a dict with keys:
		project_title, waves (list of lists of {id, title, priority, completed}),
		dependency_edges (list of {from, to}), integration_branch.
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

	result["integration_branch"] = extract_integration_branch(body)

	# Split on wave headers and parse each section
	wave_sections = re.split(r"### Wave \d+", body)
	for section in wave_sections[1:]:  # skip preamble before Wave 1
		issues: list[dict[str, Any]] = []
		for match in re.finditer(
			r"-\s*\[(?P<mark>[ xX])\]\s*\*\*(?P<id>[^*]+)\*\*:\s*(?P<title>.+?)\s*\(priority\s+(?P<priority>\d+)\)",
			section,
		):
			issues.append({
				"id": match.group("id").strip(),
				"title": match.group("title").strip(),
				"priority": int(match.group("priority")),
				# Whether the tracking body marks this sub-issue complete
				# ([x] or [X]).  rebuild_tracking_state consults this to refuse a
				# destructive from-scratch rebuild of a project that already
				# has finished work it cannot map (see ReconstructionUnsafeError).
				"completed": match.group("mark").strip().lower() == "x",
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

	# Defense-in-depth against the project #3627 failure mode.  If the tracking
	# body marks a sub-issue complete ([x]) but the discovered issue map does
	# not contain it, a from-scratch rebuild would reset current_wave to 1 and
	# re-create that finished issue as a duplicate — spawning a fresh
	# clarify/plan/implement/review run for already-merged work.  We cannot
	# faithfully represent a completed issue we are unable to map, so refuse the
	# rebuild rather than rewind.  The caller skips this tracking issue and
	# retries on the next poll cycle, by which point the state comment (or the
	# child-issue search that feeds issue_number_map) is usually readable again.
	#
	# The genuine recovery case reconstruction exists for — a project whose
	# issues were created but crashed before the first state comment was posted
	# — has no completed sub-issues yet (every checkbox is still [ ]), so it is
	# unaffected by this guard.
	unmapped_completed = sorted(
		issue["id"]
		for wave_issues in parsed["waves"]
		for issue in wave_issues
		if issue.get("completed") and issue["id"] not in issue_number_map
	)
	if unmapped_completed:
		raise ReconstructionUnsafeError(
			f"refusing to reconstruct state for tracking issue #{tracking_issue}: "
			f"the tracking body marks {len(unmapped_completed)} completed "
			f"sub-issue(s) absent from the discovered issue map "
			f"({', '.join(unmapped_completed)}); a from-scratch rebuild would "
			"rewind current_wave and duplicate finished work"
		)

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
		"tracking_body_sync_hash": tracking_body_sync_hash(body),
		"tracking_body_last_readiness_refresh_hash": tracking_body_sync_hash(body),
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


def cmd_render_tracking_body(args: argparse.Namespace) -> int:
	path = Path(args.state_file).resolve()
	with path.open("r", encoding="utf-8") as f:
		state = json.load(f)

	template_body: str | None = None
	if getattr(args, "template_body_file", None):
		template_path = Path(args.template_body_file).resolve()
		with template_path.open("r", encoding="utf-8") as f:
			template_body = f.read()

	body = render_tracking_issue_body_from_state(state, template_body=template_body)
	sys.stdout.write(body)
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
	wave_id = wave.get("wave", current_wave_idx + 1)
	all_merged = True
	any_failed = False
	validation_dispatch_safe_despite_failures = True
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
		issue_state_for_status = issue_state if issue_state in ("open", "closed") else None

		status, source = reconcile_wave_issue_status(
			issue=issue,
			labels=labels,
			issue_state=issue_state_for_status,
			pr_state=pr_state if pr_state in ("open", "closed") else None,
			pr_merged=pr_merged,
		)
		stored_status = issue.get("status")
		if (
			not is_terminal_wave_issue_status(stored_status)
			and is_terminal_wave_issue_status(status)
		):
			task_state_issue = dict(issue)
			task_state_issue["_task_state_resolved_status"] = status
			_maybe_unblock_task_state_dependents(wave_id, task_state_issue)
		if status == "review-blocked":
			any_review_blocked = True
		if status in ("closed", "implementation-failed"):
			any_failed = True
			has_blocking_failure_label = any(
				label in VALIDATION_DISPATCH_BLOCKING_FAILURE_LABELS
				for label in labels
			)
			failure_is_safe_for_validation_dispatch = (
				status == "closed"
				and (issue_state_for_status == "closed" or "ai:closed" in labels)
				and not has_blocking_failure_label
			)
			if not failure_is_safe_for_validation_dispatch:
				validation_dispatch_safe_despite_failures = False
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

	# Gate project_complete on the default branch containing the integration
	# branch tip. The shell caller computes ahead_by via the GitHub compare
	# API and passes it in as --integration-ahead-by. A value of "0" means
	# default contains the integration tip (project is genuinely complete).
	# Any non-"0" value (including "" from a fail-closed compare API error)
	# forces project_complete=False, so the orchestrator does not declare
	# completion while wave PRs remain stranded on the integration branch.
	# See shubhodeep1/binance-blessings#135 for the regression case this gate
	# prevents.
	integration_ahead_by_value = getattr(args, "integration_ahead_by", "0")
	integration_ahead_by_raw = "" if integration_ahead_by_value is None else str(integration_ahead_by_value).strip()
	integration_contained_in_default = integration_ahead_by_raw == "0"

	project_complete = (
		all_merged
		and (current_wave_idx + 1 >= len(waves))
		and integration_contained_in_default
	)

	_print_json({
		"ok": True,
		"wave": current_wave_idx + 1,
		"wave_complete": all_merged,
		"any_failed": any_failed,
		"validation_dispatch_safe_despite_failures": any_failed and validation_dispatch_safe_despite_failures,
		"any_review_blocked": any_review_blocked,
		"any_not_created": any_not_created,
		"project_complete": project_complete,
		"integration_ahead_by": integration_ahead_by_raw,
		"integration_contained_in_default": integration_contained_in_default,
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


def cmd_print_integration_ref(args: argparse.Namespace) -> int:
	"""Resolve and print integration branch metadata for a child issue."""
	repo = (args.repo or os.getenv("REPO") or os.getenv("GITHUB_REPOSITORY") or "").strip()
	issue = int(args.print_integration_ref)
	try:
		ref = resolve_integration_ref(repo=repo, issue=issue)
	except IntegrationBranchMissingError as exc:
		print(f"::error::{exc}", file=sys.stderr)
		raise
	print(ref)
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
	enable_stall_human_terminalization_raw = getattr(args, "enable_stall_human_terminalization", "false")
	if isinstance(enable_stall_human_terminalization_raw, bool):
		enable_stall_human_terminalization = enable_stall_human_terminalization_raw
	else:
		enable_stall_human_terminalization = str(enable_stall_human_terminalization_raw).lower() == "true"

	phase_thresholds: dict[str, int] | None = None
	if args.phase_thresholds_json:
		phase_thresholds = {
			k: int(v) for k, v in json.loads(args.phase_thresholds_json).items()
		}
	max_recoveries_by_phase: dict[str, int] | None = None
	if getattr(args, "max_recoveries_by_phase_json", None):
		max_recoveries_by_phase = {
			k: int(v) for k, v in json.loads(args.max_recoveries_by_phase_json).items()
		}

	head_pushed_at: dict[str, str] | None = None
	if getattr(args, "head_pushed_at_json", None):
		try:
			raw = json.loads(args.head_pushed_at_json)
		except (TypeError, ValueError, json.JSONDecodeError):
			raw = None
		if isinstance(raw, dict):
			head_pushed_at = {
				str(k): v
				for k, v in raw.items()
				if isinstance(v, str) and v
			}

	stalls = detect_stalls(
		state, issue_labels, threshold, now_ts, max_recoveries,
		phase_thresholds=phase_thresholds,
		stall_judge_trigger_count=stall_judge_trigger_count,
		enable_stall_judge=enable_stall_judge,
		enable_stall_human_terminalization=enable_stall_human_terminalization,
		max_recoveries_by_phase=max_recoveries_by_phase,
		head_pushed_at=head_pushed_at,
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


def cmd_concurrency_caps(args: argparse.Namespace) -> int:
	actions_runs_payload: dict[str, Any] = {"workflow_runs": []}
	actions_runs_file = getattr(args, "actions_runs_file", "-") or "-"
	try:
		if actions_runs_file == "-":
			raw_payload = sys.stdin.read()
		else:
			raw_payload = Path(actions_runs_file).read_text(encoding="utf-8")
	except (FileNotFoundError, PermissionError, OSError):
		raw_payload = ""

	if raw_payload.strip():
		try:
			parsed_payload = json.loads(raw_payload)
			if isinstance(parsed_payload, dict):
				actions_runs_payload = parsed_payload
		except json.JSONDecodeError:
			pass

	snapshot = build_concurrency_snapshot(
		actions_runs_payload,
		caps_path=args.caps_path,
		threshold_minutes=args.threshold_minutes,
		implementing_threshold_minutes=args.implementing_threshold_minutes,
		now_ts=args.now_ts,
	)
	print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
	return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Orchestrator helper utilities")
	parser.add_argument(
		"--print-integration-ref",
		dest="print_integration_ref",
		help="Resolve integration branch for child issue number",
	)
	parser.add_argument(
		"--repo",
		default="",
		help="GitHub repository owner/name (defaults to REPO or GITHUB_REPOSITORY)",
	)
	subparsers = parser.add_subparsers(dest="command")

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

	p_render = subparsers.add_parser("render-tracking-body", help="Render the live tracking issue body from orchestrator state")
	p_render.add_argument("--state-file", required=True)
	p_render.add_argument(
		"--template-body-file",
		default=None,
		help="Optional body template path. Defaults to .project_body_snapshot in the state file.",
	)
	p_render.set_defaults(func=cmd_render_tracking_body)

	p_next = subparsers.add_parser("next-wave", help="Get next wave to dispatch")
	p_next.add_argument("--state-file", required=True)
	p_next.set_defaults(func=cmd_next_wave)

	p_check = subparsers.add_parser("check-wave-status", help="Check current wave completion")
	p_check.add_argument("--state-file", required=True)
	p_check.add_argument("--labels-json", required=True, help='JSON: {"issue_num": ["label1", ...]}')
	p_check.add_argument("--issue-states-json", default=None, help='Optional JSON: {"issue_num": "open|closed", ...}')
	p_check.add_argument("--pr-states-json", default=None, help='Optional JSON: {"issue_num": {"state":"open|closed","merged":bool}, ...}')
	p_check.add_argument(
		"--integration-ahead-by",
		default="0",
		help=(
			"Optional integer 'ahead_by' count of the integration branch vs the "
			"default branch (from GitHub's compare API). Default '0' = the "
			"default branch contains the integration tip (project is genuinely "
			"complete). Any non-'0' value (including the empty string, which "
			"callers should pass to fail closed on a compare API error) forces "
			"project_complete=False so the orchestrator does not declare "
			"completion while wave PRs remain stranded on the integration "
			"branch. See shubhodeep1/binance-blessings#135 for the regression "
			"this gate prevents."
		),
	)
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
	p_stalls.add_argument("--max-recoveries-by-phase-json", default=None, help='Optional JSON: {"ai:done": 99, ...}. Per-phase recovery-cap overrides.')
	p_stalls.add_argument("--stall-judge-trigger-count", default="2", help="Recovery-count threshold to switch stall recovery to run_stall_judge")
	p_stalls.add_argument("--enable-stall-judge", default="true", choices=("true", "false"), help="Enable/disable stall judge escalation action")
	p_stalls.add_argument(
		"--enable-stall-human-terminalization",
		"--allow-human-terminalization",
		default="false",
		choices=("true", "false"),
		help="Allow terminal escalate_human actions in the stall recovery ladder",
	)
	p_stalls.add_argument("--now-ts", default=None, help="Current epoch seconds (default: now)")
	p_stalls.add_argument(
		"--head-pushed-at-json",
		default=None,
		help=(
			'Optional JSON: {"<issue_num>": "<ISO 8601 push timestamp>", ...} '
			"mapping wave issues to the linked PR's most recent head push "
			"time.  For phases that opt in (currently only ai:done), the "
			"stall clock is re-anchored to max(status_since_ts, "
			"headPushedAt_epoch), so a multi-cycle review_autofix loop "
			"(which leaves the phase label unchanged across cycles) is "
			"not repeatedly flagged as stalled.  Missing, null, or "
			"unparseable entries fail open to the legacy status_since_ts "
			"anchor.  Future-dated timestamps are clamped at --now-ts, "
			"so a fixed future timestamp stays fresh only until wall-clock "
			"time reaches it."
		),
	)
	p_stalls.set_defaults(func=cmd_check_stalls)

	p_ts = subparsers.add_parser("update-timestamps", help="Update issue phase timestamps in state")
	p_ts.add_argument("--state-file", required=True)
	p_ts.add_argument("--labels-json", required=True, help='JSON: {"issue_num": ["label1", ...]}')
	p_ts.add_argument("--now-ts", default=None, help="Current epoch seconds (default: now)")
	p_ts.set_defaults(func=cmd_update_timestamps)

	p_caps = subparsers.add_parser("concurrency-caps", help="Load optional phase concurrency caps and count active workflow runs")
	p_caps.add_argument("--caps-path", default=DEFAULT_CONCURRENCY_CAPS_PATH)
	p_caps.add_argument("--actions-runs-file", default="-", help="Actions runs JSON file path (default: stdin)")
	p_caps.add_argument("--threshold-minutes", type=int, default=120)
	p_caps.add_argument("--implementing-threshold-minutes", type=int, default=None)
	p_caps.add_argument("--now-ts", type=int, default=None)
	p_caps.set_defaults(func=cmd_concurrency_caps)

	return parser


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()
	args = parser.parse_args(argv)
	if args.print_integration_ref and getattr(args, "command", None):
		parser.error("--print-integration-ref cannot be combined with a subcommand")
	if args.print_integration_ref:
		try:
			return int(cmd_print_integration_ref(args))
		except IntegrationBranchMissingError:
			return 1
		except (OrchestrateError, ValueError, json.JSONDecodeError) as exc:
			print(f"ORCHESTRATE_ERROR: {exc}", file=sys.stderr)
			return 2
	if not hasattr(args, "func"):
		parser.error("a command is required unless --print-integration-ref is used")

	try:
		return int(args.func(args))
	except (OrchestrateError, ValueError, json.JSONDecodeError) as exc:
		print(f"ORCHESTRATE_ERROR: {exc}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
