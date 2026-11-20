#!/usr/bin/env python3
"""Contract tests: workflow-log-analysis delivers its report through a PR.

The `Commit and push report` step (id `commit_report`) of job
`analyze-commit-notify` used to push the new report straight to the dispatch
branch. With `WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR` (default `true`) it now
pushes a run-unique `auto/workflow-log-analysis-report-<run>-<attempt>`
branch, opens a PR, and tries a direct squash merge, then auto-merge, then
leaves the PR open. `false` keeps the legacy direct push.

These tests run the step's real `run:` body with bash against a temporary
bare origin and a fake `gh` on PATH, plus static checks on the YAML.

CI runs these cases through tests/test_workflow_log_analysis_failure_contract.py,
which collects them (ci.yml was outside this issue's file grant).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WF_PATH = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"
LINT_SCRIPT = REPO_ROOT / "scripts" / "lint_pr_body_auto_close.py"
STEP_MARKER = "      - name: Commit and push report\n"
REPORT = "analysis/workflow-optimization-2026-11-09.md"
PR_BRANCH = "auto/workflow-log-analysis-report-111-1"
DELIVER_SCRIPT = REPO_ROOT / "scripts" / "workflow_log_analysis_deliver_report.sh"
DEEP_AUDIT_PUSH_STEP = "Commit and push deep audit section"
API_REDUNDANCY_PUSH_STEP = "Commit and push API redundancy section"
SECTION_SYNC_STEP = "Sync latest report from target branch"

FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GH_LOG}"
if [ "$1 $2" = "pr create" ]; then
  printf '%s\\n' "${FAKE_GH_CREATE_OUT:-https://github.com/o/r/pull/42}"
  exit "${FAKE_GH_CREATE_RC:-0}"
fi
if [ "$1 $2" = "pr list" ]; then
  printf '%s\\n' "${FAKE_GH_LIST_OUT:-}"
  exit 0
fi
if [ "$1 $2" = "pr merge" ]; then
  case " $* " in
    *" --auto "*)
      # Simulate auto-merge landing the report right away.
      if [ -n "${FAKE_GH_AUTO_LANDS:-}" ]; then
        git push -q origin HEAD:refs/heads/main
      fi
      exit "${FAKE_GH_AUTO_RC:-0}" ;;
  esac
  # A direct merge lands the report, unless it only joined a merge queue.
  if [ "${FAKE_GH_MERGE_RC:-0}" = "0" ] && [ -z "${FAKE_GH_MERGE_QUEUED:-}" ]; then
    git push -q origin HEAD:refs/heads/main
  fi
  exit "${FAKE_GH_MERGE_RC:-0}"
fi
exit 0
"""


def _workflow_text() -> str:
	return WF_PATH.read_text(encoding="utf-8")


def _step_text() -> str:
	return _workflow_text().split(STEP_MARKER, 1)[1].split("\n      - name: ", 1)[0]


def _step_body() -> str:
	step = _step_text()
	body = step.split("        run: |\n", 1)[1]
	lines = []
	for line in body.splitlines():
		lines.append(line[10:] if line.startswith("          ") else line.strip())
	return "\n".join(lines) + "\n"


def _named_step_body(name: str, occurrence: int = 0) -> str:
	# The dedented `run:` body of the occurrence-th step called `name`.
	block = _workflow_text().split(f"      - name: {name}\n")[1 + occurrence].split("\n      - name: ", 1)[0]
	lines: list[str] = []
	for line in block.split("        run: |\n", 1)[1].splitlines():
		if line.startswith("          "):
			lines.append(line[10:])
		elif line.strip() == "":
			lines.append("")
		else:
			break
	return "\n".join(lines) + "\n"


def _clean_env() -> dict[str, str]:
	# Keep repo-pinning git variables (GIT_DIR, GIT_WORK_TREE, ...) from the
	# calling workflow out of the scratch repos (see tests/conftest.py).
	return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(cwd: Path, *args: str, env: dict[str, str]) -> str:
	proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
	return proc.stdout.strip()


class _Sandbox:
	def __init__(self, tmp: Path) -> None:
		self.tmp = tmp
		self.origin = tmp / "origin.git"
		self.work = tmp / "work"
		self.bin = tmp / "bin"
		self.runner_temp = tmp / "runner_temp"
		self.output = tmp / "github_output"
		self.gh_log = tmp / "gh_calls"
		gitconfig = tmp / "gitconfig"
		gitconfig.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n", encoding="utf-8")
		self.git_env = _clean_env()
		self.git_env.update(
			{
				"GIT_CONFIG_GLOBAL": str(gitconfig),
				"GIT_CONFIG_NOSYSTEM": "1",
				"GIT_TERMINAL_PROMPT": "0",
			}
		)
		for path in (self.bin, self.runner_temp):
			path.mkdir()
		self.output.write_text("", encoding="utf-8")
		self.gh_log.write_text("", encoding="utf-8")
		gh = self.bin / "gh"
		gh.write_text(FAKE_GH, encoding="utf-8")
		gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

		seed = tmp / "seed"
		seed.mkdir()
		_git(seed, "init", "-q", "-b", "main", env=self.git_env)
		(seed / "scripts").mkdir()
		(seed / "scripts" / "apply_analysis_on_main.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
		shutil.copy(LINT_SCRIPT, seed / "scripts" / "lint_pr_body_auto_close.py")
		(seed / "analysis").mkdir()
		(seed / "analysis" / ".keep").write_text("", encoding="utf-8")
		# The purge removes only docs this processing report lists (plan P5).
		(seed / "analysis" / "recommendation-processing-report.md").write_text(
			"# Recommendation processing report\n", encoding="utf-8"
		)
		_git(seed, "add", "-A", env=self.git_env)
		_git(seed, "commit", "-q", "-m", "base", env=self.git_env)
		_git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin), env=self.git_env)
		_git(seed, "push", "-q", self.origin.as_uri(), "HEAD:refs/heads/main", env=self.git_env)
		_git(tmp, "clone", "-q", self.origin.as_uri(), str(self.work), env=self.git_env)
		self.base_main = self.origin_ref("refs/heads/main")

	def origin_ref(self, ref: str) -> str:
		proc = subprocess.run(
			["git", "rev-parse", "--verify", "-q", ref],
			cwd=self.origin,
			env=self.git_env,
			capture_output=True,
			text=True,
			check=False,
		)
		return proc.stdout.strip() if proc.returncode == 0 else ""

	def origin_has(self, ref: str, path: str) -> bool:
		proc = subprocess.run(
			["git", "cat-file", "-e", f"{ref}:{path}"], cwd=self.origin, env=self.git_env, capture_output=True, check=False
		)
		return proc.returncode == 0

	def write_report(self, path: str = REPORT) -> None:
		target = self.work / path
		target.write_text("# report\n\n- finding\n", encoding="utf-8")

	def run(self, report_file: str = REPORT, **extra: str) -> subprocess.CompletedProcess[str]:
		env = dict(self.git_env)
		env.update(
			{
				"PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
				"GITHUB_OUTPUT": str(self.output),
				"GITHUB_REF_TYPE": "branch",
				"GITHUB_REF_NAME": "main",
				"GITHUB_RUN_ID": "111",
				"GITHUB_RUN_ATTEMPT": "1",
				"GITHUB_REPOSITORY": "o/r",
				"GITHUB_SERVER_URL": "https://github.com",
				"RUNNER_TEMP": str(self.runner_temp),
				"REPORT_RETENTION_DAYS": "30",
				"GH_TOKEN": "x",
				"WLA_ANALYZE_REPORT_FILE": report_file,
				"FAKE_GH_LOG": str(self.gh_log),
				"WORKFLOW_LOG_ANALYSIS_REPORT_MERGE_WAIT_MINUTES": "0",
				"WORKFLOW_LOG_ANALYSIS_REPORT_MERGE_POLL_SECS": "1",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		env.pop("WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR", None)
		# Issue #9135: a caller shell value must not trip the PR-mode PAT gate.
		env.pop("WLA_REPORT_PR_PAT_SET", None)
		env.update(extra)
		return subprocess.run(
			["bash", "-c", _step_body()], cwd=self.work, env=env, capture_output=True, text=True, check=False
		)

	def run_named_step(self, name: str, occurrence: int = 0, **extra: str) -> subprocess.CompletedProcess[str]:
		# A section job step (issue #9503): the report already reached main.
		env = dict(self.git_env)
		env.update(
			{
				"PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
				"GITHUB_OUTPUT": str(self.output),
				"GITHUB_REF_TYPE": "branch",
				"GITHUB_REF_NAME": "main",
				"GITHUB_RUN_ID": "111",
				"GITHUB_RUN_ATTEMPT": "1",
				"FAKE_GH_LOG": str(self.gh_log),
				"REPORT_FILE": REPORT,
				"REPORT_BRANCH": "",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		env.pop("WORKFLOW_LOG_ANALYSIS_REPORT_SECTIONS_VIA_PR", None)
		env.update(extra)
		self.output.write_text("", encoding="utf-8")
		return subprocess.run(
			["bash", "-c", _named_step_body(name, occurrence)],
			cwd=self.work,
			env=env,
			capture_output=True,
			text=True,
			check=False,
		)

	def land_report_on_main(self, *extra_files: str) -> str:
		# Another clone lands the report on main (a push or a squash merge,
		# after which the run branch is gone), then any unrelated files.
		other = self.tmp / "lander"
		_git(self.tmp, "clone", "-q", self.origin.as_uri(), str(other), env=self.git_env)
		(other / REPORT).write_text("# report\n\n- finding\n", encoding="utf-8")
		_git(other, "add", "-A", env=self.git_env)
		_git(other, "commit", "-q", "-m", "report", env=self.git_env)
		for rel in extra_files:
			(other / rel).write_text("unrelated\n", encoding="utf-8")
			_git(other, "add", "-A", env=self.git_env)
			_git(other, "commit", "-q", "-m", f"unrelated {rel}", env=self.git_env)
		_git(other, "push", "-q", "origin", "HEAD:refs/heads/main", env=self.git_env)
		return self.origin_ref("refs/heads/main")

	def append_section(self, heading: str) -> None:
		with (self.work / REPORT).open("a", encoding="utf-8") as handle:
			handle.write(f"\n## {heading}\n")

	def deliver_sections(self, branch: str, base: str) -> subprocess.CompletedProcess[str]:
		checkout = self.tmp / "deliver_checkout"
		_git(self.tmp, "clone", "-q", "--depth", "1", "--branch", "main", self.origin.as_uri(), str(checkout), env=self.git_env)
		env = dict(self.git_env)
		env.update(
			{
				"PATH": f"{self.bin}{os.pathsep}{env.get('PATH', '')}",
				"GITHUB_OUTPUT": str(self.output),
				"GITHUB_REF_TYPE": "branch",
				"GITHUB_REF_NAME": "main",
				"GITHUB_REPOSITORY": "o/r",
				"GITHUB_RUN_ID": "111",
				"GITHUB_SERVER_URL": "https://github.com",
				"RUNNER_TEMP": str(self.runner_temp),
				"FAKE_GH_LOG": str(self.gh_log),
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"GH_TOKEN": "x",
				"REPORT_PUSH_TOKEN": "x",
				"REPORT_BRANCH": "",
				"REPORT_FILE": REPORT,
				"AUDIT_STATUS": "completed",
				"WLA_DELIVER_SECTIONS_ONLY": "true",
				"WLA_DELIVER_SECTIONS_BRANCH": branch,
				"WLA_DELIVER_SECTIONS_BASE_SHA": base,
				"PYTHONDONTWRITEBYTECODE": "1",
			}
		)
		self.output.write_text("", encoding="utf-8")
		return subprocess.run(
			["bash", str(DELIVER_SCRIPT)], cwd=checkout, env=env, capture_output=True, text=True, check=False, timeout=120
		)

	def outputs(self) -> dict[str, str]:
		result: dict[str, str] = {}
		for line in self.output.read_text(encoding="utf-8").splitlines():
			key, _, value = line.partition("=")
			result[key] = value
		return result

	def gh_calls(self) -> list[str]:
		return self.gh_log.read_text(encoding="utf-8").splitlines()


def _delivery_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
	return [line for line in proc.stdout.splitlines() if line.startswith("WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY ")]


def test_report_pr_default_mode_opens_and_merges_pr() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run()
		assert proc.returncode == 0, proc.stderr + proc.stdout
		head = _git(box.work, "rev-parse", "HEAD", env=box.git_env)
		# The (faked) PR merge delivers the report; the step checks it landed.
		assert box.origin_has("refs/heads/main", REPORT)
		# The run branch is deleted after the merge.
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
		calls = box.gh_calls()
		assert len(calls) == 2, calls
		assert calls[0].startswith(f"pr create --base main --head {PR_BRANCH} ")
		assert f"--title chore: add workflow log analysis report ({REPORT})" in calls[0]
		assert re.fullmatch(r"[0-9a-f]{40}", head)
		assert calls[1] == f"pr merge 42 --squash --match-head-commit {head}"
		out = box.outputs()
		assert out["committed"] == "true"
		assert out["report_file"] == REPORT
		assert out["delivery"] == "pr_merged"
		assert out["report_pr_number"] == "42"
		assert out["report_pr_url"] == "https://github.com/o/r/pull/42"
		assert out["report_branch"] == "main"
		assert _delivery_lines(proc) == [
			f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY mode=pr outcome=pr_merged pr=42 base=main file={REPORT}"
		]
		body = (box.runner_temp / "wla_report_pr_body.md").read_text(encoding="utf-8")
		assert REPORT in body
		assert "#" not in body.replace("## Workflow", "")


def test_report_pr_queued_direct_merge_is_not_reported_merged() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_QUEUED="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		# Accepted into a merge queue but not merged: no pr_merged, and the
		# head branch is kept so the queued PR is not closed.
		assert box.origin_ref("refs/heads/main") == box.base_main
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)
		calls = box.gh_calls()
		assert len(calls) == 2, calls
		out = box.outputs()
		assert out["delivery"] == "pr_auto_merge"
		assert out["report_branch"] == PR_BRANCH
		assert "not on main yet" in proc.stdout


def test_report_pr_auto_merge_drops_purge_deletions() -> None:
	old_report = "analysis/workflow-optimization-2020-01-01.md"
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		(box.work / old_report).write_text("# old\n", encoding="utf-8")
		# Processed, so the purge would delete it (plan P5).
		with (box.work / "analysis" / "recommendation-processing-report.md").open("a", encoding="utf-8") as handle:
			handle.write("- workflow-optimization-2020-01-01.md: applied\n")
		_git(box.work, "add", "-A", env=box.git_env)
		_git(box.work, "commit", "-q", "-m", "old report", env=box.git_env)
		_git(box.work, "push", "-q", "origin", "HEAD:main", env=box.git_env)
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_RC="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		# Auto-merge can land after the purge guard's answer is stale, so the
		# PR keeps the old report and carries only the new one.
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", old_report)
		assert "WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=auto_merge_pending count=1" in proc.stdout
		branch_head = box.origin_ref(f"refs/heads/{PR_BRANCH}")
		calls = box.gh_calls()
		assert len(calls) == 3, calls
		assert calls[2] == f"pr merge 42 --auto --squash --match-head-commit {branch_head}", calls
		out = box.outputs()
		assert out["delivery"] == "pr_auto_merge"
		# The kept doc is not reported as purged.
		assert out["purged_docs"] == ""


def test_report_pr_queued_direct_merge_drops_purge_deletions_first() -> None:
	# A merge queue accepts the direct merge (exit 0) and lands it later, so
	# the purge deletions must leave the PR before that merge call too.
	old_report = "analysis/workflow-optimization-2020-01-01.md"
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		(box.work / old_report).write_text("# old\n", encoding="utf-8")
		with (box.work / "analysis" / "recommendation-processing-report.md").open("a", encoding="utf-8") as handle:
			handle.write("- workflow-optimization-2020-01-01.md: applied\n")
		_git(box.work, "add", "-A", env=box.git_env)
		_git(box.work, "commit", "-q", "-m", "old report", env=box.git_env)
		_git(box.work, "push", "-q", "origin", "HEAD:main", env=box.git_env)
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_QUEUED="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", old_report)
		assert "WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=auto_merge_pending count=1" in proc.stdout
		branch_head = box.origin_ref(f"refs/heads/{PR_BRANCH}")
		calls = box.gh_calls()
		assert len(calls) == 2, calls
		assert calls[1] == f"pr merge 42 --squash --match-head-commit {branch_head}", calls
		out = box.outputs()
		assert out["delivery"] == "pr_auto_merge"
		assert out["purged_docs"] == ""


def test_report_pr_purge_drop_restores_a_doc_git_would_pair_as_a_rename() -> None:
	# A purged doc with the same text as the new report is a rename (R) to
	# git's default diff, so --diff-filter=D alone would miss it and the PR
	# would still delete it. The purge-drop must classify with --no-renames.
	old_report = "analysis/workflow-optimization-2020-01-01.md"
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		(box.work / old_report).write_text("# report\n\n- finding\n", encoding="utf-8")
		with (box.work / "analysis" / "recommendation-processing-report.md").open("a", encoding="utf-8") as handle:
			handle.write("- workflow-optimization-2020-01-01.md: applied\n")
		_git(box.work, "add", "-A", env=box.git_env)
		_git(box.work, "commit", "-q", "-m", "old report", env=box.git_env)
		_git(box.work, "push", "-q", "origin", "HEAD:main", env=box.git_env)
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_QUEUED="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", old_report)
		assert "WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED reason=auto_merge_pending count=1" in proc.stdout
		assert "INFO: purged" not in proc.stdout
		assert "INFO: selected 1 workflow-optimization report(s)" in proc.stdout
		assert box.outputs()["purged_docs"] == ""


def test_report_pr_auto_merge_when_direct_merge_refused() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_RC="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_ref("refs/heads/main") == box.base_main
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)
		calls = box.gh_calls()
		assert len(calls) == 3, calls
		assert calls[2].startswith("pr merge 42 --auto --squash --match-head-commit ")
		out = box.outputs()
		assert out["delivery"] == "pr_auto_merge"
		assert out["report_branch"] == PR_BRANCH
		assert out["committed"] == "true"
		assert len(_delivery_lines(proc)) == 1


def test_report_pr_auto_merge_wait_sees_the_report_land() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_RC="1", FAKE_GH_AUTO_LANDS="1", WORKFLOW_LOG_ANALYSIS_REPORT_MERGE_WAIT_MINUTES="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_has("refs/heads/main", REPORT)
		# The run branch is deleted once the report is on the target branch.
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
		out = box.outputs()
		assert out["delivery"] == "pr_merged"
		assert out["report_branch"] == "main"
		assert "not on main yet" not in proc.stdout
		assert _delivery_lines(proc) == [
			f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY mode=pr outcome=pr_merged pr=42 base=main file={REPORT}"
		]


def test_report_pr_auto_merge_not_landed_warns_about_skipped_audits() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_RC="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "deep-audit and api-redundancy are skipped" in proc.stdout
		assert box.outputs()["delivery"] == "pr_auto_merge"


def test_report_pr_renames_when_a_pending_report_pr_holds_the_path() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		# An earlier run's report still waits in its PR branch, not on main.
		other = Path(raw) / "other"
		_git(Path(raw), "clone", "-q", box.origin.as_uri(), str(other), env=box.git_env)
		(other / REPORT).write_text("# earlier run\n", encoding="utf-8")
		_git(other, "add", "-A", env=box.git_env)
		_git(other, "commit", "-q", "-m", "earlier report", env=box.git_env)
		_git(other, "push", "-q", "origin", "HEAD:refs/heads/auto/workflow-log-analysis-report-100-1", env=box.git_env)
		box.write_report()
		proc = box.run()
		assert proc.returncode == 0, proc.stderr + proc.stdout
		renamed = "analysis/workflow-optimization-2026-11-09-2.md"
		assert box.outputs()["report_file"] == renamed
		assert f"({renamed})" in box.gh_calls()[0]


def test_report_pr_unrecognized_switch_value_warns_and_uses_pr() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR="flase")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "::warning::WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR has an unrecognized value" in proc.stdout
		assert "flase" not in proc.stdout
		assert box.outputs()["delivery"] == "pr_merged"


def test_report_pr_number_parsed_from_enterprise_url() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_CREATE_OUT="https://ghe.example.com/o/r/pull/314")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		out = box.outputs()
		assert out["report_pr_number"] == "314"
		assert out["report_pr_url"] == "https://ghe.example.com/o/r/pull/314"


def test_report_pr_merge_wait_clamped_to_job_budget() -> None:
	import time

	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		started = time.monotonic()
		# The job started an hour ago, so a 20-minute wait ends at once.
		proc = box.run(
			FAKE_GH_MERGE_RC="1",
			WORKFLOW_LOG_ANALYSIS_REPORT_MERGE_WAIT_MINUTES="20",
			WLA_JOB_START_EPOCH=str(int(time.time()) - 3600),
		)
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert time.monotonic() - started < 120
		assert box.outputs()["delivery"] == "pr_auto_merge"


def test_report_pr_stays_open_when_both_merges_refused() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_MERGE_RC="1", FAKE_GH_AUTO_RC="1")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "::warning::" in proc.stdout and "https://github.com/o/r/pull/42" in proc.stdout
		out = box.outputs()
		assert out["delivery"] == "pr_open"
		assert out["report_branch"] == PR_BRANCH
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)
		assert _delivery_lines(proc) == [
			f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY mode=pr outcome=pr_open pr=42 base=main file={REPORT}"
		]


def test_report_pr_create_failure_fails_the_step() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(FAKE_GH_CREATE_RC="1", FAKE_GH_CREATE_OUT="HTTP 502: Bad Gateway")
		assert proc.returncode != 0
		assert "committed=true" not in box.output.read_text(encoding="utf-8")
		assert len(box.gh_calls()) == 1
		# The report is kept on the run branch for an operator.
		assert box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT)


def test_report_pr_already_exists_looks_up_the_open_pr() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(
			FAKE_GH_CREATE_RC="1",
			FAKE_GH_CREATE_OUT=f'a pull request for branch "{PR_BRANCH}" into branch "main" already exists:\nhttps://github.com/o/r/pull/7',
			FAKE_GH_LIST_OUT="7 https://github.com/o/r/pull/7",
		)
		assert proc.returncode == 0, proc.stderr + proc.stdout
		calls = box.gh_calls()
		assert calls[1].startswith(f"pr list --head {PR_BRANCH} --base main --state open --json number,url")
		assert calls[2].startswith("pr merge 7 --squash --match-head-commit ")
		out = box.outputs()
		assert out["report_pr_number"] == "7"
		assert out["delivery"] == "pr_merged"


def test_report_pr_disabled_keeps_legacy_direct_push() -> None:
	for value in ("false", "OFF", "0", "no"):
		with tempfile.TemporaryDirectory() as raw:
			box = _Sandbox(Path(raw))
			box.write_report()
			proc = box.run(WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR=value)
			assert proc.returncode == 0, proc.stderr + proc.stdout
			assert box.gh_calls() == []
			assert box.origin_has("refs/heads/main", REPORT)
			assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
			out = box.outputs()
			assert out["committed"] == "true"
			assert out["delivery"] == "pushed"
			assert out["report_pr_number"] == ""
			assert out["report_branch"] == "main"
			assert _delivery_lines(proc) == [
				f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY mode=push outcome=pushed pr=none base=main file={REPORT}"
			]


def test_report_pr_renames_on_upstream_collision() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		# Another run publishes the same dated path after this run's checkout.
		other = Path(raw) / "other"
		_git(Path(raw), "clone", "-q", box.origin.as_uri(), str(other), env=box.git_env)
		(other / REPORT).write_text("# other run\n", encoding="utf-8")
		_git(other, "add", "-A", env=box.git_env)
		_git(other, "commit", "-q", "-m", "other report", env=box.git_env)
		_git(other, "push", "-q", "origin", "HEAD:main", env=box.git_env)
		box.write_report()
		proc = box.run()
		assert proc.returncode == 0, proc.stderr + proc.stdout
		renamed = "analysis/workflow-optimization-2026-11-09-2.md"
		out = box.outputs()
		assert out["report_file"] == renamed
		assert f"({renamed})" in box.gh_calls()[0]
		assert out["delivery"] == "pr_merged"


def test_report_pr_no_changes_makes_no_gh_calls() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		# Report already committed and identical: nothing to commit.
		box.write_report()
		_git(box.work, "add", "-A", env=box.git_env)
		_git(box.work, "commit", "-q", "-m", "same", env=box.git_env)
		proc = box.run()
		assert proc.returncode == 0, proc.stderr + proc.stdout
		out = box.outputs()
		assert out["committed"] == "false"
		assert "delivery" not in out
		assert box.gh_calls() == []
		assert _delivery_lines(proc) == []


def test_report_pr_rejects_malformed_report_path() -> None:
	for bad in ("../x.md", "", "analysis/workflow-optimization-2026-11-09.md;id", "analysis/other.md"):
		with tempfile.TemporaryDirectory() as raw:
			box = _Sandbox(Path(raw))
			box.write_report()
			proc = box.run(report_file=bad)
			assert proc.returncode != 0, bad
			assert box.gh_calls() == []
			assert box.output.read_text(encoding="utf-8") == ""
			assert box.origin_ref("refs/heads/main") == box.base_main
			assert _git(box.work, "rev-parse", "HEAD", env=box.git_env) == box.base_main


def test_report_pr_missing_pat_fails_before_any_push() -> None:
	# Issue #9135: PR mode without GH_PAT fails closed before any push or gh
	# call, instead of opening a PR with github.token that starts no checks.
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(WLA_REPORT_PR_PAT_SET="false")
		assert proc.returncode != 0, proc.stderr + proc.stdout
		assert (
			f"::error::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=token_missing branch={PR_BRANCH}"
			in proc.stdout
		)
		assert "workflow-log-analysis-report" in proc.stdout
		assert box.gh_calls() == []
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
		assert box.origin_ref("refs/heads/main") == box.base_main
		assert "committed=true" not in box.output.read_text(encoding="utf-8")
		assert _delivery_lines(proc) == []
	# Push mode keeps the github.token fallback and still pushes the report.
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.write_report()
		proc = box.run(WLA_REPORT_PR_PAT_SET="false", WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR="false")
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "reason=token_missing" not in proc.stdout
		assert box.origin_has("refs/heads/main", REPORT)
		assert box.gh_calls() == []
		assert box.outputs()["delivery"] == "pushed"


def test_report_pr_ruleset_rejection_fails_the_step() -> None:
	# Plan P1 tests: a GH013 refusal of the run-branch push logs
	# reason=push_rejected_by_ruleset and fails the step.
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		hook = box.origin / "hooks" / "pre-receive"
		hook.write_text("#!/bin/sh\necho 'error: GH013: Repository rule violations found' >&2\nexit 1\n", encoding="utf-8")
		hook.chmod(0o755)
		box.write_report()
		proc = box.run()
		assert proc.returncode != 0, proc.stderr + proc.stdout
		assert (
			f"::error::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=push_rejected_by_ruleset branch={PR_BRANCH}"
			in proc.stdout
		)
		assert box.gh_calls() == []
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
		assert box.origin_ref("refs/heads/main") == box.base_main
		assert "committed=true" not in box.output.read_text(encoding="utf-8")


def test_report_pr_static_contract() -> None:
	wf = _workflow_text()
	step = _step_text()
	# Identifier immutability: name, id, if and the legacy env entries.
	assert step.startswith("        id: commit_report\n        if: steps.analyze.outputs.analysis_status == 'completed'\n")
	assert "WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR: ${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR || 'true' }}" in step
	assert "WLA_ANALYZE_REPORT_FILE: ${{ steps.analyze.outputs.report_file }}" in step
	assert "${{" not in _step_body()
	# Issue #9135: PR mode requires GH_PAT; only a boolean reaches the shell.
	assert "WLA_REPORT_PR_PAT_SET: ${{ secrets.GH_PAT != '' && 'true' || 'false' }}" in step
	assert '[ "${WLA_REPORT_PR_PAT_SET:-true}" != "true" ]' in _step_body()
	for line in _step_body().splitlines():
		if "echo" in line or "printf" in line:
			assert not re.search(r"\$\{?(GH_TOKEN|GH_PAT|AI_COMMENT_JOB_TOKEN)\b", line), line
	# Legacy outputs still written.
	assert 'echo "committed=true" >> "$GITHUB_OUTPUT"' in step
	assert 'echo "report_file=${REPORT_FILE}" >> "$GITHUB_OUTPUT"' in step
	# Legacy push command kept for push mode.
	assert 'git push origin "HEAD:${TARGET_BRANCH}"' in step
	# Order: lint before create before merge.
	lint_at = step.index("python3 scripts/lint_pr_body_auto_close.py")
	create_at = step.index('report_pr_create_out="$(gh pr create')
	merge_at = step.index('if gh pr merge "${report_pr_number}"')
	assert lint_at < create_at < merge_at
	for line in step.splitlines():
		if "gh pr merge" in line and not line.lstrip().startswith("#"):
			assert "--match-head-commit" in line, line
	assert "--admin" not in step
	assert "GitHub API budget" in step
	# Job-level permissions and the new job output.
	job = wf.split("\n  analyze-commit-notify:\n", 1)[1].split("\n  deep-audit:\n", 1)[0]
	header = job.split("    env:\n", 1)[0]
	assert "    permissions:\n" in header
	assert "      pull-requests: write\n" in header
	for grant in ("contents: write", "actions: read", "issues: write", "id-token: write"):
		assert f"      {grant}\n" in header, grant
	assert "      report_delivery: ${{ steps.commit_report.outputs.delivery }}\n" in header
	# The workflow-level block is not widened.
	top = wf.split("\njobs:\n", 1)[0]
	assert "pull-requests: write" not in top
	# deep-audit waits for the report to be on the target branch.
	deep = wf.split("\n  deep-audit:\n", 1)[1].split("\n    runs-on:", 1)[0]
	assert (
		"&& (needs.analyze-commit-notify.outputs.report_delivery == '' "
		"|| needs.analyze-commit-notify.outputs.report_delivery == 'pushed' "
		"|| needs.analyze-commit-notify.outputs.report_delivery == 'pr_merged')"
	) in deep
	# Telegram links the PR while the report is not on the branch.
	assert "WLA_REPORT_PR_URL: ${{ steps.commit_report.outputs.report_pr_url || '' }}" in wf
	assert "WLA_REPORT_DELIVERY: ${{ steps.commit_report.outputs.delivery || '' }}" in wf
	assert 'MSG+="Report PR: ${WLA_REPORT_PR_URL}"' in wf
	# A failed delivery is reported as a failure, not with a dead blob link.
	assert "WLA_COMMIT_OUTCOME: ${{ steps.commit_report.outcome || '' }}" in wf
	assert '[ "${WLA_COMMIT_OUTCOME:-}" = "failure" ]' in wf
	assert "WORKFLOW_LOG_ANALYSIS_REPORT_MERGE_WAIT_MINUTES: ${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_MERGE_WAIT_MINUTES || '10' }}" in step
	# The run-branch push is leased, never a bare --force.
	assert "git push --force origin" not in step
	assert '--force-with-lease="refs/heads/${REPORT_PR_BRANCH}:${report_last_pushed_sha}"' in step
	# A retry leases to the run branch's remote head.
	assert 'report_last_pushed_sha="$(git ls-remote origin "refs/heads/${REPORT_PR_BRANCH}"' in step
	# The merge wait is clamped to the job budget.
	assert 'echo "WLA_JOB_START_EPOCH=$(date +%s)" >> "$GITHUB_ENV"' in wf
	assert "WLA_JOB_START_EPOCH + 55 * 60" in step
	# Telegram: a cancelled delivery is a failure, an unlanded PR is a WARNING,
	# and the run branch is named only in PR mode.
	assert '[ "${WLA_COMMIT_OUTCOME:-}" = "cancelled" ]' in wf
	assert "WLA_REPORT_VIA_PR: ${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR || 'true' }}" in wf
	telegram = wf.split("      - name: Send Telegram notification\n", 1)[1].split("\n  deep-audit:\n", 1)[0]
	assert 'ALERT_LEVEL="WARNING"\n            fi\n          else' in telegram
	# Only a PR left open pages at WARNING; an armed auto-merge stays DEBUG.
	assert 'if [ "${WLA_REPORT_DELIVERY:-}" = "pr_open" ]; then\n              ALERT_LEVEL="WARNING"' in telegram
	assert '= "pr_auto_merge" ] || [ "${WLA_REPORT_DELIVERY:-}" = "pr_open" ]; then\n              ALERT_LEVEL' not in telegram


# --- section delivery (issue #9503, project #9116 P1b) -------------------

def test_section_push_mode_report_pushed_creates_run_branch_for_delivery() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		# Push mode delivered the report straight to main; the deep-audit job
		# synced that tip and appended its section.
		main_tip = box.land_report_on_main()
		_git(box.work, "pull", "-q", "--rebase", "origin", "main", env=box.git_env)
		box.append_section("Deep audit")
		proc = box.run_named_step(DEEP_AUDIT_PUSH_STEP)
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_ref("refs/heads/main") == main_tip
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}^") == main_tip
		section = subprocess.run(
			["git", "show", f"{PR_BRANCH}:{REPORT}"], cwd=box.origin, env=box.git_env, capture_output=True, text=True, check=True
		).stdout
		assert "## Deep audit" in section
		out = box.outputs()
		assert out["section_branch"] == PR_BRANCH
		assert out["section_base_sha"] == main_tip
		assert (
			f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=section_pushed branch={PR_BRANCH} base={main_tip} job=deep-audit"
			in proc.stdout
		)
		assert "reason=no_report_branch" not in proc.stdout
		assert box.gh_calls() == []
		# deliver-report opens one sections-only PR for that branch.
		delivered = box.deliver_sections(PR_BRANCH, main_tip)
		assert delivered.returncode == 0, delivered.stderr + delivered.stdout
		assert f"WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=pr_opened branch={PR_BRANCH} pr=42 file={REPORT}" in delivered.stdout
		calls = box.gh_calls()
		assert [call for call in calls if call.startswith("pr create")] == calls and len(calls) == 1, calls
		assert "sections" in calls[0]
		assert not box.origin_has(f"refs/heads/{PR_BRANCH}", REPORT.replace(".md", "-2.md"))


def test_section_pr_merged_recreates_deleted_run_branch() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		# The report PR was squash-merged, its branch deleted, and main then
		# gained an unrelated commit before the section jobs ran.
		main_tip = box.land_report_on_main("unrelated.txt")
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
		sync = box.run_named_step(SECTION_SYNC_STEP, 0)
		assert sync.returncode == 0, sync.stderr + sync.stdout
		assert _git(box.work, "rev-parse", "HEAD", env=box.git_env) == main_tip
		box.append_section("Deep audit")
		proc = box.run_named_step(DEEP_AUDIT_PUSH_STEP)
		assert proc.returncode == 0, proc.stderr + proc.stdout
		deep_tip = box.origin_ref(f"refs/heads/{PR_BRANCH}")
		assert deep_tip and box.origin_ref(f"refs/heads/{PR_BRANCH}^") == main_tip
		assert box.outputs() == {"section_branch": PR_BRANCH, "section_base_sha": main_tip}
		# api-redundancy continues on deep-audit's branch: a fast-forward.
		sync = box.run_named_step(SECTION_SYNC_STEP, 1, SECTION_BRANCH=PR_BRANCH)
		assert sync.returncode == 0, sync.stderr + sync.stdout
		assert _git(box.work, "rev-parse", "HEAD", env=box.git_env) == deep_tip
		box.append_section("API redundancy")
		proc = box.run_named_step(API_REDUNDANCY_PUSH_STEP, SECTION_BRANCH=PR_BRANCH, SECTION_BASE_SHA=main_tip)
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}^") == deep_tip
		assert box.outputs() == {"section_branch": PR_BRANCH, "section_base_sha": main_tip}
		assert f"outcome=section_pushed branch={PR_BRANCH} base={main_tip} job=api-redundancy" in proc.stdout
		assert box.origin_ref("refs/heads/main") == main_tip
		delivered = box.deliver_sections(PR_BRANCH, main_tip)
		assert delivered.returncode == 0, delivered.stderr + delivered.stdout
		assert "outcome=pr_opened" in delivered.stdout


def test_section_no_changes_pushes_nothing() -> None:
	with tempfile.TemporaryDirectory() as raw:
		box = _Sandbox(Path(raw))
		box.land_report_on_main()
		_git(box.work, "pull", "-q", "--rebase", "origin", "main", env=box.git_env)
		proc = box.run_named_step(DEEP_AUDIT_PUSH_STEP)
		assert proc.returncode == 0, proc.stderr + proc.stdout
		assert "No deep audit section changes to commit." in proc.stdout
		assert "section_branch" not in box.outputs()
		assert box.origin_ref(f"refs/heads/{PR_BRANCH}") == ""
		assert box.gh_calls() == []
		assert "WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY" not in proc.stdout


def test_section_steps_static_contract() -> None:
	wf = _workflow_text()
	for name, step_id in ((DEEP_AUDIT_PUSH_STEP, "deep_audit_section_push"), (API_REDUNDANCY_PUSH_STEP, "api_redundancy_section_push")):
		block = wf.split(f"      - name: {name}\n", 1)[1].split("\n      - name: ", 1)[0]
		assert block.startswith(f"        id: {step_id}\n"), name
		assert (
			"WORKFLOW_LOG_ANALYSIS_REPORT_SECTIONS_VIA_PR: ${{ vars.WORKFLOW_LOG_ANALYSIS_REPORT_SECTIONS_VIA_PR || 'true' }}"
			in block
		), name
		body = _named_step_body(name)
		assert "${{" not in body
		assert '"${WORKFLOW_LOG_ANALYSIS_REPORT_SECTIONS_VIA_PR:-true}"' in body
		assert "HEAD:${TARGET_BRANCH}" not in body and "HEAD:${GITHUB_REF_NAME}" not in body
		assert "--force" not in body and "--admin" not in body
		assert 'git push origin "HEAD:refs/heads/${REPORT_BRANCH}"' in body
		assert 'REPORT_BRANCH="auto/workflow-log-analysis-report-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in body
		assert "Never push to the default" in body
	for job_id, step_id in (("deep-audit", "deep_audit_section_push"), ("api-redundancy", "api_redundancy_section_push")):
		job = wf.split(f"\n  {job_id}:\n", 1)[1].split("\n    steps:\n", 1)[0]
		assert f"section_branch: ${{{{ steps.{step_id}.outputs.section_branch }}}}" in job
		assert f"section_base_sha: ${{{{ steps.{step_id}.outputs.section_base_sha }}}}" in job


def main() -> int:
	test_report_pr_default_mode_opens_and_merges_pr()
	test_report_pr_queued_direct_merge_is_not_reported_merged()
	test_report_pr_auto_merge_drops_purge_deletions()
	test_report_pr_queued_direct_merge_drops_purge_deletions_first()
	test_report_pr_purge_drop_restores_a_doc_git_would_pair_as_a_rename()
	test_report_pr_auto_merge_when_direct_merge_refused()
	test_report_pr_auto_merge_wait_sees_the_report_land()
	test_report_pr_auto_merge_not_landed_warns_about_skipped_audits()
	test_report_pr_renames_when_a_pending_report_pr_holds_the_path()
	test_report_pr_unrecognized_switch_value_warns_and_uses_pr()
	test_report_pr_number_parsed_from_enterprise_url()
	test_report_pr_merge_wait_clamped_to_job_budget()
	test_report_pr_stays_open_when_both_merges_refused()
	test_report_pr_create_failure_fails_the_step()
	test_report_pr_already_exists_looks_up_the_open_pr()
	test_report_pr_disabled_keeps_legacy_direct_push()
	test_report_pr_renames_on_upstream_collision()
	test_report_pr_no_changes_makes_no_gh_calls()
	test_report_pr_rejects_malformed_report_path()
	test_report_pr_missing_pat_fails_before_any_push()
	test_report_pr_ruleset_rejection_fails_the_step()
	test_report_pr_static_contract()
	test_section_push_mode_report_pushed_creates_run_branch_for_delivery()
	test_section_pr_merged_recreates_deleted_run_branch()
	test_section_no_changes_pushes_nothing()
	test_section_steps_static_contract()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
