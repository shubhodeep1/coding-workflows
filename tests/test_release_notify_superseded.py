"""Release notification reports SUPERSEDED, not FAILED, for a cancelled smoke test.

test-and-mark-stable.yml's e2e-smoke-test job sits in a concurrency group
with cancel-in-progress: true, so a newer release run cancels an older run's
smoke test. Run 38986967451 (v1.35.2) sent "Release v1.35.2 FAILED" for
exactly that. These tests run the real "Send Telegram notification" step
with stubbed `gh` and `curl` and check the header it sends.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
RUN_ID = "38986967451"
NEWER_RUN_ID = "38993222716"
GREEN_JOBS = {
	"needs.validate-scripts.result": "success",
	"needs.orphan-workflows-test.result": "success",
	"needs.orchestrate-decompose-test.result": "success",
	"needs.validate-standalone-test.result": "success",
	"needs.clarify-rejects-unsolvable-test.result": "success",
	"needs.e2e-alt-model-test.result": "success",
	"needs.validate.result": "skipped",
	"needs.release.result": "skipped",
}


def _notify_script() -> str:
	data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	for job in data["jobs"].values():
		for step in job.get("steps", []):
			if step.get("name") == "Send Telegram notification":
				return step["run"]
	raise AssertionError("Send Telegram notification step not found")


def _render(script: str, expressions: dict[str, str]) -> str:
	def substitute(match: re.Match[str]) -> str:
		expression = match.group(1).strip()
		if expression in expressions:
			return expressions[expression]
		# `a || b` fallbacks: take the first known non-empty operand.
		for operand in (part.strip() for part in expression.split("||")):
			if expressions.get(operand):
				return expressions[operand]
		return ""

	return re.sub(r"\$\{\{(.*?)\}\}", substitute, script)


def _run(tmp_path: Path, e2e: str, gh_output: str | None, **overrides: str) -> tuple[str, str, bool]:
	stub_dir = tmp_path / "bin"
	stub_dir.mkdir()
	gh_log = tmp_path / "gh_calls"
	curl_log = tmp_path / "curl_args"
	if gh_output is None:
		gh_body = f'echo "$@" >> "{gh_log}"\nexit 1\n'
	else:
		gh_body = f'echo "$@" >> "{gh_log}"\nprintf "%s" "{gh_output}"\n'
	(stub_dir / "gh").write_text("#!/bin/bash\n" + gh_body, encoding="utf-8")
	(stub_dir / "curl").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{curl_log}"\n', encoding="utf-8")
	for stub in ("gh", "curl"):
		(stub_dir / stub).chmod(0o755)

	expressions = {
		"github.server_url": "https://github.com",
		"github.repository": "shubhodeep1/coding-workflows",
		"github.run_id": RUN_ID,
		"needs.resolve-version.outputs.version": "v1.35.2",
		"needs.e2e-smoke-test.result": e2e,
		"needs.workflow-log-analysis-test.result": "success",
		**GREEN_JOBS,
		**overrides,
	}
	env = {
		"PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
		"HOME": str(tmp_path),
		"GATE_ONLY": "false",
		"RELEASE_ENABLED": "true",
		"TG_BOT_SECRET": "test-bot",
		"TG_CHAT_ID": "1",
		"GITHUB_RUN_ID": RUN_ID,
		"GITHUB_REPOSITORY": "shubhodeep1/coding-workflows",
	}
	completed = subprocess.run(
		["bash", "-c", _render(_notify_script(), expressions)],
		cwd=tmp_path,
		env=env,
		capture_output=True,
		text=True,
		check=True,
	)
	sent = curl_log.read_text(encoding="utf-8")
	text_line = next(line for line in sent.splitlines() if line.startswith("text="))
	return text_line.removeprefix("text="), completed.stdout, gh_log.exists()


def test_cancelled_smoke_test_with_newer_run_is_superseded(tmp_path: Path) -> None:
	header, stdout, gh_called = _run(tmp_path, "cancelled", NEWER_RUN_ID)
	assert header == (
		f"Release v1.35.2 SUPERSEDED: newer run {NEWER_RUN_ID} cancelled this run's "
		"e2e-smoke-test; nothing was released by this run"
	)
	assert gh_called
	assert f"RELEASE_NOTIFY_V1 outcome=superseded run_id={RUN_ID} superseded_by={NEWER_RUN_ID}" in stdout
	gh_args = (tmp_path / "gh_calls").read_text(encoding="utf-8")
	assert "repos/shubhodeep1/coding-workflows/actions/workflows/test-and-mark-stable.yml/runs" in gh_args


@pytest.mark.parametrize(
	("case", "e2e", "gh_output", "overrides", "expect_gh_call"),
	[
		("no newer run", "cancelled", "", {}, True),
		("gh fails", "cancelled", None, {}, True),
		("gh prints junk", "cancelled", "null", {}, True),
		("smoke test failed", "failure", NEWER_RUN_ID, {}, False),
		("another job failed", "cancelled", NEWER_RUN_ID, {"needs.validate-scripts.result": "failure"}, False),
		("release failed", "cancelled", NEWER_RUN_ID, {"needs.release.result": "failure"}, False),
		("orphans failed", "cancelled", NEWER_RUN_ID, {"needs.orphan-workflows-test.result": "failure"}, False),
		("decompose failed", "cancelled", NEWER_RUN_ID, {"needs.orchestrate-decompose-test.result": "failure"}, False),
		("validate-fx failed", "cancelled", NEWER_RUN_ID, {"needs.validate-standalone-test.result": "failure"}, False),
		("negative failed", "cancelled", NEWER_RUN_ID, {"needs.clarify-rejects-unsolvable-test.result": "failure"}, False),
		("alt-model failed", "cancelled", NEWER_RUN_ID, {"needs.e2e-alt-model-test.result": "failure"}, False),
		("validate failed", "cancelled", NEWER_RUN_ID, {"needs.validate.result": "failure"}, False),
	],
)
def test_other_outcomes_stay_failed(
	tmp_path: Path,
	case: str,
	e2e: str,
	gh_output: str | None,
	overrides: dict[str, str],
	expect_gh_call: bool,
) -> None:
	header, stdout, gh_called = _run(tmp_path, e2e, gh_output, **overrides)
	assert header == "Release v1.35.2 FAILED", case
	assert gh_called is expect_gh_call, case
	assert "RELEASE_NOTIFY_V1" not in stdout, case
	assert ("Superseded-run lookup failed" in stdout) is (case == "gh fails"), case


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_superseding_run_filter_picks_the_oldest_newer_run() -> None:
	match = re.search(r'--jq "(\[\.workflow_runs\[\][^"]*)"', _notify_script())
	assert match, "superseding-run jq filter not found"
	jq_filter = match.group(1).replace("${GITHUB_RUN_ID}", RUN_ID)
	sample = (
		'{"workflow_runs": [{"id": 38999999999}, {"id": %s}, {"id": %s}, {"id": 38900000000}]}'
		% (NEWER_RUN_ID, RUN_ID)
	)
	picked = subprocess.run(["jq", "-r", jq_filter], input=sample, capture_output=True, text=True, check=True)
	assert picked.stdout.strip() == NEWER_RUN_ID
	none_newer = subprocess.run(
		["jq", "-r", jq_filter], input='{"workflow_runs": [{"id": %s}]}' % RUN_ID, capture_output=True, text=True, check=True
	)
	assert none_newer.stdout.strip() == ""
