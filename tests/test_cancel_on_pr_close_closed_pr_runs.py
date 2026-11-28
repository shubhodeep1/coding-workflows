"""Issue #10260: AI Cancel on PR Close cancels the closed PR's runs even after
GitHub stopped linking them to the PR.

GitHub fills a run's `pull_requests` list with the open PRs whose head matches
at the moment of the request, so after the close the list is empty and the
linked-only filter cancelled nothing. tele-funtoken-msg-scoring PR #5800's
review run (gate done, merge job queued) kept going and merged the closed PR.
The step now also matches unlinked runs on the closed head branch that ran on
the PR's head repository and started at or after the PR was opened.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "cancel_on_pr_close.yml"
STEP = "Cancel queued/in-progress runs for closed PR branch"

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as fh:
	fh.write(" ".join(args) + "\n")
if "-X" in args and "POST" in args:
	sys.exit(0)
if args and args[0] == "api" and any(a.endswith("/actions/runs") for a in args):
	status = next((a.split("=", 1)[1] for a in args if a.startswith("status=")), "")
	runs = json.load(open(os.environ["FAKE_RUNS"]))
	sys.stdout.write(json.dumps({"workflow_runs": [r for r in runs if r["status"] == status]}))
	sys.exit(0)
sys.exit(0)
'''

BRANCH = "ai/issue-5797"
REPO = "o/r"


def _step_run_body() -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	start = text.find(f"      - name: {STEP}\n")
	assert start != -1
	lines = text[start:].splitlines()
	run_idx = next(i for i, line in enumerate(lines) if line == "        run: |")
	body: list[str] = []
	for line in lines[run_idx + 1:]:
		if line.strip() and not line.startswith(" " * 10):
			break
		body.append(line[10:])
	return "\n".join(body) + "\n"


def _run(run_id: int, *, status: str = "in_progress", linked: list[int] | None = None, head_repo: str = REPO,
		created_at: str = "2026-11-24T11:50:00Z", branch: str = BRANCH, path: str = ".github/workflows/ai-review.yml") -> dict:
	return {
		"id": run_id, "event": "pull_request", "status": status, "head_branch": branch, "path": path,
		"pull_requests": [{"number": n} for n in (linked or [])],
		"head_repository": {"full_name": head_repo}, "created_at": created_at,
	}


def _cancel(tmp_path: Path, runs: list[dict], **env_extra: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	(tmp_path / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
	calls = tmp_path / "calls.log"
	calls.write_text("", encoding="utf-8")
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
	env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_RUNS": str(tmp_path / "runs.json"), "FAKE_CALLS": str(calls),
		"EVENT_NAME": "pull_request", "PR_NUMBER": "5800", "PR_HEAD_REF": BRANCH, "REPOSITORY": REPO,
		"CURRENT_RUN_ID": "1", "PR_HEAD_REPO": REPO, "PR_CREATED_AT": "2026-11-24T11:40:00Z",
	})
	env.update(env_extra)
	res = subprocess.run(["bash", "-c", _step_run_body()], cwd=tmp_path, env=env, capture_output=True, text=True,
		check=False)
	cancelled = [line.split("/")[-2] for line in calls.read_text(encoding="utf-8").splitlines() if line.endswith("/cancel")]
	return res, cancelled


def test_unlinked_review_run_with_queued_merge_job_is_cancelled(tmp_path: Path) -> None:
	# Gate job finished, merge job queued: the run itself is in_progress and,
	# after the close, GitHub returns it with an empty pull_requests list.
	res, cancelled = _cancel(tmp_path, [_run(45386407946)])
	assert res.returncode == 0, res.stdout + res.stderr
	assert cancelled == ["45386407946"]


def test_queued_unlinked_run_is_cancelled(tmp_path: Path) -> None:
	_, cancelled = _cancel(tmp_path, [_run(11, status="queued")])
	assert cancelled == ["11"]


def test_linked_run_is_still_cancelled(tmp_path: Path) -> None:
	_, cancelled = _cancel(tmp_path, [_run(12, linked=[5800])])
	assert cancelled == ["12"]


def test_fork_run_on_the_same_branch_is_left_alone(tmp_path: Path) -> None:
	_, cancelled = _cancel(tmp_path, [_run(13, head_repo="fork/r")])
	assert cancelled == []


def test_run_linked_to_another_pr_is_left_alone(tmp_path: Path) -> None:
	_, cancelled = _cancel(tmp_path, [_run(14, linked=[6000])])
	assert cancelled == []


def test_run_older_than_the_pr_is_left_alone(tmp_path: Path) -> None:
	_, cancelled = _cancel(tmp_path, [_run(15, created_at="2026-11-24T11:00:00Z")])
	assert cancelled == []


def test_cancel_workflow_itself_is_never_cancelled(tmp_path: Path) -> None:
	_, cancelled = _cancel(tmp_path, [_run(16, path=".github/workflows/ai-cancel-on-pr-close.yml")])
	assert cancelled == []


def test_missing_env_keeps_linked_only_behaviour(tmp_path: Path) -> None:
	runs = [_run(17), _run(18, linked=[5800])]
	_, cancelled = _cancel(tmp_path, runs, PR_HEAD_REPO="")
	assert cancelled == ["18"]


def test_malformed_created_at_keeps_linked_only_behaviour(tmp_path: Path) -> None:
	runs = [_run(19), _run(20, linked=[5800])]
	_, cancelled = _cancel(tmp_path, runs, PR_CREATED_AT="yesterday")
	assert cancelled == ["20"]


def test_job_env_wires_the_head_repository_and_creation_time() -> None:
	text = WORKFLOW.read_text(encoding="utf-8")
	assert "PR_HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}" in text
	assert "PR_CREATED_AT: ${{ github.event.pull_request.created_at }}" in text
