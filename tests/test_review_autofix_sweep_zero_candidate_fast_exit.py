#!/usr/bin/env python3
"""Contract tests for the zero-candidate fast-exit in review_autofix_sweep.

The sweep enumerates open non-draft PRs, logs `AUTOFIX_SWEEP_START`, then
preflights active review-family runs by querying the Actions API for
`internal-review.yml` and `review_autofix.yml`. When enumeration returns zero
candidates, that API fanout is wasted work: there is nothing to filter,
dispatch, or skip.

This contract keeps the optimisation surgical:

1. `AUTOFIX_SWEEP_START` still logs first for observability.
2. A `total == 0` guard exits before the active-run snapshot helper or any
   `/actions/workflows/{workflow}/runs` lookup.
3. Non-zero sweeps still retain the existing snapshot helper and the loop that
   snapshots both review workflows.
4. The zero-candidate path preserves the existing `AUTOFIX_SWEEP_END`
   summary vocabulary before exiting.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import tempfile

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_SWEEP = REPO_ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"


def _review_autofix_sweep_text() -> str:
	return REVIEW_AUTOFIX_SWEEP.read_text(encoding="utf-8")


def _sweep_step_block(text: str) -> str:
	marker = "- name: Enumerate open PRs and dispatch internal-review.yml"
	start = text.find(marker)
	assert start != -1, "Missing sweep step in review_autofix_sweep.yml"
	return text[start:]


def _zero_candidate_guard_block(block: str) -> str:
	guard_start = block.find('if [ "${total}" -eq 0 ]; then')
	assert guard_start != -1, "Missing zero-candidate fast-exit guard"
	guard_end = block.find("\n          fi", guard_start)
	assert guard_end != -1, "Could not bound zero-candidate fast-exit guard"
	guard_end += len("\n          fi")
	return block[guard_start:guard_end]


def test_zero_candidate_guard_precedes_active_run_snapshot() -> None:
	"""The `total == 0` branch must cut off the sweep before any active-run
	preflight work. Regressing this ordering would bring back the idle-repo
	Actions API fanout on every 30-minute tick."""
	block = _sweep_step_block(_review_autofix_sweep_text())
	start_log = block.find('echo "AUTOFIX_SWEEP_START')
	guard_start = block.find('if [ "${total}" -eq 0 ]; then')
	helper_start = block.find("snapshot_active_review_runs() {")
	workflow_runs_lookup = block.find('actions/workflows/${workflow}/runs')

	assert start_log != -1, "Missing AUTOFIX_SWEEP_START log line"
	assert guard_start != -1, "Missing zero-candidate fast-exit guard"
	assert helper_start != -1, "Missing active-run snapshot helper"
	assert workflow_runs_lookup != -1, "Missing active-run workflow-runs API lookup"
	assert start_log < guard_start < helper_start, (
		"Zero-candidate fast-exit must come after AUTOFIX_SWEEP_START but before "
		"snapshot_active_review_runs(), otherwise the idle sweep still builds or "
		"reaches the active-run preflight path."
	)
	assert guard_start < workflow_runs_lookup, (
		"Zero-candidate fast-exit must come before the `/actions/workflows/.../runs` "
		"lookup so empty sweeps return without any active-run GH API fanout."
	)


def test_zero_candidate_guard_preserves_summary_log_before_exit() -> None:
	"""The fast path should stay observability-compatible: emit the normal
	`AUTOFIX_SWEEP_END` counters, then exit 0. A bare early exit would create
	log drift for idle sweeps."""
	guard_block = _zero_candidate_guard_block(_sweep_step_block(_review_autofix_sweep_text()))
	assert 'echo "AUTOFIX_SWEEP_END dispatched=${dispatched} skipped_active=${skipped_active} skipped_filter=${skipped_filter} skipped_skip_ai=${skipped_skip_ai} failures=${failures} candidates=${total}"' in guard_block, (
		"Zero-candidate fast-exit must preserve the existing AUTOFIX_SWEEP_END "
		"summary vocabulary before returning."
	)
	assert "exit 0" in guard_block, "Zero-candidate fast-exit guard must return successfully"


def test_non_zero_path_still_snapshots_both_review_workflows() -> None:
	"""The optimisation must not disturb non-zero sweeps: they still need the
	existing active-run snapshot helper and the loop over both review-family
	workflows."""
	block = _sweep_step_block(_review_autofix_sweep_text())
	guard_start = block.find('if [ "${total}" -eq 0 ]; then')
	assert guard_start != -1, "Missing zero-candidate fast-exit guard"
	guard_end = block.find("\n          fi", guard_start)
	assert guard_end != -1, "Could not locate end of zero-candidate fast-exit guard"
	guard_end += len("\n          fi")
	non_zero_path = block[guard_end:]

	assert "snapshot_active_review_runs() {" in non_zero_path, (
		"Non-zero path lost the active-run snapshot helper. The optimisation must "
		"only bypass it for `total == 0`."
	)
	assert "for wf in internal-review.yml review_autofix.yml; do" in non_zero_path, (
		"Non-zero path must still snapshot both review-family workflows before the "
		"per-PR loop."
	)
	assert 'snapshot_active_review_runs "${wf}"' in non_zero_path, (
		"Non-zero path must still invoke the snapshot helper for each review-family "
		"workflow."
	)


def test_budget_steps_tolerate_older_support_without_helper(tmp_path: Path) -> None:
	"""An older checkout must not fail a job solely because budget logging is new."""
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts/gh_helpers.sh").write_text("# Older support without gh_pat_budget\n")
	for workflow_name, job_name, phases in (
		("clarify.yml", "clarify", ("end",)),
		("orchestrate_poll.yml", "poll", ("end",)),
		("review_autofix_sweep.yml", "sweep", ("start", "end")),
	):
		workflow = yaml.safe_load((REPO_ROOT / ".github/workflows" / workflow_name).read_text())
		for phase in phases:
			step = next(item for item in workflow["jobs"][job_name]["steps"]
				if item["name"] == f"Record GH_PAT budget at {job_name} {phase}")
			result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
				env={**os.environ, "GH_PAT_BUDGET_FILE": str(tmp_path / "budget")},
				capture_output=True, text=True)
			assert result.returncode == 0, (workflow_name, phase, result.stderr)
			assert f"GH_PAT_BUDGET phase={phase} workflow={workflow_name.removesuffix('.yml')} job={job_name} remaining=unknown" in result.stdout


def test_pat_budget_steps_bracket_every_active_job() -> None:
	workflows = {
		"clarify.yml": ("clarify",),
		"orchestrate_poll.yml": ("poll",),
		"review_autofix_sweep.yml": ("sweep",),
		"workflow-failure-heal-intake.yml": ("intake",),
		"validation-improvements-intake.yml": ("intake",),
		"review_autofix.yml": ("gate", "codex-agent", "post-merge-validate-dispatch",
			"post-merge-force-poll", "deterministic-skip-merge", "fingerprint-cap-block"),
	}
	with tempfile.TemporaryDirectory() as test_dir:
		bin_dir = Path(test_dir)
		gh = bin_dir / "gh"
		gh.write_text('''#!/usr/bin/env bash
if [ "$2" = "-i" ]; then
  printf '%s\\n' '{"resources":{"core":{"remaining":90,"reset":1000}}}'
else
  printf '100\\t1000\\n'
fi
''')
		gh.chmod(0o755)
		for file, jobs in workflows.items():
			workflow = yaml.safe_load((REPO_ROOT / ".github/workflows" / file).read_text())
			for name in jobs:
				steps = workflow["jobs"][name]["steps"]
				budget = [step for step in steps if step["name"].startswith("Record GH_PAT budget at ")]
				assert len(budget) == 2, (file, name)
				if file.endswith("-intake.yml"):
					assert steps[0] is budget[0], (file, name)
				assert budget[1]["if"] == "always()"
				assert budget[0]["env"].get("GH_TOKEN", workflow["jobs"][name].get("env", {}).get("GH_TOKEN")) == "${{ secrets.GH_PAT }}"
				for phase, step in zip(("start", "end"), budget):
					env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
						"GH_PAT_BUDGET_FILE": str(bin_dir / f"budget-{file}-{name}"), "GH_TOKEN": "fake"}
					result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=REPO_ROOT,
						env=env, capture_output=True, text=True)
					assert result.returncode == 0, (file, name, phase, result.stderr)
					assert f"GH_PAT_BUDGET phase={phase} workflow={file.removesuffix('.yml')} job={name} remaining=" in result.stdout
					assert " reset=" in result.stdout and " used_in_job=" in result.stdout


def test_heal_intake_budget_end_with_older_helper(tmp_path: Path) -> None:
	workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/workflow-failure-heal-intake.yml").read_text())
	step = next(item for item in workflow["jobs"]["intake"]["steps"]
		if item["name"] == "Record GH_PAT budget at intake end")
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts/gh_helpers.sh").write_text("# Older support without gh_pat_budget\n")
	result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
		env={**os.environ, "GH_PAT_BUDGET_FILE": str(tmp_path / "budget")}, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert "GH_PAT_BUDGET phase=end workflow=workflow-failure-heal-intake job=intake remaining=unknown" in result.stdout


def test_sweep_batches_only_verified_current_head_handoffs() -> None:
	"""Retired Claude hand-offs must not add GraphQL reads or suppress the sweep."""
	workflow = yaml.safe_load(REVIEW_AUTOFIX_SWEEP.read_text())
	job = workflow["jobs"]["sweep"]
	assert job["permissions"]["pull-requests"] == "read" and job["permissions"]["actions"] == "write"
	rerun = next(item for item in job["steps"] if item["name"] == "Re-run cancelled CI failed jobs once")
	assert rerun["env"]["GH_TOKEN"] == "${{ github.token }}"
	step = next(item for item in job["steps"] if item["name"].startswith("Enumerate open PRs"))
	assert step["env"]["READ_TOKEN"] == "${{ github.token }}"
	assert step["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	text = step["run"]
	assert 'GH_TOKEN="${READ_TOKEN}" gh api --paginate' in text
	assert 'gh api graphql' not in text
	assert 'reason=claude_fixer_awaiting_session' not in text


def test_sweep_handoff_skips_only_complete_trusted_same_head(tmp_path: Path) -> None:
	"""Legacy hand-off markers cannot prevent a successor review dispatch."""
	workflow = yaml.safe_load(REVIEW_AUTOFIX_SWEEP.read_text())
	body = next(step["run"] for step in workflow["jobs"]["sweep"]["steps"] if step["name"].startswith("Enumerate open PRs"))
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text('''#!/usr/bin/env bash
if [ "$1:$2" = "workflow:run" ]; then echo "$*" >> "$DISPATCH_LOG"; exit 0; fi
if [ "$1:$2" = "api:user" ]; then echo trusted; exit 0; fi
if [ "$1:$2" = "api:graphql" ]; then cat "$GRAPHQL"; exit 0; fi
case "$*" in
  *"/pulls"*) cat "$PRS" ;;
  *"/runs"*) echo '{"workflow_runs":[]}' ;;
  *) exit 1 ;;
esac
''')
	gh.chmod(0o755)
	prs = tmp_path / "prs.json"
	graphql = tmp_path / "graphql.json"
	dispatch_log = tmp_path / "dispatch.log"
	head = "a" * 40
	prs.write_text(json.dumps([{"number": 7, "draft": False, "head": {"ref": "claude/x", "sha": head},
		"title": "example", "body": ""}]))
	pr = {"number": 7, "headRefOid": head, "isDraft": False, "mergeStateStatus": "CLEAN", "title": "example",
		"labels": {"pageInfo": {"hasNextPage": False}, "nodes": []},
		"comments": {"pageInfo": {"hasPreviousPage": False}, "nodes": [{"author": {"login": "trusted"},
			"body": "## Review round 1: findings handed to the Claude session\n<!-- ai:claude-fixer-handoff:v1 kind=findings head=" + head + " round=1 -->"}]}}
	env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "pat", "READ_TOKEN": "read",
		"REPOSITORY": "o/r", "PRS": str(prs), "GRAPHQL": str(graphql), "DISPATCH_LOG": str(dispatch_log),
		"DRY_RUN": "false", "ALLOW_WORKFLOW_EDITS": "true", "SWEEP_STALE_QUEUED_MINUTES": "0",
		"HEAD_REF_FILTER": "", "RUNNER_TEMP": str(tmp_path)}
	def run(payload: dict) -> str:
		graphql.write_text(json.dumps(payload))
		dispatch_log.write_text("")
		result = subprocess.run(["bash", "-euo", "pipefail", "-c", body], env=env, capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		return dispatch_log.read_text()
	assert run({"data": {"repository": {"p7": pr}}})
	assert run({"data": {"repository": {"p7": {**pr, "comments": {"pageInfo": {"hasPreviousPage": True}, "nodes": pr["comments"]["nodes"]}}}}})
	assert run({"data": {"repository": {"p7": {**pr, "headRefOid": "b" * 40}}}})
	assert run({"data": {"repository": {"p7": pr}}, "errors": [{"message": "partial"}]})


def test_reclarify_failure_marker_and_idle_poller_permissions() -> None:
	clarify = yaml.safe_load((REPO_ROOT / ".github/workflows/clarify.yml").read_text())["jobs"]["clarify"]
	marker = next(step for step in clarify["steps"] if step["name"] == "Queue failed reclarify for poller")
	assert marker["env"]["GH_TOKEN"] == "${{ github.token }}"
	assert "failure()" in marker["if"] and "steps.post_clear.outcome != 'success'" in marker["if"]
	assert "SOURCE_COMMENT_ID" in marker["run"] and "ai:reclarify-requeue" in marker["run"]
	assert marker["run"].index('issues/${ISSUE_NUMBER}/comments') < marker["run"].index('issues/${ISSUE_NUMBER}/labels')
	assert 'for requeue_label_attempt in 1 2 3; do' in marker["run"]
	assert 'Failed to label reclarify request for poller replay' in marker["run"]
	for path in (".github/workflows/internal-clarify.yml", "workflow-templates/ai-clarify.yml"):
		assert yaml.safe_load((REPO_ROOT / path).read_text())["permissions"]["issues"] == "write"
	poller = yaml.safe_load((REPO_ROOT / ".github/workflows/orchestrate_poll.yml").read_text())["jobs"]["poll"]
	replay = next(step for step in poller["steps"] if step["name"] == "Replay failed trusted reclarify commands")
	assert "if" not in replay, "idle ticks must also run recovery"
	assert replay["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert 'RECLARIFY_REQUEUE_SWEEP_ONLY: "true"' in (REPO_ROOT / ".github/workflows/orchestrate_poll.yml").read_text()


def test_reclarify_creates_missing_discovery_label_after_marker(tmp_path: Path) -> None:
	"""A missing label must not strand a marker-only request on a new install."""
	clarify = yaml.safe_load((REPO_ROOT / ".github/workflows/clarify.yml").read_text())["jobs"]["clarify"]
	marker = next(step for step in clarify["steps"] if step["name"] == "Queue failed reclarify for poller")
	gh = tmp_path / "gh"
	gh.write_text('''#!/usr/bin/env bash
if [[ "$1:$2" == "api:-X" && "$*" == *"/comments"* ]]; then
  printf 'marker\\n' >> "$CALL_LOG"
elif [[ "$1:$2" == "api:-X" && "$*" == *"/labels"* ]]; then
  if [ -f "$LABEL_CREATED" ]; then
    printf 'add\\n' >> "$CALL_LOG"
  else
    printf 'missing\\n' >> "$CALL_LOG"
    exit 1
  fi
elif [ "$1:$2" = "label:create" ]; then
  printf 'create\\n' >> "$CALL_LOG"
  touch "$LABEL_CREATED"
else
  exit 1
fi
''')
	gh.chmod(0o755)
	log = tmp_path / "calls"
	env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GH_TOKEN": "fake",
		"ISSUE_NUMBER": "7", "SOURCE_COMMENT_ID": "11", "SOURCE_COMMENT_BODY": "/reclarify",
		"SOURCE_ASSOCIATION": "OWNER", "REPOSITORY": "o/r", "CALL_LOG": str(log),
		"LABEL_CREATED": str(tmp_path / "label-created")}
	result = subprocess.run(["bash", "-e", "-c", marker["run"]], env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert log.read_text().splitlines() == ["marker", "missing", "create", "add"]


if __name__ == "__main__":
	test_zero_candidate_guard_precedes_active_run_snapshot()
	test_zero_candidate_guard_preserves_summary_log_before_exit()
	test_non_zero_path_still_snapshots_both_review_workflows()
	with tempfile.TemporaryDirectory() as test_dir:
		test_budget_steps_tolerate_older_support_without_helper(Path(test_dir))
	test_pat_budget_steps_bracket_every_active_job()
	with tempfile.TemporaryDirectory() as test_dir:
		test_heal_intake_budget_end_with_older_helper(Path(test_dir))
	test_sweep_batches_only_verified_current_head_handoffs()
	with tempfile.TemporaryDirectory() as test_dir:
		test_sweep_handoff_skips_only_complete_trusted_same_head(Path(test_dir))
	test_reclarify_failure_marker_and_idle_poller_permissions()
	with tempfile.TemporaryDirectory() as test_dir:
		test_reclarify_creates_missing_discovery_label_after_marker(Path(test_dir))
	print("All review_autofix_sweep zero-candidate fast-exit contract tests passed.")
