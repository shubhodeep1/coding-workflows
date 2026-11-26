#!/usr/bin/env python3
"""The "work is delivered by open PR #N" planning block pages WARNING, not CRITICAL.

Issues #9215, #9224, #9228, #9230 and #9231 all stopped planning with the
planner's #8335 self-check "work is delivered by open PR #9246; resume once it
is merged". That block needs no human: the unblock judge parks the issue on a
pr_on_branch hold and resumes it after the PR merges (#9135 got its hold about
an hour after the block). The "Handle blocked planning output" step of
plan.yml still told the issue "human input required" and sent five CRITICAL
pages. These tests run that step's real body against a stub `gh` and stub
helper scripts.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
STEP_NAME = "Handle blocked planning output"
WAIT_REASON = "work is delivered by open PR #9246; resume once it is merged"

STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG}"
for arg in "$@"; do
	case "${arg}" in
		body=*) printf '%s' "${arg#body=}" > "${STUB_BODY}" ;;
	esac
done
exit 0
"""

STUB_GH_HELPERS = "gh_retry() { \"$@\"; }\n"
STUB_LABEL_HELPERS = "ensure_label_exists() { :; }\n"
STUB_TG_HELPERS = (
	"tg_send_tracked()\n{\n"
	"\tprintf 'level=%s\\n' \"$3\" > \"${STUB_TG}\"\n"
	"\tprintf '%s\\n' \"$2\" >> \"${STUB_TG}\"\n"
	"}\n"
)


def _step_run_from_text() -> str:
	# No PyYAML (the pre-push gate's container): cut the step's `run: |` block
	# out of the workflow text and strip its indentation.
	lines = PLAN_WF.read_text(encoding="utf-8").splitlines()
	start = lines.index(f"      - name: {STEP_NAME}")
	run_at = next(i for i in range(start + 1, len(lines)) if lines[i] == "        run: |")
	body: list[str] = []
	for line in lines[run_at + 1:]:
		if line.strip() and not line.startswith(" " * 10):
			break
		body.append(line[10:])
	return "\n".join(body).rstrip("\n") + "\n"


def _step_run() -> str:
	try:
		import yaml
	except ModuleNotFoundError:
		return _step_run_from_text()
	workflow = yaml.safe_load(PLAN_WF.read_text(encoding="utf-8"))
	for job in workflow["jobs"].values():
		for step in job.get("steps", []):
			if step.get("name") == STEP_NAME:
				return step["run"]
	raise AssertionError(f"step not found: {STEP_NAME!r}")


def _render(script: str) -> str:
	replacements = {
		"${{ github.repository }}": "o/r",
		"${{ github.server_url }}": "https://github.com",
		"${{ github.run_id }}": "123",
	}
	for expression, value in replacements.items():
		script = script.replace(expression, value)
	leftover = re.findall(r"\$\{\{[^}]*\}\}", script)
	assert not leftover, leftover
	return script


class PlanBlockedWaitOnPrTests(unittest.TestCase):
	def _run(
		self,
		reason: str,
		*,
		unblock_judge_enabled: str = "true",
		unblock_judge_wait_enabled: str = "true",
		resolved_gate_enabled: str | None = None,
		labels: list[str] | None = None,
	) -> tuple[str, str, str]:
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			scripts_dir = root / "scripts"
			scripts_dir.mkdir()
			(scripts_dir / "gh_helpers.sh").write_text(STUB_GH_HELPERS, encoding="utf-8")
			(scripts_dir / "label_helpers.sh").write_text(STUB_LABEL_HELPERS, encoding="utf-8")
			(scripts_dir / "tg_helpers.sh").write_text(STUB_TG_HELPERS, encoding="utf-8")
			bin_dir = root / "bin"
			bin_dir.mkdir()
			gh = bin_dir / "gh"
			gh.write_text(STUB_GH, encoding="utf-8")
			gh.chmod(0o755)
			(root / "step.sh").write_text(_render(_step_run()), encoding="utf-8")
			meta_file = root / "issue_meta.json"
			if labels is not None:
				meta_file.write_text(json.dumps({"labels": [{"name": name} for name in labels]}), encoding="utf-8")
			env = {
				**os.environ,
				"PATH": f"{bin_dir}:{os.environ['PATH']}",
				"BLOCKED_REASON": reason,
				"UNBLOCK_JUDGE_ENABLED": unblock_judge_enabled,
				"UNBLOCK_JUDGE_WAIT_ENABLED": unblock_judge_wait_enabled,
				"ISSUE_NUMBER": "9215",
				"ISSUE_URL": "https://github.com/o/r/issues/9215",
				"PLAN_COMMENT_FILE": str(root / "comment.md"),
				"PLAN_PROGRESS_COMMENT_ID": "",
				"STUB_LOG": str(root / "gh.log"),
				"STUB_BODY": str(root / "body.md"),
				"STUB_TG": str(root / "tg.txt"),
				"ISSUE_META_FILE": str(meta_file) if labels is not None else "",
			}
			env.pop("CHECK_TRIAGE_RESOLVED_GATE_ENABLED", None)
			if resolved_gate_enabled is not None:
				env["CHECK_TRIAGE_RESOLVED_GATE_ENABLED"] = resolved_gate_enabled
			result = subprocess.run(
				["bash", str(root / "step.sh")],
				cwd=root,
				env=env,
				capture_output=True,
				text=True,
				timeout=60,
			)
			self.assertEqual(result.returncode, 0, result.stderr)
			return (
				(root / "body.md").read_text(encoding="utf-8"),
				(root / "tg.txt").read_text(encoding="utf-8"),
				result.stdout,
			)

	def test_open_pr_wait_pages_warning_and_says_it_resumes(self) -> None:
		body, tg, stdout = self._run(WAIT_REASON)
		first_line = body.splitlines()[0]
		self.assertEqual(first_line, "Planning blocked: waiting on open PR #9246.")
		self.assertIn(f"Reason: {WAIT_REASON}", body)
		self.assertIn("The unblock judge resumes this issue after PR #9246 merges.", body)
		self.assertNotIn("human input required", body)
		self.assertNotIn("/answer", body)
		self.assertTrue(tg.startswith("level=WARNING\n"), tg)
		self.assertIn("waiting on open PR #9246", tg)
		self.assertIn("PLAN_BLOCKED_WAIT issue=9215 pr=9246 level=WARNING", stdout)

	def test_other_reasons_keep_the_human_input_comment_and_critical_page(self) -> None:
		body, tg, stdout = self._run("missing API key")
		self.assertEqual(body.splitlines()[0], "Planning blocked: human input required.")
		self.assertIn("Provide the required input, then reply with /answer.", body)
		self.assertTrue(tg.startswith("level=CRITICAL\n"), tg)
		self.assertIn("Planning blocked for issue #9215: missing API key", tg)
		self.assertNotIn("PLAN_BLOCKED_WAIT", stdout)

	def test_nothing_to_implement_reason_keeps_the_header_the_unblock_judge_matches(self) -> None:
		# scripts/unblock_judge.sh closes these issues only when the comment's
		# first line is exactly "Planning blocked: human input required."
		body, tg, _ = self._run("work is already on the planning ref; nothing to implement")
		self.assertEqual(body.splitlines()[0], "Planning blocked: human input required.")
		self.assertTrue(tg.startswith("level=CRITICAL\n"), tg)

	def test_a_reason_with_extra_text_is_not_treated_as_a_wait(self) -> None:
		for reason in (
			WAIT_REASON + " and also add a secret",
			"work is delivered by open PR #0; resume once it is merged",
			"work is delivered by open PR #abc; resume once it is merged",
		):
			body, tg, _ = self._run(reason)
			self.assertEqual(body.splitlines()[0], "Planning blocked: human input required.", reason)
			self.assertTrue(tg.startswith("level=CRITICAL\n"), reason)

	def test_disabled_unblock_judge_keeps_the_human_input_comment_and_critical_page(self) -> None:
		# With UNBLOCK_JUDGE_ENABLED=false nothing resumes the issue, so the wait
		# branch must not claim that no action is needed.
		body, tg, stdout = self._run(WAIT_REASON, unblock_judge_enabled="false")
		self.assertEqual(body.splitlines()[0], "Planning blocked: human input required.")
		self.assertNotIn("No action is needed", body)
		self.assertTrue(tg.startswith("level=CRITICAL\n"), tg)
		self.assertNotIn("PLAN_BLOCKED_WAIT", stdout)

	def test_disabled_judge_waits_keep_the_human_input_comment_and_critical_page(self) -> None:
		# UNBLOCK_JUDGE_WAIT_ENABLED=false takes `wait` off the judge's menu
		# (--wait-disabled), so no pr_on_branch hold resumes the issue.
		for value in ("false", "no"):
			with self.subTest(value=value):
				body, tg, stdout = self._run(WAIT_REASON, unblock_judge_wait_enabled=value)
				self.assertEqual(body.splitlines()[0], "Planning blocked: human input required.")
				self.assertNotIn("No action is needed", body)
				self.assertTrue(tg.startswith("level=CRITICAL\n"), tg)
				self.assertNotIn("PLAN_BLOCKED_WAIT", stdout)
		body, tg, _ = self._run(WAIT_REASON, unblock_judge_wait_enabled="")
		self.assertEqual(body.splitlines()[0], "Planning blocked: waiting on open PR #9246.")
		self.assertTrue(tg.startswith("level=WARNING\n"), tg)

	def test_check_triage_issue_promises_closure_only_while_the_judge_and_gate_run(self) -> None:
		# Issue #9812: scripts/unblock_judge.sh closes a blocked ai:check-triage
		# issue only while the judge runs and CHECK_TRIAGE_RESOLVED_GATE_ENABLED
		# is not false; otherwise the issue keeps the human-input text at CRITICAL.
		triage_sentence = "This is a CI-failure triage issue."
		for gate in (None, "true", "TRUE", ""):
			with self.subTest(gate=gate):
				body, tg, _ = self._run("missing API key", resolved_gate_enabled=gate, labels=["ai:check-triage"])
				self.assertIn(triage_sentence, body)
				self.assertTrue(tg.startswith("level=WARNING\n"), tg)
				self.assertIn("(CI triage; the unblock judge will re-check)", tg)
		for gate, judge in (("false", "true"), ("FALSE", "true"), (" false ", "true"), ("true", "false"), (None, "false")):
			with self.subTest(gate=gate, judge=judge):
				body, tg, _ = self._run(
					"missing API key", unblock_judge_enabled=judge, resolved_gate_enabled=gate, labels=["ai:check-triage"],
				)
				self.assertEqual(body.splitlines()[0], "Planning blocked: human input required.")
				self.assertNotIn(triage_sentence, body)
				self.assertIn("Provide the required input, then reply with /answer.", body)
				self.assertTrue(tg.startswith("level=CRITICAL\n"), tg)
				self.assertNotIn("CI triage", tg)
		# Without the label nothing changes.
		body, tg, _ = self._run("missing API key", labels=["bug"])
		self.assertNotIn(triage_sentence, body)
		self.assertTrue(tg.startswith("level=CRITICAL\n"), tg)

	def test_workflow_passes_the_resolved_gate_switch(self) -> None:
		text = PLAN_WF.read_text(encoding="utf-8")
		step = text[text.index(f"      - name: {STEP_NAME}"):]
		step = step[:step.index("        run: |")]
		self.assertIn(
			"CHECK_TRIAGE_RESOLVED_GATE_ENABLED: ${{ vars.CHECK_TRIAGE_RESOLVED_GATE_ENABLED || 'true' }}", step,
		)

	def test_wait_comment_keeps_the_automation_prefix(self) -> None:
		# orchestrate_lib.py skips comments opening with "planning blocked:" when
		# it looks for an unrouted human reply.
		body, _, _ = self._run(WAIT_REASON)
		self.assertTrue(body.lower().startswith("planning blocked:"))
		lib = (REPO_ROOT / "scripts" / "orchestrate_lib.py").read_text(encoding="utf-8")
		self.assertIn('"planning blocked:",', lib)


if __name__ == "__main__":
	unittest.main()
