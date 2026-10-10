#!/usr/bin/env python3
"""The review sweep must not dispatch a review for a merge-train queued PR.

`scripts/review_merge_train.sh` labels a PR `ai:merge-queued` while an older
overlapping PR is open, and its `release` subcommand (run from
`orchestrate_poll.yml` on every tick and `cancel_on_pr_close.yml` on every
close) re-dispatches the review itself once the blockers are gone. The sweep
dispatched those PRs anyway: on 2026-10-09 06:54 UTC run 37895976433
dispatched 31 reviews, including every queued PR in a 25-deep train, and each
run only re-ran the Merge-train gate and soft-exited.

These tests execute the sweep step's shell **as extracted from the shipped
workflow file** against a fake `gh`, so a future edit cannot drop the skip
while the tests keep passing against a stale copy.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_WF = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
STEP_MARKER = "- name: Enumerate open PRs and dispatch internal-review.yml"
NEXT_STEP_MARKER = "- name: Record GH_PAT budget at sweep end"
RUN_INDENT = " " * 10


def extract_step_script() -> str:
	"""Return the `run:` body of the enumerate-and-dispatch step, dedented."""
	text = SWEEP_WF.read_text(encoding="utf-8")
	start = text.find(STEP_MARKER)
	assert start != -1, "could not locate the sweep step"
	end = text.find(NEXT_STEP_MARKER, start)
	assert end != -1, "could not bound the sweep step"
	step = text[start:end]
	run_at = step.find("        run: |\n")
	assert run_at != -1, "sweep step has no block-scalar run body"
	body = step[run_at + len("        run: |\n"):]
	lines = []
	for line in body.splitlines():
		if line.startswith(RUN_INDENT):
			lines.append(line[len(RUN_INDENT):])
		else:
			assert line.strip() == "", f"unexpected indentation in run body: {line!r}"
			lines.append("")
	return "\n".join(lines) + "\n"


def pr(number: int, head_ref: str, labels: list[str] | None = None, draft: bool = False) -> dict:
	return {
		"number": number,
		"draft": draft,
		"title": f"PR {number}",
		"body": "",
		"head": {"ref": head_ref, "sha": "0" * 40},
		"labels": [{"name": name} for name in (labels or [])],
	}


def write_fake_gh(tmp: Path, prs: list[dict]) -> Path:
	(tmp / "pulls.json").write_text(json.dumps(prs), encoding="utf-8")
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
class SweepMergeQueuedSkipTest(unittest.TestCase):
	def setUp(self) -> None:
		self.tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"sweep_mq_{os.getpid()}_{self._testMethodName}"
		if self.tmp.exists():
			shutil.rmtree(self.tmp)
		self.tmp.mkdir(parents=True)
		self.addCleanup(shutil.rmtree, self.tmp, True)

	def run_sweep(self, prs: list[dict], **env: str) -> subprocess.CompletedProcess:
		bin_dir = write_fake_gh(self.tmp, prs)
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
			["bash", "-c", extract_step_script()],
			env=full_env, capture_output=True, text=True, cwd=self.tmp,
		)

	def dispatched(self) -> list[str]:
		log = self.tmp / "dispatches.log"
		if not log.exists():
			return []
		return [line.split("pr_number=")[1].split()[0] for line in log.read_text().splitlines()]

	def test_queued_pr_is_skipped_and_logged(self) -> None:
		result = self.run_sweep([
			pr(10, "ai/issue-1"),
			pr(11, "ai/issue-2", labels=["ai:merge-queued"]),
			pr(12, "ai/issue-3", labels=["ai:workflow-heal"]),
		])
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["10", "12"])
		self.assertIn("AUTOFIX_SWEEP_SKIP pr=#11 reason=merge_train_queued head_ref=ai/issue-2", result.stdout)
		self.assertIn("skipped_merge_queued=1", result.stdout)
		self.assertIn("dispatched=2", result.stdout)

	def test_skip_happens_before_the_active_run_preflight(self) -> None:
		"""The skip must cost no workflow-run lookups of its own and must not
		be reachable only through the dispatch branch."""
		text = SWEEP_WF.read_text(encoding="utf-8")
		step = text[text.find(STEP_MARKER):]
		skip_at = step.find('reason=merge_train_queued')
		preflight_at = step.find('# Preflight (mirrors scripts/orchestrate_poll_process.sh')
		dispatch_at = step.find('gh workflow run internal-review.yml')
		self.assertNotEqual(skip_at, -1)
		self.assertTrue(skip_at < preflight_at < dispatch_at)

	def test_disabled_train_keeps_dispatching_queued_prs(self) -> None:
		"""With MERGE_TRAIN_ENABLED off nothing releases the label, so the
		sweep must keep dispatching or the PR would never be reviewed."""
		for value in ("false", "0", "off", "no", "FALSE"):
			with self.subTest(value=value):
				shutil.rmtree(self.tmp)
				self.tmp.mkdir()
				result = self.run_sweep([pr(11, "ai/issue-2", labels=["ai:merge-queued"])], MERGE_TRAIN_ENABLED=value)
				self.assertEqual(result.returncode, 0, result.stderr)
				self.assertEqual(self.dispatched(), ["11"])
				self.assertIn("skipped_merge_queued=0", result.stdout)

	def test_enabled_spellings_match_the_merge_train_script(self) -> None:
		for value in ("true", "1", "yes", "on", "TRUE", "On"):
			with self.subTest(value=value):
				shutil.rmtree(self.tmp)
				self.tmp.mkdir()
				result = self.run_sweep([pr(11, "ai/issue-2", labels=["ai:merge-queued"])], MERGE_TRAIN_ENABLED=value)
				self.assertEqual(result.returncode, 0, result.stderr)
				self.assertEqual(self.dispatched(), [])
				self.assertIn("skipped_merge_queued=1", result.stdout)

	def test_unset_variable_defaults_to_enabled(self) -> None:
		result = self.run_sweep([pr(11, "ai/issue-2", labels=["ai:merge-queued"])])
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), [])

	def test_label_removed_by_hand_is_dispatched_again(self) -> None:
		"""The documented one-shot bypass (remove the label, re-run the
		review) must still reach the sweep."""
		result = self.run_sweep([pr(11, "ai/issue-2", labels=["ai:security"])])
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(self.dispatched(), ["11"])

	def test_snapshot_file_keeps_its_shape_for_the_ci_rerun_helper(self) -> None:
		"""scripts/ci_cancelled_rerun.py reads the same snapshot; the added
		labels field must sit beside the validated fields, not replace them."""
		result = self.run_sweep([pr(11, "ai/issue-2", labels=["ai:merge-queued"])])
		self.assertEqual(result.returncode, 0, result.stderr)
		snapshot = json.loads((self.tmp / "ci-cancelled-pr-snapshot.json").read_text())
		self.assertEqual(snapshot[0]["labels"], ["ai:merge-queued"])
		for key in ("number", "head_ref", "head_sha", "title", "body"):
			self.assertIn(key, snapshot[0])

	def test_workflow_passes_the_repo_variable(self) -> None:
		text = SWEEP_WF.read_text(encoding="utf-8")
		self.assertIn("MERGE_TRAIN_ENABLED: ${{ vars.MERGE_TRAIN_ENABLED || 'true' }}", text)


if __name__ == "__main__":
	unittest.main()
