#!/usr/bin/env python3
"""Release-gate clarify waits name the Clarify run and fail fast on clarify's
own failure comment (issue #9223).

Runs the real wait-clarify step of the e2e-smoke-test job and the alt-model
wait step against a stub ``gh``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"

TEST_REPO = "owner/repo"
ISSUE_NUMBER = "9206"
ISSUE_TITLE = "[E2E Smoke Test] Update smoke-test canary (run 1)"
ALT_TITLE = "[E2E Smoke Test alt-model] update canary (run 1)"
CREATED_AFTER = "2026-11-11T00:26:00Z"
REAL_DATE = shutil.which("date") or "/bin/date"

# Stub gh: serves issue labels (STUB_LABELS), the comment list (STUB_COMMENTS)
# and one actions/runs page (STUB_RUNS); applies --jq with the real jq and logs
# every call to STUB_GH_LOG.
STUB_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["STUB_GH_LOG"], "a", encoding="utf-8") as log:
	log.write(json.dumps(args) + "\n")
if not args or args[0] != "api":
	sys.exit(1)
path = args[1]
jq_filter = args[args.index("--jq") + 1] if "--jq" in args else None
issue_path = "repos/" + os.environ["TEST_REPO"] + "/issues/" + os.environ["ISSUE_NUMBER"]
if path == issue_path:
	payload = {"labels": [{"name": n} for n in os.environ.get("STUB_LABELS", "").split(",") if n]}
elif path.startswith(issue_path + "/comments"):
	payload = json.loads(os.environ.get("STUB_COMMENTS", "[]"))
elif "/actions/runs?" in path:
	payload = {"workflow_runs": json.loads(os.environ.get("STUB_RUNS", "[]"))}
else:
	sys.stderr.write("stub gh: unexpected path " + path + "\n")
	sys.exit(1)
text = json.dumps(payload)
if jq_filter is not None:
	text = subprocess.run(["jq", "-r", jq_filter], input=text, capture_output=True, text=True, check=True).stdout.rstrip("\n")
sys.stdout.write(text)
'''


def _doc() -> dict:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(job: str, step_id: str) -> dict:
	for step in _doc()["jobs"][job]["steps"]:
		if step.get("id") == step_id:
			return step
	raise AssertionError(f"{job}/{step_id} not found")


def _run(run_id: int, title: str = ISSUE_TITLE, conclusion: str | None = "failure", status: str = "completed", created_at: str = "2026-11-11T00:30:00Z") -> dict:
	return {"id": run_id, "name": "Internal: AI Clarify", "display_title": title, "conclusion": conclusion, "status": status, "created_at": created_at}


def _comment(body: str, association: str = "OWNER", login: str = "shubhodeep1") -> dict:
	return {"body": body, "author_association": association, "user": {"login": login}}


def _failed_body(run_id: str, repo: str = TEST_REPO) -> str:
	return f"AI clarification workflow failed for https://github.com/{repo}/issues/{ISSUE_NUMBER}. Run: https://github.com/{repo}/actions/runs/{run_id}"


def _execute(script: str, env_extra: dict[str, str], fake_date: bool = False) -> tuple[int, dict[str, str], list[list[str]], str]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(STUB_GH, encoding="utf-8")
		gh.chmod(0o755)
		sleep = bin_dir / "sleep"
		sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
		sleep.chmod(0o755)
		if fake_date:
			# First `date +%s` sets the deadline at 4500; every later read is past it.
			date = bin_dir / "date"
			date.write_text(
				f'#!/bin/sh\nif [ "$1" != "+%s" ]; then exec {REAL_DATE} "$@"; fi\n'
				'if [ -f "$STUB_DATE_MARK" ]; then echo 999999; else : > "$STUB_DATE_MARK"; echo 0; fi\n',
				encoding="utf-8",
			)
			date.chmod(0o755)
		log_file = tmp_path / "gh.log"
		log_file.touch()
		output_file = tmp_path / "github_output"
		output_file.touch()
		env = dict(os.environ)
		env.update({
			"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
			"TEST_REPO": TEST_REPO,
			"ISSUE_NUMBER": ISSUE_NUMBER,
			"POLL_INTERVAL": "0",
			"GITHUB_OUTPUT": str(output_file),
			"STUB_GH_LOG": str(log_file),
			"STUB_DATE_MARK": str(tmp_path / "date_mark"),
		})
		env.update(env_extra)
		proc = subprocess.run(["bash", "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
		outputs: dict[str, str] = {}
		for line in output_file.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
		return proc.returncode, outputs, calls, proc.stdout + proc.stderr


def _run_wait_clarify(comments: list[dict], runs: list[dict], phase_timeout: str = "0", labels: str = "ai:clarification") -> tuple[int, dict[str, str], str]:
	script = _step("e2e-smoke-test", "wait-clarify")["run"].replace("${{ steps.create-issue.outputs.created_after }}", CREATED_AFTER)
	rc, outputs, _calls, log = _execute(script, {
		"ISSUE_TITLE": ISSUE_TITLE,
		"PHASE_TIMEOUT": phase_timeout,
		"STUB_LABELS": labels,
		"STUB_COMMENTS": json.dumps(comments),
		"STUB_RUNS": json.dumps(runs),
	})
	return rc, outputs, log


def _error_lines(log: str) -> list[str]:
	return [line for line in log.splitlines() if line.startswith("::error::")]


def test_failure_comment_for_newest_run_fails_fast() -> None:
	rc, outputs, log = _run_wait_clarify([_comment(_failed_body("777"))], [_run(777)], phase_timeout="60")
	assert rc == 1, log
	assert outputs.get("status") == "clarify_failed", outputs
	errors = _error_lines(log)
	assert any("Clarify phase failed" in e and "https://github.com/owner/repo/actions/runs/777" in e for e in errors), log
	assert "Clarify phase stalled" not in log


def test_failure_comment_for_older_run_keeps_waiting_and_timeout_names_newest() -> None:
	runs = [_run(777), _run(778, conclusion=None, status="in_progress", created_at="2026-11-11T00:40:00Z")]
	rc, outputs, log = _run_wait_clarify([_comment(_failed_body("777"))], runs)
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "clarify_failed" not in json.dumps(outputs)
	assert "newer Clarify run 778 exists" in log, log
	assert any("Clarify phase stalled — no activity for 0 minutes (Clarify run: https://github.com/owner/repo/actions/runs/778)" in e for e in _error_lines(log)), log


def test_failure_comment_from_outsider_is_ignored() -> None:
	rc, outputs, log = _run_wait_clarify([_comment(_failed_body("777"), association="NONE", login="someone")], [_run(777)])
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs


def test_cancelled_comment_does_not_fail_fast_and_is_named_at_timeout() -> None:
	body = f"AI clarification workflow was cancelled/timed out for https://github.com/{TEST_REPO}/issues/{ISSUE_NUMBER}. Run: https://github.com/{TEST_REPO}/actions/runs/555"
	rc, outputs, log = _run_wait_clarify([_comment(body, association="COLLABORATOR")], [])
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	stall = [e for e in _error_lines(log) if "Clarify phase stalled" in e]
	assert stall and "no non-skipped Clarify run titled" in stall[0], log
	assert "(cancelled Clarify run: https://github.com/owner/repo/actions/runs/555)" in stall[0], log


def test_timeout_names_found_clarify_run() -> None:
	rc, outputs, log = _run_wait_clarify([], [_run(901, conclusion=None, status="in_progress")])
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert "::error::Clarify phase stalled — no activity for 0 minutes (Clarify run: https://github.com/owner/repo/actions/runs/901)" in _error_lines(log), log


def test_timeout_without_a_run_says_so() -> None:
	rc, outputs, log = _run_wait_clarify([], [])
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert any(f"no non-skipped Clarify run titled '{ISSUE_TITLE}' found" in e for e in _error_lines(log)), log


def test_hostile_comment_bodies_do_not_inject_or_fail_fast() -> None:
	comments = [
		_comment("AI clarification workflow failed for x\n::error::injected Run: https://github.com/evil/repo/actions/runs/777"),
		_comment("AI clarification workflow failed for y Run: https://github.com/owner/repo/actions/runs/abc"),
	]
	rc, outputs, log = _run_wait_clarify(comments, [_run(777)])
	assert rc == 1, log
	assert outputs.get("status") == "timeout", outputs
	assert not any(line.startswith("::error::injected") for line in log.splitlines()), log
	assert "injected" not in log


def test_clarification_needed_bot_comment_is_unchanged() -> None:
	comments = [_comment("Clarification is needed: NEEDS_CLARIFICATION", association="NONE", login="github-actions[bot]")]
	rc, outputs, log = _run_wait_clarify(comments, [], phase_timeout="60")
	assert rc == 1, log
	assert outputs.get("status") == "clarification_needed", outputs


def _run_alt_wait(title: str, runs: list[dict]) -> tuple[int, list[list[str]], str]:
	script = _step("e2e-alt-model-test", "wait")["run"]
	rc, _outputs, calls, log = _execute(script, {
		"ALT_ISSUE_TITLE": title,
		"ALT_CREATED_AFTER": CREATED_AFTER if title else "",
		"STUB_LABELS": "ai:clarification",
		"STUB_RUNS": json.dumps(runs),
	}, fake_date=True)
	return rc, calls, log


def test_alt_model_deadline_names_scoped_clarify_run() -> None:
	runs = [_run(321, title=ALT_TITLE), _run(999, title=ISSUE_TITLE, created_at="2026-11-11T00:50:00Z")]
	rc, _calls, log = _run_alt_wait(ALT_TITLE, runs)
	assert rc == 1, log
	assert "::error::Alt-model run timed out before reaching review stage (Clarify run: https://github.com/owner/repo/actions/runs/321)" in _error_lines(log), log


def test_alt_model_deadline_without_title_makes_no_runs_lookup() -> None:
	rc, calls, log = _run_alt_wait("", [_run(321, title=ALT_TITLE)])
	assert rc == 1, log
	assert "::error::Alt-model run timed out before reaching review stage" in _error_lines(log), log
	assert not any("/actions/runs?" in call[1] for call in calls if len(call) > 1), calls


def test_alt_model_title_and_created_after_wiring() -> None:
	create = _step("e2e-alt-model-test", "create")
	assert 'echo "created_after=${ALT_CREATED_AT}" >> "$GITHUB_OUTPUT"' in create["run"]
	assert 'echo "title=${TITLE}" >> "$GITHUB_OUTPUT"' in create["run"]
	wait = _step("e2e-alt-model-test", "wait")
	assert wait["env"]["ALT_ISSUE_TITLE"] == "${{ steps.create.outputs.title }}"
	assert wait["env"]["ALT_CREATED_AFTER"] == "${{ steps.create.outputs.created_after }}"
	assert "steps.create.outputs.title" not in wait["run"]
	assert "steps.create.outputs.created_after" not in wait["run"]
