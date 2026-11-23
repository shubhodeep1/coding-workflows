#!/usr/bin/env bash
# drift_audit.sh — Scan recent review/autofix logs for persistent fingerprint drift.

set -euo pipefail

DRIFT_AUDIT_ENABLED="${DRIFT_AUDIT_ENABLED:-false}"
case "$(printf '%s' "${DRIFT_AUDIT_ENABLED}" | tr '[:upper:]' '[:lower:]')" in
	1|true|yes|on)
		;;
	*)
		echo "drift-audit: DRIFT_AUDIT_ENABLED=${DRIFT_AUDIT_ENABLED}; skipping."
		exit 0
		;;
esac

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
[[ "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]] || {
	echo "GITHUB_REPOSITORY must be in owner/repo format" >&2
	exit 1
}
command -v gh >/dev/null 2>&1 || {
	echo "gh is required but not installed" >&2
	exit 1
}
command -v python3 >/dev/null 2>&1 || {
	echo "python3 is required but not installed" >&2
	exit 1
}
DRIFT_AUDIT_HAVE_JQ="false"
if command -v jq >/dev/null 2>&1; then
	DRIFT_AUDIT_HAVE_JQ="true"
else
	echo "::warning::drift-audit: jq not found; notifications will omit run counts." >&2
fi
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

DRIFT_AUDIT_SUMMARY_FILE=""
if ! DRIFT_AUDIT_SUMMARY_FILE="$(mktemp "${TMPDIR:-/tmp}/drift-audit-summary.XXXXXX" 2>/dev/null)"; then
	echo "::warning::drift-audit: could not create temp summary file; notifications will omit run counts." >&2
fi
export DRIFT_AUDIT_SUMMARY_FILE
trap '[ -n "${DRIFT_AUDIT_SUMMARY_FILE:-}" ] && rm -f "${DRIFT_AUDIT_SUMMARY_FILE}" 2>/dev/null || true' EXIT

set +e
python3 - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from typing import Any

WORKFLOW_FILES = ("review_autofix.yml", "internal-review.yml")
LOOKBACK_HOURS = 24
RUN_LIMIT_PER_WORKFLOW = 100
PERSISTENT_RUN_THRESHOLD = 2
TRACKER_MARKER_PREFIX = "drift-audit:cluster-key="
JSON_DECODER = json.JSONDecoder()
# Completed runs with these conclusions routinely upload no logs at all —
# concurrency-superseded review runs are cancelled on every active PR branch,
# so a failed log fetch for them is expected and must not degrade the run's
# coverage verdict to "partial" (which escalates the Telegram alert to
# WARNING). Their logs are still scanned whenever the fetch succeeds.
UNSCANNABLE_CONCLUSIONS = frozenset({"cancelled", "skipped"})

# Repository checkout root. The audit only files a tracker when a
# fingerprint names a file that actually exists here, which keeps markers
# harvested from test fixtures or echoed PR diffs from opening issues.
# GitHub Actions sets GITHUB_WORKSPACE to the checkout directory.
_REPO_ROOT = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
_REPO_ROOT_REAL = os.path.realpath(_REPO_ROOT)


def _log(message: str) -> None:
	print(f"drift-audit: {message}", flush=True)


def _warn(message: str) -> None:
	print(f"::warning::drift-audit: {message}", flush=True)


def _notice(message: str) -> None:
	print(f"::notice::drift-audit: {message}", flush=True)


# Machine-readable run summary. Populated by main() and flushed by
# _write_summary() to DRIFT_AUDIT_SUMMARY_FILE so the shell wrapper can
# build the per-run Telegram alert and GitHub Actions job summary.
_RESULT: dict[str, Any] = {
	"status": "unknown",
	"processed_runs": 0,
	"marker_occurrences": 0,
	"persistent_clusters": 0,
	"skipped_absent_path": 0,
	"coverage_status": "unknown",
	"logs_fetched": 0,
	"logs_missing": 0,
	"missing_run_ids": [],
	"logs_unscannable": 0,
	"unscannable_run_ids": [],
	"created": 0,
	"edited": 0,
	"closed": 0,
	"suppressed": 0,
}


def _write_summary() -> None:
	path = os.environ.get("DRIFT_AUDIT_SUMMARY_FILE")
	if not path:
		return
	try:
		with open(path, "w", encoding="utf-8") as handle:
			json.dump(_RESULT, handle, separators=(",", ":"))
	except OSError as exc:
		_warn(f"could not write run summary to {path}: {exc}")


def _sorted_unique_run_ids(run_ids: list[str]) -> list[str]:
	return sorted(
		{run_id for run_id in run_ids if run_id},
		key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
	)


def _emit_coverage_summary(
	scanned_runs: int,
	fetched_logs: int,
	missing_run_ids: list[str],
	unscannable_run_ids: list[str],
) -> None:
	unique_missing_run_ids = _sorted_unique_run_ids(missing_run_ids)
	unique_unscannable_run_ids = _sorted_unique_run_ids(unscannable_run_ids)
	logs_missing = len(unique_missing_run_ids)
	logs_unscannable = len(unique_unscannable_run_ids)
	coverage_status = "partial" if logs_missing > 0 else "full"
	_RESULT["coverage_status"] = coverage_status
	_RESULT["logs_fetched"] = fetched_logs
	_RESULT["logs_missing"] = logs_missing
	_RESULT["missing_run_ids"] = unique_missing_run_ids
	_RESULT["logs_unscannable"] = logs_unscannable
	_RESULT["unscannable_run_ids"] = unique_unscannable_run_ids
	missing_run_ids_text = ",".join(unique_missing_run_ids) if unique_missing_run_ids else "none"
	unscannable_run_ids_text = ",".join(unique_unscannable_run_ids) if unique_unscannable_run_ids else "none"
	print(
		f"DRIFT_AUDIT_COVERAGE scanned={scanned_runs} fetched={fetched_logs} "
		f"missing={logs_missing} missing_run_ids={missing_run_ids_text} "
		f"unscannable={logs_unscannable} unscannable_run_ids={unscannable_run_ids_text}",
		flush=True,
	)


def _run_gh(args: list[str]) -> tuple[int, str, str]:
	proc = subprocess.run(
		["gh", *args],
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
	)
	return proc.returncode, proc.stdout, proc.stderr


def _gh_json(args: list[str], *, context: str, default: Any) -> Any:
	rc, stdout, stderr = _run_gh(args)
	if rc != 0:
		message = stderr.strip() or stdout.strip() or f"gh exited {rc}"
		_warn(f"{context} failed: {message}")
		return default
	payload = stdout.strip()
	if not payload:
		return default
	try:
		return json.loads(payload)
	except JSONDecodeError as exc:
		_warn(f"{context} returned invalid JSON: {exc}")
		return default


def _gh_ok(args: list[str], *, context: str) -> bool:
	rc, stdout, stderr = _run_gh(args)
	if rc == 0:
		return True
	message = stderr.strip() or stdout.strip() or f"gh exited {rc}"
	_warn(f"{context} failed: {message}")
	return False


def _parse_dt(value: str | None) -> datetime | None:
	if not value:
		return None
	try:
		return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
	except ValueError:
		return None


def _normalize_fp_key(parts: Any) -> str | None:
	if not isinstance(parts, list) or not parts or not all(isinstance(part, str) for part in parts):
		return None
	return json.dumps(parts, separators=(",", ":"))


def _decode_json_at(text: str, start: int) -> tuple[Any | None, int]:
	try:
		value, offset = JSON_DECODER.raw_decode(text[start:])
	except JSONDecodeError:
		return None, start
	return value, start + offset


def _consume_json_string(text: str, start: int, prefix: str) -> tuple[str | None, int]:
	if not text[start:].startswith(prefix):
		return None, start
	value, end = _decode_json_at(text, start + len(prefix))
	if not isinstance(value, str):
		return None, start
	return value, end


def _parse_pre_existing_marker(line: str, run: dict[str, Any]) -> dict[str, Any] | None:
	if "PRE_EXISTING_FINGERPRINT_DRIFT_V1" not in line:
		return None
	status_match = re.search(r"\bPRE_EXISTING_FINGERPRINT_DRIFT_V1\s+(?P<status>unchanged|fixed_by_resolver)\b", line)
	if not status_match:
		return None
	status = status_match.group("status")
	fp_start = line.find("fp_key=")
	if fp_start == -1:
		return None
	fp_value, fp_end = _decode_json_at(line, fp_start + len("fp_key="))
	fp_key = _normalize_fp_key(fp_value)
	if fp_key is None:
		return None
	issue_match = re.match(r"\s+issue=#(?P<issue>\d+)", line[fp_end:])
	if not issue_match:
		return None
	position = fp_end + issue_match.end()
	path, position = _consume_json_string(line, position, " path=")
	if path is None:
		return None
	pattern, pattern_end = _consume_json_string(line, position, " pattern=")
	if pattern is not None:
		position = pattern_end
	kind = None
	if status == "unchanged":
		kind_match = re.match(r"\s+kind=(?P<kind>\S+)", line[position:])
		kind = kind_match.group("kind") if kind_match else None
	return {
		"run_id": str(run["run_id"]),
		"workflow_file": run["workflow_file"],
		"created_at": run.get("created_at"),
		"url": run.get("url"),
		"issue_number": int(issue_match.group("issue")),
		"fp_key": fp_key,
		"fp_key_parts": list(fp_value),
		"marker_type": "PRE_EXISTING_FINGERPRINT_DRIFT_V1",
		"pre_existing_status": status,
		"path": path,
		"pattern": pattern,
		"kind": kind,
	}


def _parse_quarantined_marker(line: str, run: dict[str, Any]) -> dict[str, Any] | None:
	if "FINGERPRINT_QUARANTINED_V1" not in line:
		return None
	fp_start = line.find("fp_key=")
	if fp_start == -1:
		return None
	fp_value, fp_end = _decode_json_at(line, fp_start + len("fp_key="))
	fp_key = _normalize_fp_key(fp_value)
	if fp_key is None:
		return None
	issue_match = re.match(r"\s+issue=#(?P<issue>\d+)", line[fp_end:])
	if not issue_match:
		return None
	return {
		"run_id": str(run["run_id"]),
		"workflow_file": run["workflow_file"],
		"created_at": run.get("created_at"),
		"url": run.get("url"),
		"issue_number": int(issue_match.group("issue")),
		"fp_key": fp_key,
		"fp_key_parts": list(fp_value),
		"marker_type": "FINGERPRINT_QUARANTINED_V1",
		"pre_existing_status": None,
		"path": None,
		"pattern": None,
		"kind": None,
	}


def _parse_run_markers(run: dict[str, Any], log_text: str) -> list[dict[str, Any]]:
	occurrences: list[dict[str, Any]] = []
	seen: set[tuple[str, int, str, str]] = set()
	for line in log_text.splitlines():
		occurrence = _parse_pre_existing_marker(line, run)
		if occurrence is None:
			occurrence = _parse_quarantined_marker(line, run)
		if occurrence is None:
			continue
		key = (
			occurrence["run_id"],
			int(occurrence["issue_number"]),
			occurrence["fp_key"],
			occurrence["marker_type"],
		)
		if key in seen:
			continue
		seen.add(key)
		occurrences.append(occurrence)
	return occurrences


def _cluster_marker_hash(fp_key: str) -> str:
	return hashlib.sha256(fp_key.encode("utf-8")).hexdigest()


def _cluster_marker_body(fp_key: str) -> str:
	return f"<!-- {TRACKER_MARKER_PREFIX}{_cluster_marker_hash(fp_key)} -->"


def _build_clusters(occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]:
	clusters: dict[str, dict[str, Any]] = {}
	for occurrence in occurrences:
		cluster = clusters.setdefault(
			occurrence["fp_key"],
			{
				"fp_key": occurrence["fp_key"],
				"fp_key_parts": occurrence["fp_key_parts"],
				"path": None,
				"pattern": None,
				"kind": None,
				"pre_existing_statuses": set(),
				"issue_numbers": set(),
				"run_ids": set(),
				"marker_types": set(),
				"runs": {},
			},
		)
		cluster["issue_numbers"].add(int(occurrence["issue_number"]))
		cluster["run_ids"].add(occurrence["run_id"])
		cluster["marker_types"].add(occurrence["marker_type"])
		cluster["runs"][occurrence["run_id"]] = {
			"run_id": occurrence["run_id"],
			"workflow_file": occurrence["workflow_file"],
			"created_at": occurrence.get("created_at"),
			"url": occurrence.get("url"),
		}
		if cluster["path"] is None and occurrence.get("path"):
			cluster["path"] = occurrence["path"]
		if cluster["pattern"] is None and occurrence.get("pattern"):
			cluster["pattern"] = occurrence["pattern"]
		if cluster["kind"] is None and occurrence.get("kind"):
			cluster["kind"] = occurrence["kind"]
		if occurrence.get("pre_existing_status"):
			cluster["pre_existing_statuses"].add(occurrence["pre_existing_status"])

	for cluster in clusters.values():
		parts = cluster.get("fp_key_parts") or []
		if cluster["path"] is None and parts:
			cluster["path"] = parts[0]
		if cluster["pattern"] is None and len(parts) > 1:
			cluster["pattern"] = parts[1]
		cluster["pre_existing_statuses"] = sorted(cluster["pre_existing_statuses"])
		cluster["issue_numbers"] = sorted(cluster["issue_numbers"])
		cluster["run_ids"] = sorted(cluster["run_ids"])
		cluster["marker_types"] = sorted(cluster["marker_types"])
		cluster["runs"] = sorted(
			cluster["runs"].values(),
			key=lambda row: ((row.get("created_at") or ""), row["run_id"], row["workflow_file"]),
			reverse=True,
		)

	return [clusters[key] for key in sorted(clusters)]


def _is_persistent_cluster(cluster: dict[str, Any]) -> bool:
	return "FINGERPRINT_QUARANTINED_V1" in cluster["marker_types"] or len(cluster["run_ids"]) >= PERSISTENT_RUN_THRESHOLD


def _cluster_has_only_fixed_markers(cluster: dict[str, Any]) -> bool:
	if "FINGERPRINT_QUARANTINED_V1" in cluster.get("marker_types", []):
		return False
	statuses = set(cluster.get("pre_existing_statuses") or [])
	return "fixed_by_resolver" in statuses and "unchanged" not in statuses


def _cluster_path_in_repo(cluster: dict[str, Any]) -> bool:
	# A genuine fingerprint always names a file tracked in the repository.
	# Markers harvested from test fixtures or echoed PR diffs reference
	# synthetic paths (e.g. scripts/example.py) that are absent from the
	# checkout, so "path is not a file in the repo" is the signal that the
	# cluster is not actionable drift and must not open a tracker issue.
	path = cluster.get("path")
	if not isinstance(path, str) or not path:
		return False
	if os.path.isabs(path):
		return False
	candidate = os.path.realpath(os.path.join(_REPO_ROOT_REAL, path))
	try:
		return os.path.commonpath([_REPO_ROOT_REAL, candidate]) == _REPO_ROOT_REAL and os.path.isfile(candidate)
	except ValueError:
		return False


def _list_recent_runs(repo: str, workflow_file: str, cutoff: datetime) -> list[dict[str, Any]]:
	runs = _gh_json(
		[
			"run",
			"list",
			"--repo",
			repo,
			"--workflow",
			workflow_file,
			"--limit",
			str(RUN_LIMIT_PER_WORKFLOW),
			"--json",
			"databaseId,createdAt,status,conclusion,url,displayTitle",
		],
		context=f"workflow run list for {workflow_file}",
		default=[],
	)
	if not isinstance(runs, list):
		return []
	selected: list[dict[str, Any]] = []
	for run in runs:
		if not isinstance(run, dict):
			continue
		run_id = run.get("databaseId")
		if run_id is None:
			continue
		if str(run.get("status") or "") != "completed":
			continue
		created_at = run.get("createdAt")
		created_dt = _parse_dt(created_at)
		if created_dt is None or created_dt < cutoff:
			continue
		selected.append(
			{
				"run_id": str(run_id),
				"workflow_file": workflow_file,
				"created_at": created_at,
				"url": run.get("url"),
				"conclusion": str(run.get("conclusion") or ""),
			}
		)
	return selected


def _fetch_run_log(repo: str, run_id: str, *, log_expected: bool = True) -> str | None:
	rc, stdout, stderr = _run_gh(["run", "view", run_id, "--repo", repo, "--log"])
	if rc != 0:
		message = stderr.strip() or stdout.strip() or f"gh exited {rc}"
		if log_expected:
			_warn(f"run #{run_id} log fetch failed: {message}")
		else:
			_log(f"run #{run_id} has no fetchable log ({message}); cancelled/skipped runs upload none.")
		return None
	return stdout


def _tracker_issue_lookup(repo: str) -> dict[str, dict[str, Any]]:
	issues = _gh_json(
		[
			"issue",
			"list",
			"--repo",
			repo,
			"--state",
			"open",
			"--limit",
			"1000",
			"--json",
			"number,title,body,url",
		],
		context="open tracker issue snapshot",
		default=[],
	)
	lookup: dict[str, dict[str, Any]] = {}
	if not isinstance(issues, list):
		return lookup
	pattern = re.compile(r"<!--\s*" + re.escape(TRACKER_MARKER_PREFIX) + r"([0-9a-f]{64})\s*-->")
	for issue in issues:
		if not isinstance(issue, dict):
			continue
		body = str(issue.get("body") or "")
		match = pattern.search(body)
		if not match:
			continue
		marker_hash = match.group(1)
		existing = lookup.get(marker_hash)
		if existing is not None:
			try:
				if int(issue.get("number") or 0) < int(existing.get("number") or 0):
					lookup[marker_hash] = issue
			except (TypeError, ValueError):
				pass
			continue
		lookup[marker_hash] = issue
	return lookup


# Batch-fetch the latest merged PR for each issue number. Mirrors the
# orchestrator poller's alias-per-issue GraphQL pattern and fails open: a
# failed batch simply falls out of the cache so the caller leaves the tracker
# issue open instead of guessing.
def _fetch_latest_merged_linked_prs(repo: str, issue_numbers: list[int]) -> dict[str, dict[str, Any]]:
	if not issue_numbers:
		return {}
	owner, name = repo.split("/", 1)
	results: dict[str, dict[str, Any]] = {}
	batch_size = 25
	for start in range(0, len(issue_numbers), batch_size):
		batch = issue_numbers[start : start + batch_size]
		fragment = []
		for offset, issue_number in enumerate(batch, start=start):
			fragment.append(
				f'''\n\t\ti{offset}: issue(number: {issue_number}) {{\n'''
				"\t\t\tnumber\n"
				"\t\t\ttimelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT]) {\n"
				"\t\t\t\tnodes {\n"
				"\t\t\t\t\t... on CrossReferencedEvent {\n"
				"\t\t\t\t\t\tsource {\n"
				"\t\t\t\t\t\t\t__typename\n"
				"\t\t\t\t\t\t\t... on PullRequest {\n"
				"\t\t\t\t\t\t\t\tnumber\n"
				"\t\t\t\t\t\t\t\tmerged\n"
				"\t\t\t\t\t\t\t\tmergedAt\n"
				"\t\t\t\t\t\t\t}\n"
				"\t\t\t\t\t\t}\n"
				"\t\t\t\t\t}\n"
				"\t\t\t\t}\n"
				"\t\t\t}\n"
				"\t\t}\n"
			)
		query = f'''query {{\n\trepository(owner: "{owner}", name: "{name}") {{{"".join(fragment)}\n\t}}\n}}'''
		response = _gh_json(
			["api", "graphql", "-f", f"query={query}"],
			context=f"linked PR batch query for issues {batch[0]}-{batch[-1]}",
			default={},
		)
		repository = (((response or {}).get("data") or {}).get("repository") or {})
		if not isinstance(repository, dict):
			continue
		for value in repository.values():
			if not isinstance(value, dict):
				continue
			issue_number = value.get("number")
			if not isinstance(issue_number, int):
				continue
			latest: dict[str, Any] | None = None
			nodes = ((value.get("timelineItems") or {}).get("nodes") or [])
			for node in nodes:
				if not isinstance(node, dict):
					continue
				source = node.get("source")
				if not isinstance(source, dict):
					continue
				if source.get("__typename") != "PullRequest" or source.get("merged") is not True:
					continue
				pr_number = source.get("number")
				if not isinstance(pr_number, int):
					continue
				candidate = {
					"number": pr_number,
					"mergedAt": source.get("mergedAt") or "",
				}
				if latest is None:
					latest = candidate
					continue
				if (candidate["mergedAt"], candidate["number"]) >= (latest["mergedAt"], latest["number"]):
					latest = candidate
			if latest is not None:
				results[str(issue_number)] = latest
	return results


def _fetch_pr_files(repo: str, pr_number: int) -> list[dict[str, Any]] | None:
	files: list[dict[str, Any]] = []
	for page in range(1, 31):
		path = f"repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
		response = _gh_json(["api", path], context=f"PR #{pr_number} files page {page}", default=None)
		if response is None:
			return None
		if not isinstance(response, list):
			_warn(f"PR #{pr_number} files page {page} was not a JSON array; leaving tracker open.")
			return None
		files.extend(item for item in response if isinstance(item, dict))
		if len(response) < 100:
			return files
	_warn(f"PR #{pr_number} exceeds the 3000-file diff audit cap; leaving tracker open.")
	return None


def _pattern_matches_patch(pattern: str, patch: str, *, line_prefixes: tuple[str, ...]) -> bool:
	selected_lines: list[str] = []
	for line in patch.splitlines():
		if not line:
			continue
		if line.startswith(("@@", "\\ No newline", "+++", "---")):
			continue
		if not line.startswith(line_prefixes):
			continue
		selected_lines.append(line[1:])
	if not selected_lines:
		return False
	selected_patch = "\n".join(selected_lines)
	try:
		return re.search(pattern, selected_patch, re.MULTILINE) is not None
	except re.error:
		return pattern in selected_patch


def _cluster_resolved_by_pr_files(cluster: dict[str, Any], files: list[dict[str, Any]]) -> bool:
	path = cluster.get("path")
	pattern = cluster.get("pattern")
	kind = str(cluster.get("kind") or "")
	if not path:
		return False
	for file_entry in files:
		filename = file_entry.get("filename")
		previous_filename = file_entry.get("previous_filename")
		status = str(file_entry.get("status") or "")
		if filename != path and previous_filename != path:
			continue
		if status == "removed":
			return kind in {"must_not_contain", "must_not_exist"}
		if status == "renamed" and previous_filename == path and filename != path:
			return kind in {"must_not_contain", "must_not_exist"}
		if kind == "must_not_exist" and previous_filename == path and filename != path:
			return True
		patch = file_entry.get("patch")
		if not isinstance(patch, str) or not pattern:
			continue
		if kind == "must_contain":
			if _pattern_matches_patch(pattern, patch, line_prefixes=("+",)):
				return True
			continue
		if kind == "must_not_contain":
			if _pattern_matches_patch(pattern, patch, line_prefixes=("-",)) and not _pattern_matches_patch(pattern, patch, line_prefixes=("+",)):
				return True
	return False


def _recheck_cluster(
	cluster: dict[str, Any],
	issue_to_pr: dict[str, dict[str, Any]],
	pr_files_cache: dict[int, list[dict[str, Any]] | None],
	repo: str,
) -> dict[str, Any]:
	if _cluster_has_only_fixed_markers(cluster):
		return {
			"applicable": False,
			"resolved": True,
			"reason": "recent review/autofix runs only reported fixed_by_resolver for this fingerprint",
		}
	inconclusive_notes: list[str] = []
	for issue_number in cluster["issue_numbers"]:
		linked_pr = issue_to_pr.get(str(issue_number))
		if not isinstance(linked_pr, dict):
			inconclusive_notes.append(f"issue #{issue_number}: no merged linked PR found")
			continue
		pr_number = linked_pr.get("number")
		if not isinstance(pr_number, int):
			inconclusive_notes.append(f"issue #{issue_number}: merged linked PR payload missing number")
			continue
		if pr_number not in pr_files_cache:
			pr_files_cache[pr_number] = _fetch_pr_files(repo, pr_number)
		files = pr_files_cache.get(pr_number)
		if files is None:
			inconclusive_notes.append(f"issue #{issue_number}: PR #{pr_number} diff unavailable")
			continue
		if _cluster_resolved_by_pr_files(cluster, files):
			path_desc = cluster.get("path") or cluster["fp_key"]
			return {
				"applicable": True,
				"resolved": True,
				"reason": f"latest merged linked PR #{pr_number} contains a resolving change for {path_desc}",
			}
	if inconclusive_notes:
		return {
			"applicable": True,
			"resolved": False,
			"reason": "recheck inconclusive — " + "; ".join(inconclusive_notes),
		}
	return {
		"applicable": True,
		"resolved": False,
		"reason": "latest merged linked PR diff did not contain a resolving change for the fingerprint path/pattern",
	}


def _render_title(cluster: dict[str, Any]) -> str:
	path = cluster.get("path") or cluster["fp_key"]
	return f"Drift audit: persistent fingerprint drift for {path}"


def _render_body(cluster: dict[str, Any], recheck: dict[str, Any]) -> str:
	lines = [
		_cluster_marker_body(cluster["fp_key"]),
		"## Summary",
		"Persistent integration fingerprint drift was observed in recent review/autofix runs.",
		"",
		f"- fp_key: `{cluster['fp_key']}`",
	]
	if cluster.get("path"):
		lines.append(f"- path: `{cluster['path']}`")
	if cluster.get("pattern"):
		lines.append(f"- pattern: `{cluster['pattern']}`")
	if cluster.get("kind"):
		lines.append(f"- kind: `{cluster['kind']}`")
	lines.extend(
		[
			f"- marker types: {', '.join(f'`{marker}`' for marker in cluster['marker_types'])}",
			f"- unique runs: {len(cluster['run_ids'])}",
			f"- related managed issues: {', '.join(f'#{issue}' for issue in cluster['issue_numbers'])}",
			"",
			"## Recent runs",
		]
	)
	for run in cluster["runs"][:10]:
		descriptor = f"run {run['run_id']}"
		if run.get("url"):
			descriptor = f"[{descriptor}]({run['url']})"
		lines.append(f"- {descriptor} — `{run['workflow_file']}` — {run.get('created_at') or 'unknown time'}")
	lines.extend(
		[
			"",
			"## Latest merged PR recheck",
			f"- applicable: {'yes' if recheck['applicable'] else 'no'}",
			f"- verdict: {'resolved — close when observed again' if recheck['resolved'] else 'keep open'}",
			f"- detail: {recheck['reason']}",
		]
	)
	return "\n".join(lines) + "\n"


def _create_issue(repo: str, cluster: dict[str, Any], body: str) -> bool:
	return _gh_ok(
		[
			"issue",
			"create",
			"--repo",
			repo,
			"--title",
			_render_title(cluster),
			"--body",
			body,
		],
		context=f"create tracker issue for {cluster['fp_key']}",
	)


def _edit_issue(repo: str, issue_number: int, cluster: dict[str, Any], body: str) -> bool:
	return _gh_ok(
		[
			"issue",
			"edit",
			str(issue_number),
			"--repo",
			repo,
			"--title",
			_render_title(cluster),
			"--body",
			body,
		],
		context=f"update tracker issue #{issue_number} for {cluster['fp_key']}",
	)


def _close_issue(repo: str, issue_number: int, reason: str) -> bool:
	return _gh_ok(
		[
			"issue",
			"close",
			str(issue_number),
			"--repo",
			repo,
			"--comment",
			f"Closing after drift-audit recheck: {reason}.",
		],
		context=f"close tracker issue #{issue_number}",
	)


def main() -> int:
	repo = os.environ["GITHUB_REPOSITORY"]
	cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
	runs: list[dict[str, Any]] = []
	seen_run_ids: set[str] = set()
	for workflow_file in WORKFLOW_FILES:
		for run in _list_recent_runs(repo, workflow_file, cutoff):
			if run["run_id"] in seen_run_ids:
				continue
			seen_run_ids.add(run["run_id"])
			runs.append(run)
	_RESULT["processed_runs"] = len(runs)
	if not runs:
		_emit_coverage_summary(0, 0, [], [])
		_RESULT["status"] = "no_runs"
		_log("no recent review/autofix runs found; nothing to audit.")
		return 0

	occurrences: list[dict[str, Any]] = []
	logs_fetched = 0
	missing_run_ids: list[str] = []
	unscannable_run_ids: list[str] = []
	for run in sorted(runs, key=lambda row: ((row.get("created_at") or ""), row["run_id"])):
		log_expected = str(run.get("conclusion") or "") not in UNSCANNABLE_CONCLUSIONS
		log_text = _fetch_run_log(repo, run["run_id"], log_expected=log_expected)
		if log_text is None:
			if log_expected:
				missing_run_ids.append(run["run_id"])
			else:
				unscannable_run_ids.append(run["run_id"])
			continue
		logs_fetched += 1
		occurrences.extend(_parse_run_markers(run, log_text))
	_emit_coverage_summary(len(runs), logs_fetched, missing_run_ids, unscannable_run_ids)
	_RESULT["marker_occurrences"] = len(occurrences)
	if not occurrences:
		_RESULT["status"] = "no_markers"
		_log("no drift markers found in recent runs.")
		return 0

	tracker_issues = _tracker_issue_lookup(repo)
	clusters = []
	skipped_absent_path = 0
	for cluster in _build_clusters(occurrences):
		if not _cluster_path_in_repo(cluster):
			skipped_absent_path += 1
			_notice(
				f"skipping cluster {cluster['fp_key']}: path "
				f"{cluster.get('path')!r} is not a file in the repository "
				"(synthetic test-fixture or stale marker, not actionable drift)."
			)
			continue
		marker_hash = _cluster_marker_hash(cluster["fp_key"])
		if _is_persistent_cluster(cluster) or (marker_hash in tracker_issues and _cluster_has_only_fixed_markers(cluster)):
			clusters.append(cluster)
	_RESULT["skipped_absent_path"] = skipped_absent_path
	_RESULT["persistent_clusters"] = len(clusters)
	if skipped_absent_path:
		_log(f"skipped {skipped_absent_path} cluster(s) whose fingerprint path is absent from the repository.")
	if not clusters:
		_RESULT["status"] = "no_clusters"
		_log("no drift clusters requiring action found.")
		return 0

	recheck_issue_numbers = sorted(
		{
			issue_number
			for cluster in clusters
			if not _cluster_has_only_fixed_markers(cluster)
			for issue_number in cluster["issue_numbers"]
		}
	)
	issue_to_pr = _fetch_latest_merged_linked_prs(repo, recheck_issue_numbers)
	pr_files_cache: dict[int, list[dict[str, Any]] | None] = {}
	created = 0
	edited = 0
	closed = 0
	suppressed = 0

	for cluster in clusters:
		marker_hash = _cluster_marker_hash(cluster["fp_key"])
		existing = tracker_issues.get(marker_hash)
		recheck = _recheck_cluster(cluster, issue_to_pr, pr_files_cache, repo)
		if recheck["resolved"]:
			if existing is not None:
				issue_number = int(existing.get("number") or 0)
				if issue_number and _close_issue(repo, issue_number, recheck["reason"]):
					closed += 1
					_notice(f"closed tracker issue #{issue_number} for {cluster['fp_key']}")
			else:
				suppressed += 1
				_notice(f"suppressed tracker creation for {cluster['fp_key']}: {recheck['reason']}")
			continue

		body = _render_body(cluster, recheck)
		if existing is not None:
			issue_number = int(existing.get("number") or 0)
			existing_title = str(existing.get("title") or "")
			existing_body = str(existing.get("body") or "")
			if existing_title == _render_title(cluster) and existing_body == body:
				_log(f"tracker issue #{issue_number} already up to date for {cluster['fp_key']}")
				continue
			if issue_number and _edit_issue(repo, issue_number, cluster, body):
				edited += 1
				_notice(f"updated tracker issue #{issue_number} for {cluster['fp_key']}")
			continue

		if _create_issue(repo, cluster, body):
			created += 1
			_notice(f"created tracker issue for {cluster['fp_key']}")

	_RESULT["created"] = created
	_RESULT["edited"] = edited
	_RESULT["closed"] = closed
	_RESULT["suppressed"] = suppressed
	_RESULT["status"] = "completed"
	_log(
		f"processed_runs={len(runs)} marker_occurrences={len(occurrences)} persistent_clusters={len(clusters)} "
		f"created={created} edited={edited} closed={closed} suppressed={suppressed}"
	)
	return 0


_exit_code = 1
try:
	_exit_code = main()
finally:
	_write_summary()
raise SystemExit(_exit_code)
PY
audit_rc=$?
set -e

# ---------------------------------------------------------------
# Per-run Telegram alert + GitHub Actions job summary.
# Best-effort: a notification failure must never change audit_rc.
# ---------------------------------------------------------------
da_status="unknown"
da_processed_runs=0
da_marker_occurrences=0
da_persistent_clusters=0
da_skipped_absent_path=0
da_coverage_status="unknown"
da_logs_fetched=0
da_logs_missing=0
da_missing_run_ids="none"
da_logs_unscannable=0
da_created=0
da_edited=0
da_closed=0
da_suppressed=0
da_summary_loaded="false"
if [ "${DRIFT_AUDIT_HAVE_JQ}" = "true" ] && [ -n "${DRIFT_AUDIT_SUMMARY_FILE:-}" ] && [ -s "${DRIFT_AUDIT_SUMMARY_FILE}" ]; then
	if da_summary_tsv="$(jq -r '
		def n: if type == "number" and . >= 0 then tostring else "0" end;
		[
			(.status // "unknown" | tostring),
			(.coverage_status // "unknown" | tostring),
			(.processed_runs // 0 | n),
			(.marker_occurrences // 0 | n),
			(.persistent_clusters // 0 | n),
			(.skipped_absent_path // 0 | n),
			(.logs_fetched // 0 | n),
			(.logs_missing // 0 | n),
			((.missing_run_ids // []) | if type == "array" and length > 0 then map(tostring) | join(",") else "none" end),
			(.created // 0 | n),
			(.edited // 0 | n),
			(.closed // 0 | n),
			(.suppressed // 0 | n),
			(.logs_unscannable // 0 | n)
		] | @tsv
	' "${DRIFT_AUDIT_SUMMARY_FILE}" 2>/dev/null)"; then
		IFS=$'\t' read -r da_status da_coverage_status da_processed_runs da_marker_occurrences da_persistent_clusters da_skipped_absent_path da_logs_fetched da_logs_missing da_missing_run_ids da_created da_edited da_closed da_suppressed da_logs_unscannable <<<"${da_summary_tsv}"
		da_summary_loaded="true"
	else
		echo "::warning::drift-audit: could not parse run summary file; notifications will omit run counts." >&2
	fi
elif [ "${DRIFT_AUDIT_HAVE_JQ}" = "true" ] && [ -n "${DRIFT_AUDIT_SUMMARY_FILE:-}" ]; then
	echo "::warning::drift-audit: run summary file is empty; notifications will omit run counts." >&2
fi
if [ "${audit_rc}" -ne 0 ]; then
	da_status="error"
elif [ "${da_summary_loaded}" != "true" ]; then
	da_status="summary_unavailable"
fi

da_status_display="${da_status}"
if [ "${da_summary_loaded}" = "true" ] && [ "${da_coverage_status}" = "partial" ] && [ "${da_status}" != "error" ]; then
	da_status_display="${da_status} (partial coverage)"
fi

da_partial_coverage_prefix=""
if [ "${da_coverage_status}" = "partial" ] && [ "${da_logs_missing}" -gt 0 ] 2>/dev/null; then
	da_partial_coverage_prefix="Partial log coverage: fetched ${da_logs_fetched}, missing ${da_logs_missing} (run IDs: ${da_missing_run_ids}). "
fi

case "${da_status}" in
	completed)
		da_detail="${da_partial_coverage_prefix}Scanned ${da_processed_runs} run(s); ${da_persistent_clusters} persistent cluster(s) -> created ${da_created}, updated ${da_edited}, closed ${da_closed}, suppressed ${da_suppressed} tracker issue(s)."
		;;
	no_runs)
		da_detail="No recent review/autofix runs to scan."
		;;
	no_markers)
		da_detail="${da_partial_coverage_prefix}Scanned ${da_processed_runs} run(s); no fingerprint-drift markers found."
		;;
	no_clusters)
		da_detail="${da_partial_coverage_prefix}Scanned ${da_processed_runs} run(s), ${da_marker_occurrences} marker(s); no persistent drift clusters needed a tracker."
		;;
	summary_unavailable)
		da_detail="Audit completed, but run metrics were unavailable; see the run log."
		;;
	error)
		da_detail="Run did not complete cleanly (exit ${audit_rc}); see the run log."
		;;
	*)
		da_detail="Completed with status '${da_status}'; see the run log."
		;;
esac
if [ "${da_logs_unscannable}" -gt 0 ] 2>/dev/null; then
	da_detail="${da_detail} ${da_logs_unscannable} cancelled/skipped run(s) had no logs to scan (expected; not counted as missing coverage)."
fi
if [ "${da_skipped_absent_path}" -gt 0 ] 2>/dev/null; then
	da_detail="${da_detail} Skipped ${da_skipped_absent_path} cluster(s) for paths absent from the repo."
fi

da_report_url=""
if [ -n "${GITHUB_SERVER_URL:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ] && [ -n "${GITHUB_RUN_ID:-}" ]; then
	da_report_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
fi

# GitHub Actions job summary — the rendered run report the alert links to.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
	{
		echo "## Drift audit"
		echo
		echo "- **Status:** ${da_status_display}"
		echo "- **Runs scanned:** ${da_processed_runs}"
		echo "- **Marker occurrences:** ${da_marker_occurrences}"
		echo "- **Persistent clusters:** ${da_persistent_clusters}"
		echo "- **Clusters skipped (path absent from repo):** ${da_skipped_absent_path}"
		if [ "${da_summary_loaded}" = "true" ] && [ "${da_status}" != "error" ] && [ "${da_coverage_status}" != "unknown" ]; then
			echo "- **Log coverage:** ${da_coverage_status}"
			echo "- **Log fetches:** fetched ${da_logs_fetched} / missing ${da_logs_missing}"
			echo "- **Missing log run IDs:** ${da_missing_run_ids}"
			echo "- **Cancelled/skipped runs without logs:** ${da_logs_unscannable}"
		fi
		echo "- **Tracker issues:** created ${da_created} / updated ${da_edited} / closed ${da_closed} / suppressed ${da_suppressed}"
		if [ -n "${da_report_url}" ]; then
			echo "- **Run:** [workflow run](${da_report_url})"
		fi
		if [ "${da_summary_loaded}" != "true" ]; then
			echo "- **Summary metrics:** unavailable (jq missing, or temp summary file missing or unreadable)"
		fi
	} >> "${GITHUB_STEP_SUMMARY}" 2>/dev/null || true
fi

# Telegram alert — fires after every audit run.
da_level="DEBUG"
if [ "${da_status}" = "error" ]; then
	da_level="ERROR"
elif [ "${da_coverage_status}" = "partial" ]; then
	da_level="WARNING"
elif [ "${da_status}" = "completed" ] && [ "$(( da_created + da_edited + da_closed ))" -gt 0 ]; then
	da_level="WARNING"
fi
da_message="Drift audit - ${GITHUB_REPOSITORY:-unknown repo}
${da_detail}"
if [ -n "${da_report_url}" ]; then
	da_message="${da_message}
Report: ${da_report_url}"
fi
da_tg_helpers="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)/tg_helpers.sh"
if [ -f "${da_tg_helpers}" ]; then
	# shellcheck source=tg_helpers.sh disable=SC1090,SC1091
	if source "${da_tg_helpers}"; then
		if type tg_send_msg >/dev/null 2>&1; then
			tg_send_msg "${da_message}" "${da_level}" >/dev/null 2>&1 \
				|| echo "::warning::drift-audit: Telegram notification failed." >&2
		else
			echo "::warning::drift-audit: tg_send_msg unavailable; skipping Telegram notification." >&2
		fi
	else
		echo "::warning::drift-audit: failed to source tg_helpers.sh; skipping Telegram notification." >&2
	fi
else
	echo "::warning::drift-audit: tg_helpers.sh not found; skipping Telegram notification." >&2
fi

exit "${audit_rc}"
