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
	assert 'echo "AUTOFIX_SWEEP_END dispatched=${dispatched} skipped_active=${skipped_active} skipped_filter=${skipped_filter} skipped_skip_ai=${skipped_skip_ai} skipped_merge_queued=${skipped_merge_queued} failures=${failures} candidates=${total} closed_smoke=${closed_smoke}"' in guard_block, (
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


# Issue #9887: the trusted gh_helpers.sh copy each instrumented job's
# "Install gh call counter" step may source. Never a PR-head checkout.
GH_CALL_COUNTER_TRUSTED_HELPERS = {
	("clarify.yml", "clarify"): "${{ env.SUPPORT_SCRIPTS_DIR || 'scripts' }}/gh_helpers.sh",
	("clarify.yml", "heal-report"): ".codex-workflow-src/scripts/gh_helpers.sh",
	("orchestrate_poll.yml", "poll"): "scripts/gh_helpers.sh",
	("review_autofix_sweep.yml", "sweep"): "scripts/gh_helpers.sh",
	("workflow-failure-heal-intake.yml", "intake"): "scripts/gh_helpers.sh",
	("workflow-failure-heal-intake.yml", "recovery-scan"): "scripts/gh_helpers.sh",
	("workflow-failure-heal-intake.yml", "heal-verify"): "scripts/gh_helpers.sh",
	("validation-improvements-intake.yml", "intake"): "scripts/gh_helpers.sh",
	("review_autofix.yml", "gate"): "${{ env.GH_API_RETRY_HELPERS }}",
	("review_autofix.yml", "codex-agent"): "${{ env.SUPPORT_SCRIPTS_DIR }}/gh_helpers.sh",
	("review_autofix.yml", "post-merge-validate-dispatch"): ".codex-workflow-src/scripts/gh_helpers.sh",
	("review_autofix.yml", "post-merge-force-poll"): ".codex-workflow-src/scripts/gh_helpers.sh",
	("review_autofix.yml", "deterministic-skip-merge"): ".codex-freshness-src/scripts/gh_helpers.sh",
	("review_autofix.yml", "fingerprint-cap-block"): ".codex-workflow-src/scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "e2e-smoke-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "e2e-alt-model-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "validate-standalone-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "workflow-log-analysis-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "orphan-workflows-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "orchestrate-decompose-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "clarify-rejects-unsolvable-test"): "scripts/gh_helpers.sh",
	("test-and-mark-stable.yml", "validate"): "scripts/gh_helpers.sh",
}
# The review pipeline contract forbids continue-on-error in this job; the
# install body still ends every path in exit 0.
GH_CALL_COUNTER_NO_CONTINUE_ON_ERROR_JOBS = {("review_autofix.yml", "post-merge-validate-dispatch")}
# Jobs that hold no GH_PAT: their budget steps must make no gh call (#9887 D6).
GH_PAT_BUDGET_NO_PAT_JOBS = {("test-and-mark-stable.yml", "validate")}


def test_pat_budget_steps_bracket_every_active_job() -> None:
	workflows: dict[str, list[str]] = {}
	for file, name in GH_CALL_COUNTER_TRUSTED_HELPERS:
		workflows.setdefault(file, []).append(name)
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
		no_gh_dir = bin_dir / "no-gh"
		no_gh_dir.mkdir()
		forbidden = no_gh_dir / "gh"
		forbidden.write_text(f'#!/usr/bin/env bash\necho called >> "{bin_dir}/forbidden-calls"\nexit 1\n')
		forbidden.chmod(0o755)
		# A counter dir staged by the real installer, used to check the end keys.
		runner_temp = bin_dir / "runner"
		runner_temp.mkdir()
		install = subprocess.run(["bash", "-c", f'source "{REPO_ROOT}/scripts/gh_helpers.sh"; gh_call_counter_install'],
			env={**os.environ, "RUNNER_TEMP": str(runner_temp), "GITHUB_PATH": ""}, capture_output=True, text=True)
		assert install.returncode == 0, install.stderr
		counter_dir = next(path for path in runner_temp.glob("gh_call_counter.*") if path.is_dir())
		counter_log = bin_dir / "counter.log"
		for file, jobs in workflows.items():
			workflow = yaml.safe_load((REPO_ROOT / ".github/workflows" / file).read_text())
			for name in jobs:
				job = workflow["jobs"][name]
				steps = job["steps"]
				budget = [step for step in steps if step.get("name", "").startswith("Record GH_PAT budget at ")]
				assert len(budget) == 2, (file, name)
				if file.endswith("-intake.yml"):
					assert steps[0] is budget[0], (file, name)
				assert budget[1]["if"] == "always()"
				no_pat = (file, name) in GH_PAT_BUDGET_NO_PAT_JOBS
				if no_pat:
					assert "GH_TOKEN" not in job.get("env", {}), (file, name)
					assert all("GH_TOKEN" not in step.get("env", {}) for step in budget), (file, name)
				else:
					assert budget[0]["env"].get("GH_TOKEN", job.get("env", {}).get("GH_TOKEN")) == "${{ secrets.GH_PAT }}"
				installs = [step for step in steps if step.get("name") == "Install gh call counter"]
				assert len(installs) == 1, (file, name)
				install_step = installs[0]
				assert steps.index(install_step) < steps.index(budget[1]), (file, name)
				if (file, name) in GH_CALL_COUNTER_NO_CONTINUE_ON_ERROR_JOBS:
					assert "continue-on-error" not in install_step, (file, name)
				else:
					assert install_step["continue-on-error"] is True, (file, name)
				assert install_step["run"].rstrip().endswith("exit 0"), (file, name)
				assert set(install_step["env"]) == {"GH_CALL_COUNTER_ENABLED", "GH_CALL_COUNTER_HELPERS"}, (file, name)
				assert install_step["env"]["GH_CALL_COUNTER_ENABLED"] == "${{ vars.GH_CALL_COUNTER_ENABLED || 'true' }}"
				helpers = install_step["env"]["GH_CALL_COUNTER_HELPERS"]
				assert helpers == GH_CALL_COUNTER_TRUSTED_HELPERS[(file, name)], (file, name, helpers)
				assert "skip-pr-head" not in helpers and "secrets." not in str(install_step["env"])
				assert "gh_call_counter_install" in install_step["run"]
				for phase, step in zip(("start", "end"), budget):
					path_dirs = f"{no_gh_dir}" if no_pat else f"{bin_dir}"
					env = {**os.environ, "PATH": f"{path_dirs}:{os.environ['PATH']}",
						"GH_PAT_BUDGET_FILE": str(bin_dir / f"budget-{file}-{name}"), "GH_TOKEN": "fake"}
					result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=REPO_ROOT,
						env=env, capture_output=True, text=True)
					assert result.returncode == 0, (file, name, phase, result.stderr)
					assert f"GH_PAT_BUDGET phase={phase} workflow={file.removesuffix('.yml')} job={name} remaining=" in result.stdout
					assert " reset=" in result.stdout and " used_in_job=" in result.stdout
					if phase == "start":
						assert "calls_in_job=" not in result.stdout, (file, name)
						continue
					assert " calls_in_job=unknown paginated_in_job=unknown calls_by_token=unknown" in result.stdout, (file, name)
					# With the counter first on PATH the end line carries this job's count.
					counter_log.write_text("v1 cmd=api paginate=0 token=pat\nv1 cmd=pr paginate=1 token=github_token\n")
					counted = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=REPO_ROOT,
						env={**env, "PATH": f"{counter_dir}:{env['PATH']}", "GH_CALL_COUNTER_LOG": str(counter_log)},
						capture_output=True, text=True)
					assert counted.returncode == 0, (file, name, counted.stderr)
					assert (" calls_in_job=2 paginated_in_job=1 calls_by_token=pat:1,github_token:1,other:0"
						in counted.stdout), (file, name, counted.stdout)
		assert not (bin_dir / "forbidden-calls").exists(), "a no-PAT budget step called gh"


def test_install_gh_call_counter_step_installs_and_tolerates_missing_helper(tmp_path: Path) -> None:
	workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/orchestrate_poll.yml").read_text())
	step = next(item for item in workflow["jobs"]["poll"]["steps"] if item.get("name") == "Install gh call counter")
	github_path = tmp_path / "github_path"
	github_path.write_text("")
	(tmp_path / "runner").mkdir()
	base = {**os.environ, "RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_PATH": str(github_path),
		"GH_CALL_COUNTER_ENABLED": "true"}
	missing = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
		env={**base, "GH_CALL_COUNTER_HELPERS": str(tmp_path / "nope.sh")}, capture_output=True, text=True)
	assert missing.returncode == 0 and github_path.read_text() == ""
	(tmp_path / "old.sh").write_text("# Older support without gh_call_counter_install\n")
	older = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
		env={**base, "GH_CALL_COUNTER_HELPERS": str(tmp_path / "old.sh")}, capture_output=True, text=True)
	assert older.returncode == 0 and github_path.read_text() == ""
	result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
		env={**base, "GH_CALL_COUNTER_HELPERS": str(REPO_ROOT / "scripts/gh_helpers.sh")}, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	persisted = github_path.read_text().splitlines()
	assert len(persisted) == 1 and "/gh_call_counter." in persisted[0]
	assert (Path(persisted[0]) / "gh").is_file()


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
  "api repos/o/r --jq .default_branch") echo main ;;
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
		test_install_gh_call_counter_step_installs_and_tolerates_missing_helper(Path(test_dir))
	with tempfile.TemporaryDirectory() as test_dir:
		test_heal_intake_budget_end_with_older_helper(Path(test_dir))
	test_sweep_batches_only_verified_current_head_handoffs()
	with tempfile.TemporaryDirectory() as test_dir:
		test_sweep_handoff_skips_only_complete_trusted_same_head(Path(test_dir))
	test_reclarify_failure_marker_and_idle_poller_permissions()
	with tempfile.TemporaryDirectory() as test_dir:
		test_reclarify_creates_missing_discovery_label_after_marker(Path(test_dir))
	print("All review_autofix_sweep zero-candidate fast-exit contract tests passed.")
