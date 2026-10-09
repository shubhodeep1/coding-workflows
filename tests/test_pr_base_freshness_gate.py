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

	def test_api_failure_is_unknown_and_proceeds_with_warning(self) -> None:
		rc, out, calls = _gate(compare_json="")
		self.assertEqual(rc, 0)
		self.assertIn("outcome=unknown reason=compare_failed", out)
		self.assertIn("::warning::", out)
		self.assertFalse(any("/update-branch" in c for c in calls))

	def test_pr_files_failure_is_unknown_and_proceeds(self) -> None:
		rc, out, _ = _gate(compare_json=_compare(1, ["a.py"]), pr_files_json="{}")
		self.assertEqual(rc, 0)
		self.assertIn("reason=pr_files_failed", out)

	def test_head_and_base_are_resolved_from_the_pr_when_omitted(self) -> None:
		pr_json = json.dumps({"head": {"sha": HEAD}, "base": {"ref": "release/v1"}})
		rc, out, calls = _gate(args='7 "" ""', pr_json=pr_json, compare_json=_compare(1, ["a.py"]), pr_files_json=_pr_files(["a.py"]))
		self.assertEqual(rc, 1)
		self.assertTrue(any(c.endswith("repos/owner/repo/pulls/7") for c in calls))
		self.assertTrue(any(f"/compare/{HEAD}...release/v1" in c for c in calls))

	def test_unresolvable_head_is_unknown_and_proceeds(self) -> None:
		rc, out, calls = _gate(args='7 "" ""', pr_json="{}")
		self.assertEqual(rc, 0)
		self.assertIn("reason=unresolved_head_or_base", out)
		self.assertFalse(any("/compare/" in c for c in calls))


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
		self.assertIn("action=defer reason=base_moved_overlap", text)

	def test_review_rb_judge_gates_both_merge_sites(self) -> None:
		text = RB_JUDGE.read_text(encoding="utf-8")
		self.assertEqual(text.count('if ! _pr_base_fresh_for_merge "${PR_NUMBER}" "${RB_JUDGED_HEAD_SHA}" "${PR_BASE_REF:-}"; then'), 2)
		for site in text.split('--squash --auto --match-head-commit "${RB_JUDGED_HEAD_SHA}"')[:-1]:
			self.assertIn("_pr_base_fresh_for_merge", site[-900:])

	def test_poller_gates_its_direct_merge_sites(self) -> None:
		text = POLLER.read_text(encoding="utf-8")
		self.assertIn('_pr_base_fresh_for_merge "${PW_PR}" "${_pw_head_sha}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${merge_pr}"', text)
		self.assertIn('_pr_base_fresh_for_merge "${RB_PR}" "${_rb_merge_sha}" "${_rb_merge_base}"', text)
		self.assertIn("type _pr_base_fresh_for_merge >/dev/null 2>&1", text)

	def test_deterministic_skip_merge_job_gates_both_auto_merge_calls(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		job = text.split("  deterministic-skip-merge:", 1)[1].split("\n  claude-fixer-auto-merge:", 1)[0]
		self.assertIn("path: .codex-freshness-src", job)
		self.assertIn("scripts/pr_checks_lib.sh", job)
		self.assertIn('[ "$(git -C .codex-freshness-src rev-parse HEAD 2>/dev/null)" = "${REVIEW_SUPPORT_SHA}" ]', job)
		self.assertEqual(job.count('if ! _pr_base_fresh_for_merge "${PR_NUMBER}" "${PR_HEAD_SHA}" ""; then'), 2)
		self.assertIn("MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}", job)

	def test_codex_agent_auto_merge_step_passes_the_variable(self) -> None:
		text = WORKFLOW.read_text(encoding="utf-8")
		step = text.split("- name: Enable auto-merge on PR", 1)[1].split("- name: ", 1)[0]
		self.assertIn("MERGE_BASE_FRESHNESS_ENABLED: ${{ vars.MERGE_BASE_FRESHNESS_ENABLED || 'true' }}", step)


if __name__ == "__main__":
	unittest.main()
