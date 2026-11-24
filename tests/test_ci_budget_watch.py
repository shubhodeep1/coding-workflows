"""scripts/ci_budget_watch.py: early CI budget warnings that open one issue per finding."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci_budget_watch.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_budget_watch as cbw  # noqa: E402


def _tree(tmp_path: Path, sizes: dict[str, int], ci: dict) -> Path:
	wf = tmp_path / ".github" / "workflows"
	wf.mkdir(parents=True)
	for name, size in sizes.items():
		(wf / name).write_text("x" * size, encoding="utf-8")
	(wf / "ci.yml").write_text(yaml.safe_dump(ci), encoding="utf-8")
	return tmp_path


CI = {"jobs": {"tests-a": {"timeout-minutes": 20}, "orchestrate-poll": {"timeout-minutes": 20}, "static": {"timeout-minutes": 15}}}


def _job(name: str, seconds: int, conclusion: str = "success") -> dict:
	return {"name": name, "started_at": "2026-10-09T10:00:00Z",
		"completed_at": f"2026-10-09T10:{seconds // 60:02d}:{seconds % 60:02d}Z", "conclusion": conclusion}


def test_workflow_size_findings_use_the_warn_threshold(tmp_path: Path) -> None:
	root = _tree(tmp_path, {"big.yml": 450_000, "small.yml": 10}, CI)
	findings = cbw.workflow_size_findings(root / ".github" / "workflows", 440_000)
	assert [(f["subject"], f["value"]) for f in findings] == [(".github/workflows/big.yml", 450_000)]


def test_job_duration_findings_match_matrix_jobs_and_skip_skipped(tmp_path: Path) -> None:
	root = _tree(tmp_path, {}, CI)
	timeouts = cbw.job_timeouts(root / ".github" / "workflows" / "ci.yml")
	jobs = [_job("tests-a", 19 * 60 + 36), _job("orchestrate-poll (2)", 16 * 60), _job("static", 5 * 60),
		_job("tests-a", 19 * 60, conclusion="skipped"), {"name": "unknown", "started_at": None, "completed_at": None}]
	findings = cbw.job_duration_findings(jobs, timeouts, 0.75)
	assert [(f["subject"], f["value"]) for f in findings] == [("tests-a", 1176), ("orchestrate-poll", 960)]
	assert findings[1]["job"] == "orchestrate-poll (2)"


def test_job_duration_findings_ignore_jobs_that_ran_no_step(tmp_path: Path, capsys) -> None:
	# #9559: a job cancelled while queued has started_at/completed_at 33m49s
	# apart but no step ran; that is wait time, not a near-timeout job.
	root = _tree(tmp_path, {}, CI)
	timeouts = cbw.job_timeouts(root / ".github" / "workflows" / "ci.yml")
	queued = {**_job("tests-a", 33 * 60 + 49, conclusion="cancelled"), "steps": []}
	assert cbw.job_duration_findings([queued], timeouts, 0.75) == []
	unstarted = {**queued, "steps": [{"name": "Set up job", "started_at": None}, {"name": "Run tests", "started_at": None}]}
	assert cbw.job_duration_findings([unstarted], timeouts, 0.75) == []
	assert "::notice::ci_budget_watch: job tests-a ran no step (conclusion=cancelled); duration not checked." in capsys.readouterr().out


def test_job_duration_findings_measure_from_first_started_step(tmp_path: Path) -> None:
	root = _tree(tmp_path, {}, CI)
	timeouts = cbw.job_timeouts(root / ".github" / "workflows" / "ci.yml")
	# Queued 15 minutes, then ran 5: no finding at a 20-minute timeout.
	job = {**_job("tests-a", 20 * 60), "steps": [
		{"name": "Run tests", "started_at": "2026-10-09T10:16:00Z"},
		{"name": "Set up job", "started_at": "2026-10-09T10:15:00Z"},
		{"name": "Post", "started_at": None},
	]}
	assert cbw.job_duration_findings([job], timeouts, 0.75) == []
	# A job that hit its timeout ran its steps; it is still reported.
	timed_out = {**_job("tests-a", 20 * 60, conclusion="cancelled"), "steps": [{"name": "Set up job", "started_at": "2026-10-09T10:00:00Z"}]}
	findings = cbw.job_duration_findings([timed_out], timeouts, 0.75)
	assert [(f["subject"], f["value"]) for f in findings] == [("tests-a", 1200)]


def test_job_duration_findings_drop_unparsable_step_starts(tmp_path: Path, capsys) -> None:
	# A malformed step time is dropped instead of moving the start later.
	root = _tree(tmp_path, {}, CI)
	timeouts = cbw.job_timeouts(root / ".github" / "workflows" / "ci.yml")
	job = {**_job("tests-a", 19 * 60), "steps": [
		{"name": "Run tests", "started_at": "2026-10-09T10:16:00Z"},
		{"name": "Broken", "started_at": "not-a-time"},
		{"name": "Set up job", "started_at": "2026-10-09T10:00:00"},
	]}
	findings = cbw.job_duration_findings([job], timeouts, 0.75)
	assert [(f["subject"], f["value"]) for f in findings] == [("tests-a", 1140)]
	# The no-step notice escapes the job name so it cannot end the command.
	queued = {**_job("tests-a\n::error::x", 60, conclusion="cancelled"), "steps": []}
	assert cbw.job_duration_findings([queued], {"tests-a\n::error::x": 20}, 0.75) == []
	assert "job tests-a%0A::error::x ran no step" in capsys.readouterr().out
	# A job still queued (no completed_at) has not run yet and is not logged.
	pending = {"name": "tests-a", "started_at": None, "completed_at": None, "conclusion": None, "steps": []}
	assert cbw.job_duration_findings([pending], timeouts, 0.75) == []
	assert "ci_budget_watch" not in capsys.readouterr().out


def test_job_duration_findings_log_jobs_with_no_usable_start(tmp_path: Path, capsys) -> None:
	# Every step time is malformed: the job is not measured, and that is logged.
	root = _tree(tmp_path, {}, CI)
	timeouts = cbw.job_timeouts(root / ".github" / "workflows" / "ci.yml")
	job = {**_job("tests-a", 19 * 60), "steps": [{"name": "Broken", "started_at": "not-a-time"}]}
	assert cbw.job_duration_findings([job], timeouts, 0.75) == []
	assert "::notice::ci_budget_watch: job tests-a has no usable start or completion time (conclusion=success); duration not checked." in capsys.readouterr().out
	# A malformed completed_at, or a null started_at with no steps list, is logged the same way.
	bad_end = {**_job("tests-a", 60), "completed_at": "not-a-time"}
	no_start = {**_job("tests-a", 60), "started_at": None}
	assert cbw.job_duration_findings([bad_end, no_start], timeouts, 0.75) == []
	assert capsys.readouterr().out.count("has no usable start or completion time") == 2
	# A non-string step start is ignored rather than crashing `_seconds`.
	numeric = {**_job("tests-a", 60), "steps": [{"name": "Odd", "started_at": 12345}]}
	assert cbw.job_duration_findings([numeric], timeouts, 0.75) == []
	assert "ran no step (conclusion=success)" in capsys.readouterr().out
	# A job still running has no completed_at and is skipped without a notice.
	running = {"name": "tests-a", "started_at": "2026-10-09T10:00:00Z", "completed_at": None, "conclusion": None}
	assert cbw.job_duration_findings([running], timeouts, 0.75) == []
	assert "ci_budget_watch" not in capsys.readouterr().out


def test_job_duration_findings_survive_non_string_job_times(tmp_path: Path, capsys) -> None:
	# A non-string job-level time is not measured and does not crash the check.
	root = _tree(tmp_path, {}, CI)
	timeouts = cbw.job_timeouts(root / ".github" / "workflows" / "ci.yml")
	odd_start = {**_job("tests-a", 60), "started_at": 12345}
	odd_end = {**_job("tests-a", 60), "completed_at": 12345}
	assert cbw.job_duration_findings([odd_start, odd_end, _job("tests-a", 19 * 60)], timeouts, 0.75)[0]["value"] == 1140
	assert capsys.readouterr().out.count("has no usable start or completion time") == 2


def test_issue_text_names_the_fix_and_carries_the_marker() -> None:
	size = {"kind": "workflow_size", "subject": ".github/workflows/review_autofix.yml", "value": 481_287, "limit": 480_000}
	assert cbw.issue_title(size) == "CI budget: .github/workflows/review_autofix.yml is 481,287 bytes (guard 480,000)"
	body = cbw.issue_body(size, "https://github.com/o/r/actions/runs/1")
	assert body.startswith("<!-- ai:ci-budget:v1 kind=workflow_size subject=.github/workflows/review_autofix.yml -->")
	assert "Never raise the guard." in body and "expanded_review_autofix_text()" in body
	dur = {"kind": "job_duration", "subject": "tests-a", "value": 1176, "limit": 1200, "job": "tests-a"}
	assert cbw.issue_title(dur) == "CI budget: CI job tests-a took 19m36s of its 20-minute timeout"
	assert "Do not raise `timeout-minutes`" in cbw.issue_body(dur, "u")


def _fake_gh(tmp_path: Path, issues: list[dict], jobs: list[dict], closed: list[dict] | None = None) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "gh.log"
	gh = bin_dir / "gh"
	gh.write_text(f"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read() if "--input" in args else ""
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{"args": args, "stdin": stdin, "token": os.environ.get("GH_TOKEN", "")}}) + "\\n")
path = args[1]
if path.endswith("/jobs?per_page=100"):
    print(json.dumps({{"jobs": {json.dumps(jobs)}}}))
elif "issues?state=open" in path:
    print(json.dumps({json.dumps(issues)}))
elif "issues?state=closed" in path and {closed is not None!r}:
    print(json.dumps({json.dumps(closed or [])}))
elif path.endswith("/issues"):
    print(json.dumps({{"number": 77}}))
elif path.endswith("/labels"):
    print(json.dumps({{"name": "ai:ci-budget"}}))
else:
    sys.exit(1)
""", encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(log)}


def _run(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
	full = {k: v for k, v in os.environ.items() if not k.startswith(("CI_BUDGET_", "GITHUB_"))}
	full.update(env)
	return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--repo", "o/r", "--run-id", "9"],
		capture_output=True, text=True, env=full, timeout=60, check=False)


def test_opens_one_issue_per_new_finding_and_skips_existing(tmp_path: Path) -> None:
	root = _tree(tmp_path, {"big.yml": 450_000}, CI)
	existing = [{"number": 5, "body": cbw.marker("workflow_size", ".github/workflows/big.yml")}]
	env = _fake_gh(tmp_path, existing, [_job("tests-a", 19 * 60)])
	env.update({"GH_TOKEN": "job-token", "CI_BUDGET_ISSUE_TOKEN": "issue-pat"})
	result = _run(root, env)
	assert result.returncode == 0, result.stderr
	calls = [json.loads(line) for line in Path(env["LOG"]).read_text().splitlines()]
	created = [c for c in calls if c["args"][1].endswith("/issues")]
	assert len(created) == 1
	assert created[0]["token"] == "issue-pat"
	assert all(c["token"] == "job-token" for c in calls if c not in created)
	payload = json.loads(created[0]["stdin"])
	assert payload["labels"] == ["ai:ci-budget"]
	labels = [c for c in calls if c["args"][1].endswith("/labels")]
	assert len(labels) == 1 and calls.index(labels[0]) < calls.index(created[0])
	assert json.loads(labels[0]["stdin"])["name"] == "ai:ci-budget"
	assert payload["title"].startswith("CI budget: CI job tests-a took 19m00s")
	assert "::warning::CI_BUDGET kind=workflow_size" in result.stdout
	assert "action=opened number=77" in result.stdout


def test_matrix_jobs_sharing_a_subject_open_one_issue(tmp_path: Path) -> None:
	root = _tree(tmp_path, {}, CI)
	env = _fake_gh(tmp_path, [], [_job("orchestrate-poll (0)", 16 * 60), _job("orchestrate-poll (1)", 17 * 60)])
	result = _run(root, env)
	assert result.returncode == 0, result.stderr
	calls = [json.loads(line) for line in Path(env["LOG"]).read_text().splitlines()]
	assert len([c for c in calls if c["args"][1].endswith("/issues")]) == 1
	assert "subject=orchestrate-poll action=exists" in result.stdout


def _closed_issue(number: int, subject: str, hours_ago: float) -> dict:
	closed_at = (cbw.datetime.now(cbw.timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
	return {"number": number, "state": "closed", "closed_at": closed_at, "body": cbw.marker("job_duration", subject) + "\nbody"}


def _created(env: dict[str, str]) -> list[dict]:
	calls = [json.loads(line) for line in Path(env["LOG"]).read_text().splitlines()]
	return [c for c in calls if c["args"][1].endswith("/issues")]


def test_recently_closed_issue_holds_back_a_marginal_refile(tmp_path: Path) -> None:
	# orchestrate-poll was re-filed a few hours after each fix closed, for a
	# job just over the 75% warning ratio. Closed two days ago: not re-filed.
	root = _tree(tmp_path, {}, CI)
	env = _fake_gh(tmp_path, [], [_job("tests-a", 16 * 60)], closed=[_closed_issue(9447, "tests-a", 48), _closed_issue(9214, "tests-a", 300), _closed_issue(9000, "other-job", 1)])
	result = _run(root, env)
	assert result.returncode == 0, result.stderr
	assert _created(env) == []
	assert "CI_BUDGET_ISSUE kind=job_duration subject=tests-a action=cooldown closed_issue=9447 cooldown_days=7 urgent_ratio=0.9" in result.stdout
	assert "::warning::CI_BUDGET kind=job_duration subject=tests-a" in result.stdout
	calls = [json.loads(line) for line in Path(env["LOG"]).read_text().splitlines()]
	closed_reads = [c for c in calls if "issues?state=closed" in c["args"][1]]
	assert len(closed_reads) == 1 and "labels=ai:ci-budget" in closed_reads[0]["args"][1] and "per_page=50" in closed_reads[0]["args"][1]


@pytest.mark.parametrize("case", ["urgent", "cooldown_over", "cooldown_off", "closed_list_fails", "other_subject"])
def test_refile_cooldown_does_not_hide_an_urgent_or_old_finding(tmp_path: Path, case: str) -> None:
	root = _tree(tmp_path, {}, CI)
	seconds = 19 * 60 if case == "urgent" else 16 * 60
	closed: list[dict] | None = [_closed_issue(9447, "other-job" if case == "other_subject" else "tests-a", 24 * 9 if case == "cooldown_over" else 48)]
	if case == "closed_list_fails":
		closed = None
	env = _fake_gh(tmp_path, [], [_job("tests-a", seconds)], closed=closed)
	if case == "cooldown_off":
		env["CI_BUDGET_REFILE_COOLDOWN_DAYS"] = "0"
	result = _run(root, env)
	assert result.returncode == 0, result.stderr
	assert len(_created(env)) == 1, result.stdout
	assert "action=opened number=77" in result.stdout and "action=cooldown" not in result.stdout
	calls = [json.loads(line) for line in Path(env["LOG"]).read_text().splitlines()]
	# No closed-issue read when the finding is urgent or the cooldown is off.
	assert bool([c for c in calls if "issues?state=closed" in c["args"][1]]) == (case not in ("urgent", "cooldown_off"))
	assert ("re-file cooldown not applied" in result.stdout) == (case == "closed_list_fails")


def test_recently_closed_issue_ignores_malformed_entries() -> None:
	now = cbw.datetime(2026, 11, 18, tzinfo=cbw.timezone.utc)
	finding = {"kind": "job_duration", "subject": "tests-a", "value": 960, "limit": 1200}
	body = cbw.marker("job_duration", "tests-a")
	issues = [None, "x", {"number": 1, "body": body}, {"number": 2, "body": body, "closed_at": "not a time"},
		{"number": 3, "body": body, "closed_at": "2026-11-17T00:00:00Z", "pull_request": {}},
		{"number": "4", "body": body, "closed_at": "2026-11-17T00:00:00Z"},
		{"number": 5, "body": body, "closed_at": "2026-11-16T00:00:00Z"}, {"number": 6, "body": body, "closed_at": "2026-11-17T00:00:00Z"}]
	assert cbw.recently_closed_issue(issues, finding, cooldown_days=7, now=now) == 6
	assert cbw.recently_closed_issue(issues, finding, cooldown_days=1, now=now) == 6
	assert cbw.recently_closed_issue(issues[:-1], finding, cooldown_days=1, now=now) is None
	assert cbw.recently_closed_issue(issues, finding, cooldown_days=0, now=now) is None


def test_api_failures_never_fail_the_job(tmp_path: Path) -> None:
	root = _tree(tmp_path, {"big.yml": 450_000}, CI)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	result = _run(root, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
	assert result.returncode == 0
	assert "could not read this run's jobs" in result.stdout
	assert "could not list open ai:ci-budget issues" in result.stdout


def test_ci_wiring() -> None:
	ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
	job = ci["jobs"]["budget-watch"]
	assert job["if"] == "always() && github.event_name == 'push' && github.ref == 'refs/heads/main'"
	assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
	assert "budget-watch" not in ci["jobs"]["lint"]["needs"]
	assert set(ci["jobs"]["lint"]["needs"]) <= set(job["needs"])
	assert "tests/test_ci_budget_watch.py" in (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	env = next(step["env"] for step in job["steps"] if "CI_BUDGET_JOB_WARN_RATIO" in (step.get("env") or {}))
	assert env["CI_BUDGET_REFILE_COOLDOWN_DAYS"] == "${{ vars.CI_BUDGET_REFILE_COOLDOWN_DAYS || '7' }}"
	assert env["CI_BUDGET_REFILE_URGENT_RATIO"] == "${{ vars.CI_BUDGET_REFILE_URGENT_RATIO || '0.9' }}"
