#!/usr/bin/env python3
"""The E2E smoke job dispatches its review runs from the default branch (issue #5093).

Security finding ``smoke-review-dispatches-unmerged-workflow``: Phase 3c (the
Bug B fallback) and the Phase 4b editor retry in
``.github/workflows/test-and-mark-stable.yml`` dispatched
``${REVIEW_WORKFLOW_FILE}`` at the smoke PR's branch, so the run executed that
branch's unmerged wrapper with ``TEST_REPO``'s secrets. Both now dispatch at
``TEST_REPO``'s default branch. The run is found by the PR run name the wrapper
sets, and tied to the smoke PR by the commit its own ``codex-agent`` log says
it checked out (``scripts/smoke_review_dispatch.sh``).

Covered here:
- the helper's three functions, against a stateful stub ``gh``;
- the inline Phase 3c registration, the Phase 4 leg (c) state machine, and the
  Phase 4b adoption and correlation, each taken verbatim from the workflow and
  run in bash with stubs;
- the Phase 4 candidate filter with a leg (c) run;
- contract checks on the workflow text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from runpy import run_path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
HELPER = REPO_ROOT / "scripts" / "smoke_review_dispatch.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REPO = "owner/repo"
BAIT = "b" * 40
PIN = BAIT
RETRY_HEAD = "c" * 40
OTHER = "d" * 40
MAIN_SHA = "e" * 40

GH_STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys

args = sys.argv[1:]
log = os.environ["GH_STUB_LOG"]
with open(log, "a", encoding="utf-8") as handle:
	handle.write(json.dumps(args) + "\n")
fixtures = os.environ["GH_STUB_DIR"]
if args[:2] == ["workflow", "run"]:
	sys.exit(int(os.environ.get("GH_STUB_WORKFLOW_RC", "0")))
if not args or args[0] != "api":
	sys.exit(97)
path = next((a for a in args[1:] if a.startswith("repos/")), None)
if path is None:
	sys.exit(98)
key = path.replace("/", "__")
counter = os.path.join(fixtures, key + ".count")
count = 1
if os.path.exists(counter):
	count = int(open(counter, encoding="utf-8").read()) + 1
with open(counter, "w", encoding="utf-8") as handle:
	handle.write(str(count))
candidates = [os.path.join(fixtures, f"{key}.{count}"), os.path.join(fixtures, key)]
chosen = next((c for c in candidates if os.path.exists(c) or os.path.exists(c + ".rc")), None)
if chosen is None:
	sys.exit(1)
if os.path.exists(chosen + ".rc"):
	sys.exit(int(open(chosen + ".rc", encoding="utf-8").read()))
body = open(chosen, encoding="utf-8", newline="").read()
if "\x1b" in body and "--allow-escape-sequences" not in args:
	# Real gh refuses a body with terminal escape sequences (job logs carry
	# ANSI colour codes) unless the flag is passed, even into a file.
	sys.stderr.write("the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway\n")
	sys.exit(1)
if "--jq" in args:
	expr = args[args.index("--jq") + 1]
	result = subprocess.run(["jq", "-r", expr], input=body, capture_output=True, text=True)
	if result.returncode != 0:
		sys.exit(1)
	sys.stdout.write(result.stdout)
else:
	sys.stdout.write(body)
'''


class GhStub:
	"""A ``gh`` on PATH that serves per-path fixtures and records every call."""

	def __init__(self) -> None:
		self.tmp = Path(tempfile.mkdtemp(prefix="smoke-review-gh-"))
		self.bin = self.tmp / "bin"
		self.fixtures = self.tmp / "fixtures"
		self.bin.mkdir()
		self.fixtures.mkdir()
		self.log = self.tmp / "calls.jsonl"
		self.log.write_text("", encoding="utf-8")
		gh = self.bin / "gh"
		gh.write_text(GH_STUB, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

	def serve(self, path: str, body: object, call: int | None = None) -> None:
		name = path.replace("/", "__") + (f".{call}" if call else "")
		text = body if isinstance(body, str) else json.dumps(body)
		(self.fixtures / name).write_text(text, encoding="utf-8", newline="")

	def fail(self, path: str, call: int | None = None, rc: int = 1) -> None:
		name = path.replace("/", "__") + (f".{call}" if call else "")
		(self.fixtures / (name + ".rc")).write_text(str(rc), encoding="utf-8")

	def calls(self) -> list[list[str]]:
		return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

	def env(self, **extra: str) -> dict[str, str]:
		env = dict(os.environ)
		env.update(
			{
				"PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
				"GH_STUB_LOG": str(self.log),
				"GH_STUB_DIR": str(self.fixtures),
			}
		)
		env.update(extra)
		return env

	def cleanup(self) -> None:
		shutil.rmtree(self.tmp, ignore_errors=True)


def _call_helper(stub: GhStub, function: str, *args: str) -> tuple[int, str]:
	script = f'source "{HELPER}"; {function} "$@"; rc=$?; printf "\\nRC=%s\\n" "$rc"'
	result = subprocess.run(
		["bash", "-c", script, "_", *args],
		capture_output=True,
		text=True,
		env=stub.env(),
		check=True,
	)
	out, _, rc = result.stdout.rpartition("\nRC=")
	return int(rc.strip()), out.strip()


def _run(**overrides: object) -> dict:
	run = {
		"id": 100,
		"name": "Internal: AI Review & Autofix",
		"path": ".github/workflows/internal-review.yml",
		"event": "workflow_dispatch",
		"status": "completed",
		"conclusion": "success",
		"head_branch": "main",
		"head_sha": MAIN_SHA,
		"created_at": "2026-09-29T10:00:00Z",
		"updated_at": "2026-09-29T10:05:00Z",
		"display_title": "Internal: AI Review & Autofix [pr:12]",
	}
	run.update(overrides)
	return run


RUNS_PATH = f"repos/{REPO}/actions/workflows/internal-review.yml/runs"


# ── smoke_review_pr_named_runs ─────────────────────────────────────────────


def test_pr_named_runs_keeps_only_this_prs_dispatch_runs_on_the_dispatch_ref() -> None:
	stub = GhStub()
	try:
		stub.serve(
			RUNS_PATH,
			{
				"workflow_runs": [
					_run(id=305),
					_run(id=301, display_title="AI Review [pr:12]"),
					_run(id=302, display_title="Internal: AI Review & Autofix [pr:112]"),
					_run(id=303, display_title="Internal: AI Review & Autofix [pr:2]"),
					_run(id=304, event="pull_request"),
					_run(id=306, head_branch="ai/issue-7"),
					_run(id=307, name=".github/workflows/internal-review.yml"),
					_run(id=308, display_title="Internal: AI Review & Autofix [pr:12] (copy)"),
					_run(id=309, display_title=None),
				]
			},
		)
		rc, out = _call_helper(stub, "smoke_review_pr_named_runs", REPO, "internal-review.yml", "main", "12")
		assert rc == 0, out
		runs = json.loads(out)
		assert [run["id"] for run in runs] == [301, 305]
		assert set(runs[0]) == {
			"id", "status", "conclusion", "created_at", "updated_at", "head_sha", "name", "path", "event", "display_title",
		}
		(call,) = stub.calls()
		assert call[:3] == ["api", "-X", "GET"]
		assert RUNS_PATH in call
		for field in ("event=workflow_dispatch", "per_page=100"):
			assert field in call and call[call.index(field) - 1] == "-f", call
		# The head branch is checked client-side, not with the API's branch
		# filter.
		assert not any(arg.startswith("branch=") for arg in call), call
	finally:
		stub.cleanup()


def test_pr_named_runs_drops_dispatch_runs_whose_head_branch_is_null_missing_or_empty() -> None:
	"""A run with no head_branch has no evidence it ran the default-branch wrapper (#5093 AD-11, #5094)."""
	stub = GhStub()
	try:
		missing = _run(id=312)
		del missing["head_branch"]
		stub.serve(
			RUNS_PATH,
			{
				"workflow_runs": [
					_run(id=313, head_branch=""),
					missing,
					_run(id=311, head_branch=None),
					_run(id=314, head_branch=None, display_title="Internal: AI Review & Autofix [pr:13]"),
					_run(id=315, head_branch=None, event="pull_request"),
					_run(id=316, head_branch="ai/issue-7"),
					_run(id=317),
				]
			},
		)
		rc, out = _call_helper(stub, "smoke_review_pr_named_runs", REPO, "internal-review.yml", "main", "12")
		assert rc == 0, out
		assert [run["id"] for run in json.loads(out)] == [317]
	finally:
		stub.cleanup()


def test_pr_named_runs_returns_an_empty_list_when_nothing_matches() -> None:
	stub = GhStub()
	try:
		stub.serve(RUNS_PATH, {"workflow_runs": []})
		assert _call_helper(stub, "smoke_review_pr_named_runs", REPO, "internal-review.yml", "main", "12") == (0, "[]")
	finally:
		stub.cleanup()


def test_pr_named_runs_rejects_bad_input_without_calling_gh() -> None:
	stub = GhStub()
	try:
		for args in (
			("owner", "internal-review.yml", "main", "12"),
			(REPO, "--help", "main", "12"),
			(REPO, "../x.yml", "main", "12"),
			(REPO, "internal-review.yml", "main;rm", "12"),
			(REPO, "internal-review.yml", "main", "0"),
			(REPO, "internal-review.yml", "main", "12a"),
			(REPO, "internal-review.yml", "", "12"),
		):
			assert _call_helper(stub, "smoke_review_pr_named_runs", *args) == (1, ""), args
		assert stub.calls() == []
	finally:
		stub.cleanup()


def test_pr_named_runs_fails_on_api_error_or_malformed_payload() -> None:
	for body in (None, {"workflow_runs": "nope"}, {"workflow_runs": [{"id": "1"}]}, "not json", ""):
		stub = GhStub()
		try:
			if body is None:
				stub.fail(RUNS_PATH)
			else:
				stub.serve(RUNS_PATH, body)
			assert _call_helper(stub, "smoke_review_pr_named_runs", REPO, "internal-review.yml", "main", "12") == (1, ""), body
		finally:
			stub.cleanup()


# ── smoke_review_checked_out_sha ───────────────────────────────────────────

JOBS_PATH = f"repos/{REPO}/actions/runs/100/jobs"
LOG_PATH = f"repos/{REPO}/actions/jobs/777/logs"
JOBS = {
	"jobs": [
		{"id": 555, "name": "review / gate"},
		{"id": 777, "name": "review / codex-agent"},
	]
}


def _log(*lines: str, newline: str = "\n") -> str:
	return newline.join(lines) + newline


def test_checked_out_sha_takes_the_first_anchored_checkout_line() -> None:
	for newline in ("\n", "\r\n"):
		stub = GhStub()
		try:
			stub.serve(JOBS_PATH, JOBS)
			stub.serve(
				LOG_PATH,
				_log(
					f"2026-09-29T10:00:01.0000000Z echo Captured INITIAL_HEAD_SHA={OTHER} for stale-base detection.",
					f"2026-09-29T10:00:02.1234567Z Captured INITIAL_HEAD_SHA={BAIT} for stale-base detection.",
					f"2026-09-29T10:09:00.0000000Z Captured INITIAL_HEAD_SHA={OTHER} for stale-base detection.",
					newline=newline,
				),
			)
			assert _call_helper(stub, "smoke_review_checked_out_sha", REPO, "100") == (0, BAIT)
			paths = [next(a for a in call if a.startswith("repos/")) for call in stub.calls()]
			assert paths == [JOBS_PATH, LOG_PATH]
			assert stub.calls()[0][:3] == ["api", "-X", "GET"]
		finally:
			stub.cleanup()


def test_checked_out_sha_accepts_any_single_prefix_token_but_nothing_looser() -> None:
	# The first-match rule carries the security property, so the timestamp's
	# shape is not checked: a changed format must not become a false rc=2.
	# Exactly one prefix token is still required, so an unprefixed line or a
	# mention after a second token never matches.
	for prefix in ("2026-09-29T10:00:02Z", "2026-09-29T10:00:02.123+00:00", "1790686283", "﻿2026-09-29T10:00:02.1234567Z"):
		stub = GhStub()
		try:
			stub.serve(JOBS_PATH, JOBS)
			stub.serve(
				LOG_PATH,
				_log(
					f"Captured INITIAL_HEAD_SHA={OTHER} for stale-base detection.",
					f"{prefix} echo Captured INITIAL_HEAD_SHA={OTHER} for stale-base detection.",
					f"{prefix}  Captured INITIAL_HEAD_SHA={OTHER} for stale-base detection.",
					f"{prefix} Captured INITIAL_HEAD_SHA={BAIT} for stale-base detection.",
				),
			)
			assert _call_helper(stub, "smoke_review_checked_out_sha", REPO, "100") == (0, BAIT), prefix
		finally:
			stub.cleanup()


def test_checked_out_sha_reads_a_log_with_terminal_escape_sequences() -> None:
	# A real codex-agent log carries ANSI colour codes (run 36571421143's had
	# 2,091), and gh refuses such a body without --allow-escape-sequences, so
	# without the flag every genuine run read as rc=1 forever.
	stub = GhStub()
	try:
		stub.serve(JOBS_PATH, JOBS)
		stub.serve(
			LOG_PATH,
			_log(
				"2026-09-29T10:00:00.0000000Z \x1b[36;1mgit fetch origin\x1b[0m",
				f"2026-09-29T10:00:02.1234567Z Captured INITIAL_HEAD_SHA={BAIT} for stale-base detection.",
				"2026-09-29T10:00:03.0000000Z \x1b[31mreviewer output\x1b[0m",
			),
		)
		assert _call_helper(stub, "smoke_review_checked_out_sha", REPO, "100") == (0, BAIT)
		log_calls = [call for call in stub.calls() if LOG_PATH in call]
		assert len(log_calls) == 1 and "--allow-escape-sequences" in log_calls[0], log_calls
	finally:
		stub.cleanup()


def test_checked_out_sha_treats_a_skipped_codex_agent_job_as_absent() -> None:
	# GitHub names a job skipped by its `if:` with the unevaluated name
	# expression (run 36641794664), which the codex-agent match must not take.
	skipped_name = "review / needs.gate.outputs.claude_branch_review == 'true' && 'codex-agent (claude-branch-review)' || 'codex-agent'"
	stub = GhStub()
	try:
		stub.serve(JOBS_PATH, {"jobs": [{"id": 555, "name": "review / gate"}, {"id": 777, "name": skipped_name}]})
		assert _call_helper(stub, "smoke_review_checked_out_sha", REPO, "100") == (3, "")
		assert [call for call in stub.calls() if LOG_PATH in call] == []
	finally:
		stub.cleanup()


def test_checked_out_sha_return_codes() -> None:
	cases = [
		("missing line", JOBS, _log("2026-09-29T10:00:01Z nothing here"), 2),
		("no codex-agent job", {"jobs": [{"id": 555, "name": "review / gate"}]}, None, 3),
		("malformed jobs", {"jobs": "x"}, None, 1),
		("jobs api failure", None, None, 1),
		("log api failure", JOBS, None, 1),
	]
	for label, jobs, log, expected in cases:
		stub = GhStub()
		try:
			if jobs is None:
				stub.fail(JOBS_PATH)
			else:
				stub.serve(JOBS_PATH, jobs)
			if log is None:
				stub.fail(LOG_PATH)
			else:
				stub.serve(LOG_PATH, log)
			assert _call_helper(stub, "smoke_review_checked_out_sha", REPO, "100") == (expected, ""), label
		finally:
			stub.cleanup()


def test_checked_out_sha_rejects_bad_input_without_calling_gh() -> None:
	stub = GhStub()
	try:
		for args in (("owner", "100"), (REPO, "0"), (REPO, "1x"), (REPO, "")):
			assert _call_helper(stub, "smoke_review_checked_out_sha", *args) == (4, ""), args
		assert stub.calls() == []
	finally:
		stub.cleanup()


# ── smoke_review_sha_descends_from ─────────────────────────────────────────

COMPARE_PATH = f"repos/{REPO}/compare/{BAIT}...{OTHER}"


def test_descends_from_maps_compare_status() -> None:
	for status, expected in (("identical", 0), ("ahead", 0), ("behind", 1), ("diverged", 1), ("weird", 2)):
		stub = GhStub()
		try:
			stub.serve(COMPARE_PATH, {"status": status})
			assert _call_helper(stub, "smoke_review_sha_descends_from", REPO, BAIT, OTHER) == (expected, ""), status
			(call,) = stub.calls()
			assert call[:3] == ["api", "-X", "GET"] and "per_page=1" in call
		finally:
			stub.cleanup()
	stub = GhStub()
	try:
		stub.fail(COMPARE_PATH)
		assert _call_helper(stub, "smoke_review_sha_descends_from", REPO, BAIT, OTHER) == (2, "")
	finally:
		stub.cleanup()
	stub = GhStub()
	try:
		for args in ((REPO, "b" * 39, OTHER), (REPO, BAIT, "D" * 40), ("owner", BAIT, OTHER)):
			assert _call_helper(stub, "smoke_review_sha_descends_from", *args) == (2, ""), args
		assert stub.calls() == []
	finally:
		stub.cleanup()


# ── workflow text ──────────────────────────────────────────────────────────


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _e2e_steps() -> dict[str, dict]:
	workflow = yaml.safe_load(_workflow_text())
	steps = workflow["jobs"]["e2e-smoke-test"]["steps"]
	return {step["id"]: step for step in steps if "id" in step}


def _between(text: str, start: str, end: str) -> str:
	begin = text.index(start)
	return text[begin : text.index(end, begin)]


def test_no_review_dispatch_runs_at_the_smoke_branch() -> None:
	steps = _e2e_steps()
	for step_id in ("inject-bait", "verify-bait-removed"):
		body = steps[step_id]["run"]
		assert '--ref "${BRANCH}"' not in body, step_id
		dispatches = re.findall(r'gh workflow run "\$\{REVIEW_WORKFLOW_FILE\}"[^\n]*\\\n\s*--repo "\$\{TEST_REPO\}" \\\n\s*--ref "([^"]+)"', body)
		assert dispatches == ["${REVIEW_DISPATCH_REF}"], (step_id, dispatches)
		assert steps[step_id]["env"]["REVIEW_DISPATCH_REF"] == "${{ steps.prereqs.outputs.test_repo_default_branch }}"
	job = _between(_workflow_text(), "  e2e-smoke-test:\n", "  validate-scripts:\n")
	assert '--ref "${BRANCH}"' not in job


def test_prereqs_reads_the_default_branch_from_its_existing_repo_call() -> None:
	body = _e2e_steps()["prereqs"]["run"]
	assert body.count('gh api "repos/${TEST_REPO}"') == 1
	assert "[.full_name, .default_branch] | @tsv" in body
	assert "^[A-Za-z0-9._/-]+$ ]]" in body
	assert 'echo "test_repo_default_branch=${TEST_REPO_DEFAULT_BRANCH}" >> "$GITHUB_OUTPUT"' in body


def test_phase3c_registers_its_dispatch_or_skips_it() -> None:
	body = _e2e_steps()["inject-bait"]["run"]
	block = _between(body, ". ./scripts/smoke_review_dispatch.sh", 'echo "bug_b_run_id=${BUG_B_RUN_ID}" >> "$GITHUB_OUTPUT"')
	baseline = block.index("elif ! BUG_B_BASELINE_RUNS=$(smoke_review_pr_named_runs")
	dispatch = block.index('if gh workflow run "${REVIEW_WORKFLOW_FILE}"')
	assert baseline < dispatch
	assert "BUG_B_REGISTRATION_DEADLINE=$(( $(date +%s) + 90 ))" in block
	assert "select(.id > $baseline_id)" in block


def test_phase4_reads_leg_c_by_id_and_admits_it_through_extra() -> None:
	step = _e2e_steps()["wait-review"]
	assert step["env"]["BUG_B_RUN_ID"] == "${{ steps.inject-bait.outputs.bug_b_run_id }}"
	body = step["run"]
	assert '] + \\$extra) as \\$m' in body
	assert '"repos/${TEST_REPO}/actions/runs/${BUG_B_RUN_ID}"' in body
	assert '"repos/${TEST_REPO}/actions/runs?branch=${PR_BRANCH}&per_page=100"' in body


def test_phase4b_registers_from_the_pr_named_listing_and_reports_unverified_runs() -> None:
	body = _e2e_steps()["verify-bait-removed"]["run"]
	retry = _between(body, "# ── Retry: adopt active work or redispatch", "# ── Attempt 2")
	registration = _between(retry, 'if [ -z "${RETRY_RUN_ID}" ]; then', 'echo "✓ Registered retry review run')
	assert "smoke_review_pr_named_runs" in registration
	assert "RETRY_RUNS_QUERY" not in registration
	assert 'echo "status=retry_run_unverified" >> "$GITHUB_OUTPUT"' in retry
	assert "#   retry_run_unverified" in _workflow_text()


def test_new_test_is_wired_into_ci() -> None:
	assert "tests/test_smoke_review_dispatch.py" in CI_WORKFLOW.read_text(encoding="utf-8")


# ── inline blocks, run verbatim ────────────────────────────────────────────


def _run_bash(script: str, env: dict[str, str], cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=cwd)


def _phase3c_block() -> str:
	body = _e2e_steps()["inject-bait"]["run"]
	start = body.index("# §15: one listing call for the baseline")
	end = body.index('echo "bug_b_run_id=${BUG_B_RUN_ID}" >> "$GITHUB_OUTPUT"')
	return body[start:end] + 'echo "bug_b_run_id=${BUG_B_RUN_ID}" >> "$GITHUB_OUTPUT"\n'


def _phase3c(stub: GhStub, **env: str) -> tuple[subprocess.CompletedProcess, str]:
	output = stub.tmp / "github_output"
	output.write_text("", encoding="utf-8")
	script = "set -euo pipefail\nsleep() { :; }\n" + _phase3c_block()
	result = _run_bash(
		script,
		stub.env(
			GITHUB_OUTPUT=str(output),
			TEST_REPO=REPO,
			REVIEW_WORKFLOW_FILE="internal-review.yml",
			PR_NUMBER="12",
			**env,
		),
	)
	return result, output.read_text(encoding="utf-8")


def test_phase3c_dispatches_at_the_default_branch_and_registers_the_new_run() -> None:
	stub = GhStub()
	try:
		stub.serve(RUNS_PATH, {"workflow_runs": [_run(id=200)]}, call=1)
		stub.serve(RUNS_PATH, {"workflow_runs": [_run(id=200)]}, call=2)
		stub.serve(RUNS_PATH, {"workflow_runs": [_run(id=200), _run(id=250, status="queued"), _run(id=240, display_title="AI Review [pr:9]")]}, call=3)
		result, output = _phase3c(stub, REVIEW_DISPATCH_REF="main")
		assert result.returncode == 0, result.stderr
		assert output == "bug_b_run_id=250\n"
		dispatches = [call for call in stub.calls() if call[:2] == ["workflow", "run"]]
		assert dispatches == [["workflow", "run", "internal-review.yml", "--repo", REPO, "--ref", "main", "-f", "pr_number=12"]]
	finally:
		stub.cleanup()


def test_phase3c_skips_the_dispatch_without_a_baseline_or_a_ref() -> None:
	for env in ({"REVIEW_DISPATCH_REF": "main"}, {"REVIEW_DISPATCH_REF": ""}):
		stub = GhStub()
		try:
			stub.fail(RUNS_PATH)
			result, output = _phase3c(stub, **env)
			assert result.returncode == 0, result.stderr
			assert output == "bug_b_run_id=\n"
			assert not [call for call in stub.calls() if call[:2] == ["workflow", "run"]]
			assert "relying on pull_request:synchronize event only" in result.stdout
		finally:
			stub.cleanup()


def _phase4_leg_c_block() -> str:
	body = _e2e_steps()["wait-review"]["run"]
	start = body.index("BUG_B_EXTRA='[]'")
	end = body.index('REVIEW_RUN=""', start)
	return body[start:end]


def _phase4_leg_c(run: dict | None, sha_rc: int = 0, sha: str = BAIT, state: str = "") -> dict[str, str]:
	tmp = Path(tempfile.mkdtemp(prefix="smoke-leg-c-"))
	try:
		(tmp / "run.json").write_text(json.dumps(run) if run is not None else "", encoding="utf-8")
		script = "\n".join(
			[
				"set -euo pipefail",
				f'gh_api_safe_quiet_print() {{ [ -s "{tmp}/run.json" ] || return 1; cat "{tmp}/run.json"; }}',
				f'smoke_review_checked_out_sha() {{ echo called >> "{tmp}/sha_calls"; printf "%s\\n" "{sha}"; return {sha_rc}; }}',
				f"TEST_REPO={REPO}; BUG_B_RUN_ID=55; PIN_SHA={PIN}; BAIT_SHA={BAIT}; BUG_B_STATE='{state}'",
				_phase4_leg_c_block(),
				'printf "STATE=%s\\nEXTRA=%s\\n" "${BUG_B_STATE}" "${BUG_B_EXTRA}"',
			]
		)
		result = _run_bash(script, dict(os.environ))
		assert result.returncode == 0, result.stderr
		values = dict(line.split("=", 1) for line in result.stdout.splitlines() if re.match(r"^(STATE|EXTRA)=", line))
		values["sha_calls"] = str(len((tmp / "sha_calls").read_text().splitlines())) if (tmp / "sha_calls").exists() else "0"
		values["stdout"] = result.stdout
		return values
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def test_phase4_leg_c_states() -> None:
	active = _run(id=55, status="in_progress", conclusion=None)
	done = _run(id=55)

	out = _phase4_leg_c(active)
	assert (out["STATE"], json.loads(out["EXTRA"])[0]["id"], out["sha_calls"]) == ("", 55, "0")

	out = _phase4_leg_c(done, sha=BAIT)
	assert (out["STATE"], json.loads(out["EXTRA"])[0]["id"]) == ("verified", 55)

	out = _phase4_leg_c(done, sha=OTHER)
	assert (out["STATE"], out["EXTRA"]) == ("rejected", "[]")
	assert "not the bait/pin commit" in out["stdout"]

	out = _phase4_leg_c(done, sha_rc=1, sha="")
	assert (out["STATE"], out["EXTRA"]) == ("", "[]")

	for rc in (2, 3):
		out = _phase4_leg_c(done, sha_rc=rc, sha="")
		assert (out["STATE"], out["EXTRA"]) == ("rejected", "[]"), rc

	out = _phase4_leg_c(done, state="verified")
	assert (out["STATE"], json.loads(out["EXTRA"])[0]["id"], out["sha_calls"]) == ("verified", 55, "0")

	out = _phase4_leg_c(done, state="rejected")
	assert (out["EXTRA"], out["sha_calls"]) == ("[]", "0")

	out = _phase4_leg_c(None)
	assert (out["STATE"], out["EXTRA"]) == ("", "[]")

	out = _phase4_leg_c(_run(id=56))
	assert (out["STATE"], out["EXTRA"], out["sha_calls"]) == ("", "[]", "0")


def test_phase4_filter_picks_a_verified_leg_c_run_and_still_prefers_leg_a() -> None:
	phantom = run_path(str(REPO_ROOT / "tests" / "test_test_and_mark_stable_phantom_review_run_filter.py"))
	program = phantom["_pinned_filter"]()
	jq = phantom["_jq"]
	leg_c = _run(id=55)
	assert jq(program, [], [leg_c])["id"] == 55
	leg_a = _run(id=40, event="pull_request", head_sha="pinsha", head_branch="ai/issue-7", created_at="2026-09-29T10:10:00Z")
	chosen = jq(program, [leg_a], [_run(id=55, status="in_progress", conclusion=None)])
	assert chosen["id"] == 40
	assert jq(program, [], []) is None


def _phase4b_adoption_block() -> str:
	body = _e2e_steps()["verify-bait-removed"]["run"]
	start = body.index("RETRY_PR_NAMED_ACTIVE=$(")
	end = body.index(".[0] // empty')", start) + len(".[0] // empty')")
	return body[start:end]


def _adopt(branch_runs: list[dict], pr_named: list[dict]) -> dict | None:
	env = dict(os.environ)
	env.update(
		{
			"RETRY_RUNS_JSON": json.dumps({"workflow_runs": branch_runs}),
			"RETRY_PR_NAMED_JSON": json.dumps(pr_named),
			"BAIT_SHA": BAIT,
			"RETRY_DISPATCH_SHA": RETRY_HEAD,
			"PRIOR_REVIEW_RUN": "10",
		}
	)
	script = "set -euo pipefail\n" + _phase4b_adoption_block() + '\nprintf "%s" "${RETRY_ACTIVE_RUN}"\n'
	result = _run_bash(script, env)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout) if result.stdout else None


def test_phase4b_adopts_the_oldest_active_branch_or_pr_named_run() -> None:
	branch = _run(id=30, event="pull_request", head_branch="ai/issue-7", head_sha=BAIT, status="queued", conclusion=None, created_at="2026-09-29T10:03:00Z")
	named = _run(id=31, status="in_progress", conclusion=None, created_at="2026-09-29T10:01:00Z")
	assert _adopt([branch], [named]) == {"id": 31, "created_at": "2026-09-29T10:01:00Z", "retry_source": "pr_named"}
	assert _adopt([branch], [_run(id=31, created_at="2026-09-29T10:01:00Z")])["retry_source"] == "branch"
	assert _adopt([], [_run(id=10, status="queued", conclusion=None)]) is None
	assert _adopt([{**branch, "head_sha": OTHER}], []) is None


def _phase4b_correlation_block() -> str:
	body = _e2e_steps()["verify-bait-removed"]["run"]
	start = body.index('if [ "${RETRY_SOURCE}" = "pr_named" ]; then')
	end = body.index("# ── Attempt 2", start)
	return body[start:end]


def _correlate(source: str, sha_rcs: list[int], sha: str, descends_rc: int = 0, deadline_passed: bool = False) -> tuple[int, str, str]:
	tmp = Path(tempfile.mkdtemp(prefix="smoke-correlate-"))
	try:
		output = tmp / "github_output"
		output.write_text("", encoding="utf-8")
		(tmp / "rcs").write_text("\n".join(str(rc) for rc in sha_rcs) + "\n", encoding="utf-8")
		script = "\n".join(
			[
				"set -euo pipefail",
				"sleep() { :; }",
				f'smoke_review_checked_out_sha() {{ local rc; rc=$(head -n 1 "{tmp}/rcs"); if [ "$(wc -l < "{tmp}/rcs")" -gt 1 ]; then sed -i 1d "{tmp}/rcs"; fi; [ "$rc" -eq 0 ] && printf "%s\\n" "{sha}"; return "$rc"; }}',
				f'smoke_review_sha_descends_from() {{ echo "$*" >> "{tmp}/descends"; return {descends_rc}; }}',
				f"TEST_REPO={REPO}; RETRY_RUN_ID=77; BRANCH=ai/issue-7; RETRY_SOURCE={source}",
				f"BAIT_SHA={BAIT.upper()}; RETRY_DISPATCH_SHA={RETRY_HEAD}",
				f"DEADLINE=$(( $(date +%s) {'- 5' if deadline_passed else '+ 600'} ))",
				f'GITHUB_OUTPUT="{output}"',
				"(",
				_phase4b_correlation_block(),
				'echo "PASSED"',
				")",
			]
		)
		result = _run_bash(script, dict(os.environ))
		descends = (tmp / "descends").read_text(encoding="utf-8") if (tmp / "descends").exists() else ""
		return result.returncode, output.read_text(encoding="utf-8") + result.stdout, descends
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def test_phase4b_correlation_outcomes() -> None:
	rc, out, descends = _correlate("pr_named", [0], BAIT)
	assert rc == 0 and "PASSED" in out and descends == ""

	rc, out, descends = _correlate("pr_named", [0], RETRY_HEAD)
	assert rc == 0 and "PASSED" in out and descends == ""

	rc, out, descends = _correlate("pr_named", [0], OTHER, descends_rc=0)
	assert rc == 0 and "PASSED" in out
	assert descends.split() == [REPO, BAIT, OTHER]

	rc, out, _ = _correlate("pr_named", [0], OTHER, descends_rc=1)
	assert rc == 1 and "status=retry_run_unverified" in out and "PASSED" not in out

	rc, out, _ = _correlate("pr_named", [0], OTHER, descends_rc=2, deadline_passed=True)
	assert rc == 1 and "status=retry_run_unverified" in out

	rc, out, _ = _correlate("pr_named", [1, 1, 0], BAIT)
	assert rc == 0 and "PASSED" in out

	rc, out, _ = _correlate("pr_named", [1], BAIT, deadline_passed=True)
	assert rc == 1 and "status=retry_run_unverified" in out

	for sha_rc in (2, 3, 4):
		rc, out, _ = _correlate("pr_named", [sha_rc], BAIT)
		assert rc == 1 and "status=retry_run_unverified" in out, sha_rc

	rc, out, _ = _correlate("branch", [3], OTHER)
	assert rc == 0 and "PASSED" in out and "status=" not in out


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_") and callable(value)]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} smoke review dispatch checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
