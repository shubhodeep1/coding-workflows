#!/usr/bin/env python3
"""Tests for the scheduled drift audit job."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "drift_audit.sh"

FP_KEY = '["scripts/example.py","EXPECTED_LINE"]'
PRE_EXISTING_MARKER = (
	"::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged "
	f"fp_key={FP_KEY} issue=#1500 path=\"scripts/example.py\" pattern=\"EXPECTED_LINE\" kind=must_contain"
)
FIXED_BY_RESOLVER_MARKER = (
	"::notice::PRE_EXISTING_FINGERPRINT_DRIFT_V1 fixed_by_resolver "
	f"fp_key={FP_KEY} issue=#1500 path=\"scripts/example.py\""
)
QUARANTINED_MARKER = f"::warning::FINGERPRINT_QUARANTINED_V1 fp_key={FP_KEY} issue=#1500"


def _write_exec(path: Path, body: str) -> None:
	path.write_text(body, encoding="utf-8")
	path.chmod(0o755)


def _cluster_marker(fp_key: str) -> str:
	marker_hash = hashlib.sha256(fp_key.encode("utf-8")).hexdigest()
	return f"<!-- drift-audit:cluster-key={marker_hash} -->"


def _flag_value(args: list[str], flag: str) -> str:
	for idx, arg in enumerate(args):
		if arg == flag and idx + 1 < len(args):
			return args[idx + 1]
	return ""


def _iso(hours_ago: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _install_mock_gh(bin_dir: Path, state_file: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
if state_path.exists():
	state = json.loads(state_path.read_text(encoding="utf-8"))
else:
	state = {}
args = sys.argv[1:]


def save() -> None:
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag: str) -> str:
	for idx, arg in enumerate(args):
		if arg == flag and idx + 1 < len(args):
			return args[idx + 1]
	return ""


state.setdefault("calls", []).append(args)

if args[:2] == ["run", "list"]:
	workflow = first_value("--workflow")
	state.setdefault("run_list_args", []).append(args)
	save()
	print(json.dumps((state.get("run_list_responses") or {}).get(workflow, [])))
	sys.exit(0)

if args[:2] == ["run", "view"] and "--log" in args:
	run_id = args[2] if len(args) > 2 else ""
	state.setdefault("run_view_args", []).append(args)
	response = (state.get("run_logs") or {}).get(run_id)
	stdout = ""
	stderr = ""
	exit_code = 0
	if isinstance(response, dict):
		stdout = str(response.get("stdout", ""))
		stderr = str(response.get("stderr", ""))
		exit_code = int(response.get("exit_code", 0))
	elif isinstance(response, str):
		stdout = response
	else:
		stderr = f"no mock log for run {run_id}"
		exit_code = 1
	save()
	if stdout:
		print(stdout, end="")
	if stderr:
		print(stderr, file=sys.stderr, end="")
	sys.exit(exit_code)

if args[:2] == ["issue", "list"]:
	state.setdefault("issue_list_args", []).append(args)
	save()
	print(json.dumps(state.get("issue_list_response", [])))
	sys.exit(0)

if args[:2] == ["issue", "create"]:
	state.setdefault("issue_create_args", []).append(args)
	repo = first_value("--repo") or "owner/repo"
	next_issue_number = int(state.get("next_issue_number", 9100))
	state["next_issue_number"] = next_issue_number + 1
	save()
	print(f"https://github.com/{repo}/issues/{next_issue_number}")
	sys.exit(0)

if args[:2] == ["issue", "edit"]:
	state.setdefault("issue_edit_args", []).append(args)
	save()
	sys.exit(0)

if args[:2] == ["issue", "close"]:
	state.setdefault("issue_close_args", []).append(args)
	save()
	sys.exit(0)

if args and args[0] == "api":
	path = ""
	for arg in args[1:]:
		if not arg.startswith("-"):
			path = arg
			break
	if path == "graphql":
		state.setdefault("graphql_args", []).append(args)
		response = state.get("graphql_response", {})
		if isinstance(response, list):
			payload = response.pop(0) if response else {}
			state["graphql_response"] = response
		else:
			payload = response
		save()
		print(json.dumps(payload))
		sys.exit(0)
	state.setdefault("api_args", []).append(args)
	responses = state.get("api_responses", {}) or {}
	matched = responses.get(path)
	if matched is None:
		for pattern, payload in responses.items():
			if pattern and pattern in path:
				matched = payload
				break
	stdout = matched
	stderr = ""
	exit_code = 0
	if isinstance(matched, dict) and "exit_code" in matched:
		stdout = matched.get("stdout", "")
		stderr = str(matched.get("stderr", ""))
		exit_code = int(matched.get("exit_code", 0))
	save()
	if exit_code != 0:
		if stdout:
			print(stdout, end="")
		if stderr:
			print(stderr, file=sys.stderr, end="")
		sys.exit(exit_code)
	if isinstance(stdout, str):
		print(stdout, end="")
	else:
		print(json.dumps(stdout if stdout is not None else []))
	sys.exit(0)

save()
print(f"unexpected gh args: {args}", file=sys.stderr)
sys.exit(1)
'''
	_write_exec(bin_dir / "gh", gh_script)
	state_file.write_text("{}", encoding="utf-8")


def _run_drift_audit(state: dict, *, enabled: bool = True) -> tuple[subprocess.CompletedProcess[str], dict]:
	with tempfile.TemporaryDirectory(prefix="drift-audit-test-") as td:
		tmp_path = Path(td)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir(parents=True, exist_ok=True)
		state_file = tmp_path / "gh-state.json"
		_install_mock_gh(bin_dir, state_file)
		state_file.write_text(json.dumps(state), encoding="utf-8")

		env = os.environ.copy()
		env.update(
			{
				"DRIFT_AUDIT_ENABLED": "true" if enabled else "false",
				"GH_TOKEN": "test-token",
				"GITHUB_REPOSITORY": "owner/repo",
				"MOCK_GH_STATE_FILE": str(state_file),
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		proc = subprocess.run(
			["bash", str(SCRIPT_PATH)],
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
			encoding="utf-8",
		)
		final_state = json.loads(state_file.read_text(encoding="utf-8"))
		return proc, final_state


def test_drift_audit_gate_disabled_skips_without_gh_calls() -> None:
	proc, state = _run_drift_audit({}, enabled=False)
	assert proc.returncode == 0, proc.stderr
	assert "DRIFT_AUDIT_ENABLED=false; skipping." in proc.stdout
	assert state.get("calls", []) == []


def test_drift_audit_dedups_runs_into_one_tracker_issue() -> None:
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 101, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/101"},
			],
			"internal-review.yml": [
				{"databaseId": 202, "createdAt": _iso(2), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/202"},
			],
		},
		"run_logs": {
			"101": PRE_EXISTING_MARKER + "\n" + PRE_EXISTING_MARKER + "\n",
			"202": PRE_EXISTING_MARKER + "\n",
		},
		"issue_list_response": [],
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert len(final_state.get("issue_create_args", [])) == 1
	assert final_state.get("issue_edit_args", []) == []
	assert final_state.get("issue_close_args", []) == []

	create_args = final_state["issue_create_args"][0]
	body = _flag_value(create_args, "--body")
	assert _cluster_marker(FP_KEY) in body
	assert "run 101" in body
	assert "run 202" in body
	assert body.count("run 101") == 1


def test_drift_audit_edits_existing_issue_instead_of_recreating() -> None:
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 101, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/101"},
			],
			"internal-review.yml": [
				{"databaseId": 202, "createdAt": _iso(2), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/202"},
			],
		},
		"run_logs": {
			"101": PRE_EXISTING_MARKER + "\n",
			"202": PRE_EXISTING_MARKER + "\n",
		},
		"issue_list_response": [
			{
				"number": 9100,
				"title": "stale tracker",
				"body": _cluster_marker(FP_KEY) + "\nstale body\n",
				"url": "https://example.test/issues/9100",
			},
		],
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert final_state.get("issue_create_args", []) == []
	assert len(final_state.get("issue_edit_args", [])) == 1
	assert final_state.get("issue_close_args", []) == []
	edit_args = final_state["issue_edit_args"][0]
	assert edit_args[2] == "9100"
	assert _cluster_marker(FP_KEY) in _flag_value(edit_args, "--body")


def test_drift_audit_closes_resolved_quarantined_cluster() -> None:
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 301, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/301"},
			],
			"internal-review.yml": [
				{"databaseId": 302, "createdAt": _iso(2), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/302"},
			],
		},
		"run_logs": {
			"301": PRE_EXISTING_MARKER + "\n",
			"302": QUARANTINED_MARKER + "\n",
		},
		"issue_list_response": [
			{
				"number": 9100,
				"title": "tracker",
				"body": _cluster_marker(FP_KEY) + "\nopen\n",
				"url": "https://example.test/issues/9100",
			},
		],
		"graphql_response": {
			"data": {
				"repository": {
					"i0": {
						"number": 1500,
						"timelineItems": {
							"nodes": [
								{
									"source": {
										"__typename": "PullRequest",
										"number": 77,
										"merged": True,
										"mergedAt": _iso(3),
									},
								}
							],
						},
					},
				},
			},
		},
		"api_responses": {
			"repos/owner/repo/pulls/77/files?per_page=100&page=1": [
				{"filename": "scripts/example.py", "status": "modified", "patch": "+EXPECTED_LINE\n"},
			],
		},
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert final_state.get("issue_create_args", []) == []
	assert final_state.get("issue_edit_args", []) == []
	assert len(final_state.get("issue_close_args", [])) == 1
	close_args = final_state["issue_close_args"][0]
	assert close_args[2] == "9100"
	assert "PR #77" in _flag_value(close_args, "--comment")


def test_drift_audit_fail_open_when_pr_diff_is_unavailable() -> None:
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 401, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/401"},
			],
			"internal-review.yml": [
				{"databaseId": 402, "createdAt": _iso(2), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/402"},
			],
		},
		"run_logs": {
			"401": PRE_EXISTING_MARKER + "\n",
			"402": QUARANTINED_MARKER + "\n",
		},
		"issue_list_response": [
			{
				"number": 9100,
				"title": "tracker",
				"body": _cluster_marker(FP_KEY) + "\nstale body\n",
				"url": "https://example.test/issues/9100",
			},
		],
		"graphql_response": {
			"data": {
				"repository": {
					"i0": {
						"number": 1500,
						"timelineItems": {
							"nodes": [
								{
									"source": {
										"__typename": "PullRequest",
										"number": 78,
										"merged": True,
										"mergedAt": _iso(3),
									},
								}
							],
						},
					},
				},
			},
		},
		"api_responses": {
			"repos/owner/repo/pulls/78/files?per_page=100&page=1": {
				"stderr": "synthetic PR diff failure",
				"exit_code": 1,
			},
		},
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert final_state.get("issue_create_args", []) == []
	assert len(final_state.get("issue_edit_args", [])) == 1
	assert final_state.get("issue_close_args", []) == []
	assert "recheck inconclusive" in _flag_value(final_state["issue_edit_args"][0], "--body")
	assert "synthetic PR diff failure" in proc.stdout


def test_drift_audit_batches_multiple_issue_queries_inside_repository_scope() -> None:
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 451, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/451"},
			],
			"internal-review.yml": [
				{"databaseId": 452, "createdAt": _iso(2), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/452"},
			],
		},
		"run_logs": {
			"451": PRE_EXISTING_MARKER + "\n",
			"452": QUARANTINED_MARKER + "\n" + f"::warning::FINGERPRINT_QUARANTINED_V1 fp_key={FP_KEY} issue=#1501\n",
		},
		"issue_list_response": [],
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert len(final_state.get("graphql_args", [])) == 1
	query_form = _flag_value(final_state["graphql_args"][0], "-f")
	assert query_form.startswith("query=")
	query = query_form[len("query=") :]
	assert "i0: issue(number: 1500)" in query
	assert "i1: issue(number: 1501)" in query
	assert query.count("{") == query.count("}")


def test_drift_audit_matches_anchored_patterns_against_patch_content() -> None:
	anchored_fp_key = '["scripts/example.py","^EXPECTED_LINE$"]'
	anchored_pre_existing_marker = (
		"::warning::PRE_EXISTING_FINGERPRINT_DRIFT_V1 unchanged "
		f"fp_key={anchored_fp_key} issue=#1500 path=\"scripts/example.py\" pattern=\"^EXPECTED_LINE$\" kind=must_contain"
	)
	anchored_quarantined_marker = f"::warning::FINGERPRINT_QUARANTINED_V1 fp_key={anchored_fp_key} issue=#1500"
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 461, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/461"},
			],
			"internal-review.yml": [
				{"databaseId": 462, "createdAt": _iso(2), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/462"},
			],
		},
		"run_logs": {
			"461": anchored_pre_existing_marker + "\n",
			"462": anchored_quarantined_marker + "\n",
		},
		"issue_list_response": [
			{
				"number": 9100,
				"title": "tracker",
				"body": _cluster_marker(anchored_fp_key) + "\nopen\n",
				"url": "https://example.test/issues/9100",
			},
		],
		"graphql_response": {
			"data": {
				"repository": {
					"i0": {
						"number": 1500,
						"timelineItems": {
							"nodes": [
								{
									"source": {
										"__typename": "PullRequest",
										"number": 79,
										"merged": True,
										"mergedAt": _iso(3),
									},
								}
							],
						},
					},
				},
			},
		},
		"api_responses": {
			"repos/owner/repo/pulls/79/files?per_page=100&page=1": [
				{"filename": "scripts/example.py", "status": "modified", "patch": "+EXPECTED_LINE\n"},
			],
		},
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert final_state.get("issue_create_args", []) == []
	assert final_state.get("issue_edit_args", []) == []
	assert len(final_state.get("issue_close_args", [])) == 1
	assert "PR #79" in _flag_value(final_state["issue_close_args"][0], "--comment")


def test_drift_audit_closes_existing_issue_after_fixed_by_resolver_markers() -> None:
	state = {
		"run_list_responses": {
			"review_autofix.yml": [
				{"databaseId": 501, "createdAt": _iso(1), "status": "completed", "conclusion": "success", "url": "https://example.test/runs/501"},
			],
			"internal-review.yml": [],
		},
		"run_logs": {
			"501": FIXED_BY_RESOLVER_MARKER + "\n",
		},
		"issue_list_response": [
			{
				"number": 9100,
				"title": "tracker",
				"body": _cluster_marker(FP_KEY) + "\nopen\n",
				"url": "https://example.test/issues/9100",
			},
		],
	}
	proc, final_state = _run_drift_audit(state)
	assert proc.returncode == 0, proc.stderr
	assert final_state.get("issue_create_args", []) == []
	assert final_state.get("issue_edit_args", []) == []
	assert len(final_state.get("issue_close_args", [])) == 1
	assert "fixed_by_resolver" in _flag_value(final_state["issue_close_args"][0], "--comment")


def main() -> int:
	test_drift_audit_gate_disabled_skips_without_gh_calls()
	test_drift_audit_dedups_runs_into_one_tracker_issue()
	test_drift_audit_edits_existing_issue_instead_of_recreating()
	test_drift_audit_closes_resolved_quarantined_cluster()
	test_drift_audit_fail_open_when_pr_diff_is_unavailable()
	test_drift_audit_batches_multiple_issue_queries_inside_repository_scope()
	test_drift_audit_matches_anchored_patterns_against_patch_content()
	test_drift_audit_closes_existing_issue_after_fixed_by_resolver_markers()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
