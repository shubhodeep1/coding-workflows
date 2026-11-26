#!/usr/bin/env python3
"""Tests for the delta suite on a push to main (issue #10156).

A push to `main` runs static-checks, `tests-main-delta`
(scripts/ci_main_delta.sh) and `lint`; the seven gated test jobs run only
when the delta job asks for the full suite. The full suite also runs on a
`schedule`. These tests pin:

  * `select`: the base is the head of the newest successful push or
    scheduled ci.yml run on main; every untrusted selection writes
    `full_required=true` and exits 0 (never a pass on an unknown selection);
  * `run-tests`: pass, failure and timeout;
  * the ci.yml wiring (job, gates, trigger, concurrency, `lint`) and the
    `lint` aggregate's behaviour for delta, scheduled and other runs.

Run directly: `python3 -m pytest tests/test_ci_main_delta.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci_main_delta.sh"
SYNC_QUICK = REPO_ROOT / "scripts" / "ci_sync_quick.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REPO = "owner/repo"
DELTA_JOB = "tests-main-delta"
GATED_JOBS = (
	"tests-hooks-and-orchestrator",
	"tests-heal-plan-and-validation",
	"tests-promote-stall-and-review",
	"tests-release-and-log-analysis",
	"orchestrate-poll",
	"tests-system-python",
	"tests-sandbox-python",
)
GATED_IF = (
	"!cancelled() && inputs.mode != 'quick' && inputs.docs_only != 'true' && inputs.sync_quick != 'true'"
	" && (github.event_name != 'push' || github.ref != 'refs/heads/main' || vars.MAIN_PUSH_DELTA_CI_ENABLED == 'false'"
	" || (needs.tests-main-delta.result == 'success' && needs.tests-main-delta.outputs.full_required == 'true'))"
)
DELTA_IF = "inputs.mode != 'quick' && github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.MAIN_PUSH_DELTA_CI_ENABLED != 'false'"

# Maps a changed `src/<name>.sh` to `tests/test_<name>.py` and passes changed
# tests/ paths through; FAKE_SELECTOR_MODE / FAKE_SELECTOR_EXTRA steer it.
FAKE_SELECTOR = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
changed = args[args.index("--changed-files-file") + 1]
out = args[args.index("--out") + 1]
mode = os.environ.get("FAKE_SELECTOR_MODE", "")
if mode == "fail":
	sys.exit(3)
if mode == "badjson":
	open(out, "w").write("{not json")
	sys.exit(0)
paths = []
for line in open(changed, encoding="utf-8"):
	parts = line.rstrip("\n").split("\t")
	path = parts[-1]
	if path.startswith("src/") and path.endswith(".sh"):
		paths.append("tests/test_" + path[4:-3] + ".py")
	elif path.startswith("tests/"):
		paths.append(path)
paths += [p for p in os.environ.get("FAKE_SELECTOR_EXTRA", "").split(",") if p]
affected = [{"path": p, "reasons": ["fake"]} for p in paths]
if mode == "nonlist":
	affected = {"path": "tests/test_a.py"}
if mode == "nonobject":
	affected = ["tests/test_a.py"]
open(out, "w").write(json.dumps({"changed_files": [], "affected_tests": affected}))
'''

FAKE_GH = r'''#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as fh:
	fh.write(" ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_GH_EXIT", "0") != "0":
	sys.exit(int(os.environ["FAKE_GH_EXIT"]))
sys.stdout.write(open(os.environ["FAKE_GH_RESPONSE"], encoding="utf-8").read())
'''

PASSING_TEST = "def test_ok():\n\tassert True\n"
FAILING_TEST = "def test_bad():\n\tassert False\n"


def _clean_env(tmp_path: Path) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_", "GH_", "CI_MAIN_DELTA", "MAIN_PUSH_DELTA", "RUNNER_"))}
	env.pop("BASH_ENV", None)
	env.update({
		"HOME": str(tmp_path / "home"),
		"GIT_CONFIG_GLOBAL": os.devnull,
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	(tmp_path / "home").mkdir(exist_ok=True)
	return env


class Repo:
	"""A scratch git repo with the script, a fake selector and a fake gh."""

	def __init__(self, tmp_path: Path):
		self.tmp = tmp_path
		self.root = tmp_path / "repo"
		self.root.mkdir()
		self.env = _clean_env(tmp_path)
		self.bin = tmp_path / "bin"
		self.bin.mkdir()
		gh = self.bin / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(0o755)
		self.gh_log = tmp_path / "gh.log"
		self.gh_log.write_text("", encoding="utf-8")
		self.response = tmp_path / "runs.json"
		self.output = tmp_path / "github_output"
		self.tests_file = tmp_path / "delta-tests.txt"
		self.env.update({
			"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
			"FAKE_GH_LOG": str(self.gh_log),
			"FAKE_GH_RESPONSE": str(self.response),
			"GITHUB_OUTPUT": str(self.output),
			"CI_MAIN_DELTA_TESTS_FILE": str(self.tests_file),
			"CI_MAIN_DELTA_REPOSITORY": REPO,
			"TMPDIR": str(tmp_path),
		})
		(self.root / "scripts").mkdir()
		shutil.copy(SCRIPT, self.root / "scripts" / "ci_main_delta.sh")
		shutil.copy(SYNC_QUICK, self.root / "scripts" / "ci_sync_quick.sh")
		hooks = self.root / "workflow-templates" / ".claude" / "hooks"
		hooks.mkdir(parents=True)
		(hooks / "prepush_affected_tests.py").write_text(FAKE_SELECTOR, encoding="utf-8")
		(self.root / ".github" / "workflows").mkdir(parents=True)
		(self.root / ".github" / "workflows" / "ci.yml").write_text(
			"jobs:\n  x:\n    steps:\n      - run: PYTHONDONTWRITEBYTECODE=1 python3 tests/test_ci_job_split_contract.py\n", encoding="utf-8")
		(self.root / "tests").mkdir()
		(self.root / "src").mkdir()
		for name in ("a", "b"):
			(self.root / "src" / f"{name}.sh").write_text("echo 0\n", encoding="utf-8")
			(self.root / "tests" / f"test_{name}.py").write_text(PASSING_TEST, encoding="utf-8")
		# A CI-structure test (CI_SYNC_QUICK_WIRING_PATHS) that ci.yml runs as a script.
		(self.root / "tests" / "test_ci_job_split_contract.py").write_text("print('structure ok')\n", encoding="utf-8")
		(self.root / "tests" / "test_orchestrate_poll_process.py").write_text(PASSING_TEST, encoding="utf-8")
		self.git("init", "-q", "-b", "main")
		self.base = self.commit("base")

	def git(self, *args: str) -> str:
		return subprocess.run(["git", *args], cwd=self.root, env=self.env, check=True, capture_output=True, text=True).stdout.strip()

	def commit(self, message: str, files: dict[str, str] | None = None) -> str:
		for rel, content in (files or {}).items():
			path = self.root / rel
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(content, encoding="utf-8")
		self.git("add", "-A")
		self.git("commit", "-q", "--allow-empty", "-m", message)
		return self.git("rev-parse", "HEAD")

	def runs(self, *runs: dict) -> None:
		self.response.write_text(json.dumps({"workflow_runs": list(runs)}), encoding="utf-8")

	def run(self, mode: str, **env: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
		self.output.write_text("", encoding="utf-8")
		full = {**self.env, "CI_MAIN_DELTA_HEAD_SHA": self.git("rev-parse", "HEAD"), **env}
		proc = subprocess.run(["bash", "scripts/ci_main_delta.sh", mode], cwd=self.root, env=full, capture_output=True, text=True, timeout=300)
		outputs: dict[str, str] = {}
		for line in self.output.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			outputs[key] = value
		return proc, outputs

	def listed(self) -> list[str]:
		return [line for line in self.tests_file.read_text(encoding="utf-8").splitlines() if line]


def _run(sha: str, *, event: str = "push", conclusion: str = "success", created: str = "2026-11-26T01:00:00Z", **extra) -> dict:
	run = {"head_sha": sha, "head_branch": "main", "event": event, "status": "completed", "conclusion": conclusion,
		"path": ".github/workflows/ci.yml@refs/heads/main", "created_at": created}
	run.update(extra)
	return run


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
	return Repo(tmp_path)


def _terminal(proc: subprocess.CompletedProcess[str]) -> str:
	lines = [line for line in proc.stdout.splitlines() if line.startswith("CI_MAIN_DELTA ") and "outcome=note" not in line and "outcome=start" not in line]
	assert lines, proc.stdout + proc.stderr
	return lines[-1]


# --- select -------------------------------------------------------------------


def test_green_base_selects_every_change_since_it(repo: Repo) -> None:
	# Two pushes since the green base: the second push's run replaced the
	# first one's waiting run, so its range must cover both changes.
	repo.commit("push 1", files={"src/a.sh": "echo 1\n"})
	head = repo.commit("push 2", files={"src/b.sh": "echo 2\n"})
	repo.runs(_run(repo.base))
	proc, outputs = repo.run("select")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert outputs == {"full_required": "false"}
	assert repo.listed() == ["tests/test_a.py", "tests/test_b.py", "tests/test_ci_job_split_contract.py"]
	line = _terminal(proc)
	assert line == f"CI_MAIN_DELTA mode=select outcome=ok reason=selected base={repo.base[:12]} head={head[:12]} changed=2 tests=2"
	calls = repo.gh_log.read_text(encoding="utf-8").splitlines()
	assert calls == [f"api repos/{REPO}/actions/workflows/ci.yml/runs?branch=main&status=completed&per_page=30"]


def test_scheduled_run_is_a_base_and_other_events_and_cancelled_runs_are_ignored(repo: Repo) -> None:
	mid = repo.commit("push 1", files={"src/a.sh": "echo 1\n"})
	repo.commit("push 2", files={"src/b.sh": "echo 2\n"})
	repo.runs(
		_run(repo.base, created="2026-11-26T00:00:00Z"),
		_run(mid, event="schedule", created="2026-11-26T01:00:00Z"),
		_run(mid, event="workflow_dispatch", conclusion="failure", created="2026-11-26T02:00:00Z"),
		_run(mid, conclusion="cancelled", created="2026-11-26T03:00:00Z"),
		_run(mid, conclusion="failure", created="2026-11-26T04:00:00Z", path=".github/workflows/other.yml"),
		_run(mid, conclusion="failure", created="2026-11-26T05:00:00Z", head_branch="stable"),
	)
	proc, outputs = repo.run("select")
	assert outputs == {"full_required": "false"}, proc.stdout
	assert repo.listed() == ["tests/test_b.py", "tests/test_ci_job_split_contract.py"]
	assert f"base={mid[:12]}" in _terminal(proc)


def test_head_equal_to_base_runs_only_the_structure_tests(repo: Repo) -> None:
	repo.runs(_run(repo.base))
	proc, outputs = repo.run("select")
	assert outputs == {"full_required": "false"}
	assert repo.listed() == ["tests/test_ci_job_split_contract.py"]
	assert "outcome=ok reason=no_changes" in _terminal(proc)


def test_deleted_test_is_skipped(repo: Repo) -> None:
	repo.git("rm", "-q", "tests/test_a.py")
	repo.commit("drop test")
	repo.runs(_run(repo.base))
	proc, outputs = repo.run("select")
	assert outputs == {"full_required": "false"}, proc.stdout
	assert repo.listed() == ["tests/test_ci_job_split_contract.py"]


@pytest.mark.parametrize(
	("setup", "reason"),
	[
		("no_runs", "no_green_base"),
		("only_cancelled", "no_green_base"),
		("red_newest", "base_red"),
		("invalid_response", "invalid_response"),
		("bad_sha", "no_green_base"),
		("read_failed", "read_failed"),
	],
)
def test_untrusted_range_runs_the_full_suite(repo: Repo, setup: str, reason: str) -> None:
	repo.commit("push", files={"src/a.sh": "echo 1\n"})
	env: dict[str, str] = {}
	if setup == "no_runs":
		repo.runs()
	elif setup == "only_cancelled":
		repo.runs(_run(repo.base, conclusion="cancelled"))
	elif setup == "red_newest":
		repo.runs(_run(repo.base, created="2026-11-26T00:00:00Z"), _run(repo.base, event="schedule", conclusion="failure", created="2026-11-26T01:00:00Z"))
	elif setup == "invalid_response":
		repo.response.write_text("[]", encoding="utf-8")
	elif setup == "bad_sha":
		repo.runs(_run("not-a-sha"))
	elif setup == "read_failed":
		env["FAKE_GH_EXIT"] = "1"
	proc, outputs = repo.run("select", **env)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert outputs == {"full_required": "true"}
	assert f"outcome=full_required reason={reason} " in _terminal(proc)


def test_base_not_ancestor_runs_the_full_suite(repo: Repo) -> None:
	repo.git("checkout", "-q", "-b", "side")
	side = repo.commit("side", files={"src/a.sh": "side\n"})
	repo.git("checkout", "-q", "main")
	repo.commit("main", files={"src/b.sh": "main\n"})
	repo.runs(_run(side))
	proc, outputs = repo.run("select")
	assert outputs == {"full_required": "true"}
	assert "reason=base_not_ancestor" in _terminal(proc)


def test_head_mismatch_and_invalid_input_run_the_full_suite(repo: Repo) -> None:
	repo.runs(_run(repo.base))
	proc, outputs = repo.run("select", CI_MAIN_DELTA_HEAD_SHA="f" * 40)
	assert outputs == {"full_required": "true"} and "reason=head_mismatch" in _terminal(proc)
	proc, outputs = repo.run("select", CI_MAIN_DELTA_REPOSITORY="bad repo")
	assert outputs == {"full_required": "true"} and "reason=invalid_input" in _terminal(proc)
	assert repo.gh_log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
	("env", "reason"),
	[
		({"FAKE_SELECTOR_MODE": "fail"}, "selector_failed"),
		({"FAKE_SELECTOR_MODE": "badjson"}, "selector_failed"),
		({"FAKE_SELECTOR_MODE": "nonlist"}, "selection_invalid"),
		({"FAKE_SELECTOR_MODE": "nonobject"}, "selection_invalid"),
		({"FAKE_SELECTOR_EXTRA": "tests/../scripts/x.py"}, "selection_invalid"),
		({"FAKE_SELECTOR_EXTRA": "scripts/ci_main_delta.sh"}, "selection_invalid"),
		({"FAKE_SELECTOR_EXTRA": "tests/test_orchestrate_poll_process.py"}, "sharded_test_selected"),
		({"MAIN_PUSH_DELTA_MAX_TEST_FILES": "1"}, "over_cap"),
	],
)
def test_untrusted_selection_runs_the_full_suite(repo: Repo, env: dict, reason: str) -> None:
	repo.commit("push 1", files={"src/a.sh": "echo 1\n"})
	repo.commit("push 2", files={"src/b.sh": "echo 2\n"})
	repo.runs(_run(repo.base))
	proc, outputs = repo.run("select", **env)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert outputs == {"full_required": "true"}
	assert f"reason={reason} " in _terminal(proc)


def test_cap_within_limit_and_invalid_cap_default(repo: Repo) -> None:
	repo.commit("push 1", files={"src/a.sh": "echo 1\n"})
	repo.commit("push 2", files={"src/b.sh": "echo 2\n"})
	repo.runs(_run(repo.base))
	_proc, outputs = repo.run("select", MAIN_PUSH_DELTA_MAX_TEST_FILES="2")
	assert outputs == {"full_required": "false"}
	proc, outputs = repo.run("select", MAIN_PUSH_DELTA_MAX_TEST_FILES="0")
	assert outputs == {"full_required": "false"}
	assert "CI_MAIN_DELTA mode=select outcome=note reason=cap_invalid_default cap=60" in proc.stdout


# --- run-tests ----------------------------------------------------------------


def test_run_tests_pass(repo: Repo) -> None:
	repo.tests_file.write_text("tests/test_a.py\ntests/test_ci_job_split_contract.py\n", encoding="utf-8")
	proc, outputs = repo.run("run-tests")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert outputs == {"full_required": "false"}
	assert "structure ok" in proc.stdout
	assert "CI_MAIN_DELTA mode=run_tests outcome=start reason=selected script=1 pytest=1 shell=0" in proc.stdout
	assert "outcome=ok reason=tests_passed" in _terminal(proc)


def test_run_tests_failure_fails_the_job(repo: Repo) -> None:
	(repo.root / "tests" / "test_a.py").write_text(FAILING_TEST, encoding="utf-8")
	(repo.root / "tests" / "test_s.sh").write_text("exit 0\n", encoding="utf-8")
	repo.tests_file.write_text("tests/test_a.py\ntests/test_s.sh\n", encoding="utf-8")
	proc, outputs = repo.run("run-tests")
	assert proc.returncode == 1
	assert outputs.get("full_required") == "false"
	assert "outcome=fail reason=tests_failed" in _terminal(proc)


def test_run_tests_timeout_runs_the_full_suite(repo: Repo) -> None:
	stub = repo.bin / "timeout"
	stub.write_text("#!/bin/sh\nexit 124\n", encoding="utf-8")
	stub.chmod(0o755)
	repo.tests_file.write_text("tests/test_a.py\n", encoding="utf-8")
	proc, outputs = repo.run("run-tests")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert outputs == {"full_required": "true"}
	assert "outcome=full_required reason=tests_timed_out" in _terminal(proc)


@pytest.mark.parametrize(("content", "reason"), [(None, "list_missing"), ("tests/../x.py\n", "list_invalid"), ("/etc/passwd.py\n", "list_invalid")])
def test_run_tests_rejects_a_bad_list(repo: Repo, content: str | None, reason: str) -> None:
	if content is not None:
		repo.tests_file.write_text(content, encoding="utf-8")
	proc, outputs = repo.run("run-tests")
	assert proc.returncode == 1
	assert outputs == {}
	assert f"outcome=fail reason={reason}" in _terminal(proc)


def test_usage_error() -> None:
	proc = subprocess.run(["bash", str(SCRIPT), "bogus"], capture_output=True, text=True)
	assert proc.returncode == 2


def test_script_never_runs_selector_argv() -> None:
	text = SCRIPT.read_text(encoding="utf-8")
	assert ".argv" not in text and "static_checks" not in text and "registry_checks" not in text
	assert "set -uo pipefail" in text


# --- ci.yml wiring ------------------------------------------------------------


def _yaml():
	return pytest.importorskip("yaml")


def _ci() -> dict:
	return _yaml().safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def test_delta_job_wiring() -> None:
	ci = _ci()
	job = ci["jobs"][DELTA_JOB]
	assert job["if"] == DELTA_IF
	assert job["permissions"] == {"contents": "read", "actions": "read"}
	assert job["timeout-minutes"] == 20
	assert job["outputs"]["full_required"] == "${{ steps.main_delta_select.outputs.full_required == 'true' || steps.main_delta_run.outputs.full_required == 'true' }}"
	steps = {step.get("name"): step for step in job["steps"]}
	assert job["steps"][0]["name"] == "Checkout repository"
	assert job["steps"][0]["with"]["fetch-depth"] == 0
	assert job["steps"][0]["with"]["persist-credentials"] is False
	select = steps["Select the delta tests"]
	assert select["id"] == "main_delta_select"
	assert select["run"] == "bash scripts/ci_main_delta.sh select"
	assert select["env"]["MAIN_PUSH_DELTA_MAX_TEST_FILES"] == "${{ vars.MAIN_PUSH_DELTA_MAX_TEST_FILES || '60' }}"
	assert select["env"]["CI_MAIN_DELTA_HEAD_SHA"] == "${{ github.sha }}"
	assert select["env"]["CI_MAIN_DELTA_BRANCH"] == "main"
	run = steps["Run the delta tests"]
	assert run["id"] == "main_delta_run"
	assert run["if"] == "steps.main_delta_select.outputs.full_required == 'false'"
	assert run["run"] == "bash scripts/ci_main_delta.sh run-tests"


def test_gated_jobs_wait_for_the_delta_job() -> None:
	jobs = _ci()["jobs"]
	for job_id in GATED_JOBS:
		assert jobs[job_id]["needs"] == DELTA_JOB, job_id
		assert jobs[job_id]["if"] == GATED_IF, job_id
	assert DELTA_JOB in jobs["lint"]["needs"]
	assert DELTA_JOB in jobs["budget-watch"]["needs"]


def test_schedule_trigger_and_concurrency() -> None:
	ci = _ci()
	on = ci.get("on", ci.get(True))
	assert on["schedule"] == [{"cron": "30 4,10,16,22 * * *"}]
	group = ci["concurrency"]["group"]
	assert "github.event_name == 'schedule' && format('ci-schedule-{0}', github.ref)" in group
	assert group.index("ci-schedule-") < group.index("format('ci-{0}', github.run_id)")
	# Scheduled runs are never cancelled.
	assert "schedule" not in ci["concurrency"]["cancel-in-progress"]


def test_lint_env_and_status_run_kind() -> None:
	steps = {step.get("name"): step for step in _ci()["jobs"]["lint"]["steps"]}
	env = steps["Require every CI job to succeed"]["env"]
	assert env["CI_MAIN_DELTA_REQUESTED"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.MAIN_PUSH_DELTA_CI_ENABLED != 'false' }}"
	assert env["CI_MAIN_DELTA_JOB"] == DELTA_JOB
	assert "CI_FINAL_STATUS_RUN_KIND" in steps["Report the final CI result as a commit status"]["env"]


def test_workflow_text_carries_the_switch_without_yaml() -> None:
	text = CI_WORKFLOW.read_text(encoding="utf-8")
	assert text.count("needs.tests-main-delta.outputs.full_required == 'true'))\"") == len(GATED_JOBS)
	assert "bash scripts/ci_main_delta.sh select" in text
	assert 'cron: "30 4,10,16,22 * * *"' in text


# --- lint aggregate -----------------------------------------------------------


def _aggregate(needs: dict, *, delta_requested: str, sync_quick: str = "false", docs_only_verified: str = "false") -> subprocess.CompletedProcess[str]:
	step = next(s for s in _ci()["jobs"]["lint"]["steps"] if s.get("name") == "Require every CI job to succeed")
	env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
	env.update({
		"CI_NEEDS_JSON": json.dumps(needs),
		"CI_DOCS_ONLY_VERIFIED": docs_only_verified,
		"CI_DOCS_ONLY_GATED_JOBS": step["env"]["CI_DOCS_ONLY_GATED_JOBS"],
		"CI_SYNC_QUICK_REQUESTED": sync_quick,
		"CI_SYNC_QUICK_JOB": "tests-sync-quick",
		"CI_MAIN_DELTA_REQUESTED": delta_requested,
		"CI_MAIN_DELTA_JOB": DELTA_JOB,
	})
	return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True)


def _needs(gated: str, delta: str, full_required: str | None = "false") -> dict:
	needs = {"static-checks": {"result": "success", "outputs": {}}, "tests-sync-quick": {"result": "skipped", "outputs": {}}}
	for job_id in GATED_JOBS:
		needs[job_id] = {"result": gated, "outputs": {}}
	needs[DELTA_JOB] = {"result": delta, "outputs": {} if full_required is None else {"full_required": full_required}}
	return needs


@pytest.mark.parametrize(
	("gated", "delta", "full_required", "requested", "passes"),
	[
		# Push to main, delta suite held: the seven skips are accepted.
		("skipped", "success", "false", "true", True),
		# Delta asked for the full suite: the seven must run and pass.
		("skipped", "success", "true", "true", False),
		("success", "success", "true", "true", True),
		("failure", "success", "true", "true", False),
		# Delta failed, or its output is missing.
		("skipped", "failure", "false", "true", False),
		("skipped", "success", None, "true", False),
		("skipped", "success", "", "true", False),
		# Scheduled / stable / project / dispatch runs (not requested): the
		# delta skip is accepted, a skipped test job is not.
		("success", "skipped", "false", "false", True),
		("skipped", "skipped", "false", "false", False),
		# Switch off on a push to main: same as any other run.
		("success", "skipped", None, "false", True),
	],
)
def test_aggregate_matrix(gated: str, delta: str, full_required: str | None, requested: str, passes: bool) -> None:
	proc = _aggregate(_needs(gated, delta, full_required), delta_requested=requested)
	assert (proc.returncode == 0) is passes, proc.stdout + proc.stderr


def test_aggregate_delta_job_failing_is_named() -> None:
	proc = _aggregate(_needs("skipped", "failure"), delta_requested="true")
	assert proc.returncode == 1
	assert DELTA_JOB in proc.stdout
