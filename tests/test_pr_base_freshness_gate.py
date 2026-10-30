#!/usr/bin/env python3
"""Merge-base freshness gate (scripts/pr_checks_lib.sh, operator decision Q35: A).

PR #6741 merged on 2026-10-09 with check-runs from the previous day; #6549
had meanwhile changed the code its new tests exercised, and `main` CI went
red. The gate re-validates a green PR only when the base gained commits that
touch a file the PR also touches: it requests GitHub's update-branch merge
(which fires `synchronize`, so CI and review run on the combined tree) and
tells the caller to skip the merge this round. Every other case (base
unchanged, base commits on other files, gate disabled, API failure) lets the
merge proceed as before.

These tests source the shipped library with stubbed `gh_retry`/`_safe_gh_jq`
transports, the same pattern as tests/test_pr_checks_lib_required_filter.py.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The gate's run: body may live in a review_autofix_step_*.sh script (§24);
# read the workflow with those bodies inlined again.
from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
ENABLE_AUTO_MERGE = REPO_ROOT / "scripts" / "review_enable_auto_merge.sh"
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"

HEAD = "a" * 40


def _compare(ahead_by: int, files: list[str], commits: list[str] | None = None) -> str:
	payload: dict = {"ahead_by": ahead_by, "behind_by": 3, "files": [{"filename": f} for f in files]}
	if commits is not None:
		payload["commits"] = [{"sha": sha} for sha in commits]
	return json.dumps(payload)


def _pr_files(files: list[str]) -> str:
	# Production `--paginate --slurp` shape: an array of pages, each an array.
	return json.dumps([[{"filename": f} for f in files]])


def _run(body: str, *, compare_json: str = "", pr_files_json: str = "{}", pr_json: str = "{}",
		update_rc: int = 0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	preamble = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  printf '%s\\n' "$*" >> "${{CALL_LOG}}"
  case "$*" in
    *"/update-branch"*) return "${{UPDATE_RC}}" ;;
    *"/compare/"*) [ -n "${{COMPARE_JSON}}" ] || return 1; printf '%s' "${{COMPARE_JSON}}" ;;
    *"/files"*) printf '%s' "${{PR_FILES_JSON}}" ;;
    *"/pulls/"*) printf '%s' "${{PR_JSON}}" ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
source {str(LIB)!r}
"""
	call_log = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_base_freshness_calls_{os.getpid()}.log"
	if call_log.exists():
		call_log.unlink()
	full_env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"PR_CHECKS_REPOSITORY": "owner/repo",
		"COMPARE_JSON": compare_json,
		"PR_FILES_JSON": pr_files_json,
		"PR_JSON": pr_json,
		"UPDATE_RC": str(update_rc),
		"CALL_LOG": str(call_log),
		# Keep the post-update head poll to one short read in tests.
		"MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS": "1",
		"MERGE_BASE_SYNC_HEAD_POLL_SECONDS": "1",
	}
	if env:
		full_env.update(env)
	res = subprocess.run(["bash", "-c", preamble + body], env=full_env, capture_output=True, text=True, timeout=60)
	res.calls = call_log.read_text().splitlines() if call_log.exists() else []  # type: ignore[attr-defined]
	return res


def _gate(**kw) -> tuple[int, str, list[str]]:
	args = kw.pop("args", f"7 {HEAD} main")
	body = f'_pr_base_fresh_for_merge {args}; rc=$?; printf "%s\\n" "rc=${{rc}}"'
	res = _run(body, **kw)
	assert res.returncode == 0, res.stderr
	rc = int([line for line in res.stdout.splitlines() if line.startswith("rc=")][-1][3:])
	return rc, res.stdout + res.stderr, res.calls  # type: ignore[attr-defined]


class FreshnessOutcomes(unittest.TestCase):
	def test_base_unchanged_is_fresh_and_merges(self) -> None:
		rc, out, calls = _gate(compare_json=_compare(0, []), pr_files_json=_pr_files(["a.py"]))
		self.assertEqual(rc, 0)
		self.assertIn("outcome=fresh", out)
		self.assertFalse(any("/files" in c for c in calls), "no PR-files read when the base did not move")
		self.assertFalse(any("/update-branch" in c for c in calls))

	def test_base_commits_on_other_files_are_clean_and_merge(self) -> None:
		rc, out, calls = _gate(compare_json=_compare(2, ["docs/x.md", "scripts/other.sh"]),
			pr_files_json=_pr_files(["scripts/mine.sh", "tests/test_mine.py"]))
		self.assertEqual(rc, 0)
		self.assertIn("outcome=clean", out)
		self.assertIn("behind_by=2", out)
		self.assertFalse(any("/update-branch" in c for c in calls))

	def test_overlap_requests_branch_update_and_defers(self) -> None:
		"""The #6741 shape: the base changed a file the PR also changes."""
		rc, out, calls = _gate(compare_json=_compare(5, ["scripts/orchestrate_poll_process.sh", "tests/test_orchestrate_poll_process.py"]),
			pr_files_json=_pr_files(["tests/test_orchestrate_poll_process.py"]))
		self.assertEqual(rc, 1)
		self.assertIn("outcome=overlap", out)
		self.assertIn("overlap=tests/test_orchestrate_poll_process.py", out)
		self.assertIn("MERGE_BASE_SYNC pr=7 head_sha=" + HEAD + " action=update_branch outcome=accepted", out)
		update_calls = [c for c in calls if "/update-branch" in c]
		self.assertEqual(len(update_calls), 1)
		self.assertIn(f"expected_head_sha={HEAD}", update_calls[0])
		self.assertIn("-X PUT", update_calls[0])

	def test_rename_counts_under_both_names(self) -> None:
		compare = json.dumps({"ahead_by": 1, "files": [{"filename": "new/name.py", "previous_filename": "old/name.py"}]})
		rc, out, _ = _gate(compare_json=compare, pr_files_json=_pr_files(["old/name.py"]))
		self.assertEqual(rc, 1)
		self.assertIn("overlap=old/name.py", out)

	def test_failed_branch_update_still_defers(self) -> None:
		rc, out, _ = _gate(compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]), update_rc=1)
		self.assertEqual(rc, 1)
		self.assertIn("action=update_branch outcome=failed", out)

	def test_truncated_base_diff_counts_as_overlap(self) -> None:
		rc, out, calls = _gate(compare_json=_compare(40, [f"f{i}.py" for i in range(300)]), pr_files_json=_pr_files(["zzz.py"]))
		self.assertEqual(rc, 1)
		self.assertIn("reason=base_diff_truncated", out)
		self.assertFalse(any("/files" in c for c in calls), "no PR-files read when the base diff is already too large to prove anything")

	# Security-pass finding merge-freshness-unknown-proceeds: an unverifiable
	# freshness result defers the merge (no branch update is requested).
	def test_api_failure_is_unknown_and_defers(self) -> None:
		rc, out, calls = _gate(compare_json="")
		self.assertEqual(rc, 1)
		self.assertIn("outcome=unknown reason=compare_failed action=defer", out)
		self.assertIn("::warning::", out)
		self.assertFalse(any("/update-branch" in c for c in calls))

	def test_pr_files_failure_is_unknown_and_defers(self) -> None:
		rc, out, calls = _gate(compare_json=_compare(1, ["a.py"]), pr_files_json="{}")
		self.assertEqual(rc, 1)
		self.assertIn("reason=pr_files_failed action=defer", out)
		self.assertFalse(any("/update-branch" in c for c in calls))

	def test_head_and_base_are_resolved_from_the_pr_when_omitted(self) -> None:
		pr_json = json.dumps({"head": {"sha": HEAD}, "base": {"ref": "release/v1"}})
		rc, out, calls = _gate(args='7 "" ""', pr_json=pr_json, compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]))
		self.assertEqual(rc, 1)
		self.assertTrue(any(c.endswith("repos/owner/repo/pulls/7") for c in calls))
		self.assertTrue(any(f"/compare/{HEAD}...release/v1" in c for c in calls))

	def test_unresolvable_head_is_unknown_and_defers(self) -> None:
		rc, out, calls = _gate(args='7 "" ""', pr_json="{}")
		self.assertEqual(rc, 1)
		self.assertIn("reason=unresolved_head_or_base action=defer", out)
		self.assertFalse(any("/compare/" in c for c in calls))
		self.assertFalse(any("/update-branch" in c for c in calls))


class Switch(unittest.TestCase):
	def test_disabled_values_skip_every_read(self) -> None:
		for value in ("false", "0", "off", "no", "FALSE"):
			with self.subTest(value=value):
				rc, out, calls = _gate(compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]),
					env={"MERGE_BASE_FRESHNESS_ENABLED": value})
				self.assertEqual(rc, 0)
				self.assertIn("outcome=disabled", out)
				self.assertEqual(calls, [])

	def test_enabled_spellings(self) -> None:
		for value in ("true", "1", "yes", "on", "TRUE"):
			with self.subTest(value=value):
				rc, out, _ = _gate(compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]),
					env={"MERGE_BASE_FRESHNESS_ENABLED": value})
				self.assertEqual(rc, 1)

	def test_unset_defaults_to_enabled(self) -> None:
		rc, _, _ = _gate(compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]))
		self.assertEqual(rc, 1)


class Wiring(unittest.TestCase):
	"""Every merge path consults the gate before it enables or performs a merge."""

	def test_review_enable_auto_merge_defers_on_overlap(self) -> None:
		text = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
		self.assertIn('source "${SCRIPT_DIR}/pr_checks_lib.sh"', text)
		gate_at = text.index('_pr_base_fresh_for_merge "${PR_NUMBER}" "${INITIAL_HEAD_SHA}" "${_orch_pr_base_ref}"')
		squash_at = text.index('--squash --auto --match-head-commit "${INITIAL_HEAD_SHA}"')
		merge_commit_at = text.index('--merge --auto --match-head-commit "${INITIAL_HEAD_SHA}"')
		self.assertLess(gate_at, merge_commit_at)
		self.assertLess(gate_at, squash_at)
		self.assertIn("action=defer reason=$(base_freshness_defer_reason)", text)
		self.assertIn('echo "base_moved_overlap"', text)
		self.assertIn('echo "base_freshness_unknown"', text)

	def test_review_rb_judge_gates_both_merge_sites(self) -> None:
		text = RB_JUDGE.read_text(encoding="utf-8")
		self.assertEqual(text.count('if ! PR_CHECKS_REPOSITORY="${REPOSITORY}" _pr_base_fresh_for_merge "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" "${PR_BASE_REF:-}"; then'), 2)
		sites = text.split('"${RB_MERGE_METHOD_FLAG:---squash}" --auto --match-head-commit "${RB_JUDGED_HEAD_SHA}"')[:-1]
		self.assertEqual(len(sites), 2)
		for site in sites:
			self.assertIn("_pr_base_fresh_for_merge", site[-1200:])
		# merge_with_followup's synchronous merge is gated too.
		self.assertIn('_pr_base_fresh_for_merge "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" "${PR_BASE_REF}"; then', text)

	def test_poller_gates_its_direct_merge_sites(self) -> None:
		text = POLLER.read_text(encoding="utf-8")
		self.assertIn('_pr_base_fresh_for_merge "${PW_PR}" "${_pw_head_sha}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${merge_pr}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${RB_PR}" "${_rb_merge_sha}" "${_rb_merge_base}"', text)
		# Review-blocked force-merge, no-fix and merge_with_followup paths.
		self.assertIn('_pr_base_fresh_for_merge "${RB_PR}" "${_rb_fm_sha}" "${_rb_fm_base}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${RB_PR}" "${_rb_nofix_sha}" "${_rb_nofix_base}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${RB_PR}" "${_rb_mwf_sha}" "${_rb_mwf_base}"', text)
		self.assertIn("type _pr_base_fresh_for_merge >/dev/null 2>&1", text)

	def test_deterministic_skip_merge_job_gates_both_auto_merge_calls(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		job = text.split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertIn("path: .codex-freshness-src", job)
		self.assertIn("scripts/pr_checks_lib.sh", job)
		self.assertIn('[ "$(git -C .codex-freshness-src rev-parse HEAD 2>/dev/null)" = "${REVIEW_SUPPORT_SHA:-}" ]', job)
		self.assertEqual(job.count('if ! _pr_base_fresh_for_merge "${PR_NUMBER}" "${PR_HEAD_SHA}" ""; then'), 2)
		self.assertIn("MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}", job)

	def test_codex_agent_auto_merge_step_passes_the_variable(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		step = text.split("- name: Enable auto-merge on PR", 1)[1].split("- name: ", 1)[0]
		self.assertIn("MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}", step)


class FreshnessTruncatedPrFiles(unittest.TestCase):
	def test_capped_pr_file_list_counts_as_overlap(self) -> None:
		rc, out, _ = _gate(compare_json=_compare(1, ["base_only.py"]), pr_files_json=_pr_files([f"p{i}.py" for i in range(3000)]))
		self.assertEqual(rc, 1)
		self.assertIn("reason=pr_files_truncated", out)


# ---------------------------------------------------------------------------
# Required-checks wait before `gh pr merge --auto` (same library).
# ---------------------------------------------------------------------------

def _runs(*entries: tuple[str, str, str]) -> str:
	"""Production --paginate --slurp shape: an array of page objects."""
	return json.dumps([{"check_runs": [
		{"name": name, "status": status, "conclusion": conclusion or None, "details_url": "https://github.com/o/r/actions/runs/999/job/1"}
		for name, status, conclusion in entries
	]}])


def _wait(runs_sequence: list[str], *, max_minutes: str = "1", poll: str = "1", env: dict[str, str] | None = None,
		ci_runs_sequence: list[str] | None = None, base: str = "main") -> tuple[int, str]:
	"""Each check-runs read returns the next fixture; the last one repeats.

	ci_runs_sequence does the same for the actions/runs?head_sha= read.
	"""
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	seq_file = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_checks_wait_seq_{os.getpid()}.json"
	seq_file.write_text(json.dumps(runs_sequence), encoding="utf-8")
	counter = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_checks_wait_n_{os.getpid()}"
	counter.write_text("0", encoding="utf-8")
	ci_seq_file = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_checks_wait_ci_seq_{os.getpid()}.json"
	ci_seq_file.write_text(json.dumps(ci_runs_sequence or [json.dumps({"workflow_runs": []})]), encoding="utf-8")
	ci_counter = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_checks_wait_ci_n_{os.getpid()}"
	ci_counter.write_text("0", encoding="utf-8")
	preamble = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  case "$*" in
    *"/protection"*) printf '%s' '' ;;
    *"/check-runs"*)
      n="$(cat "{counter}")"; printf '%s' "$((n + 1))" > "{counter}"
      jq -r --argjson n "${{n}}" '.[ ([$n, (length - 1)] | min) ]' "{seq_file}" ;;
    *"/actions/runs?head_sha="*)
      n="$(cat "{ci_counter}")"; printf '%s' "$((n + 1))" > "{ci_counter}"
      jq -r --argjson n "${{n}}" '.[ ([$n, (length - 1)] | min) ]' "{ci_seq_file}" ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
source {str(LIB)!r}
_pr_wait_for_required_checks 7 {HEAD} {shlex.quote(base)} {max_minutes}; rc=$?
printf 'rc=%s outcome=%s\\n' "${{rc}}" "${{PR_CHECKS_WAIT_OUTCOME}}"
"""
	full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PR_CHECKS_REPOSITORY": "owner/repo",
		"AUTO_MERGE_CHECKS_POLL_SECONDS": poll, "GITHUB_RUN_ID": "4242", "PYTHONDONTWRITEBYTECODE": "1"}
	full_env.update(env or {})
	res = subprocess.run(["bash", "-c", preamble], env=full_env, capture_output=True, text=True, timeout=120)
	assert res.returncode == 0, res.stderr
	last = [line for line in res.stdout.splitlines() if line.startswith("rc=")][-1]
	return int(last.split()[0][3:]), res.stdout


def _ci_runs(*entries: tuple) -> str:
	"""actions/runs?head_sha= shape: workflow name, status, run id[, conclusion]."""
	return json.dumps({"workflow_runs": [
		{"name": e[0], "status": e[1], "id": e[2], "conclusion": e[3] if len(e) > 3 else None} for e in entries
	]})


GREEN = _runs(("CI", "completed", "success"), ("lint", "completed", "success"), ("review / gate", "completed", "success"))
RED = _runs(("CI", "completed", "failure"), ("lint", "completed", "success"), ("review / gate", "completed", "success"))
RUNNING = _runs(("CI", "in_progress", ""), ("lint", "completed", "success"), ("review / gate", "completed", "success"))
EMPTY = json.dumps([{"check_runs": []}])
RUNNING_SELF_ONLY = json.dumps([{"check_runs": [
	{"name": "CI", "status": "completed", "conclusion": "success", "details_url": "https://github.com/o/r/actions/runs/999/job/1"},
	{"name": "review / codex-agent", "status": "in_progress", "conclusion": None, "details_url": "https://github.com/o/r/actions/runs/4242/job/7"},
]}])


class RequiredChecksWait(unittest.TestCase):
	def test_green_required_set_returns_immediately(self) -> None:
		rc, out = _wait([GREEN])
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok waited_s=0", out)

	def test_settled_failure_refuses_without_waiting(self) -> None:
		rc, out = _wait([RED])
		self.assertEqual(rc, 1)
		self.assertIn("outcome=failed waited_s=0 pending=0", out)

	def test_pending_then_green_waits_and_proceeds(self) -> None:
		rc, out = _wait([RUNNING, RUNNING, GREEN])
		self.assertEqual(rc, 0)
		self.assertIn("still running; waiting 1s", out)
		self.assertIn("outcome=ok waited_s=2", out)

	def test_pending_then_failure_refuses(self) -> None:
		rc, out = _wait([RUNNING, RED])
		self.assertEqual(rc, 1)
		self.assertIn("outcome=failed waited_s=1", out)

	def test_budget_exhausted_times_out(self) -> None:
		rc, out = _wait([RUNNING], max_minutes="0")
		self.assertEqual(rc, 1)
		self.assertIn("outcome=timeout waited_s=0 pending=1", out)

	def test_own_run_never_counts_as_pending(self) -> None:
		rc, out = _wait([RUNNING_SELF_ONLY])
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok", out)

	def test_query_failure_refuses_once_the_budget_is_spent(self) -> None:
		rc, out = _wait(["{}"], max_minutes="0")
		self.assertEqual(rc, 1)
		self.assertIn("outcome=query_failed", out)

	def test_transient_query_failure_is_retried(self) -> None:
		rc, out = _wait(["{}", GREEN])
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok waited_s=1", out)

	def test_no_registered_check_runs_waits_for_ci_to_register(self) -> None:
		"""A fresh push with no check-runs yet must not read as green."""
		rc, out = _wait([EMPTY, RUNNING, RED])
		self.assertEqual(rc, 1)
		self.assertIn("no check-runs registered", out)
		self.assertIn("outcome=failed waited_s=2", out)

	def test_no_check_runs_at_all_proceeds_after_the_grace(self) -> None:
		rc, out = _wait([EMPTY])
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok waited_s=2", out)

	def test_queued_ci_run_keeps_waiting_until_it_completes(self) -> None:
		"""A fast check already passed but CI's run is still queued: do not read the head as green."""
		queued = _ci_runs(("CI", "queued", 101))
		done = _ci_runs(("CI", "completed", 101))
		rc, out = _wait([GREEN], ci_runs_sequence=[queued, queued, done])
		self.assertEqual(rc, 0, out)
		self.assertIn("1 CI workflow run(s) on", out)
		self.assertIn("outcome=ok", out)

	def test_ci_run_that_never_completes_times_out(self) -> None:
		rc, out = _wait([GREEN], max_minutes="0", ci_runs_sequence=[_ci_runs(("CI", "in_progress", 101))])
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=timeout", out)
		self.assertIn("reason=ci_run_pending", out)

	def test_unlisted_workflows_and_the_own_run_do_not_wait(self) -> None:
		runs = _ci_runs(("Security audit", "in_progress", 101), ("CI", "in_progress", 4242))
		rc, out = _wait([GREEN], ci_runs_sequence=[runs])
		self.assertEqual(rc, 0, out)
		self.assertNotIn("CI workflow run(s)", out)

	def test_wait_workflows_can_be_disabled_or_renamed(self) -> None:
		runs = _ci_runs(("Build", "queued", 101))
		rc, out = _wait([GREEN], ci_runs_sequence=[runs], env={"AUTO_MERGE_WAIT_WORKFLOWS": "none"})
		self.assertEqual(rc, 0, out)
		rc, out = _wait([GREEN], max_minutes="0", ci_runs_sequence=[runs], env={"AUTO_MERGE_WAIT_WORKFLOWS": "Build, CI"})
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=ci_run_pending", out)

	def test_wait_workflow_names_match_case_insensitively(self) -> None:
		runs = _ci_runs(("CI", "queued", 101))
		rc, out = _wait([GREEN], max_minutes="0", ci_runs_sequence=[runs], env={"AUTO_MERGE_WAIT_WORKFLOWS": "ci"})
		self.assertEqual(rc, 1, out)
		self.assertIn("reason=ci_run_pending", out)

	def test_ci_run_cancelled_before_its_jobs_refuses(self) -> None:
		"""A queued CI run cancelled before any job started leaves no check-runs: not green."""
		runs = _ci_runs(("CI", "completed", 101, "cancelled"))
		rc, out = _wait([GREEN], ci_runs_sequence=[runs])
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=failed", out)
		self.assertIn("reason=ci_run_cancelled", out)

	def test_cancelled_run_superseded_by_a_finished_run_proceeds(self) -> None:
		runs = _ci_runs(("CI", "completed", 101, "cancelled"), ("CI", "completed", 102, "success"))
		rc, out = _wait([GREEN], ci_runs_sequence=[runs])
		self.assertEqual(rc, 0, out)

	def test_failed_runs_read_falls_back_to_the_check_runs(self) -> None:
		rc, out = _wait([GREEN], ci_runs_sequence=["not json"])
		self.assertEqual(rc, 0, out)

	def test_allow_all_sentinel_proceeds(self) -> None:
		rc, out = _wait([RED], env={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""})
		self.assertEqual(rc, 0)
		self.assertIn("outcome=allow_all", out)


THIS_REPO = {"PR_CHECKS_REPOSITORY": "shubhodeep1/coding-workflows"}
NO_LINT = _runs(("CI", "completed", "success"), ("review / gate", "completed", "success"))
LINT_RUNNING = _runs(("CI", "completed", "success"), ("lint", "in_progress", ""), ("review / gate", "completed", "success"))
CI_DONE = [json.dumps({"workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "id": 999}]})]
LINT_SELF_ONLY = json.dumps([{"check_runs": [
	{"name": "CI", "status": "completed", "conclusion": "success", "details_url": "https://github.com/o/r/actions/runs/999/job/1"},
	{"name": "lint", "status": "completed", "conclusion": "success", "details_url": "https://github.com/o/r/actions/runs/4242/job/7"},
]}])


class RequiredCiCheckWait(unittest.TestCase):
	"""Issue #6963: the configured CI check must exist on the head and succeed."""

	def test_empty_listing_never_authorizes_when_ci_check_is_required(self) -> None:
		rc, out = _wait([EMPTY], max_minutes="0", env=THIS_REPO, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1)
		self.assertIn("outcome=timeout", out)
		self.assertIn("ci_check=lint ci_state=absent", out)
		self.assertNotIn("outcome=ok", out)

	def test_missing_ci_check_is_waited_for_even_when_other_checks_are_green(self) -> None:
		rc, out = _wait([NO_LINT, GREEN], env=THIS_REPO, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 0)
		self.assertIn("required CI check 'lint' is absent", out)
		self.assertIn("outcome=ok waited_s=1", out)

	def test_old_review_gate_success_alone_does_not_authorize(self) -> None:
		only_gate = _runs(("review / gate", "completed", "success"))
		rc, out = _wait([only_gate], max_minutes="0", env=THIS_REPO, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1)
		self.assertIn("ci_state=absent", out)

	def test_pending_then_success_proceeds(self) -> None:
		rc, out = _wait([LINT_RUNNING, GREEN], env=THIS_REPO, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok waited_s=1", out)
		self.assertIn("ci_check=lint ci_state=success", out)

	def test_unsuccessful_ci_conclusions_refuse(self) -> None:
		for conclusion in ("failure", "skipped", "neutral", "cancelled"):
			with self.subTest(conclusion=conclusion):
				runs = _runs(("CI", "completed", "success"), ("lint", "completed", conclusion))
				rc, out = _wait([runs], env=THIS_REPO, ci_runs_sequence=CI_DONE)
				self.assertEqual(rc, 1)
				self.assertIn("ci_check=lint ci_state=failure", out)
				self.assertIn("outcome=failed", out)

	def test_a_failing_duplicate_outranks_a_success(self) -> None:
		runs = _runs(("lint", "completed", "success"), ("lint", "completed", "failure"))
		rc, out = _wait([runs], env={**THIS_REPO, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "CI"}, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1)
		self.assertIn("outcome=failed", out)

	def test_own_run_never_satisfies_the_ci_check(self) -> None:
		rc, out = _wait([LINT_SELF_ONLY], max_minutes="0", env=THIS_REPO, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1)
		self.assertIn("ci_state=absent", out)

	def test_allow_all_still_waits_for_the_ci_check(self) -> None:
		rc, out = _wait([NO_LINT], max_minutes="0", env={**THIS_REPO, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""}, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1)
		self.assertIn("outcome=timeout", out)

	def test_none_restores_the_legacy_grace(self) -> None:
		rc, out = _wait([EMPTY], env={**THIS_REPO, "AUTO_MERGE_REQUIRED_CI_CHECK": "none"})
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok waited_s=2", out)
		self.assertIn("ci_check=none ci_state=disabled", out)

	def test_consumer_default_keeps_the_legacy_behaviour(self) -> None:
		rc, out = _wait([NO_LINT])
		self.assertEqual(rc, 0)
		self.assertIn("outcome=ok waited_s=0 pending=0 ci_check=none", out)

	def test_custom_check_name_with_spaces(self) -> None:
		rc, out = _wait([NO_LINT], env={"AUTO_MERGE_REQUIRED_CI_CHECK": "review / gate"}, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 0)
		self.assertIn("ci_state=success", out)

	def test_same_named_check_from_another_workflow_does_not_count(self) -> None:
		"""lint-pr-body-auto-close.yml's job is also named `lint`; only a CI run's `lint` counts."""
		other = json.dumps([{"check_runs": [
			{"name": "lint", "status": "completed", "conclusion": "success", "details_url": "https://github.com/o/r/actions/runs/555/job/1"},
		]}])
		rc, out = _wait([other], max_minutes="0", env=THIS_REPO, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1, out)
		self.assertIn("ci_state=absent", out)
		rc, out = _wait([other], max_minutes="0", env={**THIS_REPO, "ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""}, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1, out)
		self.assertIn("ci_state=absent", out)

	def test_routed_fix_pr_waits_for_its_own_ci_lint(self) -> None:
		"""Issue #7283: a fix PR routed to a failing PR's head branch (base
		ai/issue-<n>) gets its own CI run, and its merge waits for `lint`."""
		routed = "ai/issue-6564"
		rc, out = _wait([EMPTY], max_minutes="0", env=THIS_REPO, ci_runs_sequence=CI_DONE, base=routed)
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=timeout", out)
		self.assertIn("ci_check=lint ci_state=absent", out)
		rc, out = _wait([LINT_RUNNING, GREEN], env=THIS_REPO, ci_runs_sequence=CI_DONE, base=routed)
		self.assertEqual(rc, 0, out)
		self.assertIn("ci_check=lint ci_state=success", out)
		failed = _runs(("CI", "completed", "success"), ("lint", "completed", "failure"))
		rc, out = _wait([failed], env=THIS_REPO, ci_runs_sequence=CI_DONE, base=routed)
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=failed", out)

	def test_unreadable_runs_listing_never_trusts_a_success(self) -> None:
		rc, out = _wait([GREEN], max_minutes="0", env=THIS_REPO, ci_runs_sequence=["not json"])
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=timeout", out)
		self.assertIn("ci_state=unknown", out)

	def test_invalid_name_in_a_consumer_never_authorizes(self) -> None:
		rc, out = _wait([GREEN], max_minutes="0", env={"AUTO_MERGE_REQUIRED_CI_CHECK": "lint,test"}, ci_runs_sequence=CI_DONE)
		self.assertEqual(rc, 1, out)
		self.assertIn("outcome=timeout", out)
		self.assertIn("ci_check=__invalid__ ci_state=unknown", out)

	def test_wait_workflows_none_matches_by_name_only(self) -> None:
		rc, out = _wait([GREEN], env={**THIS_REPO, "AUTO_MERGE_WAIT_WORKFLOWS": "none"})
		self.assertEqual(rc, 0, out)
		self.assertIn("ci_state=success", out)


class RequiredChecksWiring(unittest.TestCase):
	def test_required_ci_check_variable_reaches_every_auto_merge_path(self) -> None:
		line = "AUTO_MERGE_REQUIRED_CI_CHECK: ${{ vars.AUTO_MERGE_REQUIRED_CI_CHECK || '' }}"
		text = WORKFLOW.read_text(encoding="utf-8")
		job = text.split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertIn(line, job)
		step = text.split("- name: Enable auto-merge on PR", 1)[1].split("- name: ", 1)[0]
		self.assertIn(line, step)
		judge = text.split("JUDGE_REASONING_EFFORT: ${{ vars.THINKING_LEVEL_REVIEW_BLOCKED_JUDGE", 1)[1][:1500]
		self.assertIn(line, judge)

	def test_deterministic_skip_merge_passes_the_pr_base_to_the_wait(self) -> None:
		"""The `lint` default is scoped by base ref; an empty base would require it on bases CI never runs on."""
		text = WORKFLOW.read_text(encoding="utf-8")
		job = text.split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertIn("PR_BASE_REF: ${{ needs.gate.outputs.retargeted_base_ref }}", job)
		self.assertEqual(job.count('_pr_wait_for_required_checks "${PR_NUMBER}" "${PR_HEAD_SHA}" "${PR_BASE_REF:-}"'), 2)
		self.assertNotIn('_pr_wait_for_required_checks "${PR_NUMBER}" "${PR_HEAD_SHA}" ""', job)

	def test_review_enable_auto_merge_waits_before_both_tails(self) -> None:
		text = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
		wait_at = text.index('_pr_wait_for_required_checks "${PR_NUMBER}" "${INITIAL_HEAD_SHA}" "${_orch_pr_base_ref}"')
		self.assertLess(wait_at, text.index('--merge --auto --match-head-commit "${INITIAL_HEAD_SHA}"'))
		self.assertLess(wait_at, text.index('--squash --auto --match-head-commit "${INITIAL_HEAD_SHA}"'))
		self.assertIn("reason=required_checks_${PR_CHECKS_WAIT_OUTCOME:-unknown}", text)

	def test_deterministic_skip_merge_waits_before_both_auto_merge_calls(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		job = text.split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertEqual(job.count('elif ! _pr_wait_for_required_checks "${PR_NUMBER}" "${PR_HEAD_SHA}" "${PR_BASE_REF:-}"; then'), 2)
		self.assertIn("AUTO_MERGE_CHECKS_WAIT_MINUTES: ${{ vars.AUTO_MERGE_CHECKS_WAIT_MINUTES || '45' }}", job)
		self.assertIn("AUTO_MERGE_WAIT_WORKFLOWS: ${{ vars.AUTO_MERGE_WAIT_WORKFLOWS || 'CI' }}", job)

	def test_codex_agent_auto_merge_step_passes_the_wait_variables(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		step = text.split("- name: Enable auto-merge on PR", 1)[1].split("- name: ", 1)[0]
		self.assertIn("AUTO_MERGE_CHECKS_WAIT_MINUTES: ${{ vars.AUTO_MERGE_CHECKS_WAIT_MINUTES || '45' }}", step)
		self.assertIn("AUTO_MERGE_CHECKS_POLL_SECONDS: ${{ vars.AUTO_MERGE_CHECKS_POLL_SECONDS || '60' }}", step)
		self.assertIn("AUTO_MERGE_WAIT_WORKFLOWS: ${{ vars.AUTO_MERGE_WAIT_WORKFLOWS || 'CI' }}", step)

	def test_freshness_is_rechecked_after_a_wait_on_every_auto_merge_path(self) -> None:
		job = WORKFLOW.read_text(encoding="utf-8").split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertEqual(job.count('elif [ "${PR_CHECKS_WAIT_WAITED_S:-0}" -gt 0 ] 2>/dev/null && ! _pr_base_fresh_for_merge "${PR_NUMBER}" "${PR_HEAD_SHA}" ""; then'), 2)
		judge = RB_JUDGE.read_text(encoding="utf-8")
		self.assertEqual(judge.count('elif [ "${PR_CHECKS_WAIT_WAITED_S:-0}" -gt 0 ] 2>/dev/null \\\n          && ! PR_CHECKS_REPOSITORY="${REPOSITORY}" _pr_base_fresh_for_merge'), 2)

	def test_review_blocked_judge_step_passes_the_wait_variables(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		step = text.split("JUDGE_REASONING_EFFORT: ${{ vars.THINKING_LEVEL_REVIEW_BLOCKED_JUDGE", 1)[1][:1500]
		self.assertIn("AUTO_MERGE_CHECKS_WAIT_MINUTES: ${{ vars.AUTO_MERGE_CHECKS_WAIT_MINUTES || '45' }}", step)
		self.assertIn("AUTO_MERGE_CHECKS_POLL_SECONDS: ${{ vars.AUTO_MERGE_CHECKS_POLL_SECONDS || '60' }}", step)
		self.assertIn("AUTO_MERGE_WAIT_WORKFLOWS: ${{ vars.AUTO_MERGE_WAIT_WORKFLOWS || 'CI' }}", step)

	def test_review_rb_judge_waits_before_both_auto_merge_calls(self) -> None:
		text = RB_JUDGE.read_text(encoding="utf-8")
		self.assertEqual(text.count('elif ! PR_CHECKS_REPOSITORY="${REPOSITORY}" _pr_wait_for_required_checks "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" "${PR_BASE_REF:-}"; then'), 2)

	def test_missing_library_fails_the_wait_closed(self) -> None:
		for path in (ENABLE_AUTO_MERGE, RB_JUDGE, WORKFLOW):
			with self.subTest(path=path.name):
				text = path.read_text(encoding="utf-8")
				stub = text.split("_pr_wait_for_required_checks()", 1)[1][:400]
				self.assertIn("return 1", stub)

	def test_review_enable_auto_merge_rechecks_freshness_after_a_wait(self) -> None:
		text = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
		wait_at = text.index('_pr_wait_for_required_checks "${PR_NUMBER}" "${INITIAL_HEAD_SHA}" "${_orch_pr_base_ref}"')
		recheck_at = text.index('[ "${PR_CHECKS_WAIT_WAITED_S:-0}" -gt 0 ]')
		self.assertLess(wait_at, recheck_at)
		self.assertLess(recheck_at, text.index('--squash --auto --match-head-commit "${INITIAL_HEAD_SHA}"'))


# ---------------------------------------------------------------------------
# Head-bound poller merges (security-pass finding
# poller-merge-unbound-to-checked-head).
# ---------------------------------------------------------------------------

def _poller_function(name: str) -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index(f"\n{name}()\n") + 1
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def _merge_bound(args: str, *, auto_rc: int = 0, sync_rc: int = 0) -> tuple[int, str, list[str]]:
	log = Path(os.environ.get("TMPDIR", "/tmp")) / f"orch_merge_bound_{os.getpid()}.log"
	if log.exists():
		log.unlink()
	script = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
gh() {{
  printf '%s\\n' "$*" >> "${{CALL_LOG}}"
  case "$*" in
    *" --auto "*) return "${{AUTO_RC}}" ;;
    *) return "${{SYNC_RC}}" ;;
  esac
}}
{_poller_function("_orch_squash_merge_bound")}
_orch_squash_merge_bound {args}; rc=$?
printf 'rc=%s outcome=%s\\n' "${{rc}}" "${{ORCH_MERGE_BOUND_OUTCOME}}"
"""
	env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GITHUB_REPOSITORY": "owner/repo", "CALL_LOG": str(log),
		"AUTO_RC": str(auto_rc), "SYNC_RC": str(sync_rc)}
	res = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
	assert res.returncode == 0, res.stderr
	rc = int([line for line in res.stdout.splitlines() if line.startswith("rc=")][-1].split()[0][3:])
	calls = log.read_text().splitlines() if log.exists() else []
	return rc, res.stdout, calls


class HeadBoundPollerMerge(unittest.TestCase):
	def test_unresolved_head_refuses_without_calling_gh(self) -> None:
		for args in ("7 ''", "7 abc123", "7 " + "A" * 40, "7 " + "a" * 39):
			with self.subTest(args=args):
				rc, out, calls = _merge_bound(args)
				self.assertEqual(rc, 2)
				self.assertIn("outcome=refused reason=unresolved_head_sha", out)
				self.assertEqual(calls, [])

	def test_auto_mode_binds_both_attempts(self) -> None:
		rc, out, calls = _merge_bound(f"7 {HEAD} auto", auto_rc=1, sync_rc=0)
		self.assertEqual(rc, 0)
		self.assertIn("outcome=merged", out)
		self.assertEqual(len(calls), 2)
		for call in calls:
			self.assertIn(f"--match-head-commit {HEAD}", call)
		self.assertIn("--auto", calls[0])
		self.assertNotIn("--auto", calls[1])

	def test_auto_mode_enabled_skips_direct_merge(self) -> None:
		rc, out, calls = _merge_bound(f"7 {HEAD}")
		self.assertEqual(rc, 0)
		self.assertIn("outcome=enabled", out)
		self.assertEqual(len(calls), 1)

	def test_sync_mode_and_total_failure(self) -> None:
		rc, _, calls = _merge_bound(f"7 {HEAD} sync")
		self.assertEqual(rc, 0)
		self.assertEqual(len(calls), 1)
		self.assertNotIn("--auto", calls[0])
		rc, out, calls = _merge_bound(f"7 {HEAD}", auto_rc=1, sync_rc=1)
		self.assertEqual(rc, 1)
		self.assertIn("outcome=failed", out)
		self.assertEqual(len(calls), 2)

	def test_every_poller_merge_is_head_bound(self) -> None:
		lines = POLLER.read_text(encoding="utf-8").splitlines()
		offenders = []
		for number, line in enumerate(lines, 1):
			code = line.strip()
			if code.startswith("#") or "gh pr merge" not in code or "--disable-auto" in code:
				continue
			# _noop_gate_failure_alert only posts a comment and a Telegram
			# message; its text can name the merge command (#7156).
			if code.startswith(("echo", "tg_send_msg", "tg_notify", "_noop_gate_failure_alert")) or code.lstrip("\"'").startswith("echo"):
				continue
			if "--match-head-commit" in code or "_rb_mwf_match_arg" in code:
				continue
			offenders.append(f"{number}: {code}")
		self.assertEqual(offenders, [])

	def test_final_merge_checks_and_merge_share_the_head(self) -> None:
		text = POLLER.read_text(encoding="utf-8")
		self.assertIn('_pr_checks_completed "${final_pr}" "${pr_head_sha}" "${default_branch}"', text)
		self.assertNotIn('_pr_checks_completed "${final_pr}" "" "${default_branch}"', text)
		self.assertIn('--squash --delete-branch --match-head-commit "${pr_head_sha}"', text)
		for site in ('_orch_squash_merge_bound "${merge_pr}" "${merge_head_sha}" auto',
				'_orch_squash_merge_bound "${PW_PR}" "${_pw_head_sha}" auto',
				'_orch_squash_merge_bound "${RTM_PR}" "${_rtm_head_sha}" auto',
				'_orch_squash_merge_bound "${RB_PR}" "${_rb_merge_sha}" auto',
				'_orch_squash_merge_bound "${RB_PR}" "${_rb_fm_sha}" auto',
				'_orch_squash_merge_bound "${RB_PR}" "${_rb_nofix_sha}" auto',
				):
			self.assertIn(site, text)
		self.assertIn('--squash --auto --match-head-commit "${N_HEAD_SHA}"', text)
		self.assertIn('_pr_checks_completed "${merge_pr}" "${merge_head_sha}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${merge_pr}" "${merge_head_sha}" "${merge_base_ref}"', text)


# --- Update markers and the per-PR update cap (#7084, Q60: A) ----------------
# #6654 was updated from main four times in two hours: every update restarted
# the full review cycle, and main moved again before it finished.

SYNC_LOGIN = "workflow-bot"
BASE_TIP = "d" * 40
UPDATE_SHA = "e" * 40


def _sync_marker(sync: int, *, pr: int = 7, author: str = SYNC_LOGIN, bound: bool = False) -> dict:
	fields = f" base={BASE_TIP} merge={UPDATE_SHA}" if bound else ""
	return {"user": {"login": author}, "body": f"**Branch updated from `main` before merging** (update {sync} of at most 2).\n\n<!-- ai:merge-base-sync:v1 pr={pr} from={'c' * 40} sync={sync}{fields} -->"}


def _update_commit(parents: list[str] | None = None, *, verified: bool = True, committer: str = "web-flow") -> dict:
	return {"parents": [{"sha": s} for s in (parents if parents is not None else [HEAD, BASE_TIP])],
		"committer": {"login": committer}, "commit": {"verification": {"verified": verified}}}


def _sync_gate(comments: list[dict] | None, *, max_syncs: str | None = None, user_ok: bool = True,
		new_head: str | None = UPDATE_SHA, head_changes_at: int = 1, commit: dict | None = None,
		compare_commits: list[str] | None = None, base_status: str = "", attempts: str = "1") -> tuple[int, str, list[str]]:
	"""Overlap case with the user, comments, PR-head poll and commit reads answered from fixtures."""
	comments_json = json.dumps([comments]) if comments is not None else ""
	user_json = json.dumps({"login": SYNC_LOGIN}) if user_ok else ""
	commit_json = json.dumps(commit if commit is not None else _update_commit())
	new_head_json = json.dumps({"head": {"sha": new_head or HEAD}})
	old_head_json = json.dumps({"head": {"sha": HEAD}})
	body = f'''
sleep() {{ :; }}
rm -f "${{CALL_LOG}}.poll"
eval "base_$(declare -f _safe_gh_jq)"
_safe_gh_jq() {{
  case "$*" in
    user) printf '%s\\n' "$*" >> "${{CALL_LOG}}"; [ -n {user_json!r} ] || return 1; printf '%s' {user_json!r} ;;
    *"/issues/7/comments?per_page=100"*) printf '%s\\n' "$*" >> "${{CALL_LOG}}"; [ -n {comments_json!r} ] || return 1; printf '%s' {comments_json!r} ;;
    *"repos/owner/repo/pulls/7")
      printf '%s\\n' "$*" >> "${{CALL_LOG}}"
      # A file counter: each call runs in a command-substitution subshell.
      poll_n=$(( $(cat "${{CALL_LOG}}.poll" 2>/dev/null || echo 0) + 1 ))
      printf '%s' "${{poll_n}}" > "${{CALL_LOG}}.poll"
      if [ "${{poll_n}}" -ge {head_changes_at} ]; then printf '%s' {new_head_json!r}; else printf '%s' {old_head_json!r}; fi ;;
    *"repos/owner/repo/commits/"*) printf '%s\\n' "$*" >> "${{CALL_LOG}}"; printf '%s' {commit_json!r} ;;
    *"/compare/{BASE_TIP}...main") printf '%s\\n' "$*" >> "${{CALL_LOG}}"; printf '{{"status":"%s"}}' {base_status!r} ;;
    *) base__safe_gh_jq "$@" ;;
  esac
}}
_pr_base_fresh_for_merge 7 {HEAD} main; rc=$?; printf "%s\\n" "rc=${{rc}}"
'''
	env = {"MERGE_BASE_SYNC_HEAD_POLL_ATTEMPTS": attempts}
	if max_syncs is not None:
		env["MERGE_BASE_FRESHNESS_MAX_SYNCS"] = max_syncs
	commits = compare_commits if compare_commits is not None else ["1" * 40, BASE_TIP]
	res = _run(body, compare_json=_compare(len(commits) if compare_commits != [] else 2, ["agents.md"], commits or None),
		pr_files_json=_pr_files(["agents.md"]), env=env)
	assert res.returncode == 0, res.stderr
	rc = int([line for line in res.stdout.splitlines() if line.startswith("rc=")][-1][3:])
	return rc, res.stdout + res.stderr, res.calls  # type: ignore[attr-defined]


def _posted(calls: list[str]) -> list[str]:
	return [c for c in calls if c.startswith("-X POST repos/owner/repo/issues/7/comments")]


class FreshnessSyncCap(unittest.TestCase):
	def test_first_update_posts_a_marker_bound_to_the_update_commit(self) -> None:
		rc, out, calls = _sync_gate([])
		self.assertEqual(rc, 1)
		self.assertIn("update 1, warning threshold 2", out)
		# The comment body spans several log lines; match on the whole log.
		log = "\n".join(calls)
		self.assertEqual(len(_posted(calls)), 1)
		self.assertIn(f"<!-- ai:merge-base-sync:v1 pr=7 from={HEAD} sync=1 base={BASE_TIP} merge={UPDATE_SHA} -->", log)
		self.assertIn("**Branch updated from `main` before merging** (update 1; warning threshold 2)", log)
		self.assertIn(f"MERGE_BASE_SYNC_MARKER pr=7 head_sha={HEAD} sync=1 merge={UPDATE_SHA} outcome=posted", out)

	def test_the_head_poll_waits_for_the_update_commit(self) -> None:
		rc, out, calls = _sync_gate([], head_changes_at=3, attempts="5")
		self.assertEqual(rc, 1)
		self.assertEqual(len([c for c in calls if c.endswith("repos/owner/repo/pulls/7")]), 3)
		self.assertEqual(len(_posted(calls)), 1)

	def test_second_update_counts_only_the_workflow_logins_markers_for_this_pr(self) -> None:
		rc, out, calls = _sync_gate([_sync_marker(1), _sync_marker(1, author="someone-else"), _sync_marker(1, pr=8)])
		self.assertEqual(rc, 1)
		self.assertIn(f"<!-- ai:merge-base-sync:v1 pr=7 from={HEAD} sync=2 base={BASE_TIP} merge={UPDATE_SHA} -->", "\n".join(calls))

	def test_after_the_cap_remaining_overlap_still_updates_and_never_merges(self) -> None:
		# Security-pass finding merge-base-sync-cap-proceeds-on-overlap.
		rc, out, calls = _sync_gate([_sync_marker(1), _sync_marker(2, bound=True)])
		self.assertEqual(rc, 1)
		self.assertIn("outcome=overlap action=update reason=max_syncs_exceeded syncs=2 max=2", out)
		self.assertNotIn("action=proceed", out)
		self.assertEqual(len([c for c in calls if "/update-branch" in c]), 1)
		self.assertIn(f"sync=3 base={BASE_TIP} merge={UPDATE_SHA}", "\n".join(calls))

	def test_the_threshold_is_configurable_and_validated(self) -> None:
		rc, out, _ = _sync_gate([_sync_marker(1)], max_syncs="1")
		self.assertEqual(rc, 1)
		self.assertIn("reason=max_syncs_exceeded syncs=1 max=1", out)
		rc, out, _calls = _sync_gate([_sync_marker(1)], max_syncs="nine")
		self.assertEqual(rc, 1)
		self.assertNotIn("max_syncs_exceeded", out, "an invalid threshold falls back to 2")

	def test_threshold_zero_warns_from_the_first_update_and_reads_no_comments(self) -> None:
		rc, out, calls = _sync_gate(None, max_syncs="0", user_ok=False)
		self.assertEqual(rc, 1)
		self.assertIn("reason=max_syncs_exceeded syncs=0 max=0", out)
		self.assertFalse(any(c == "user" or "/comments?per_page" in c for c in calls))
		self.assertEqual(len([c for c in calls if "/update-branch" in c]), 1)

	def test_an_unreadable_count_updates_as_before(self) -> None:
		rc, out, calls = _sync_gate(None, user_ok=False)
		self.assertEqual(rc, 1)
		self.assertEqual(len([c for c in calls if "/update-branch" in c]), 1)
		self.assertIn("update 1, warning threshold 2", out)

	def test_unobserved_or_unverified_update_commits_get_no_marker(self) -> None:
		cases = (
			({"new_head": None}, "head_not_observed"),
			({"commit": _update_commit(["f" * 40, BASE_TIP])}, "parents_mismatch"),
			({"commit": _update_commit([HEAD])}, "parents_mismatch"),
			({"commit": _update_commit(verified=False)}, "not_github_merge"),
			({"commit": _update_commit(committer="someone")}, "not_github_merge"),
			({"commit": _update_commit([HEAD, "2" * 40])}, "base_mismatch"),
		)
		for kwargs, reason in cases:
			with self.subTest(reason=reason, kwargs=kwargs):
				rc, out, calls = _sync_gate([], **kwargs)
				self.assertEqual(rc, 1)
				self.assertIn(f"MERGE_BASE_SYNC_MARKER pr=7 head_sha={HEAD} outcome=skipped reason={reason}", out)
				self.assertEqual(_posted(calls), [])

	def test_a_truncated_commit_list_posts_no_marker(self) -> None:
		# Containment in the base would also accept a web conflict-resolver
		# merge against a newer base tip, so an unknown compared tip fails
		# safe: no head poll, no marker, the next run reviews in full.
		for status in ("ahead", "identical", "diverged", ""):
			with self.subTest(status=status):
				rc, out, calls = _sync_gate([], compare_commits=[], base_status=status)
				self.assertEqual(rc, 1)
				self.assertIn(f"MERGE_BASE_SYNC_MARKER pr=7 head_sha={HEAD} outcome=skipped reason=base_tip_unknown", out)
				self.assertFalse(any(f"/compare/{BASE_TIP}...main" in c for c in calls))
				self.assertFalse(any(c.endswith("repos/owner/repo/pulls/7") or "/commits/" in c for c in calls))
				self.assertEqual(_posted(calls), [])

	def test_the_base_tip_is_recorded_only_for_a_complete_commit_list(self) -> None:
		body = f'_pr_base_freshness 7 {HEAD} main >/dev/null; echo "tip=${{PR_BASE_FRESHNESS_BASE_SHA}}"'
		res = _run(body, compare_json=_compare(2, ["a.py"], ["1" * 40, BASE_TIP]), pr_files_json=_pr_files(["a.py"]))
		self.assertIn(f"tip={BASE_TIP}", res.stdout)
		res = _run(body, compare_json=_compare(300, ["a.py"], ["1" * 40, BASE_TIP]), pr_files_json=_pr_files(["a.py"]))
		self.assertIn("tip=\n", res.stdout + "\n")

	def test_a_failed_update_posts_no_marker(self) -> None:
		body_calls = _run(
			f'_pr_base_fresh_for_merge 7 {HEAD} main; echo "rc=$?"',
			compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]), update_rc=1,
		).calls  # type: ignore[attr-defined]
		self.assertFalse(any("-X POST" in c for c in body_calls))

	def test_a_short_head_sha_gets_no_marker(self) -> None:
		res = _run('_pr_base_sync_marker_post 7 abc1234 1 2', env={})
		self.assertIn("outcome=skipped reason=short_head_sha", res.stdout)
		self.assertFalse(any("-X POST" in c for c in res.calls))  # type: ignore[attr-defined]

	def test_an_unbound_update_gets_no_marker(self) -> None:
		res = _run(f'_pr_base_sync_marker_post 7 {HEAD} 1 2', env={})
		self.assertIn("outcome=skipped reason=unbound_update", res.stdout)
		self.assertFalse(any("-X POST" in c for c in res.calls))  # type: ignore[attr-defined]

	def test_the_count_includes_legacy_and_bound_markers(self) -> None:
		comments = json.dumps([[_sync_marker(1), _sync_marker(2, bound=True), _sync_marker(3, author="x")]])
		body = f'''
eval "base_$(declare -f _safe_gh_jq)"
_safe_gh_jq() {{
  case "$*" in
    user) printf '%s' {json.dumps({"login": SYNC_LOGIN})!r} ;;
    *"/issues/7/comments"*) printf '%s' {comments!r} ;;
    *) base__safe_gh_jq "$@" ;;
  esac
}}
_pr_base_sync_count 7; echo "count=${{PR_BASE_SYNC_COUNT}}"
'''
		self.assertIn("count=2", _run(body).stdout)


class LibraryMissingFallbacks(unittest.TestCase):
	"""Same defect class: unverified freshness must never authorize a merge."""

	def test_every_fallback_defers(self) -> None:
		for path in (ENABLE_AUTO_MERGE, RB_JUDGE, POLLER, WORKFLOW):
			with self.subTest(path=path.name):
				text = path.read_text(encoding="utf-8")
				stub = text.split("_pr_base_fresh_for_merge()", 1)[1][:600]
				# The stub's first return statement must be the fail-closed one.
				first_return = stub[stub.index("return "):][:8]
				self.assertEqual(first_return, "return 1")
				stub = stub[:stub.index("return ")]
				self.assertIn("library_unavailable", stub)
				self.assertIn('PR_BASE_FRESHNESS_OUTCOME="unknown"', stub)


class FreshnessSyncWiring(unittest.TestCase):
	def test_every_merge_path_passes_the_cap(self) -> None:
		for path in (WORKFLOW, REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"):
			text = path.read_text(encoding="utf-8")
			self.assertEqual(
				text.count("MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}"),
				text.count("MERGE_BASE_FRESHNESS_MAX_SYNCS: ${{ vars.MERGE_BASE_FRESHNESS_MAX_SYNCS || '2' }}"),
				path.name,
			)

	def test_the_gate_routes_a_verified_update_to_deterministic_skip_merge(self) -> None:
		# The gate body lives in scripts/review_autofix_step_evaluate_gate.sh (#6972).
		text = expanded_review_autofix_text()
		self.assertIn('or ($b | contains("<!-- ai:merge-base-sync:v1 "))', text)
		self.assertIn("MERGE_BASE_SYNC_FAST_PATH_ENABLED: ${{ vars.MERGE_BASE_SYNC_FAST_PATH_ENABLED || 'true' }}", text)
		block = text.split("gate_freshness_sync_fast_path()", 1)[1].split("# Size check uses", 1)[0]
		self.assertIn('bash .codex-head-gate-src/scripts/review_head_gate.sh freshness-sync "${REPOSITORY}" "${PR_NUMBER}"', block)
		self.assertIn('[ "${HEAD_GATE_HELPER_VERIFIED:-false}" = "true" ] || return 1', block)
		# The skip job runs no conflict resolver, and it runs the single-issue
		# security pass only with AUTOFIX_SKIP_RUNS_SECURITY_PASS=true (#8333),
		# so both suppressors apply before any API call.
		fast_path = block.split("if [ \"${FORCE_REVIEW}\" = \"true\" ]; then", 1)[0]
		self.assertIn('reason=merge_conflict pr=${PR_NUMBER}', fast_path)
		self.assertIn('reason=single_issue_security_pass pr=${PR_NUMBER} candidate=freshness_sync', fast_path)
		self.assertLess(fast_path.index("reason=single_issue_security_pass"), fast_path.index("gate_fetch_marker_comments"))
		# The security-pass eligibility rule must match the doc-only /
		# small-diff skip's copy, or a drift could merge an eligible PR unaudited.
		skip_block = text.split('SECURITY_PASS_SKIP_SUPPRESSED="false"', 1)[1].split("SECURITY_PASS_SKIP_SUPPRESSED=\"true\"", 1)[0]
		for cond in ('[ "${SINGLE_ISSUE_SECURITY_PASS_ENABLED:-false}" != "false" ]',
				'&& [ -n "${DEFAULT_BRANCH:-}" ] && [ "${pr_base_ref}" = "${DEFAULT_BRANCH}" ]',
				'&& [ "${pr_head_repo}" = "${REPOSITORY}" ]',
				'&& ! [[ "${pr_head_ref}" =~ ${ORCH_INTEGRATION_BRANCH_PATTERN:-^orchestrator/project-} ]]',
				'&& [[ ",${pr_labels}," != *,e2e-smoke-test,* ]]'):
			self.assertIn(cond, fast_path)
			self.assertIn(cond, skip_block)
		# #8333: with AUTOFIX_SKIP_RUNS_SECURITY_PASS=true the skip job runs
		# the pass; a PR labelled ai:security-pass-failed stays on codex-agent.
		# Anything other than an exact `true` keeps today's suppression.
		switch_off = 'if [ "${AUTOFIX_SKIP_RUNS_SECURITY_PASS:-false}" != "true" ]; then'
		label_check = '[[ ",${pr_labels}," == *,ai:security-pass-failed,* ]]'
		for path_text, candidate in ((fast_path, "freshness_sync"), (text.split('SECURITY_PASS_SKIP_SUPPRESSED="false"', 1)[1].split("FRESHNESS_CI_FAILED_SKIP_SUPPRESSED=", 1)[0], "${candidate_skip_reason}")):
			self.assertLess(path_text.index(switch_off), path_text.index("reason=single_issue_security_pass"))
			self.assertLess(path_text.index("reason=single_issue_security_pass"), path_text.index(label_check))
			self.assertIn(f"reason=security_pass_failed pr=${{PR_NUMBER}} candidate={candidate}", path_text)
			self.assertLess(path_text.index(label_check), path_text.index('DET_SKIP_SECURITY_PASS_REQUIRED="true"'))
		self.assertIn("AUTOFIX_GATE_DET_SKIP_SECURITY_PASS pr=${PR_NUMBER} candidate=${DET_SKIP_REASON} route=skip_job", text)
		branch = block.split("elif gate_freshness_sync_fast_path; then", 1)[1]
		for line in ('DETERMINISTIC_SKIP="true"', 'DET_SKIP_REASON="freshness_sync"', 'SHOULD_RUN="false"'):
			self.assertIn(line, branch)
		# force-review still wins over the fast path.
		self.assertLess(block.index('if [ "${FORCE_REVIEW}" = "true" ]; then'), block.index("elif gate_freshness_sync_fast_path; then"))


if __name__ == "__main__":
	unittest.main()
