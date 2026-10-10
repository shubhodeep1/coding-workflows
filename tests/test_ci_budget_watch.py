"""scripts/ci_budget_watch.py: early CI budget warnings that open one issue per finding."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

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


def test_issue_text_names_the_fix_and_carries_the_marker() -> None:
	size = {"kind": "workflow_size", "subject": ".github/workflows/review_autofix.yml", "value": 481_287, "limit": 480_000}
	assert cbw.issue_title(size) == "CI budget: .github/workflows/review_autofix.yml is 481,287 bytes (guard 480,000)"
	body = cbw.issue_body(size, "https://github.com/o/r/actions/runs/1")
	assert body.startswith("<!-- ai:ci-budget:v1 kind=workflow_size subject=.github/workflows/review_autofix.yml -->")
	assert "Never raise the guard." in body and "expanded_review_autofix_text()" in body
	dur = {"kind": "job_duration", "subject": "tests-a", "value": 1176, "limit": 1200, "job": "tests-a"}
	assert cbw.issue_title(dur) == "CI budget: CI job tests-a took 19m36s of its 20-minute timeout"
	assert "Do not raise `timeout-minutes`" in cbw.issue_body(dur, "u")


def _fake_gh(tmp_path: Path, issues: list[dict], jobs: list[dict]) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	log = tmp_path / "gh.log"
	gh = bin_dir / "gh"
	gh.write_text(f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
stdin = sys.stdin.read() if "--input" in args else ""
with open({str(log)!r}, "a") as fh:
    fh.write(json.dumps({{"args": args, "stdin": stdin}}) + "\\n")
path = args[1]
if path.endswith("/jobs?per_page=100"):
    print(json.dumps({{"jobs": {json.dumps(jobs)}}}))
elif "issues?state=open" in path:
    print(json.dumps({json.dumps(issues)}))
elif path.endswith("/issues"):
    print(json.dumps({{"number": 77}}))
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
	result = _run(root, env)
	assert result.returncode == 0, result.stderr
	calls = [json.loads(line) for line in Path(env["LOG"]).read_text().splitlines()]
	created = [c for c in calls if c["args"][1].endswith("/issues")]
	assert len(created) == 1
	payload = json.loads(created[0]["stdin"])
	assert payload["labels"] == ["ai:ci-budget"]
	assert payload["title"].startswith("CI budget: CI job tests-a took 19m00s")
	assert "::warning::CI_BUDGET kind=workflow_size" in result.stdout
	assert "action=opened number=77" in result.stdout


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
