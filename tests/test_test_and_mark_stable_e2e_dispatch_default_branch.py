#!/usr/bin/env python3
"""The E2E smoke gate dispatches its review runs from the default branch (issue #5520).

Phase 3c (the "Bug B" fallback after the bait commit) and Phase 4b (the retry)
of ``.github/workflows/test-and-mark-stable.yml`` used to dispatch
``${REVIEW_WORKFLOW_FILE}`` with ``--ref "${BRANCH}"``, which ran the smoke PR
branch's own copy of the wrapper with the test repo's secrets (finding
``e2e-dispatch-executes-unreviewed-workflow``). Both now dispatch without
``--ref``. Such a run has the default branch as its head, so Phase 4 and
Phase 4b find it through a workflow-scoped listing (``workflow_dispatch``
event, default branch, wrapper path, exact PR run name; a run name alone can be
forged, issue #5094) and bind it to the PR head it reviewed: the first
``Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.`` line in its
``codex-agent`` job log.

The helpers are taken from the workflow text and run in bash with a stubbed
GitHub API, so these tests exercise the shell the release gate runs.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

HELPERS_START = "          # ── Default-branch review dispatch runs (issue #5520) ──\n"
HELPERS_END = "          # ── end of the default-branch review dispatch helpers ──\n"

PIN = "a" * 40
OTHER = "b" * 40
BAIT_CREATED_AT = "2026-09-30T08:00:00Z"
PR = "4242"
INTERNAL_TITLE = f"Internal: AI Review & Autofix [pr:{PR}]"
INTERNAL_PATH = ".github/workflows/internal-review.yml"

# Stubs the step's gh api wrapper. Fixture files live in $FIXTURES: the
# default branch in default_branch.txt, the dispatch listing in listing.json,
# jobs in jobs_<run>.json, job logs in log_<job>.txt. A missing fixture is an
# API failure. Every call is appended to $CALLS.
STUB_API = r"""
stub_api() {
	printf '%s\n' "$1" >> "${CALLS}"
	case "$1" in
		"repos/${TEST_REPO}")
			[ -f "${FIXTURES}/default_branch.txt" ] || return 1
			cat "${FIXTURES}/default_branch.txt" ;;
		*/actions/workflows/*/runs\?*)
			[ -f "${FIXTURES}/listing.json" ] || return 1
			cat "${FIXTURES}/listing.json" ;;
		*/actions/runs/*/jobs*)
			local run_id="${1#*actions/runs/}"
			run_id="${run_id%%/*}"
			[ -f "${FIXTURES}/jobs_${run_id}.json" ] || return 1
			cat "${FIXTURES}/jobs_${run_id}.json" ;;
		*/actions/jobs/*/logs)
			local job_id="${1#*actions/jobs/}"
			job_id="${job_id%%/*}"
			[ -f "${FIXTURES}/log_${job_id}.txt" ] || return 1
			cat "${FIXTURES}/log_${job_id}.txt" ;;
		*) return 1 ;;
	esac
}
"""


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step_body(step_id: str) -> str:
	data = yaml.safe_load(_workflow_text())
	for job in data["jobs"].values():
		for step in job.get("steps", []):
			if step.get("id") == step_id:
				return step["run"]
	raise AssertionError(f"step {step_id} not found")


def _helper_blocks() -> list[str]:
	text = _workflow_text()
	blocks = []
	start = 0
	while True:
		begin = text.find(HELPERS_START, start)
		if begin == -1:
			break
		end = text.index(HELPERS_END, begin) + len(HELPERS_END)
		blocks.append(text[begin:end])
		start = end
	return blocks


def _function(body: str, name: str) -> str:
	match = re.search(rf"^( *){re.escape(name)}\(\) \{{\n.*?^\1\}}\n", body, re.MULTILINE | re.DOTALL)
	assert match, f"function {name} not found"
	return match.group(0)


def _phase4_combine_block() -> str:
	body = _step_body("wait-review")
	start = body.index("E2E_REVIEW_DISPATCH_WATCHING=0\n")
	end = body.index("# If rate-limited, skip this cycle entirely")
	return body[start:end]


def _dispatch_run(run_id: int, status: str = "completed", conclusion: str | None = "success", **overrides: object) -> dict:
	run = {
		"id": run_id,
		"name": "Internal: AI Review & Autofix",
		"display_title": INTERNAL_TITLE,
		"path": INTERNAL_PATH,
		"event": "workflow_dispatch",
		"head_branch": "main",
		"head_sha": "c" * 40,
		"status": status,
		"conclusion": conclusion,
		"created_at": "2026-09-30T08:05:00Z",
		"updated_at": "2026-09-30T08:30:00Z",
	}
	run.update(overrides)
	return run


def _codex_jobs(job_id: int) -> dict:
	return {
		"jobs": [
			{"id": job_id - 1, "name": "review / gate", "status": "completed"},
			{"id": job_id, "name": "review / codex-agent", "status": "completed"},
		]
	}


def _reviewed_log(sha: str) -> str:
	return "\n".join(
		[
			"2026-09-30T08:06:00.0000000Z ##[group]Run set -euo pipefail",
			'2026-09-30T08:06:00.0000001Z echo "Captured INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA} for stale-base detection."',
			"2026-09-30T08:06:01.0000000Z Captured INITIAL_HEAD_SHA=1234567 for stale-base detection.",
			f"2026-09-30T08:06:02.0000000Z Captured INITIAL_HEAD_SHA={sha} for stale-base detection.",
			f"2026-09-30T08:20:00.0000000Z Captured INITIAL_HEAD_SHA={OTHER} for stale-base detection.",
		]
	)


def _run_bash(script: str, fixtures: dict[str, str], env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, list[str]]:
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		fixture_dir = tmp_path / "fixtures"
		fixture_dir.mkdir()
		for name, content in fixtures.items():
			(fixture_dir / name).write_text(content, encoding="utf-8")
		calls = tmp_path / "calls.txt"
		calls.write_text("", encoding="utf-8")
		full_env = {
			"PATH": "/usr/local/bin:/usr/bin:/bin",
			"FIXTURES": str(fixture_dir),
			"CALLS": str(calls),
			"TEST_REPO": "owner/repo",
			"REVIEW_WORKFLOW_FILE": "internal-review.yml",
			"PR_NUMBER": PR,
		}
		full_env.update(env or {})
		proc = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + STUB_API + "E2E_REVIEW_DISPATCH_API=stub_api\n" + _helper_blocks()[0] + script],
			capture_output=True,
			text=True,
			env=full_env,
		)
		return proc, [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


# ── Static contracts ──────────────────────────────────────────────────


def test_review_dispatches_carry_no_ref() -> None:
	for step_id in ("inject-bait", "verify-bait-removed"):
		body = _step_body(step_id)
		calls = re.findall(r'gh workflow run "\$\{REVIEW_WORKFLOW_FILE\}"(?:[^\n]*\\\n)*[^\n]*', body)
		assert len(calls) == 1, (step_id, calls)
		assert "--ref" not in calls[0], (step_id, calls[0])
		assert '--repo "${TEST_REPO}"' in calls[0]
		assert '-f pr_number="${PR_NUMBER}"' in calls[0]
	assert "(Bug B fallback)" in _step_body("inject-bait")


def test_both_steps_carry_the_same_helper_block() -> None:
	blocks = _helper_blocks()
	assert len(blocks) == 2
	assert blocks[0] == blocks[1]
	for step_id in ("wait-review", "verify-bait-removed"):
		assert "# ── Default-branch review dispatch runs (issue #5520) ──" in _step_body(step_id)
	assert "E2E_REVIEW_DISPATCH_API=gh_api_safe_quiet_print\n" in _step_body("wait-review")
	assert "E2E_REVIEW_DISPATCH_API=gh_api_with_retry\n" in _step_body("verify-bait-removed")


def test_listing_is_workflow_scoped_to_dispatch_runs_on_the_default_branch() -> None:
	block = _helper_blocks()[0]
	assert (
		'"repos/${TEST_REPO}/actions/workflows/${REVIEW_WORKFLOW_FILE}/runs?event=workflow_dispatch&branch=${E2E_REVIEW_DISPATCH_DEFAULT_BRANCH}&per_page=100"'
		in block
	)
	assert ".event == \"workflow_dispatch\" and .head_branch == $branch and .path == $path and .display_title == $title" in block


def test_review_autofix_still_logs_the_reviewed_head_after_checkout() -> None:
	text = REVIEW_AUTOFIX.read_text(encoding="utf-8")
	line = 'echo "Captured INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA} for stale-base detection."'
	assert text.count(line) == 1
	checkout = text[text.index("- name: Checkout PR head branch") :]
	checkout = checkout[: checkout.index("\n      - name:", 1)]
	assert line in checkout
	assert checkout.index('git reset --hard "refs/remotes/origin/${HEAD_REF}"') < checkout.index(line)
	assert 'INITIAL_HEAD_SHA="$(git rev-parse HEAD)"' in checkout


def test_phase4_keeps_the_pinned_branch_filter_and_adds_the_dispatch_selection_after_it() -> None:
	body = _step_body("wait-review")
	branch_filter = body.index('REVIEW_RUN=$(gh_api_safe_quiet_print "repos/${TEST_REPO}/actions/runs?branch=${PR_BRANCH}&per_page=100"')
	selection = body.index("e2e_review_dispatch_pick\n")
	guard = body.index("# If rate-limited, skip this cycle entirely")
	assert branch_filter < selection < guard
	assert 'if [ -n "${PIN_SHA:-}" ] && [ "${RATE_LIMIT_BACKOFF:-0}" -eq 0 ]; then' in body
	assert 'if [ "$FAILED_STEPS" -gt 0 ] && [ "${E2E_REVIEW_DISPATCH_WATCHING}" = "0" ]; then' in body
	assert "if [ \"${E2E_REVIEW_DISPATCH_WATCHING}\" = \"0\" ] && grep -qaF '::warning::Editor summary contains failure/fallback markers'" in body


def test_phase4b_registers_only_default_branch_dispatch_runs_and_checks_the_reviewed_head() -> None:
	retry = _step_body("verify-bait-removed")
	retry = retry[retry.index("# ── Retry: adopt active work") : retry.index("# ── Attempt 2")]
	assert "BAIT_CREATED_AT: ${{ steps.inject-bait.outputs.bait_created_at }}" in _workflow_text()
	assert 'if [ "${E2E_RETRY_DISPATCH_READY}" != "1" ]; then' in retry
	assert retry.index('if [ "${E2E_RETRY_DISPATCH_READY}" != "1" ]; then') < retry.index('gh workflow run "${REVIEW_WORKFLOW_FILE}"')
	registration = retry[retry.index('if [ "$(date +%s)" -ge "${RETRY_REGISTRATION_DEADLINE}" ]') :]
	registration = registration[: registration.index("RETRY_RUN_JSON=")]
	assert 'E2E_RETRY_DISPATCH_RUNS=$(e2e_review_dispatch_runs "${PR_NUMBER}")' in registration
	assert "select(.id > $baseline_id and .id != $prior_run_id)" in registration
	assert "RETRY_RUNS_QUERY" not in registration
	assert "RETRY_BASELINE_ID=$(printf '%s\\n' \"${RETRY_RUNS_JSON}\" | jq -r '[.workflow_runs[].id] | max // 0')" in retry
	assert "E2E_RETRY_DISPATCH_BASELINE_ID=$(printf '%s\\n' \"${E2E_RETRY_DISPATCH_RUNS}\" | jq -r '[.[].id] | max // 0')" in retry
	assert 'RETRY_BASELINE_ID="${E2E_RETRY_DISPATCH_BASELINE_ID}"' in retry
	assert "--argjson branch_runs" not in retry
	assert "select(.created_at >= $bait_created_at)" in retry
	check_marker = 'if ! e2e_review_dispatch_resolve_head "${RETRY_RUN_ID}"; then'
	check = retry[retry.index(check_marker) :]
	assert 'echo "status=retry_dispatch_failed" >> "$GITHUB_OUTPUT"' in check
	assert 'echo "status=retry_workflow_failed" >> "$GITHUB_OUTPUT"' in check
	assert retry.index('if [ "${RETRY_CONCL}" != "success" ]; then') < retry.index('if [ "${E2E_RETRY_FROM_DISPATCH}" = "1" ]; then') < retry.index(check_marker)


# ── Executed helpers ──────────────────────────────────────────────────


def test_title_matches_only_the_wrappers_that_name_their_runs() -> None:
	proc, _ = _run_bash(
		'for f in internal-review.yml ai-review.yml review_autofix.yml custom.yml; do REVIEW_WORKFLOW_FILE="$f"; printf "%s=%s\\n" "$f" "$(e2e_review_dispatch_title 7)"; done\n',
		{},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.splitlines() == [
		"internal-review.yml=Internal: AI Review & Autofix [pr:7]",
		"ai-review.yml=AI Review [pr:7]",
		"review_autofix.yml=",
		"custom.yml=",
	]


def test_runs_filter_requires_event_branch_path_and_exact_title() -> None:
	runs = [
		_dispatch_run(10),
		_dispatch_run(11, path=".github/workflows/review_autofix.yml"),
		_dispatch_run(12, head_branch="ai/issue-9"),
		_dispatch_run(13, event="pull_request"),
		_dispatch_run(14, display_title="Internal: AI Review & Autofix [pr:9]"),
		_dispatch_run(15, display_title=f"AI Review [pr:{PR}]"),
		_dispatch_run(16, display_title=f"x {INTERNAL_TITLE}"),
	]
	proc, calls = _run_bash(
		'e2e_review_dispatch_resolve_default_branch\nruns=$(e2e_review_dispatch_runs "${PR_NUMBER}")\nprintf "%s" "${runs}" | jq -c "[.[].id]"\n',
		{"default_branch.txt": "main", "listing.json": json.dumps({"total_count": len(runs), "workflow_runs": runs})},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "[10]"
	assert calls == [
		"repos/owner/repo",
		"repos/owner/repo/actions/workflows/internal-review.yml/runs?event=workflow_dispatch&branch=main&per_page=100",
	]


def test_runs_listing_is_empty_without_a_title_or_a_default_branch_and_fails_on_bad_payloads() -> None:
	proc, calls = _run_bash('e2e_review_dispatch_runs "${PR_NUMBER}"\n', {"listing.json": "{}"}, {"REVIEW_WORKFLOW_FILE": "review_autofix.yml"})
	assert proc.returncode == 0 and proc.stdout == "[]" and calls == []
	proc, calls = _run_bash('e2e_review_dispatch_runs "${PR_NUMBER}"\n', {"listing.json": "{}"})
	assert proc.returncode == 0 and proc.stdout == "[]" and calls == []
	proc, _ = _run_bash(
		'E2E_REVIEW_DISPATCH_DEFAULT_BRANCH=main\nif e2e_review_dispatch_runs "${PR_NUMBER}"; then echo ok; else echo failed; fi\n',
		{"listing.json": '{"message": "Not Found"}'},
	)
	assert proc.stdout.strip().endswith("failed"), proc.stdout
	proc, _ = _run_bash(
		'E2E_REVIEW_DISPATCH_DEFAULT_BRANCH=main\nif e2e_review_dispatch_runs "${PR_NUMBER}"; then echo ok; else echo failed; fi\n',
		{},
	)
	assert proc.stdout.strip() == "failed"


def test_default_branch_is_resolved_once_and_rejected_when_malformed() -> None:
	proc, calls = _run_bash(
		"e2e_review_dispatch_resolve_default_branch\ne2e_review_dispatch_resolve_default_branch\necho \"${E2E_REVIEW_DISPATCH_DEFAULT_BRANCH}\"\n",
		{"default_branch.txt": "main"},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "main"
	assert calls == ["repos/owner/repo"]
	proc, _ = _run_bash(
		'if e2e_review_dispatch_resolve_default_branch; then echo ok; else echo "failed:${E2E_REVIEW_DISPATCH_DEFAULT_BRANCH}"; fi\n',
		{"default_branch.txt": "main&x=1"},
	)
	assert proc.stdout.strip() == "failed:"


def test_reviewed_head_is_the_first_expanded_line_and_is_cached() -> None:
	proc, calls = _run_bash(
		'e2e_review_dispatch_resolve_head 10\ne2e_review_dispatch_resolve_head 10\necho "${E2E_REVIEW_DISPATCH_HEADS[10]}"\n',
		{"jobs_10.json": json.dumps(_codex_jobs(501)), "log_501.txt": _reviewed_log(PIN)},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == PIN
	assert calls == ["repos/owner/repo/actions/runs/10/jobs?per_page=100", "repos/owner/repo/actions/jobs/501/logs"]


def test_reviewed_head_is_none_without_a_codex_agent_job_or_line() -> None:
	no_job = {"jobs": [{"id": 1, "name": "review / gate"}]}
	proc, calls = _run_bash(
		'e2e_review_dispatch_resolve_head 10\necho "${E2E_REVIEW_DISPATCH_HEADS[10]}"\n',
		{"jobs_10.json": json.dumps(no_job)},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "none"
	assert len(calls) == 1
	proc, _ = _run_bash(
		'e2e_review_dispatch_resolve_head 10\necho "${E2E_REVIEW_DISPATCH_HEADS[10]}"\n',
		{
			"jobs_10.json": json.dumps({"jobs": [{"id": 7, "name": "codex-agent (claude-branch-review)"}]}),
			"log_7.txt": 'echo "Captured INITIAL_HEAD_SHA=${INITIAL_HEAD_SHA} for stale-base detection."\nCaptured INITIAL_HEAD_SHA=abc for stale-base detection.\n',
		},
	)
	assert proc.stdout.strip() == "none"


def test_reviewed_head_api_failure_caches_nothing() -> None:
	proc, calls = _run_bash(
		'if e2e_review_dispatch_resolve_head 10; then echo ok; else echo failed; fi\necho "cached=${E2E_REVIEW_DISPATCH_HEADS[10]+x}"\n',
		{"jobs_10.json": json.dumps(_codex_jobs(501))},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.splitlines() == ["failed", "cached="]
	assert calls[-1] == "repos/owner/repo/actions/jobs/501/logs"


def test_reviewed_head_empty_log_is_unresolved_not_none() -> None:
	"""An empty log body can be a log GitHub has not stored yet: caching
	"none" would reject the run for good (Phase 4b: retry_workflow_failed),
	so it is a failed read the next poll retries, with its own warning."""
	proc, calls = _run_bash(
		'if e2e_review_dispatch_resolve_head 10; then echo ok; else echo failed; fi\necho "cached=${E2E_REVIEW_DISPATCH_HEADS[10]+x}"\n',
		{"jobs_10.json": json.dumps(_codex_jobs(501)), "log_501.txt": ""},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.splitlines() == ["failed", "cached="]
	assert "review run #10's codex-agent job log came back empty" in proc.stderr
	assert calls[-1] == "repos/owner/repo/actions/jobs/501/logs"


def test_reviewed_head_temp_file_failure_is_unresolved_with_its_own_warning() -> None:
	proc, calls = _run_bash(
		'mktemp() { return 1; }\nif e2e_review_dispatch_resolve_head 10; then echo ok; else echo failed; fi\necho "cached=${E2E_REVIEW_DISPATCH_HEADS[10]+x}"\n',
		{"jobs_10.json": json.dumps(_codex_jobs(501)), "log_501.txt": _reviewed_log(PIN)},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.splitlines() == ["failed", "cached="]
	assert "could not create a temp file for review run #10's codex-agent job log" in proc.stderr
	assert calls == ["repos/owner/repo/actions/runs/10/jobs?per_page=100"]


# ── Phase 4 selection, executed ───────────────────────────────────────


def _phase4(review_run: object, runs: list[dict], fixtures: dict[str, str] | None = None, bait_created_at: str = BAIT_CREATED_AT) -> tuple[str, list[str]]:
	pick = _function(_step_body("wait-review"), "e2e_review_dispatch_pick")
	combine = _phase4_combine_block()
	script = (
		pick
		+ f"PIN_SHA={PIN}\nBAIT_CREATED_AT='{bait_created_at}'\nRATE_LIMIT_BACKOFF=0\n"
		+ "REVIEW_RUN=\"${START_RUN}\"\n"
		+ combine
		+ 'echo "RESULT id=$(printf "%s" "${REVIEW_RUN:-null}" | jq -r "if type == \\"object\\" then .id else \\"none\\" end" 2>/dev/null || echo none) watching=${E2E_REVIEW_DISPATCH_WATCHING}"\n'
	)
	all_fixtures = {"default_branch.txt": "main", "listing.json": json.dumps({"workflow_runs": runs})}
	all_fixtures.update(fixtures or {})
	start_run = review_run if isinstance(review_run, str) else json.dumps(review_run)
	proc, calls = _run_bash(script, all_fixtures, {"START_RUN": start_run})
	assert proc.returncode == 0, proc.stderr
	result = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
	assert len(result) == 1, proc.stdout
	return proc.stdout, calls


def _reviewed(run_id: int, sha: str) -> dict[str, str]:
	job_id = run_id * 10
	return {f"jobs_{run_id}.json": json.dumps(_codex_jobs(job_id)), f"log_{job_id}.txt": _reviewed_log(sha)}


def test_completed_dispatch_run_with_matching_head_beats_an_active_branch_run() -> None:
	branch_active = {"id": 1, "status": "in_progress", "conclusion": None}
	out, _ = _phase4(branch_active, [_dispatch_run(20)], _reviewed(20, PIN))
	assert "RESULT id=20 watching=0" in out
	assert f"E2E_REVIEW_DISPATCH_CORRELATION pr={PR} run=20 reviewed_head={PIN} pin={PIN} result=match" in out


def test_completed_branch_run_is_kept() -> None:
	branch_done = {"id": 1, "status": "completed", "conclusion": "success"}
	out, _ = _phase4(branch_done, [_dispatch_run(20)], _reviewed(20, PIN))
	assert "RESULT id=1 watching=0" in out


def test_completed_dispatch_run_with_another_head_is_never_accepted() -> None:
	out, calls = _phase4("null", [_dispatch_run(20)], _reviewed(20, OTHER))
	assert "RESULT id=none watching=0" in out
	assert "result=mismatch" in out
	no_head = {"jobs_21.json": json.dumps({"jobs": [{"id": 5, "name": "review / gate"}]})}
	out, _ = _phase4("", [_dispatch_run(21)], no_head)
	assert "RESULT id=none watching=0" in out
	assert "reviewed_head=none" in out and "result=no_reviewed_head" in out


def test_unresolved_head_is_retried_and_not_accepted() -> None:
	out, _ = _phase4("null", [_dispatch_run(20)], {"jobs_20.json": json.dumps(_codex_jobs(200))})
	assert "RESULT id=none watching=0" in out
	assert "result=unresolved" in out


def test_active_dispatch_run_is_watched_only_when_the_branch_found_nothing_usable() -> None:
	active = _dispatch_run(30, status="in_progress", conclusion=None, created_at="2026-09-30T08:01:00Z")
	newer_active = _dispatch_run(31, status="queued", conclusion=None, created_at="2026-09-30T08:02:00Z")
	out, calls = _phase4("null", [newer_active, active])
	assert "RESULT id=30 watching=1" in out
	assert not [call for call in calls if "/jobs" in call]
	cancelled_branch = {"id": 1, "status": "completed", "conclusion": "cancelled"}
	out, _ = _phase4(cancelled_branch, [active])
	assert "RESULT id=30 watching=1" in out
	branch_active = {"id": 1, "status": "in_progress", "conclusion": None}
	out, _ = _phase4(branch_active, [active])
	assert "RESULT id=1 watching=0" in out


def test_runs_created_before_the_bait_or_cancelled_are_ignored() -> None:
	before = _dispatch_run(40, status="in_progress", conclusion=None, created_at="2026-09-30T07:59:59Z")
	cancelled = _dispatch_run(41, conclusion="cancelled")
	out, calls = _phase4("null", [before, cancelled], _reviewed(41, PIN))
	assert "RESULT id=none watching=0" in out
	assert not [call for call in calls if "/jobs" in call]


def test_without_a_bait_timestamp_only_completed_runs_can_match() -> None:
	active = _dispatch_run(50, status="in_progress", conclusion=None)
	out, _ = _phase4("null", [active, _dispatch_run(51)], _reviewed(51, PIN), bait_created_at="")
	assert "RESULT id=51 watching=0" in out
	out, _ = _phase4("null", [active], bait_created_at="")
	assert "RESULT id=none watching=0" in out


def test_phase4b_baseline_takes_the_highest_id_of_large_listings() -> None:
	body = _step_body("verify-bait-removed")
	start = body.index("RETRY_BASELINE_ID=$(printf")
	end = body.index("fi\n", body.index('RETRY_BASELINE_ID="${E2E_RETRY_DISPATCH_BASELINE_ID}"')) + len("fi\n")
	snippet = body[start:end]
	padding = "x" * 3000
	branch_runs = {"workflow_runs": [{"id": 100 + i, "pad": padding} for i in range(100)]}
	dispatch_runs = [{"id": 900 + i, "pad": padding} for i in range(100)]
	with tempfile.TemporaryDirectory() as tmp:
		branch_file = Path(tmp) / "branch.json"
		dispatch_file = Path(tmp) / "dispatch.json"
		branch_file.write_text(json.dumps(branch_runs), encoding="utf-8")
		dispatch_file.write_text(json.dumps(dispatch_runs), encoding="utf-8")
		for dispatch, expected in ((dispatch_file, "999"), (None, "199")):
			script = (
				"set -euo pipefail\n"
				f'RETRY_RUNS_JSON="$(cat {branch_file})"\n'
				+ (f'E2E_RETRY_DISPATCH_RUNS="$(cat {dispatch})"\n' if dispatch else 'E2E_RETRY_DISPATCH_RUNS="[]"\n')
				+ snippet
				+ 'echo "${RETRY_BASELINE_ID}"\n'
			)
			proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
			assert proc.returncode == 0, proc.stderr
			assert proc.stdout.strip() == expected


def test_wrapper_without_a_pr_run_name_makes_no_call() -> None:
	pick = _function(_step_body("wait-review"), "e2e_review_dispatch_pick")
	proc, calls = _run_bash(
		pick + f"PIN_SHA={PIN}\nBAIT_CREATED_AT={BAIT_CREATED_AT}\ne2e_review_dispatch_pick\necho \"[${{E2E_REVIEW_DISPATCH_DONE_RUN}}][${{E2E_REVIEW_DISPATCH_ACTIVE_RUN}}]\"\n",
		{"default_branch.txt": "main", "listing.json": json.dumps({"workflow_runs": [_dispatch_run(60)]})},
		{"REVIEW_WORKFLOW_FILE": "review_autofix.yml"},
	)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.strip() == "[][]"
	assert calls == []


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
