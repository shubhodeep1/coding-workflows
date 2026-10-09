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
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
ENABLE_AUTO_MERGE = REPO_ROOT / "scripts" / "review_enable_auto_merge.sh"
RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"

HEAD = "a" * 40


def _compare(ahead_by: int, files: list[str]) -> str:
	return json.dumps({"ahead_by": ahead_by, "behind_by": 3, "files": [{"filename": f} for f in files]})


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
		for site in text.split('--squash --auto --match-head-commit "${RB_JUDGED_HEAD_SHA}"')[:-1]:
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


def _wait(runs_sequence: list[str], *, max_minutes: str = "1", poll: str = "1", env: dict[str, str] | None = None) -> tuple[int, str]:
	"""Each check-runs read returns the next fixture; the last one repeats."""
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	seq_file = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_checks_wait_seq_{os.getpid()}.json"
	seq_file.write_text(json.dumps(runs_sequence), encoding="utf-8")
	counter = Path(os.environ.get("TMPDIR", "/tmp")) / f"pr_checks_wait_n_{os.getpid()}"
	counter.write_text("0", encoding="utf-8")
	preamble = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  case "$*" in
    *"/protection"*) printf '%s' '' ;;
    *"/check-runs"*)
      n="$(cat "{counter}")"; printf '%s' "$((n + 1))" > "{counter}"
      jq -r --argjson n "${{n}}" '.[ ([$n, (length - 1)] | min) ]' "{seq_file}" ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
source {str(LIB)!r}
_pr_wait_for_required_checks 7 {HEAD} main {max_minutes}; rc=$?
printf 'rc=%s outcome=%s\\n' "${{rc}}" "${{PR_CHECKS_WAIT_OUTCOME}}"
"""
	full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PR_CHECKS_REPOSITORY": "owner/repo",
		"AUTO_MERGE_CHECKS_POLL_SECONDS": poll, "GITHUB_RUN_ID": "4242", "PYTHONDONTWRITEBYTECODE": "1"}
	full_env.update(env or {})
	res = subprocess.run(["bash", "-c", preamble], env=full_env, capture_output=True, text=True, timeout=120)
	assert res.returncode == 0, res.stderr
	last = [line for line in res.stdout.splitlines() if line.startswith("rc=")][-1]
	return int(last.split()[0][3:]), res.stdout


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

	def test_allow_all_sentinel_proceeds(self) -> None:
		rc, out = _wait([RED], env={"ORCH_FINAL_MERGE_REQUIRED_CHECKS": ""})
		self.assertEqual(rc, 0)
		self.assertIn("outcome=allow_all", out)


class RequiredChecksWiring(unittest.TestCase):
	def test_review_enable_auto_merge_waits_before_both_tails(self) -> None:
		text = ENABLE_AUTO_MERGE.read_text(encoding="utf-8")
		wait_at = text.index('_pr_wait_for_required_checks "${PR_NUMBER}" "${INITIAL_HEAD_SHA}" "${_orch_pr_base_ref}"')
		self.assertLess(wait_at, text.index('--merge --auto --match-head-commit "${INITIAL_HEAD_SHA}"'))
		self.assertLess(wait_at, text.index('--squash --auto --match-head-commit "${INITIAL_HEAD_SHA}"'))
		self.assertIn("reason=required_checks_${PR_CHECKS_WAIT_OUTCOME:-unknown}", text)

	def test_deterministic_skip_merge_waits_before_both_auto_merge_calls(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		job = text.split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertEqual(job.count('elif ! _pr_wait_for_required_checks "${PR_NUMBER}" "${PR_HEAD_SHA}" ""; then'), 2)
		self.assertIn("AUTO_MERGE_CHECKS_WAIT_MINUTES: ${{ vars.AUTO_MERGE_CHECKS_WAIT_MINUTES || '45' }}", job)

	def test_codex_agent_auto_merge_step_passes_the_wait_variables(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		step = text.split("- name: Enable auto-merge on PR", 1)[1].split("- name: ", 1)[0]
		self.assertIn("AUTO_MERGE_CHECKS_WAIT_MINUTES: ${{ vars.AUTO_MERGE_CHECKS_WAIT_MINUTES || '45' }}", step)
		self.assertIn("AUTO_MERGE_CHECKS_POLL_SECONDS: ${{ vars.AUTO_MERGE_CHECKS_POLL_SECONDS || '60' }}", step)

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
			if code.startswith(("echo", "tg_send_msg", "tg_notify")) or code.lstrip("\"'").startswith("echo"):
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


if __name__ == "__main__":
	unittest.main()
