#!/usr/bin/env python3
"""Required-checks wait: cached stable reads, back-off and read routing (issue #9926).

`_pr_wait_for_required_checks` (scripts/pr_checks_lib.sh) used to re-read
branch protection, the runs listing, check-runs and (on the gate-supersede
path) the commit statuses on every 60 s poll for up to 45 minutes. These tests
pin the three changes, with the pass/fail outcomes unchanged:

1. branch protection is read once per wait, statuses once per head SHA unless
   a status is still `pending` (a failed read is never cached);
2. the poll interval doubles while the snapshot is unchanged, up to
   AUTO_MERGE_CHECKS_POLL_MAX_SECONDS, resets on a change, and the last sleep
   is clamped to the deadline;
3. with `gh_read` defined, the per-poll reads go through
   `gh_read --same-repo-only api ...`; a 0-minute wait makes today's calls.

`sleep` is stubbed (logged, no real time passes), as are `gh_retry`,
`_safe_gh_jq` and, where a test asks for it, `gh_read`.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
HEAD = "a" * 40
HEAD2 = "b" * 40


def _check_runs(*entries: tuple) -> str:
	"""--paginate --slurp shape; entry = (id, name, status, conclusion[, completed_at])."""
	runs = []
	for e in entries:
		run = {"id": e[0], "name": e[1], "status": e[2], "conclusion": e[3] or None,
			"details_url": "https://github.com/o/r/actions/runs/999/job/1"}
		if len(e) > 4:
			run["completed_at"] = e[4]
		runs.append(run)
	return json.dumps([{"check_runs": runs}])


def _ci_runs(*entries: tuple) -> str:
	return json.dumps({"workflow_runs": [
		{"name": e[0], "status": e[1], "id": e[2], "conclusion": e[3] if len(e) > 3 else None} for e in entries
	]})


GREEN = _check_runs((1, "CI", "completed", "success"), (2, "lint", "completed", "success"))
RED = _check_runs((1, "CI", "completed", "failure"), (2, "lint", "completed", "success"))
RUNNING = _check_runs((1, "CI", "in_progress", ""), (2, "lint", "completed", "success"))
RUNNING_MORE = _check_runs((1, "CI", "in_progress", ""), (2, "lint", "completed", "success"), (3, "build", "queued", ""))
EMPTY = json.dumps([{"check_runs": []}])
PROTECTION = json.dumps({"required_status_checks": {"contexts": ["CI"]}})

# Gate-supersede path: the newest `review / gate` run failed, another check is
# still running, so the wait keeps polling and reads the head's statuses.
GATE_FAILED_RUNNING = _check_runs(
	(1, "build", "in_progress", ""),
	(7001, "review / gate", "completed", "failure", "2026-11-10T00:00:00Z"),
)


def _status(state: str = "success", *, sid: int = 1, ctx: str = "ai-review/head-gate", sha: str = HEAD) -> dict:
	return {"id": sid, "context": ctx, "state": state, "created_at": "2026-11-10T00:02:41Z",
		"url": f"https://api.github.com/repos/owner/repo/statuses/{sha}", "creator": {"login": "workflow-bot"}}


def _wait(*, check_runs: list[str], ci_runs: list[str] | None = None, statuses: list[str] | None = None,
		protection: str = "", max_minutes: str = "5", poll: str = "60", poll_max: str | None = None,
		env: dict[str, str] | None = None, gh_read: bool = False, base: str = "main",
		heads: tuple[str, ...] = (HEAD,)) -> tuple[list[tuple[int, str]], list[str], str]:
	"""Run one wait per head in one shell; returns ([(rc, outcome)], call log, output)."""
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")
	tmp = Path(tempfile.mkdtemp(prefix="pr_wait_backoff_"))
	for name, seq in (("check_runs", check_runs), ("ci_runs", ci_runs or [json.dumps({"workflow_runs": []})]),
			("statuses", statuses or ["[]"])):
		(tmp / f"{name}.json").write_text(json.dumps(seq), encoding="utf-8")
	call_log = tmp / "calls.log"
	read_stub = ""
	if gh_read:
		read_stub = """
gh_read() {
  printf 'READ %s\\n' "$*" >> "${CALL_LOG}"
  [ "${READ_FAIL:-0}" != "1" ] || return 1
  [ "${1:-}" = "--same-repo-only" ] && shift
  [ "${1:-}" = "api" ] && shift
  _fixture "$@"
}
"""
	waits = "\n".join(
		f'_pr_wait_for_required_checks 7 {h} {shlex.quote(base)} {max_minutes}; rc=$?\n'
		f'printf "WAIT rc=%s outcome=%s waited=%s\\n" "${{rc}}" "${{PR_CHECKS_WAIT_OUTCOME}}" "${{PR_CHECKS_WAIT_WAITED_S}}"'
		for h in heads
	)
	script = f"""
set -uo pipefail
gh_retry() {{ "$@"; }}
_seq() {{
  local f="${{SEQ_DIR}}/$1.json" c="${{SEQ_DIR}}/$1.n" n
  n="$(cat "$c" 2>/dev/null || echo 0)"; printf '%s' "$((n + 1))" > "$c"
  jq -r --argjson n "$n" '.[ ([$n, (length - 1)] | min) ]' "$f"
}}
_fixture() {{
  local out
  case "$*" in
    *"/protection"*) [ -n "${{PROTECTION_JSON}}" ] || return 1; printf '%s' "${{PROTECTION_JSON}}" ;;
    *"/check-runs"*) _seq check_runs ;;
    *"/actions/runs?head_sha="*) _seq ci_runs ;;
    *"/statuses"*) out="$(_seq statuses)"; [ "${{out}}" != "FAIL" ] || return 1; printf '%s' "${{out}}" ;;
    user) printf '%s' '{{"login":"workflow-bot"}}' ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
_safe_gh_jq() {{ printf 'SAFE %s\\n' "$*" >> "${{CALL_LOG}}"; _fixture "$@"; }}
{read_stub}
source {str(LIB)!r}
sleep() {{ printf 'SLEEP %s\\n' "$1" >> "${{CALL_LOG}}"; }}
{waits}
"""
	full_env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"PR_CHECKS_REPOSITORY": "owner/repo",
		"GITHUB_RUN_ID": "4242",
		"AUTO_MERGE_CHECKS_POLL_SECONDS": poll,
		"SEQ_DIR": str(tmp),
		"CALL_LOG": str(call_log),
		"PROTECTION_JSON": protection,
	}
	if poll_max is not None:
		full_env["AUTO_MERGE_CHECKS_POLL_MAX_SECONDS"] = poll_max
	full_env.update(env or {})
	res = subprocess.run(["bash", "-c", script], env=full_env, capture_output=True, text=True, timeout=120, check=False)
	assert res.returncode == 0, res.stderr
	results = []
	for line in res.stdout.splitlines():
		if line.startswith("WAIT "):
			fields = dict(part.split("=", 1) for part in line[5:].split())
			results.append((int(fields["rc"]), fields["outcome"]))
	calls = call_log.read_text().splitlines() if call_log.exists() else []
	shutil.rmtree(tmp, ignore_errors=True)
	return results, calls, res.stdout + res.stderr


def _sleeps(calls: list[str]) -> list[int]:
	return [int(c.split()[1]) for c in calls if c.startswith("SLEEP ")]


def _count(calls: list[str], needle: str) -> int:
	"""GET reads only: the timeout's final-CI pending mark POSTs a status."""
	return len([c for c in calls if needle in c and not c.startswith("SLEEP ") and " -X " not in f" {c} "])


class ProtectionReadOncePerWait(unittest.TestCase):
	def test_protection_is_read_once_across_ticks(self) -> None:
		results, calls, out = _wait(check_runs=[RUNNING], protection=PROTECTION, max_minutes="5")
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertGreaterEqual(_count(calls, "/check-runs"), 3)
		self.assertEqual(_count(calls, "/protection"), 1, calls)

	def test_a_failed_protection_read_is_not_repeated_within_the_wait(self) -> None:
		results, calls, out = _wait(check_runs=[RUNNING], protection="", max_minutes="3")
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertEqual(_count(calls, "/protection"), 1, calls)

	def test_each_wait_reads_protection_afresh(self) -> None:
		results, calls, _ = _wait(check_runs=[GREEN], protection=PROTECTION, heads=(HEAD, HEAD2))
		self.assertEqual(results, [(0, "ok"), (0, "ok")])
		self.assertEqual(_count(calls, "/protection"), 2, calls)


class StatusesMemo(unittest.TestCase):
	ENV: ClassVar[dict[str, str]] = {"ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*"}

	def test_statuses_are_read_once_per_head_when_none_is_pending(self) -> None:
		statuses = [json.dumps([_status("failure")])]
		results, calls, out = _wait(check_runs=[GATE_FAILED_RUNNING], statuses=statuses, max_minutes="4", env=self.ENV)
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertGreaterEqual(_count(calls, "/check-runs"), 3)
		self.assertEqual(_count(calls, "/statuses"), 1, calls)
		self.assertIn("reason=status_not_success", out)

	def test_a_pending_head_gate_status_is_re_read_every_tick(self) -> None:
		statuses = [json.dumps([_status("pending")])]
		results, calls, out = _wait(check_runs=[GATE_FAILED_RUNNING], statuses=statuses, max_minutes="4", env=self.ENV)
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertEqual(_count(calls, "/statuses"), _count(calls, "/check-runs"), calls)

	def test_superseded_and_unrelated_pending_statuses_do_not_block_the_memo(self) -> None:
		# The statuses list holds history: an older pending head-gate entry and
		# the never-resolved final-ci-merge-pending mark sit next to the newest
		# head-gate status, which the decision reads.
		older = _status("pending", sid=1)
		older["created_at"] = "2026-11-10T00:01:00Z"
		statuses = [json.dumps([older, _status("failure", sid=2),
			_status("pending", sid=3, ctx="ai-review/final-ci-merge-pending")])]
		results, calls, out = _wait(check_runs=[GATE_FAILED_RUNNING], statuses=statuses, max_minutes="4", env=self.ENV)
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertGreaterEqual(_count(calls, "/check-runs"), 3)
		self.assertEqual(_count(calls, "/statuses"), 1, calls)
		self.assertIn("reason=status_not_success", out)

	def test_a_failed_read_is_not_cached(self) -> None:
		statuses = ["FAIL", json.dumps([_status("failure")])]
		results, calls, out = _wait(check_runs=[GATE_FAILED_RUNNING], statuses=statuses, max_minutes="4", env=self.ENV)
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertEqual(_count(calls, "/statuses"), 2, calls)
		self.assertIn("reason=status_read_failed", out)

	def test_a_new_head_reads_its_statuses(self) -> None:
		statuses = [json.dumps([_status("failure")])]
		results, calls, _ = _wait(check_runs=[GATE_FAILED_RUNNING], statuses=statuses, max_minutes="3", env=self.ENV,
			heads=(HEAD, HEAD2))
		self.assertEqual(results, [(1, "timeout"), (1, "timeout")])
		self.assertEqual(_count(calls, f"/commits/{HEAD}/statuses"), 1, calls)
		self.assertEqual(_count(calls, f"/commits/{HEAD2}/statuses"), 1, calls)

	def test_a_cached_superseding_status_still_merges(self) -> None:
		statuses = [json.dumps([_status("success")])]
		runs = _check_runs((7001, "review / gate", "completed", "failure", "2026-11-10T00:00:00Z"))
		results, calls, out = _wait(check_runs=[GATE_FAILED_RUNNING, runs], statuses=statuses, max_minutes="4", env=self.ENV)
		self.assertEqual(results, [(0, "ok")], out)
		self.assertEqual(_count(calls, "/statuses"), 1, calls)
		self.assertEqual(_count(calls, "SAFE user"), 1, calls)


class BackOff(unittest.TestCase):
	def test_unchanged_snapshot_doubles_up_to_the_cap_and_clamps_the_last_sleep(self) -> None:
		results, calls, out = _wait(check_runs=[RUNNING], max_minutes="10", poll_max="180")
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertEqual(_sleeps(calls), [60, 120, 180, 180, 60])
		self.assertEqual(sum(_sleeps(calls)), 600)
		self.assertIn("outcome=timeout waited_s=600", out)

	def test_default_cap_is_180(self) -> None:
		_, calls, _ = _wait(check_runs=[RUNNING], max_minutes="10")
		self.assertEqual(_sleeps(calls), [60, 120, 180, 180, 60])

	def test_a_changed_snapshot_resets_to_the_base(self) -> None:
		seq = [RUNNING, RUNNING, RUNNING_MORE]
		results, calls, out = _wait(check_runs=seq, max_minutes="10", poll_max="180")
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertEqual(_sleeps(calls), [60, 120, 60, 120, 180, 60])
		self.assertLessEqual(sum(_sleeps(calls)), 600)

	def test_a_queued_to_in_progress_change_resets_to_the_base(self) -> None:
		queued = _check_runs((1, "CI", "queued", ""), (2, "lint", "completed", "success"))
		results, calls, out = _wait(check_runs=[queued, RUNNING], max_minutes="10", poll_max="180")
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertEqual(_sleeps(calls), [60, 60, 120, 180, 180])

	def test_the_deadline_is_never_exceeded(self) -> None:
		for minutes in ("1", "2", "7"):
			with self.subTest(minutes=minutes):
				_, calls, out = _wait(check_runs=[RUNNING], max_minutes=minutes, poll="45", poll_max="180")
				self.assertEqual(sum(_sleeps(calls)), int(minutes) * 60, calls)
				self.assertIn(f"waited_s={int(minutes) * 60} ", out)

	def test_invalid_or_low_cap_turns_back_off_off(self) -> None:
		for value in ("abc", "0", "30", "9999999", "-5", ""):
			with self.subTest(value=value):
				_, calls, _ = _wait(check_runs=[RUNNING], max_minutes="4", poll_max=value)
				expected = [60, 120, 60] if value == "" else [60, 60, 60, 60]
				self.assertEqual(_sleeps(calls), expected)

	def test_cap_equal_to_base_disables_back_off(self) -> None:
		_, calls, _ = _wait(check_runs=[RUNNING], max_minutes="4", poll_max="60")
		self.assertEqual(_sleeps(calls), [60, 60, 60, 60])


class ZeroMinuteWait(unittest.TestCase):
	ENV: ClassVar[dict[str, str]] = {"AUTO_MERGE_REQUIRED_CI_CHECK": "lint"}

	def test_zero_minute_wait_makes_todays_calls_and_never_routes(self) -> None:
		for with_read in (False, True):
			with self.subTest(gh_read=with_read):
				results, calls, out = _wait(check_runs=[RUNNING], ci_runs=[_ci_runs(("CI", "in_progress", 999))],
					protection=PROTECTION, max_minutes="0", env=self.ENV, gh_read=with_read)
				self.assertEqual(results, [(1, "timeout")], out)
				self.assertEqual(calls, [
					f"SAFE repos/owner/repo/actions/runs?head_sha={HEAD}&per_page=100",
					"SAFE repos/owner/repo/branches/main/protection",
					f"SAFE --paginate --slurp repos/owner/repo/commits/{HEAD}/check-runs?per_page=100",
				])


class ReadRouting(unittest.TestCase):
	ENV: ClassVar[dict[str, str]] = {"AUTO_MERGE_REQUIRED_CI_CHECK": "lint", "ORCH_FINAL_MERGE_REQUIRED_CHECKS": "*"}

	def test_per_tick_reads_go_through_gh_read_when_defined(self) -> None:
		statuses = [json.dumps([_status("success")])]
		results, calls, out = _wait(check_runs=[GATE_FAILED_RUNNING], ci_runs=[_ci_runs(("CI", "in_progress", 999))],
			statuses=statuses, max_minutes="3", env=self.ENV, gh_read=True)
		self.assertEqual(results, [(1, "timeout")], out)
		reads = [c for c in calls if c.startswith("READ ")]
		safes = [c for c in calls if c.startswith("SAFE ") and not c.startswith("SAFE -X ")]
		self.assertTrue(reads)
		for call in reads:
			self.assertTrue(call.startswith("READ --same-repo-only api "), call)
		self.assertTrue(any("/actions/runs?head_sha=" in c for c in reads))
		self.assertTrue(any("/check-runs" in c for c in reads))
		self.assertTrue(any("/statuses" in c for c in reads))
		# The stub logs READ then serves the fixture directly: protection and
		# the identity read stay on the caller's token (_safe_gh_jq).
		self.assertEqual(sorted({c.split()[1] for c in safes}), ["repos/owner/repo/branches/main/protection", "user"])

	def test_a_failed_routed_read_falls_back_to_safe_gh_jq(self) -> None:
		results, calls, out = _wait(check_runs=[GREEN], max_minutes="3", gh_read=True, env={"READ_FAIL": "1"})
		self.assertEqual(results, [(0, "ok")], out)
		self.assertTrue(any(c.startswith("READ ") and "/check-runs" in c for c in calls), calls)
		self.assertTrue(any(c.startswith("SAFE ") and "/check-runs" in c for c in calls), calls)
		self.assertIn("routed read failed", out)

	def test_without_gh_read_every_read_uses_safe_gh_jq(self) -> None:
		results, calls, out = _wait(check_runs=[RUNNING], ci_runs=[_ci_runs(("CI", "in_progress", 999))],
			max_minutes="3", env=self.ENV)
		self.assertEqual(results, [(1, "timeout")], out)
		self.assertFalse([c for c in calls if c.startswith("READ ")])
		self.assertTrue(any("/check-runs" in c and c.startswith("SAFE ") for c in calls))

	def test_gh_read_is_used_only_by_the_router(self) -> None:
		text = LIB.read_text(encoding="utf-8")
		code = [ln for ln in text.splitlines() if "gh_read" in ln and not ln.lstrip().startswith("#")]
		self.assertEqual(len(code), 2, code)
		self.assertNotIn("gh pr merge", "\n".join(code))
		router = text.split("_pr_checks_wait_read()\n{", 1)[1].split("\n}\n", 1)[0]
		self.assertIn("gh_retry gh_read --same-repo-only api \"$@\"", router)
		# One routed attempt; the caller-token fallback owns the retries.
		self.assertIn("GH_RETRY_MAX_ATTEMPTS=1 gh_retry gh_read --same-repo-only api", router)
		self.assertIn('[ "${_pr_wait_route_reads:-0}" = "1" ]', router)
		# Protection and the identity read never route.
		self.assertIn('gh_retry _safe_gh_jq "repos/${repo}/branches/${protection_ref:-${base_ref}}/protection"', text)
		self.assertIn("gh_retry _safe_gh_jq user", text)


class OutcomesUnchangedWithBackOff(unittest.TestCase):
	def test_green_is_ok(self) -> None:
		results, calls, out = _wait(check_runs=[GREEN], poll_max="180")
		self.assertEqual(results, [(0, "ok")])
		self.assertIn("outcome=ok waited_s=0", out)
		self.assertEqual(_sleeps(calls), [])

	def test_settled_failure_is_failed(self) -> None:
		results, _, out = _wait(check_runs=[RUNNING, RED], poll_max="180")
		self.assertEqual(results, [(1, "failed")])
		self.assertIn("outcome=failed waited_s=60", out)

	def test_timeout(self) -> None:
		results, _, out = _wait(check_runs=[RUNNING], max_minutes="2", poll_max="180")
		self.assertEqual(results, [(1, "timeout")])
		self.assertIn("outcome=timeout waited_s=120", out)

	def test_no_check_runs_yet_takes_two_base_interval_graces(self) -> None:
		results, calls, out = _wait(check_runs=[EMPTY], poll_max="180")
		self.assertEqual(results, [(0, "ok")])
		self.assertEqual(_sleeps(calls), [60, 60])
		self.assertIn("outcome=ok waited_s=120", out)

	def test_cancelled_ci_run_is_failed(self) -> None:
		results, _, out = _wait(check_runs=[GREEN], ci_runs=[_ci_runs(("CI", "completed", 101, "cancelled"))], poll_max="180")
		self.assertEqual(results, [(1, "failed")])
		self.assertIn("reason=ci_run_cancelled", out)

	def test_pending_ci_run_backs_off_then_proceeds(self) -> None:
		queued = _ci_runs(("CI", "queued", 101))
		done = _ci_runs(("CI", "completed", 101, "success"))
		results, calls, out = _wait(check_runs=[GREEN], ci_runs=[queued, queued, queued, done], max_minutes="10", poll_max="180")
		self.assertEqual(results, [(0, "ok")], out)
		self.assertEqual(_sleeps(calls), [60, 120, 180])


if __name__ == "__main__":
	unittest.main()
