#!/usr/bin/env python3
"""Zombie run reaper in the review autofix sweep (P11b, root cause 9).

GitHub can wedge an Actions run in `queued` for months; a normal cancel
returns 409 and the sweep's SWEEP_STALE_QUEUED_MINUTES cutoff only stopped
such a run from suppressing dispatch. `scripts/review_autofix_sweep_reaper.sh`
(sourced by the sweep's enumerate step) cancels runs queued longer than
ZOMBIE_RUN_MAX_QUEUED_HOURS, force-cancels on 409, touches at most 10 runs a
sweep, reuses the listing the sweep already fetched and never fails the sweep.

The tests run the enumerate step's shell **as extracted from the shipped
workflow file**, with the shipped helper staged where the workflow loads it
(`.ci-rerun-support/scripts/`), against a fake `gh`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_autofix_sweep_merge_queued_skip import (  # noqa: E402
	STEP_MARKER,
	SWEEP_WF,
	extract_step_script,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "review_autofix_sweep_reaper.sh"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
WORKFLOWS = ("internal-review.yml", "review_autofix.yml")
STATUSES = ("queued", "in_progress", "pending")


def iso_hours_ago(hours: float) -> str:
	when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
	return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(run_id: int, hours: float | None, status: str = "queued", workflow: str = "internal-review.yml") -> dict:
	entry = {
		"id": run_id,
		"status": status,
		"event": "pull_request",
		"head_branch": f"feature/{run_id}",
		"display_title": "Codex PR Self-Healing Semantic Agent",
		"path": f".github/workflows/{workflow}@refs/heads/main",
	}
	if hours is not None:
		entry["created_at"] = iso_hours_ago(hours)
	return entry


def normalise(lines: list[str]) -> list[str]:
	"""Drop the per-run clock reading so two sweeps seconds apart compare equal."""
	return [re.sub(r"queued_age_minutes=\d+", "queued_age_minutes=N", line) for line in lines]


def write_fake_gh(
	tmp: Path,
	listings: dict[tuple[str, str], list[dict]],
	post_status: dict[str, str],
	failed_listings: tuple[tuple[str, str], ...] = (),
) -> Path:
	"""listings: (workflow, status) -> runs. post_status: "<id>/<action>" -> ok|409|500.

	A (workflow, status) pair in failed_listings answers with HTTP 403, which
	leaves the sweep's active-run snapshot incomplete.
	"""
	(tmp / "pulls.json").write_text(json.dumps([{
		"number": 10,
		"draft": False,
		"title": "PR 10",
		"body": "",
		"head": {"ref": "ai/issue-1", "sha": "0" * 40},
		"labels": [],
	}]), encoding="utf-8")
	runs_dir = tmp / "runs"
	runs_dir.mkdir()
	for wf in WORKFLOWS:
		for status in STATUSES:
			if (wf, status) in failed_listings:
				continue
			runs = listings.get((wf, status), [])
			(runs_dir / f"{wf}_{status}.json").write_text(
				json.dumps({"total_count": len(runs), "workflow_runs": runs}), encoding="utf-8")
	post_dir = tmp / "post"
	post_dir.mkdir()
	for key, value in post_status.items():
		run_id, action = key.split("/")
		(post_dir / f"{run_id}_{action}").write_text(value, encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(textwrap.dedent(f"""\
		#!/usr/bin/env bash
		printf '%s\\n' "$*" >> "{tmp}/gh_calls.log"
		if [ "$1" = "workflow" ] && [ "$2" = "run" ]; then
			printf '%s\\n' "$*" >> "{tmp}/dispatches.log"
			exit 0
		fi
		if [ "$1" = "api" ]; then
			args="$*"
			if [[ "${{args}}" =~ actions/runs/([0-9]+)/(cancel|force-cancel) ]]; then
				id="${{BASH_REMATCH[1]}}"; action="${{BASH_REMATCH[2]}}"
				printf '%s %s token=%s\\n' "${{action}}" "${{id}}" "${{GH_TOKEN:-}}" >> "{tmp}/posts.log"
				outcome="ok"
				[ -f "{tmp}/post/${{id}}_${{action}}" ] && outcome="$(cat "{tmp}/post/${{id}}_${{action}}")"
				case "${{outcome}}" in
					ok) printf '{{}}'; exit 0 ;;
					*) echo "gh: Cannot cancel this workflow run (HTTP ${{outcome}})" >&2; exit 1 ;;
				esac
			fi
			if [[ "${{args}}" =~ actions/workflows/([A-Za-z0-9_.-]+)/runs ]]; then
				wf="${{BASH_REMATCH[1]}}"
				status="$(printf '%s' "${{args}}" | grep -oE 'status=[a-z_]+' | head -1 | cut -d= -f2)"
				printf '%s %s\\n' "${{wf}}" "${{status}}" >> "{tmp}/listings.log"
				if [ -f "{tmp}/runs/${{wf}}_${{status}}.json" ]; then
					cat "{tmp}/runs/${{wf}}_${{status}}.json"
					exit 0
				fi
				echo "gh: Resource not accessible by integration (HTTP 403)" >&2
				exit 1
			fi
			case "${{args}}" in
				"api repos/"*" --jq .default_branch") printf 'main\\n'; exit 0 ;;
				*"/pulls "*|*"/pulls") cat "{tmp}/pulls.json"; exit 0 ;;
			esac
		fi
		echo "unexpected gh call: $*" >&2
		exit 1
		"""), encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	return bin_dir


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class SweepZombieReaperTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"sweep_zr_{os.getpid()}_{self._testMethodName}"
		self.reset()
		self.addCleanup(shutil.rmtree, self.tmp, True)

	def reset(self) -> None:
		if self.tmp.exists():
			shutil.rmtree(self.tmp)
		self.tmp.mkdir(parents=True)

	def run_sweep(self, listings=None, post_status=None, stage_helper=True, real_gh_helpers=False, failed_listings=(), **env: str):
		bin_dir = write_fake_gh(self.tmp, listings or {}, post_status or {}, failed_listings)
		if stage_helper:
			support = self.tmp / ".ci-rerun-support" / "scripts"
			support.mkdir(parents=True)
			shutil.copy(HELPER, support / HELPER.name)
		if real_gh_helpers:
			(self.tmp / "scripts").mkdir()
			shutil.copy(GH_HELPERS, self.tmp / "scripts" / GH_HELPERS.name)
		full_env = {
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"HOME": str(self.tmp),
			"TMPDIR": str(self.tmp),
			"RUNNER_TEMP": str(self.tmp),
			"REPOSITORY": "o/r",
			"GH_TOKEN": "pat-token",
			"READ_TOKEN": "read-token",
			"ZOMBIE_RUN_REAPER_TOKEN": "reaper-token",
			"HEAD_REF_FILTER": "",
			"DRY_RUN": "false",
			"ALLOW_WORKFLOW_EDITS": "true",
			"SWEEP_STALE_QUEUED_MINUTES": "120",
			"GH_RETRY_MAX_ATTEMPTS": "1",
		}
		full_env.update(env)
		return subprocess.run(
			["bash", "-c", extract_step_script()],
			env=full_env, capture_output=True, text=True, cwd=self.tmp, timeout=120,
		)

	def read_log(self, name: str) -> list[str]:
		path = self.tmp / name
		return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

	def posts(self) -> list[str]:
		return [" ".join(line.split()[:2]) for line in self.read_log("posts.log")]

	def assert_sweep_finished(self, result) -> None:
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("AUTOFIX_SWEEP_END", result.stdout)

	# --- behaviour -----------------------------------------------------------

	def test_over_cap_run_is_cancelled_and_young_run_untouched(self) -> None:
		result = self.run_sweep({("internal-review.yml", "queued"): [run(111, 30), run(222, 2)]})
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 111"])
		self.assertIn(
			"ZOMBIE_RUN_REAPED run=111 workflow=.github/workflows/internal-review.yml age_h=30 result=cancel",
			result.stdout)
		self.assertNotIn("run=222", result.stdout)
		self.assertIn("ZOMBIE_RUN_REAPER_END outcome=ran candidates=1 attempted=1 cancelled=1 force_cancelled=0 failed=0 deferred=0", result.stdout)
		# The cancel uses the reaper token (github.token), never the PAT.
		self.assertTrue(all(line.endswith("token=reaper-token") for line in self.read_log("posts.log")))
		# The reaper never stops the PR dispatch that follows it.
		self.assertEqual(len(self.read_log("dispatches.log")), 1)

	def test_409_leads_to_force_cancel(self) -> None:
		result = self.run_sweep(
			{("review_autofix.yml", "queued"): [run(333, 48, workflow="review_autofix.yml")]},
			{"333/cancel": "409"})
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 333", "force-cancel 333"])
		self.assertIn(
			"ZOMBIE_RUN_REAPED run=333 workflow=.github/workflows/review_autofix.yml age_h=48 result=force_cancel",
			result.stdout)
		self.assertNotIn("HTTP 409", result.stdout + result.stderr)

	def test_409_leads_to_force_cancel_with_real_gh_helpers(self) -> None:
		result = self.run_sweep(
			{("internal-review.yml", "queued"): [run(334, 30)]},
			{"334/cancel": "409"}, real_gh_helpers=True)
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 334", "force-cancel 334"])
		self.assertIn("run=334 workflow=.github/workflows/internal-review.yml age_h=30 result=force_cancel", result.stdout)

	def test_failure_is_logged_and_sweep_continues(self) -> None:
		result = self.run_sweep(
			{("internal-review.yml", "queued"): [run(401, 50), run(402, 40), run(403, 30)]},
			{"401/cancel": "409", "401/force-cancel": "409", "402/cancel": "500"})
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 401", "force-cancel 401", "cancel 402", "cancel 403"])
		self.assertIn("ZOMBIE_RUN_REAPED run=401 workflow=.github/workflows/internal-review.yml age_h=50 result=failed", result.stdout)
		self.assertIn("ZOMBIE_RUN_REAPED run=402 workflow=.github/workflows/internal-review.yml age_h=40 result=failed", result.stdout)
		self.assertIn("ZOMBIE_RUN_REAPED run=403 workflow=.github/workflows/internal-review.yml age_h=30 result=cancel", result.stdout)
		self.assertIn("cancelled=1 force_cancelled=0 failed=2", result.stdout)
		self.assertEqual(len(self.read_log("dispatches.log")), 1)

	def test_cap_of_ten_per_sweep(self) -> None:
		runs = [run(500 + i, 100 - i) for i in range(12)]
		result = self.run_sweep({("internal-review.yml", "queued"): runs})
		self.assert_sweep_finished(result)
		posts = self.posts()
		self.assertEqual(len(posts), 10)
		# Oldest first: the two youngest (511, 510) are deferred.
		self.assertEqual(posts, [f"cancel {500 + i}" for i in range(10)])
		self.assertEqual(result.stdout.count("ZOMBIE_RUN_REAPED "), 10)
		self.assertIn("candidates=12 attempted=10 cancelled=10 force_cancelled=0 failed=0 deferred=2", result.stdout)

	def test_listing_is_not_refetched(self) -> None:
		listings = {("internal-review.yml", "queued"): [run(601, 30)]}
		enabled = self.run_sweep(listings)
		self.assert_sweep_finished(enabled)
		enabled_listings = self.read_log("listings.log")
		enabled_calls = [c for c in self.read_log("gh_calls.log") if "/cancel" not in c and "/force-cancel" not in c]
		self.reset()
		disabled = self.run_sweep(listings, ZOMBIE_RUN_REAPER_ENABLED="false")
		self.assert_sweep_finished(disabled)
		self.assertEqual(sorted(enabled_listings), sorted(f"{wf} {s}" for wf in WORKFLOWS for s in STATUSES))
		self.assertEqual(enabled_listings, self.read_log("listings.log"))
		self.assertEqual(enabled_calls, self.read_log("gh_calls.log"))

	def test_helper_makes_no_listing_call(self) -> None:
		code = [line for line in HELPER.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
		text = "\n".join(code)
		self.assertNotIn("actions/workflows/", text)
		self.assertNotIn("-X GET", text)
		self.assertNotIn("--paginate", text)
		self.assertNotRegex(text, r"\bgh api\b")
		endpoints = re.findall(r'actions/runs/\$\{reaper_id\}/([a-z-]+)"', text)
		self.assertEqual(sorted(set(endpoints)), ["cancel", "force-cancel"])
		self.assertEqual(text.count("gh_api_retry --idempotent --optional -X POST"), 2)

	def test_switch_off_parity(self) -> None:
		for value in ("false", "0", "no", "off", "FALSE"):
			with self.subTest(value=value):
				self.reset()
				listings = {("internal-review.yml", "queued"): [run(701, 30)]}
				off = self.run_sweep(listings, ZOMBIE_RUN_REAPER_ENABLED=value)
				self.assert_sweep_finished(off)
				self.assertEqual(self.posts(), [])
				self.assertNotIn("ZOMBIE_RUN_", off.stdout + off.stderr)
				off_calls = self.read_log("gh_calls.log")
				self.reset()
				on = self.run_sweep(listings, ZOMBIE_RUN_REAPER_ENABLED="true")
				self.assert_sweep_finished(on)
				on_lines = [line for line in on.stdout.splitlines() if "ZOMBIE_RUN_" not in line]
				self.assertEqual(normalise(off.stdout.splitlines()), normalise(on_lines))
				self.assertEqual(off_calls, [c for c in self.read_log("gh_calls.log") if "/cancel" not in c])

	def test_helper_missing_does_not_fail_the_sweep(self) -> None:
		result = self.run_sweep({("internal-review.yml", "queued"): [run(801, 30)]}, stage_helper=False)
		self.assert_sweep_finished(result)
		self.assertIn("ZOMBIE_RUN_REAPER_END outcome=helper_unavailable", result.stdout)
		self.assertEqual(self.posts(), [])

	def test_dry_run_makes_no_post(self) -> None:
		result = self.run_sweep({("internal-review.yml", "queued"): [run(901, 30)]}, DRY_RUN="true")
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), [])
		self.assertIn("ZOMBIE_RUN_REAPER_END outcome=dry_run candidates=1 attempted=0", result.stdout)

	def test_dry_run_counts_only_runs_past_the_cap_as_deferred(self) -> None:
		runs = [run(950 + i, 100 - i) for i in range(12)]
		result = self.run_sweep({("internal-review.yml", "queued"): runs}, DRY_RUN="true")
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), [])
		self.assertIn("outcome=dry_run candidates=12 attempted=0 cancelled=0 force_cancelled=0 failed=0 deferred=2", result.stdout)

	def test_early_exit_logs_reaper_skip(self) -> None:
		result = self.run_sweep({("internal-review.yml", "queued"): [run(960, 30)]}, PROVIDER_OUTAGE_PAUSED="true")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("ZOMBIE_RUN_REAPER_END outcome=skipped reason=provider_outage", result.stdout)
		self.assertEqual(self.posts(), [])
		self.reset()
		off = self.run_sweep(PROVIDER_OUTAGE_PAUSED="true", ZOMBIE_RUN_REAPER_ENABLED="off")
		self.assertEqual(off.returncode, 0, off.stderr)
		self.assertNotIn("ZOMBIE_RUN_", off.stdout)
		script = extract_step_script()
		for reason in ("provider_outage", "no_candidates", "default_branch_unavailable"):
			self.assertIn(f"zombie_reaper_skip_note {reason}\n", script.replace("\r", ""))

	def test_invalid_cap_cancels_nothing(self) -> None:
		for value in ("abc", "0", "-5", "1.5", "24h"):
			with self.subTest(value=value):
				self.reset()
				result = self.run_sweep({("internal-review.yml", "queued"): [run(1001, 300)]}, ZOMBIE_RUN_MAX_QUEUED_HOURS=value)
				self.assert_sweep_finished(result)
				self.assertEqual(self.posts(), [])
				self.assertIn("ZOMBIE_RUN_REAPER_END outcome=invalid_max_queued_hours", result.stdout)

	def test_custom_cap_is_honoured(self) -> None:
		result = self.run_sweep({("internal-review.yml", "queued"): [run(1101, 30), run(1102, 50)]}, ZOMBIE_RUN_MAX_QUEUED_HOURS="48")
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 1102"])

	def test_missing_created_at_and_status_moves_are_never_reaped(self) -> None:
		moved = run(1202, 30)
		result = self.run_sweep({
			("internal-review.yml", "queued"): [run(1201, None), moved, run(1203, 30)],
			("internal-review.yml", "in_progress"): [dict(moved, status="in_progress")],
			("review_autofix.yml", "pending"): [run(1204, 300, status="pending", workflow="review_autofix.yml")],
		})
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 1203"])

	def test_incomplete_snapshot_skips_reaper(self) -> None:
		"""A failed status listing may hide the in_progress copy of a run, so
		the reaper must not cancel anything from that tick's listing."""
		result = self.run_sweep(
			{("internal-review.yml", "queued"): [run(1301, 30)]},
			failed_listings=(("review_autofix.yml", "pending"),))
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("ZOMBIE_RUN_REAPER_END outcome=snapshot_incomplete", result.stdout)
		self.assertEqual(self.posts(), [])

	def test_untrusted_run_fields_never_reach_logs(self) -> None:
		bad = run(1401, 30)
		bad["path"] = ".github/workflows/x.yml;$(id)\n::error::boom"
		result = self.run_sweep({("internal-review.yml", "queued"): [bad, dict(run(0, 30), id="1402; rm -rf /")]})
		self.assert_sweep_finished(result)
		self.assertEqual(self.posts(), ["cancel 1401"])
		# @tsv escapes the newline; the sanitiser then keeps [A-Za-z0-9_./-] only.
		self.assertIn("ZOMBIE_RUN_REAPED run=1401 workflow=.github/workflows/x.ymlidnerrorboom age_h=30 result=cancel", result.stdout)
		self.assertNotIn("::error::boom", result.stdout)
		self.assertNotIn("$(id)", result.stdout)

	# --- static workflow contract ---------------------------------------------

	def test_workflow_wiring(self) -> None:
		text = SWEEP_WF.read_text(encoding="utf-8")
		step = text[text.find(STEP_MARKER):]
		step_env = step[:step.find("        run: |")]
		self.assertIn("ZOMBIE_RUN_REAPER_ENABLED: ${{ vars.ZOMBIE_RUN_REAPER_ENABLED || 'true' }}", step_env)
		self.assertIn("ZOMBIE_RUN_MAX_QUEUED_HOURS: ${{ vars.ZOMBIE_RUN_MAX_QUEUED_HOURS || '24' }}", step_env)
		self.assertIn("ZOMBIE_RUN_REAPER_TOKEN: ${{ github.token }}", step_env)
		job = text[text.find("  sweep:"):text.find("    steps:")]
		self.assertRegex(job, r"\n      actions: write\n")
		script = extract_step_script()
		snapshot_at = script.find('snapshot_active_review_runs "${wf}"')
		source_at = script.find("source .ci-rerun-support/scripts/review_autofix_sweep_reaper.sh")
		rows_at = script.find("mapfile -t rows")
		self.assertTrue(0 <= snapshot_at < source_at < rows_at, (snapshot_at, source_at, rows_at))
		self.assertIn('"${RUNNER_TEMP:-/tmp}/sweep-active-runs-internal-review.yml.json"', script)
		self.assertIn('"${RUNNER_TEMP:-/tmp}/sweep-active-runs-review_autofix.yml.json"', script)
		# The raw listing file the helper reads is the one the snapshot writes.
		self.assertIn('local snap_raw="${RUNNER_TEMP:-/tmp}/sweep-active-runs-${workflow}.json"', script)
		self.assertNotIn("scripts/review_autofix_sweep_reaper.sh", script.replace(".ci-rerun-support/scripts/review_autofix_sweep_reaper.sh", ""))

	def test_helper_syntax(self) -> None:
		result = subprocess.run(["bash", "-n", str(HELPER)], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
	unittest.main()
