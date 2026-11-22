#!/usr/bin/env python3
"""Contract of `.github/workflows/pr-checks.yml`, the folded per-PR checks.

One job does the work that `lint-pr-body-auto-close.yml` and, for every
event but a label change, `integration-pr-readiness.yml` did in a job each
(docs/plans/fold-per-pr-checks-plan.md, CLAUDE.md §33.A). The names that
carry a verdict must not move: the check-run name `pr-body-lint` (in
ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT) and the commit status the readiness
script posts. The step contents of the lint are pinned in
tests/test_lint_pr_body_auto_close.py; this file pins the shape of the fold.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PR_CHECKS_WORKFLOW = WORKFLOWS_DIR / "pr-checks.yml"
READINESS_LABEL_WORKFLOW = WORKFLOWS_DIR / "integration-pr-readiness.yml"
FOLDED_JOB_ID = "pr-body-lint"
LABEL_EVENT_TYPES = ["labeled", "unlabeled"]

# Step ids of each group. A step may depend on the checkout and on steps of
# its own group, never on a step of the other group.
LINT_GROUP_STEP_IDS = {"pr_body_file", "pr_commit_messages"}
READINESS_GROUP_STEP_IDS = {"pr_labels_file"}
# Issue #9929: CLAUDE.md §15 loop-call lint, warning only.
GH_LOOP_LINT_GROUP_STEP_IDS = {"gh_loop_call_lint"}
EXPECTED_STEP_NAMES = [
	"Checkout repository",
	"Write PR title and body to file",
	"Write PR commit messages to file",
	"Run auto-close keyword lint",
	"Write PR labels to file",
	"Check integration PR readiness and post status",
	"Lint new GitHub API calls inside loops (CLAUDE.md §15, warning only)",
]
LINT_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[1:4]
READINESS_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[4:6]
GH_LOOP_LINT_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[6:7]
ALL_GROUP_STEP_IDS = LINT_GROUP_STEP_IDS | READINESS_GROUP_STEP_IDS | GH_LOOP_LINT_GROUP_STEP_IDS


def _load_workflow(path: Path) -> dict:
	return yaml.safe_load(path.read_text(encoding="utf-8"))


def _folded_job() -> dict:
	return _load_workflow(PR_CHECKS_WORKFLOW)["jobs"][FOLDED_JOB_ID]


def _expression_body(expression: str) -> str:
	"""The text between `${{` and `}}`, whitespace collapsed."""
	collapsed = " ".join(expression.split())
	assert collapsed.startswith("${{ ") and collapsed.endswith(" }}"), collapsed
	return collapsed[len("${{ "):-len(" }}")]


def test_lint_workflow_file_is_gone() -> None:
	assert not (WORKFLOWS_DIR / "lint-pr-body-auto-close.yml").exists()


def test_exactly_one_job_with_the_verdict_name() -> None:
	workflow = _load_workflow(PR_CHECKS_WORKFLOW)
	assert workflow["name"] == "PR checks"
	assert list(workflow["jobs"]) == [FOLDED_JOB_ID]
	job = workflow["jobs"][FOLDED_JOB_ID]
	assert "name" not in job, "a job-level name: would change the check-run name"
	assert job["runs-on"] == "ubuntu-latest"
	assert job["timeout-minutes"] == 10


def test_job_is_never_skipped() -> None:
	"""A job skipped by `if:` reports a `skipped` check-run. GitHub and
	_pr_ruleset_required_check_blockers read the newest check-run of a name
	and count `skipped` as passed, so a skipped `pr-body-lint` after a failed
	one on the same head would hide a §19 violation."""
	assert "if" not in _folded_job()


def test_label_events_stay_in_the_readiness_workflow() -> None:
	"""Label events fire on an unchanged head, many times per PR. They must
	not start (or skip) the job that carries the lint verdict."""
	pull_request = _load_workflow(PR_CHECKS_WORKFLOW)["on"]["pull_request"]
	assert pull_request["types"] == ["opened", "edited", "synchronize", "reopened", "ready_for_review"]
	readiness = _load_workflow(READINESS_LABEL_WORKFLOW)
	assert readiness["on"]["pull_request"]["types"] == LABEL_EVENT_TYPES
	assert readiness["on"]["pull_request"]["branches"] == pull_request["branches"]
	# No event starts both workflows, so no event takes two runners.
	assert not set(pull_request["types"]) & set(LABEL_EVENT_TYPES)
	assert list(readiness["jobs"]) == ["readiness"]


def test_trigger_has_no_path_filter_and_no_privileged_event() -> None:
	trigger = _load_workflow(PR_CHECKS_WORKFLOW)["on"]
	assert list(trigger) == ["pull_request"], "pull_request only: never pull_request_target"
	pull_request = trigger["pull_request"]
	assert pull_request["branches"] == ["main", "stable"]
	assert "paths" not in pull_request and "paths-ignore" not in pull_request


def test_permissions_are_the_union_and_nothing_more() -> None:
	workflow = _load_workflow(PR_CHECKS_WORKFLOW)
	assert workflow["permissions"] == {
		"contents": "read",
		"issues": "read",
		"pull-requests": "read",
		"statuses": "write",
	}
	assert workflow["permissions"] == _load_workflow(READINESS_LABEL_WORKFLOW)["permissions"]
	assert "permissions" not in workflow["jobs"][FOLDED_JOB_ID]
	text = PR_CHECKS_WORKFLOW.read_text(encoding="utf-8")
	assert "secrets." not in text, "the folded checks run PR-controlled code and take no secret"


def test_steps_keep_their_names_and_order() -> None:
	assert [step["name"] for step in _folded_job()["steps"]] == EXPECTED_STEP_NAMES


def test_every_step_after_checkout_survives_another_groups_failure() -> None:
	steps = _folded_job()["steps"]
	assert steps[0]["id"] == "checkout" and "if" not in steps[0]
	for step in steps[1:]:
		body = _expression_body(step["if"])
		assert body.startswith("!cancelled() && "), (step["name"], body)
		assert "always()" not in body and "failure()" not in body, step["name"]


def test_step_groups_do_not_depend_on_each_other() -> None:
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	for names, own_ids in (
		(LINT_GROUP_STEP_NAMES, LINT_GROUP_STEP_IDS),
		(READINESS_GROUP_STEP_NAMES, READINESS_GROUP_STEP_IDS),
		(GH_LOOP_LINT_GROUP_STEP_NAMES, GH_LOOP_LINT_GROUP_STEP_IDS),
	):
		foreign_ids = ALL_GROUP_STEP_IDS - own_ids
		for name in names:
			referenced = set(re.findall(r"steps\.([A-Za-z0-9_]+)\.", by_name[name]["if"]))
			assert not (referenced & foreign_ids), (name, referenced)
			assert referenced <= {"checkout"} | ALL_GROUP_STEP_IDS, (name, referenced)
	# Within a group a step still waits for the step it reads from.
	assert "steps.checkout.outcome == 'success'" in by_name["Write PR title and body to file"]["if"]
	assert "steps.checkout.outcome == 'success'" in by_name["Write PR labels to file"]["if"]
	assert "steps.pr_body_file.outcome == 'success'" in by_name["Run auto-close keyword lint"]["if"]
	assert "steps.pr_commit_messages.outcome != 'failure'" in by_name["Run auto-close keyword lint"]["if"]
	assert "steps.pr_labels_file.outcome == 'success'" in by_name["Check integration PR readiness and post status"]["if"]


def test_only_the_lint_can_fail_the_job() -> None:
	"""The job result is the §19 verdict the merge gate reads. The readiness
	verdict is a commit status; a failed tracking-issue read or status POST
	(a fork PR's token is read-only) must not turn `pr-body-lint` red."""
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	for name in [*READINESS_GROUP_STEP_NAMES, *GH_LOOP_LINT_GROUP_STEP_NAMES]:
		assert by_name[name].get("continue-on-error") is True, name
	for name in ["Checkout repository", *LINT_GROUP_STEP_NAMES]:
		assert "continue-on-error" not in by_name[name], name


def test_readiness_step_matches_the_label_workflow() -> None:
	"""Both workflows call the readiness script the same way, so the status
	means the same whichever event posted it."""
	label_steps = {step["name"]: step for step in _load_workflow(READINESS_LABEL_WORKFLOW)["jobs"]["readiness"]["steps"]}
	folded_steps = {step["name"]: step for step in _folded_job()["steps"]}
	for name in READINESS_GROUP_STEP_NAMES:
		assert folded_steps[name]["run"] == label_steps[name]["run"], name
		assert folded_steps[name]["env"] == label_steps[name]["env"], name


def test_step_ids_are_unique() -> None:
	step_ids = [step["id"] for step in _folded_job()["steps"] if "id" in step]
	assert len(step_ids) == len(set(step_ids)), step_ids
	assert set(step_ids) == {"checkout"} | ALL_GROUP_STEP_IDS


def test_gh_loop_call_lint_step_is_warning_only_and_makes_no_api_call() -> None:
	"""Issue #9929: the §15 lint reads the local git diff only. It takes no
	token, never fails the job and runs on every event of this workflow
	(including `edited`, whose newer check-run would otherwise drop the
	warnings)."""
	(step,) = [s for s in _folded_job()["steps"] if s["name"] in GH_LOOP_LINT_GROUP_STEP_NAMES]
	assert step["id"] == "gh_loop_call_lint"
	assert _expression_body(step["if"]) == "!cancelled() && steps.checkout.outcome == 'success'"
	assert "github.event.action" not in step["if"]
	assert step.get("continue-on-error") is True
	# scripts/lint_gh_calls_in_loops.py stops reading files after one
	# `git show` timeout so its summary line prints inside this step timeout.
	assert step["timeout-minutes"] == 5
	assert "env" not in step, "no GH_TOKEN: the lint makes no GitHub API call"
	run = step["run"]
	assert "python3 scripts/lint_gh_calls_in_loops.py" in run
	assert "git fetch --no-tags --depth=2 origin" in run
	assert '--base "${head_commit}^1" --head "${head_commit}" || true' in run
	assert "GH_TOKEN" not in run
	assert not re.search(r"(?<![\w-])gh\s", run), "no gh CLI call in the lint step"
	assert "curl" not in run
	checkout = _folded_job()["steps"][0]
	assert checkout["with"]["fetch-depth"] == 1, "the lint deepens the merge commit itself"


def test_run_bodies_carry_no_expressions() -> None:
	"""PR-controlled values (title, body, labels, head ref) reach the shell
	through `env:` only, never through `${{ }}` inside a run body."""
	for step in _folded_job()["steps"]:
		assert "${{" not in step.get("run", ""), step["name"]


def test_readiness_steps_skip_title_and_body_edits() -> None:
	"""The readiness check never ran on `edited`; an edit changes nothing
	the script reads, and a refresh from the event's label snapshot could
	undo a label change the label workflow has already acted on."""
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	assert "github.event.action != 'edited'" in by_name["Write PR labels to file"]["if"]
	for name in LINT_GROUP_STEP_NAMES:
		assert "github.event.action" not in by_name[name]["if"], name


def test_no_concurrency_group_without_revisiting_the_readiness_steps() -> None:
	"""A group lets a newer run replace a waiting one. That is safe only if
	every run performs both checks, and the readiness steps skip `edited`.
	Anyone adding a group must change both together (see the job comment)."""
	workflow = _load_workflow(PR_CHECKS_WORKFLOW)
	assert "concurrency" not in workflow
	assert "concurrency" not in workflow["jobs"][FOLDED_JOB_ID]


def test_no_other_workflow_reports_the_verdict_name() -> None:
	"""`pr-body-lint` is in the merge gate's required set; a second job
	reporting that name would be read as this one. Covers a job id and a
	job-level `name:` in the workflows and the consumer templates."""
	owners = []
	for directory in (WORKFLOWS_DIR, REPO_ROOT / "workflow-templates"):
		for path in sorted(directory.glob("*.y*ml")):
			jobs_text = path.read_text(encoding="utf-8").partition("\njobs:\n")[2]
			if re.search(rf"^  {re.escape(FOLDED_JOB_ID)}:", jobs_text, flags=re.M) or re.search(
				rf"^    name:\s*['\"]?{re.escape(FOLDED_JOB_ID)}['\"]?\s*$", jobs_text, flags=re.M
			):
				owners.append(path.relative_to(REPO_ROOT).as_posix())
	assert owners == [".github/workflows/pr-checks.yml"], owners


def main() -> int:
	test_funcs = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}", flush=True)
		except Exception as exc:  # noqa: BLE001 - report every failing case
			print(f"  FAIL  {func.__name__}: {exc}", flush=True)
			failed += 1
	print(f"\n{len(test_funcs) - failed} passed, {failed} failed, {len(test_funcs)} total", flush=True)
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
