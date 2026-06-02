#!/usr/bin/env python3
"""Contracts for the shared orchestrate-force-tick helper."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
FORCE_TICK_SCRIPT = REPO_ROOT / "scripts" / "orchestrate_force_tick.sh"
MEMORY_HELPERS = REPO_ROOT / "scripts" / "memory_helpers.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
RESOLVER_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	full_env = os.environ.copy()
	full_env["PYTHONDONTWRITEBYTECODE"] = "1"
	if env:
		full_env.update(env)
	return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False, env=full_env)


def _create_repo() -> tuple[Path, Path]:
	tmp_root = Path(tempfile.mkdtemp(prefix="force-tick-repo-"))
	bare = tmp_root / "bare.git"
	work = tmp_root / "work"
	for cmd in (
		["git", "init", "--bare", "--quiet", str(bare)],
		["git", "init", "--quiet", str(work)],
	):
		result = _run(cmd, cwd=REPO_ROOT)
		assert result.returncode == 0, result.stderr
	for key, value in (("user.name", "test"), ("user.email", "t@example.com")):
		result = _run(["git", "-C", str(work), "config", key, value], cwd=REPO_ROOT)
		assert result.returncode == 0, result.stderr
	(work / "README.md").write_text("seed\n", encoding="utf-8")
	for args in (
		("checkout", "-B", "main"),
		("add", "README.md"),
		("commit", "--quiet", "-m", "seed"),
		("remote", "add", "origin", str(bare)),
		("push", "-u", "origin", "main", "--quiet"),
	):
		result = _run(["git", "-C", str(work), *args], cwd=REPO_ROOT)
		assert result.returncode == 0, result.stderr
	return tmp_root, work


def _make_gh_mock(
	tmp_root: Path,
	issue_bodies: dict[int, str],
	*,
	pull_requests: dict[int, dict[str, str]] | None = None,
) -> tuple[Path, Path]:
	mock_dir = tmp_root / "mock-bin"
	mock_dir.mkdir(parents=True, exist_ok=True)
	issues_file = tmp_root / "issues.json"
	pulls_file = tmp_root / "pulls.json"
	workflow_runs_file = tmp_root / "workflow_runs.jsonl"
	issues_file.write_text(json.dumps({str(k): v for k, v in issue_bodies.items()}), encoding="utf-8")
	pulls_file.write_text(json.dumps({str(k): v for k, v in (pull_requests or {}).items()}), encoding="utf-8")
	if not workflow_runs_file.exists():
		workflow_runs_file.write_text("", encoding="utf-8")
	gh_path = mock_dir / "gh"
	gh_path.write_text(
		"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys


def main() -> int:
	args = sys.argv[1:]
	issues = json.loads(pathlib.Path(os.environ["GH_MOCK_ISSUES_FILE"]).read_text(encoding="utf-8"))
	pulls = json.loads(pathlib.Path(os.environ["GH_MOCK_PULL_REQUESTS_FILE"]).read_text(encoding="utf-8"))
	runs_file = pathlib.Path(os.environ["GH_MOCK_WORKFLOW_RUNS_FILE"])
	fail_issues = os.environ.get("GH_MOCK_FAIL_ISSUES") == "1"
	fail_pulls = os.environ.get("GH_MOCK_FAIL_PULLS") == "1"
	if len(args) >= 2 and args[0] == "api":
		endpoint = args[1]
		if endpoint.startswith("repos/") and "/issues/" in endpoint:
			if fail_issues:
				return 1
			issue_number = endpoint.rsplit("/issues/", 1)[1]
			body = issues.get(issue_number, "")
			if "--jq" in args:
				print(body)
			else:
				print(json.dumps({"body": body}))
			return 0
		if endpoint.startswith("repos/") and "/pulls/" in endpoint:
			if fail_pulls:
				return 1
			pr_number = endpoint.rsplit("/pulls/", 1)[1]
			pr = pulls.get(pr_number)
			if pr is None:
				return 1
			print(json.dumps({
				"body": pr.get("body", ""),
				"head": {"ref": pr.get("head_ref", "")},
				"base": {"ref": pr.get("base_ref", "")},
			}))
			return 0
		print(f"unsupported gh api endpoint: {endpoint}", file=sys.stderr)
		return 2
	if len(args) >= 3 and args[0] == "workflow" and args[1] == "run":
		workflow = args[2]
		repo = ""
		exit_code = int(os.environ.get("GH_MOCK_WORKFLOW_RUN_EXIT_CODE", "0"))
		if "--repo" in args:
			repo = args[args.index("--repo") + 1]
		if exit_code == 0:
			with runs_file.open("a", encoding="utf-8") as fh:
				fh.write(json.dumps({"workflow": workflow, "repo": repo}) + "\\n")
		return exit_code
	print(f"unsupported gh invocation: {' '.join(args)}", file=sys.stderr)
	return 2


if __name__ == "__main__":
	raise SystemExit(main())
""",
		encoding="utf-8",
	)
	os.chmod(gh_path, 0o755)
	return mock_dir, workflow_runs_file


def _run_force_tick(
	work_repo: Path,
	issue_bodies: dict[int, str],
	*,
	args: list[str],
	pull_requests: dict[int, dict[str, str]] | None = None,
	workflow_run_exit_code: int = 0,
	env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	mock_dir, runs_file = _make_gh_mock(work_repo.parent, issue_bodies, pull_requests=pull_requests)
	result = _run(
		[
			"bash",
			str(FORCE_TICK_SCRIPT),
			"--repo-root",
			str(work_repo),
			"--repo",
			"owner/repo",
			*args,
		],
		cwd=REPO_ROOT,
			env={
				"PATH": f"{mock_dir}:{os.environ['PATH']}",
				"GITHUB_REPOSITORY": "owner/repo",
				"GITHUB_RUN_ID": "9001",
				"GH_PAT": "test-token",
				"GH_TOKEN": "test-token",
				"GH_MOCK_ISSUES_FILE": str(work_repo.parent / "issues.json"),
				"GH_MOCK_PULL_REQUESTS_FILE": str(work_repo.parent / "pulls.json"),
				"GH_MOCK_WORKFLOW_RUNS_FILE": str(runs_file),
				"GH_MOCK_WORKFLOW_RUN_EXIT_CODE": str(workflow_run_exit_code),
				**(env or {}),
			},
		)
	return result, runs_file


def _run_memory_force_tick_put(
	work_repo: Path,
	*,
	tracking_issue: int,
	record: dict[str, object],
) -> subprocess.CompletedProcess[str]:
	record_file = work_repo.parent / f"incoming-force-tick-{tracking_issue}.json"
	record_file.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
	command = (
		f"source {shlex.quote(str(MEMORY_HELPERS))}; "
		f"memory_force_tick_put --repo-root {shlex.quote(str(work_repo))} "
		f"--repo owner/repo --tracking-issue {tracking_issue} --record-file {shlex.quote(str(record_file))}"
	)
	return _run(
		["bash", "-lc", command],
		cwd=REPO_ROOT,
		env={"GH_PAT": "test-token", "GH_TOKEN": "test-token"},
	)


def _workflow_runs(path: Path) -> list[dict[str, object]]:
	text = path.read_text(encoding="utf-8").strip()
	if not text:
		return []
	return [json.loads(line) for line in text.splitlines() if line.strip()]


def _read_force_tick_record(work_repo: Path, tracking_issue: int) -> dict[str, object]:
	bare = work_repo.parent / "bare.git"
	result = _run(
		[
			"git",
			f"--git-dir={bare}",
			"show",
			f"ai-memory:ai-memory/runs/force_tick/{tracking_issue}.json",
		],
		cwd=REPO_ROOT,
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def _write_force_tick_record(work_repo: Path, tracking_issue: int, payload: dict[str, object]) -> None:
	clone_dir = Path(tempfile.mkdtemp(prefix="force-tick-record-edit-"))
	try:
		bare = work_repo.parent / "bare.git"
		result = _run(["git", "clone", "--quiet", "--branch", "ai-memory", str(bare), str(clone_dir)], cwd=REPO_ROOT)
		assert result.returncode == 0, result.stderr
		for key, value in (("user.name", "test"), ("user.email", "t@example.com")):
			result = _run(["git", "-C", str(clone_dir), "config", key, value], cwd=REPO_ROOT)
			assert result.returncode == 0, result.stderr
		path = clone_dir / "ai-memory" / "runs" / "force_tick" / f"{tracking_issue}.json"
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
		for args in (
			("add", str(path.relative_to(clone_dir))),
			("commit", "--quiet", "-m", "rewrite force tick"),
			("push", "origin", "ai-memory", "--quiet"),
		):
			result = _run(["git", "-C", str(clone_dir), *args], cwd=REPO_ROOT)
			assert result.returncode == 0, result.stderr
	finally:
		shutil.rmtree(clone_dir, ignore_errors=True)


def _iso_seconds_ago(seconds: int) -> str:
	return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_force_tick_dispatches_and_writes_state() -> None:
	_, work_repo = _create_repo()
	issue_bodies = {
		501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n",
	}
	result, runs_file = _run_force_tick(
		work_repo,
		issue_bodies,
		args=[
			"--issue",
			"501",
			"--reason",
			"implement-pr-created",
			"--source-workflow",
			"implement",
			"--run-id",
			"9001",
		],
	)
	assert result.returncode == 0, result.stderr
	runs = _workflow_runs(runs_file)
	assert runs == [{"workflow": "internal-orchestrate-poll.yml", "repo": "owner/repo"}]
	record = _read_force_tick_record(work_repo, 3042)
	assert record["tracking_issue"] == 3042
	assert record["dispatch_status"] == "sent"
	assert record["last_dispatch_payload"] == {
		"issue": 501,
		"reason": "implement-pr-created",
		"run_id": 9001,
		"source_workflow": "implement",
	}


def test_force_tick_resolves_tracking_issue_from_pr_branch_refs() -> None:
	_, work_repo = _create_repo()
	result, runs_file = _run_force_tick(
		work_repo,
		{88: "Automated implementation. Closes #3058\n"},
		pull_requests={
			88: {
				"body": "Automated implementation. Closes #3058\n",
				"head_ref": "ai/issue-3058",
				"base_ref": "orchestrator/project-3042",
			}
		},
		args=["--issue", "88", "--reason", "review-post-merge", "--source-workflow", "review_autofix", "--run-id", "9001"],
	)
	assert result.returncode == 0, result.stderr
	assert _workflow_runs(runs_file) == [{"workflow": "internal-orchestrate-poll.yml", "repo": "owner/repo"}]
	record = _read_force_tick_record(work_repo, 3042)
	assert record["last_dispatch_payload"]["issue"] == 88
	assert record["dispatch_status"] == "sent"


def test_force_tick_second_call_inside_cooldown_noops() -> None:
	_, work_repo = _create_repo()
	issue_bodies = {
		501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n",
	}
	first, runs_file = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "review-blocked", "--source-workflow", "review_autofix", "--run-id", "9001"],
	)
	assert first.returncode == 0, first.stderr
	second, _ = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "review-blocked", "--source-workflow", "review_autofix", "--run-id", "9002"],
	)
	assert second.returncode == 0, second.stderr
	assert len(_workflow_runs(runs_file)) == 1
	record = _read_force_tick_record(work_repo, 3042)
	assert record["last_dispatch_payload"]["run_id"] == 9001


def test_force_tick_dispatches_again_after_cooldown() -> None:
	_, work_repo = _create_repo()
	issue_bodies = {
		501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n",
	}
	first, runs_file = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "validation-finalized", "--source-workflow", "validate", "--run-id", "9001"],
	)
	assert first.returncode == 0, first.stderr
	record = _read_force_tick_record(work_repo, 3042)
	record["last_attempted_timestamp"] = _iso_seconds_ago(90)
	record["last_dispatch_timestamp"] = _iso_seconds_ago(90)
	_write_force_tick_record(work_repo, 3042, record)
	second, _ = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "validation-finalized", "--source-workflow", "validate", "--run-id", "9002"],
	)
	assert second.returncode == 0, second.stderr
	assert len(_workflow_runs(runs_file)) == 2
	updated = _read_force_tick_record(work_repo, 3042)
	assert updated["last_dispatch_payload"]["run_id"] == 9002


def test_force_tick_disabled_run_does_not_consume_reenabled_window() -> None:
	_, work_repo = _create_repo()
	issue_bodies = {
		501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n",
	}
	disabled, runs_file = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "review-blocked", "--source-workflow", "review_autofix", "--run-id", "9001"],
		env={"FORCE_TICK_ENABLED": "false"},
	)
	assert disabled.returncode == 0, disabled.stderr
	assert _workflow_runs(runs_file) == []
	record = _read_force_tick_record(work_repo, 3042)
	assert record["dispatch_status"] == "disabled"

	reenabled, _ = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "review-blocked", "--source-workflow", "review_autofix", "--run-id", "9002"],
	)
	assert reenabled.returncode == 0, reenabled.stderr
	assert _workflow_runs(runs_file) == [{"workflow": "internal-orchestrate-poll.yml", "repo": "owner/repo"}]
	updated = _read_force_tick_record(work_repo, 3042)
	assert updated["dispatch_status"] == "sent"
	assert updated["last_dispatch_payload"]["run_id"] == 9002


def test_force_tick_records_dispatch_failure_status() -> None:
	_, work_repo = _create_repo()
	issue_bodies = {
		501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n",
	}
	result, runs_file = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "validation-finalized", "--source-workflow", "validate", "--run-id", "9001"],
		workflow_run_exit_code=1,
	)
	assert result.returncode == 0, result.stderr
	assert _workflow_runs(runs_file) == []
	record = _read_force_tick_record(work_repo, 3042)
	assert record["dispatch_status"] == "failed"


def test_force_tick_noops_when_tracking_issue_is_absent() -> None:
	_, work_repo = _create_repo()
	result, runs_file = _run_force_tick(
		work_repo,
		{501: "Standalone issue without orchestrator metadata\n"},
		args=["--issue", "501", "--reason", "review-post-merge", "--source-workflow", "review_autofix", "--run-id", "9001"],
	)
	assert result.returncode == 0, result.stderr
	assert _workflow_runs(runs_file) == []
	assert "GitHub metadata lookup failed" not in result.stdout


def test_force_tick_warns_when_tracking_issue_lookups_fail() -> None:
	_, work_repo = _create_repo()
	result, runs_file = _run_force_tick(
		work_repo,
		{501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n"},
		args=["--issue", "501", "--reason", "review-blocked", "--source-workflow", "review_autofix", "--run-id", "9001"],
		env={"GH_MOCK_FAIL_PULLS": "1", "GH_MOCK_FAIL_ISSUES": "1"},
	)
	assert result.returncode == 0, result.stderr
	assert _workflow_runs(runs_file) == []
	assert "GitHub metadata lookup failed" in result.stdout


def test_memory_force_tick_put_refuses_same_window_overwrite_for_new_attempt() -> None:
	_, work_repo = _create_repo()
	issue_bodies = {
		501: "- Tracking issue: #3042\n- Managed by: AI Orchestrator\n",
	}
	seed, _ = _run_force_tick(
		work_repo,
		issue_bodies,
		args=["--issue", "501", "--reason", "implement-pr-created", "--source-workflow", "implement", "--run-id", "9001"],
	)
	assert seed.returncode == 0, seed.stderr

	current_record = {
		"schema_version": "force_tick.v1",
		"tracking_issue": 3042,
		"last_attempted_timestamp": _iso_seconds_ago(1),
		"last_attempt_payload": {
			"issue": 501,
			"reason": "review-blocked",
			"run_id": 9001,
			"source_workflow": "review_autofix",
		},
		"dispatch_status": "pending",
		"last_dispatch_timestamp": None,
		"last_dispatch_payload": None,
	}
	_write_force_tick_record(work_repo, 3042, current_record)

	incoming_record = {
		"schema_version": "force_tick.v1",
		"tracking_issue": 3042,
		"last_attempted_timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
		"last_attempt_payload": {
			"issue": 501,
			"reason": "review-blocked",
			"run_id": 9002,
			"source_workflow": "review_autofix",
		},
		"dispatch_status": "pending",
		"last_dispatch_timestamp": None,
		"last_dispatch_payload": None,
	}
	put = _run_memory_force_tick_put(work_repo, tracking_issue=3042, record=incoming_record)
	assert put.returncode == 0, put.stderr
	payload = json.loads(put.stdout)
	assert payload["stored"] is False
	assert payload["record"]["last_attempted_timestamp"] == current_record["last_attempted_timestamp"]
	stored = _read_force_tick_record(work_repo, 3042)
	assert stored["last_attempted_timestamp"] == current_record["last_attempted_timestamp"]


def test_memory_helpers_export_force_tick_wrappers() -> None:
	text = MEMORY_HELPERS.read_text(encoding="utf-8")
	assert "memory_force_tick_get()" in text
	assert "memory_force_tick_put()" in text


def test_phase_end_paths_call_shared_force_tick_helper() -> None:
	implement_text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	review_text = REVIEW_WORKFLOW.read_text(encoding="utf-8")
	validate_text = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	resolver_text = RESOLVER_SCRIPT.read_text(encoding="utf-8")

	assert "orchestrate_force_tick.sh" in implement_text
	assert "bash scripts/orchestrate_force_tick.sh" in implement_text
	assert "orchestrate_force_tick.sh" in review_text
	assert review_text.count("orchestrate_force_tick.sh") >= 4
	assert "bash scripts/orchestrate_force_tick.sh" in validate_text
	assert "orchestrate_force_tick.sh" in resolver_text
	assert 'gh workflow run "${_poll_workflow}"' not in resolver_text
	assert "Immediate orchestrator-poll dispatch helper failed" in resolver_text


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	passed = 0
	for func in test_functions:
		func()
		passed += 1
	print(f"OK: {passed} force-tick contract tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
