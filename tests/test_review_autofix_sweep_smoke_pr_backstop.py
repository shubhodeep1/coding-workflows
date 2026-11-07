#!/usr/bin/env python3
"""The review sweep closes stranded release-gate smoke PRs (issue #8953).

Release runs 40608957733 .. 41941001401 left `e2e-smoke-test` PRs open for
days: the gate gave up while Implement sat `pending`, and the late run opened a
PR for the closed smoke issue. The sweep then re-reviewed those PRs every tick
(83.5% of Claude pool spend on Nov 5-7). The backstop closes a PR that carries
the `e2e-smoke-test` label, has an `ai/issue-<N>` head and the pipeline login
as author, once issue <N> is closed or the PR is older than
SWEEP_SMOKE_PR_MAX_AGE_HOURS (default 6, 0 disables the age rule). A closed
smoke PR is never dispatched, and an API error never blocks the tick.

The tests run the sweep step's shell as extracted from the shipped workflow
against a fake `gh`. The cases are collected by
tests/test_review_autofix_sweep_merge_queued_skip.py, which CI already runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
STEP_MARKER = "- name: Enumerate open PRs and dispatch internal-review.yml"
RUN_HEADER = "        run: |\n"
RUN_INDENT = " " * 10
BLOCK_END_RE = re.compile(r"^(?: {0,9}#| {0,6}- | {0,8}[A-Za-z_][\w-]*:(?:\s|$))")
PIPELINE = "pipeline-bot"


def _extract_step_script() -> str:
	text = SWEEP_WF.read_text(encoding="utf-8")
	start = text.find(STEP_MARKER)
	assert start != -1, "could not locate the sweep step"
	run_at = text.find(RUN_HEADER, start)
	assert run_at != -1
	lines: list[str] = []
	for line in text[run_at + len(RUN_HEADER):].splitlines():
		if line.startswith(RUN_INDENT):
			lines.append(line[len(RUN_INDENT):])
		elif line.strip() == "":
			lines.append("")
		else:
			assert BLOCK_END_RE.match(line), f"run body ended on an unexpected line: {line!r}"
			break
	return "\n".join(lines).rstrip("\n") + "\n"


def _iso(hours_ago: float) -> str:
	return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def smoke_pr(number: int, head_ref: str = "", labels: list[str] | None = None,
		author: str = PIPELINE, hours_ago: float = 1.0) -> dict:
	return {
		"number": number,
		"draft": False,
		"title": f"PR {number}",
		"body": "",
		"created_at": _iso(hours_ago),
		"user": {"login": author},
		"head": {"ref": head_ref or f"ai/issue-{number + 1000}", "sha": "0" * 40},
		"labels": [{"name": name} for name in (["e2e-smoke-test"] if labels is None else labels)],
	}


def _write_fake_gh(tmp: Path, prs: list[dict], issue_states: dict[int, str],
		login: str | None, patch_fails: bool) -> Path:
	(tmp / "pulls.json").write_text(json.dumps(prs), encoding="utf-8")
	for number, state in issue_states.items():
		(tmp / f"issue_{number}.state").write_text(state, encoding="utf-8")
	if login is not None:
		(tmp / "login").write_text(login, encoding="utf-8")
	if patch_fails:
		(tmp / "patch_fails").write_text("", encoding="utf-8")
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
			case "$*" in
				"api user --jq .login")
					[ -f "{tmp}/login" ] || exit 1
					cat "{tmp}/login"; exit 0 ;;
				"api -X PATCH repos/o/r/pulls/"*)
					[ -f "{tmp}/patch_fails" ] && exit 1
					printf '%s\\n' "$*" >> "{tmp}/closes.log"; exit 0 ;;
				"api repos/o/r/issues/"*"/comments "*)
					printf '%s\\n' "$*" >> "{tmp}/comments.log"; exit 0 ;;
				"api repos/o/r/issues/"*)
					n="${{2#repos/o/r/issues/}}"
					[ -f "{tmp}/issue_${{n}}.state" ] || exit 1
					cat "{tmp}/issue_${{n}}.state"; exit 0 ;;
				"api repos/"*" --jq .default_branch") printf 'main\\n'; exit 0 ;;
				*"/pulls "*|*"/pulls") cat "{tmp}/pulls.json"; exit 0 ;;
				*"/actions/workflows/"*) printf '%s' '{{"total_count":0,"workflow_runs":[]}}'; exit 0 ;;
			esac
		fi
		echo "unexpected gh call: $*" >&2
		exit 1
		"""), encoding="utf-8")
	gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
	return bin_dir


@unittest.skipUnless(shutil.which("jq"), "jq is required")
class SweepSmokePrBackstopTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"sweep_smoke_{os.getpid()}_{self._testMethodName}"
		if self.tmp.exists():
			shutil.rmtree(self.tmp)
		self.tmp.mkdir(parents=True)
		self.addCleanup(shutil.rmtree, self.tmp, True)

	def run_sweep(self, prs: list[dict], issue_states: dict[int, str] | None = None,
			login: str | None = PIPELINE, patch_fails: bool = False, **env: str) -> subprocess.CompletedProcess:
		bin_dir = _write_fake_gh(self.tmp, prs, issue_states or {}, login, patch_fails)
		full_env = {
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"RUNNER_TEMP": str(self.tmp),
			"REPOSITORY": "o/r",
			"GH_TOKEN": "x",
			"READ_TOKEN": "x",
			"HEAD_REF_FILTER": "",
			"DRY_RUN": "false",
			"ALLOW_WORKFLOW_EDITS": "true",
			"SWEEP_STALE_QUEUED_MINUTES": "120",
		}
		full_env.update(env)
		return subprocess.run(
			["bash", "-c", _extract_step_script()],
			env=full_env, capture_output=True, text=True, cwd=self.tmp,
		)

	def _lines(self, name: str) -> list[str]:
		path = self.tmp / name
		return path.read_text().splitlines() if path.exists() else []

	def dispatched(self) -> list[str]:
		return [line.split("pr_number=")[1].split()[0] for line in self._lines("dispatches.log")]

	def closed(self) -> list[str]:
		return [line.split("repos/o/r/pulls/")[1].split()[0] for line in self._lines("closes.log")]

	def test_closes_when_issue_closed(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40")], {40: "closed"})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), ["5"])
		self.assertEqual(self.dispatched(), [])
		self.assertIn("AUTOFIX_SWEEP_SMOKE_CLOSE pr=#5 reason=issue_closed", result.stdout)
		self.assertIn("closed_smoke=1", result.stdout)
		comments = "\n".join(self._lines("comments.log"))
		self.assertIn("issues/5/comments", comments)
		self.assertNotRegex(comments, r"#[0-9]")

	def test_closes_when_older_than_max_age(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40", hours_ago=7)], {40: "open"})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), ["5"])
		self.assertIn("AUTOFIX_SWEEP_SMOKE_CLOSE pr=#5 reason=max_age", result.stdout)
		self.assertEqual(self.dispatched(), [])

	def test_leaves_non_matching_prs_alone(self) -> None:
		prs = [
			smoke_pr(1, "ai/issue-41", labels=[], hours_ago=50),
			smoke_pr(2, "feature/issue-42", hours_ago=50),
			smoke_pr(3, "ai/issue-43", author="human", hours_ago=50),
			smoke_pr(4, "ai/issue-44", hours_ago=1),
		]
		result = self.run_sweep(prs, {41: "closed", 42: "closed", 43: "closed", 44: "open"})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), [])
		self.assertEqual(self.dispatched(), ["1", "2", "3", "4"])
		self.assertIn("closed_smoke=0", result.stdout)

	def test_zero_max_age_disables_only_the_age_rule(self) -> None:
		prs = [smoke_pr(5, "ai/issue-40", hours_ago=50), smoke_pr(6, "ai/issue-41", hours_ago=50)]
		result = self.run_sweep(prs, {40: "open", 41: "closed"}, SWEEP_SMOKE_PR_MAX_AGE_HOURS="0")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), ["6"])
		self.assertEqual(self.dispatched(), ["5"])

	def test_invalid_max_age_falls_back_to_six(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40", hours_ago=7)], {40: "open"},
			SWEEP_SMOKE_PR_MAX_AGE_HOURS="six")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), ["5"])

	def test_issue_read_error_keeps_only_age_rule(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40", hours_ago=1)], {})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), [])
		self.assertEqual(self.dispatched(), ["5"])
		self.assertIn("AUTOFIX_SWEEP_END", result.stdout)

	def test_login_error_closes_nothing(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40")], {40: "closed"}, login=None)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), [])
		self.assertIn("AUTOFIX_SWEEP_SMOKE_CLOSE_FAILED pr=#5 reason=author_unavailable", result.stdout)
		self.assertIn("AUTOFIX_SWEEP_END", result.stdout)

	def test_close_error_does_not_dispatch_or_block(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40"), smoke_pr(6, "ai/issue-41", labels=[])],
			{40: "closed"}, patch_fails=True)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn("AUTOFIX_SWEEP_SMOKE_CLOSE_FAILED pr=#5 reason=close_failed", result.stdout)
		self.assertEqual(self.dispatched(), ["6"])
		self.assertIn("closed_smoke=0", result.stdout)

	def test_dry_run_closes_nothing(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40")], {40: "closed"}, DRY_RUN="true")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.closed(), [])
		self.assertEqual(self.dispatched(), [])
		self.assertIn("AUTOFIX_SWEEP_SMOKE_CLOSE pr=#5 reason=issue_closed dry_run=true", result.stdout)

	def test_smoke_pr_with_skip_marker_or_merge_queue_is_still_closed(self) -> None:
		queued = smoke_pr(5, "ai/issue-40", labels=["e2e-smoke-test", "ai:merge-queued"])
		skip = smoke_pr(6, "ai/issue-41")
		skip["title"] = "[skip ai] smoke"
		result = self.run_sweep([queued, skip], {40: "closed", 41: "closed"})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(sorted(self.closed()), ["5", "6"])
		self.assertIn("closed_smoke=2", result.stdout)

	def test_login_read_once_per_tick(self) -> None:
		result = self.run_sweep([smoke_pr(5, "ai/issue-40"), smoke_pr(6, "ai/issue-41")], {40: "closed", 41: "closed"})
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual([c for c in self._lines("gh_calls.log") if c == "api user --jq .login"], ["api user --jq .login"])

	def test_no_smoke_pr_means_no_extra_reads(self) -> None:
		result = self.run_sweep([smoke_pr(1, "ai/issue-41", labels=[])])
		self.assertEqual(result.returncode, 0, result.stderr)
		calls = self._lines("gh_calls.log")
		self.assertFalse([c for c in calls if c.startswith("api user") or "/issues/" in c], calls)

	def test_workflow_wires_default_and_projection(self) -> None:
		text = SWEEP_WF.read_text(encoding="utf-8")
		self.assertIn("SWEEP_SMOKE_PR_MAX_AGE_HOURS: ${{ vars.SWEEP_SMOKE_PR_MAX_AGE_HOURS || '6' }}", text)
		self.assertIn('created_at: (.created_at // "")', text)
		self.assertIn('author: (.user.login // "")', text)
		step = text[text.find(STEP_MARKER):]
		self.assertTrue(step.find("AUTOFIX_SWEEP_SMOKE_CLOSE pr=") < step.find("reason=skip_ai_marker")
			< step.find("# Preflight (mirrors scripts/orchestrate_poll_process.sh"))


if __name__ == "__main__":
	unittest.main()
