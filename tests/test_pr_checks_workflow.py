#!/usr/bin/env python3
"""Contract of `.github/workflows/pr-checks.yml`, the folded per-PR checks.

One job does the work that `lint-pr-body-auto-close.yml`,
`lint-plan-archival.yml` and, for every
event but a label change, `integration-pr-readiness.yml` did in a job each
(docs/plans/fold-per-pr-checks-plan.md, CLAUDE.md §33.A). The names that
carry a verdict must not move: the check-run name `pr-body-lint` (in
ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT) and the commit status the readiness
script posts. The step contents of the lint are pinned in
tests/test_lint_pr_body_auto_close.py; this file pins the shape of the fold.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
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
ARCHIVAL_GROUP_STEP_IDS = {"plan_archival_added_files", "plan_archival_body_file"}
READINESS_GROUP_STEP_IDS = {"pr_labels_file"}
# Issue #9929: CLAUDE.md §15 loop-call lint, warning only.
GH_LOOP_LINT_GROUP_STEP_IDS = {"gh_loop_call_lint"}
EXPECTED_STEP_NAMES = [
	"Checkout repository",
	"Write PR title and body to file",
	"Write PR commit messages to file",
	"Run auto-close keyword lint",
	"Enumerate added files in PR",
	"Write PR body to file",
	"Run plan-archival completeness lint",
	"Write PR labels to file",
	"Check integration PR readiness and post status",
	"Lint new GitHub API calls inside loops (CLAUDE.md §15, warning only)",
]
LINT_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[1:4]
ARCHIVAL_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[4:7]
READINESS_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[7:9]
GH_LOOP_LINT_GROUP_STEP_NAMES = EXPECTED_STEP_NAMES[9:10]
ALL_GROUP_STEP_IDS = (
	LINT_GROUP_STEP_IDS | ARCHIVAL_GROUP_STEP_IDS | READINESS_GROUP_STEP_IDS | GH_LOOP_LINT_GROUP_STEP_IDS
)
ARCHIVAL_SCRATCH_DIR = "/tmp/plan-archival-lint"


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


def test_plan_archival_workflow_file_is_gone() -> None:
	"""Its job was named `lint`, the check-run name the `main` ruleset
	requires and means ci.yml's aggregate job by."""
	assert not (WORKFLOWS_DIR / "lint-plan-archival.yml").exists()


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
		(ARCHIVAL_GROUP_STEP_NAMES, ARCHIVAL_GROUP_STEP_IDS),
		(READINESS_GROUP_STEP_NAMES, READINESS_GROUP_STEP_IDS),
		(GH_LOOP_LINT_GROUP_STEP_NAMES, GH_LOOP_LINT_GROUP_STEP_IDS),
	):
		foreign_ids = ALL_GROUP_STEP_IDS - own_ids
		for name in names:
			referenced = set(re.findall(r"steps\.([A-Za-z0-9_]+)\.", by_name[name]["if"]))
			assert not (referenced & foreign_ids), (name, referenced)
			assert referenced <= {"checkout"} | own_ids, (name, referenced)
	# Within a group a step still waits for the step it reads from.
	assert "steps.checkout.outcome == 'success'" in by_name["Write PR title and body to file"]["if"]
	assert "steps.checkout.outcome == 'success'" in by_name["Write PR labels to file"]["if"]
	assert "steps.checkout.outcome == 'success'" in by_name["Enumerate added files in PR"]["if"]
	assert "steps.plan_archival_added_files.outcome == 'success'" in by_name["Write PR body to file"]["if"]
	assert "steps.plan_archival_body_file.outcome == 'success'" in by_name["Run plan-archival completeness lint"]["if"]
	assert "steps.pr_body_file.outcome == 'success'" in by_name["Run auto-close keyword lint"]["if"]
	assert "steps.pr_commit_messages.outcome != 'failure'" in by_name["Run auto-close keyword lint"]["if"]
	assert "steps.pr_labels_file.outcome == 'success'" in by_name["Check integration PR readiness and post status"]["if"]


def test_only_the_lint_can_fail_the_job() -> None:
	"""The job result carries the two lint verdicts the merge gate reads
	(§19 PR-body lint, plan-archival lint). The readiness
	verdict is a commit status; a failed tracking-issue read or status POST
	(a fork PR's token is read-only) must not turn `pr-body-lint` red."""
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	for name in [*READINESS_GROUP_STEP_NAMES, *GH_LOOP_LINT_GROUP_STEP_NAMES]:
		assert by_name[name].get("continue-on-error") is True, name
	for name in ["Checkout repository", *LINT_GROUP_STEP_NAMES, *ARCHIVAL_GROUP_STEP_NAMES]:
		assert "continue-on-error" not in by_name[name], name


def test_checkout_depth_is_the_merge_commit_and_its_parents() -> None:
	"""The plan-archival group diffs the merge commit against its first
	parent, which needs depth 2 and no more."""
	checkout = _folded_job()["steps"][0]
	assert checkout["uses"].startswith("actions/checkout@")
	assert checkout["with"] == {"fetch-depth": 2}


def test_plan_archival_lint_runs_only_for_added_completed_files() -> None:
	"""The step condition replaces the old workflow's `paths:` filter. The
	enumerate step runs on every event (also `edited`: the PR body is an
	input of the lint), and the lint steps only when it reports a file
	added under docs/completed/."""
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	enumerate_step = by_name["Enumerate added files in PR"]
	assert _expression_body(enumerate_step["if"]) == "!cancelled() && steps.checkout.outcome == 'success'"
	assert "steps.plan_archival_added_files.outputs.needed == 'true'" in by_name["Write PR body to file"]["if"]
	for name in ARCHIVAL_GROUP_STEP_NAMES:
		assert "github.event.action" not in by_name[name]["if"], name
	assert enumerate_step["env"] == {"PLAN_ARCHIVAL_HEAD_SHA": "${{ github.event.pull_request.head.sha }}"}, (
		"the enumerate step reads the checkout and the event's head SHA only: no token, no API call"
	)
	lint_step = by_name["Run plan-archival completeness lint"]
	assert lint_step["env"] == {"GH_TOKEN": "${{ github.token }}"}
	assert "scripts/lint_plan_archival_completeness.py" in lint_step["run"]
	assert f"--pr-body-file {ARCHIVAL_SCRATCH_DIR}/body.txt" in lint_step["run"]
	assert f"--added-files-file {ARCHIVAL_SCRATCH_DIR}/added.txt" in lint_step["run"]


def test_plan_archival_failure_names_itself() -> None:
	"""A red `pr-body-lint` otherwise means a §19 violation. Every failing
	exit of the plan-archival group prints an `::error` line that names the
	plan-archival lint and says it is not the PR-body lint."""
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	for name in ("Enumerate added files in PR", "Run plan-archival completeness lint"):
		body = by_name[name]["run"]
		error_lines = [line for line in body.splitlines() if "::error" in line]
		assert error_lines, name
		exit_lines = [line for line in body.splitlines() if re.match(r"\s*exit ", line)]
		assert len(exit_lines) == len(error_lines), name
		for line in error_lines:
			assert "::error title=Plan-archival completeness lint (not the PR-body lint)::[lint_plan_archival] outcome=" in line, line
	lint_body = by_name["Run plan-archival completeness lint"]["run"]
	assert 'exit "${plan_archival_rc}"' in lint_body, "the lint script's exit code is the step's"


def _git_test_env() -> dict:
	"""The caller's environment without any GIT_* variable (a hook or
	`git rebase --exec` exports GIT_DIR and GIT_INDEX_FILE, which would point
	these commands at the real repository), plus a fixed identity."""
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update({
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
		"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
	})
	return env


def _git(repo_dir: Path, *args: str) -> str:
	result = subprocess.run(
		["git", *args], cwd=repo_dir, env=_git_test_env(),
		capture_output=True, text=True, timeout=60, check=True,
	)
	return result.stdout.strip()


def _run_enumerate_step(repo_dir: Path, scratch_dir: Path, event_head_sha: str | None = None) -> tuple[int, str, str]:
	"""Run the enumerate step's shell body in repo_dir. Returns (exit code,
	combined output, GITHUB_OUTPUT text). event_head_sha defaults to the
	tip of the PR branch, as in a `pull_request` event."""
	by_name = {step["name"]: step for step in _folded_job()["steps"]}
	body = by_name["Enumerate added files in PR"]["run"]
	assert ARCHIVAL_SCRATCH_DIR in body
	body = body.replace(ARCHIVAL_SCRATCH_DIR, str(scratch_dir / "plan-archival-lint"))
	github_output = scratch_dir / "github_output.txt"
	github_output.write_text("", encoding="utf-8")
	env = _git_test_env()
	env["GITHUB_OUTPUT"] = str(github_output)
	env["PLAN_ARCHIVAL_HEAD_SHA"] = event_head_sha or _git(repo_dir, "rev-parse", "pr-branch")
	result = subprocess.run(
		["bash", "-c", body], cwd=repo_dir, env=env, capture_output=True, text=True, timeout=60, check=False,
	)
	return result.returncode, result.stdout + result.stderr, github_output.read_text(encoding="utf-8")


def _merge_commit_repo(root: Path, head_changes) -> Path:
	"""A repository shaped like the `pull_request` checkout: a base commit
	on `main`, one more base commit, a PR branch from the first, and HEAD on
	the merge commit of the PR branch into `main`."""
	repo_dir = root / "repo"
	repo_dir.mkdir()
	_git(repo_dir, "init", "-q", "-b", "main")
	(repo_dir / "docs" / "plans").mkdir(parents=True)
	(repo_dir / "docs" / "completed").mkdir(parents=True)
	(repo_dir / "docs" / "plans" / "moved-plan.md").write_text("# plan\n\nbody line\n" * 20, encoding="utf-8")
	(repo_dir / "docs" / "completed" / "old.md").write_text("old\n", encoding="utf-8")
	_git(repo_dir, "add", "-A")
	_git(repo_dir, "commit", "-q", "-m", "base")
	_git(repo_dir, "checkout", "-q", "-b", "pr-branch")
	head_changes(repo_dir)
	_git(repo_dir, "add", "-A")
	_git(repo_dir, "commit", "-q", "-m", "head")
	_git(repo_dir, "checkout", "-q", "main")
	# A file added on the base after the branch point is not the PR's.
	(repo_dir / "docs" / "completed" / "base-only.md").write_text("base\n", encoding="utf-8")
	_git(repo_dir, "add", "-A")
	_git(repo_dir, "commit", "-q", "-m", "base moves on")
	_git(repo_dir, "merge", "-q", "--no-ff", "-m", "merge", "pr-branch")
	return repo_dir


def test_enumerate_step_lists_what_the_pr_adds() -> None:
	def _moves_a_plan_and_adds_code(repo_dir: Path) -> None:
		# `git mv` into docs/completed/ is an archival: git reports it as
		# a rename, which the old workflow's `--diff-filter=A` dropped.
		_git(repo_dir, "mv", "docs/plans/moved-plan.md", "docs/completed/moved-plan.md")
		(repo_dir / "docs" / "completed" / "plän.md").write_text("new\n", encoding="utf-8")
		(repo_dir / "new_code.py").write_text("x = 1\n", encoding="utf-8")
		# A rename inside docs/completed/ is not an archival.
		_git(repo_dir, "mv", "docs/completed/old.md", "docs/completed/renamed.md")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _moves_a_plan_and_adds_code)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 0, output
		added = (Path(tmp) / "plan-archival-lint" / "added.txt").read_text(encoding="utf-8").splitlines()
		assert sorted(added) == ["docs/completed/moved-plan.md", "docs/completed/plän.md", "new_code.py"], added
		assert github_output == "needed=true\n", github_output
		assert "[lint_plan_archival] outcome=enumerated added_files=3 lint_needed=true" in output, output


def test_enumerate_step_skips_the_lint_without_an_added_completed_file() -> None:
	def _edits_and_deletes_only(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "old.md").write_text("edited\n", encoding="utf-8")
		(repo_dir / "new_code.py").write_text("x = 1\n", encoding="utf-8")
		# Moving a plan out of docs/completed/ is not an archival either.
		_git(repo_dir, "mv", "docs/plans/moved-plan.md", "docs/moved-plan.md")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _edits_and_deletes_only)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 0, output
		assert github_output == "needed=false\n", github_output
		assert "lint_needed=false" in output, output


def test_enumerate_step_fails_closed_without_a_merge_commit() -> None:
	def _adds_a_completed_plan(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "x.md").write_text("new\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_a_completed_plan)
		_git(repo_dir, "checkout", "-q", "--detach", "pr-branch")
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 1, output
		assert "outcome=not_a_merge_commit" in output, output
		assert github_output == "", "a failed enumerate step must not report needed=false"


def test_enumerate_step_fails_closed_on_a_stale_merge_commit() -> None:
	"""A merge ref GitHub has not rebuilt merges the previous head: its diff
	would miss what the new push added."""
	def _adds_a_completed_plan(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "x.md").write_text("new\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_a_completed_plan)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp), event_head_sha="0" * 40)
		assert rc == 1, output
		assert "outcome=stale_merge_commit" in output, output
		assert github_output == ""


def test_enumerate_step_fails_closed_on_an_unlistable_path() -> None:
	"""The lint script reads one path per line. A line break in the name of
	a file added under docs/completed/ cannot be passed that way, also when
	another added file there is listable."""
	def _adds_a_plan_with_a_newline_in_its_name(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "a\nb.md").write_text("new\n", encoding="utf-8")
		(repo_dir / "docs" / "completed" / "ok.md").write_text("fine\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_a_plan_with_a_newline_in_its_name)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 1, output
		assert "outcome=enumerate_failed exit_code=3" in output, output
		assert "has a line break in its name or a name that is not valid UTF-8; rename it" in output, output
		assert github_output == ""


def test_enumerate_step_failure_other_than_exit_3_gives_no_rename_advice() -> None:
	"""Only the parser's own exit 3 means a file must be renamed. Any other
	failure (here: added.txt cannot be written) names no file-name cause."""
	def _adds_a_completed_plan(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "x.md").write_text("new\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_a_completed_plan)
		# A directory where the parser writes added.txt makes its write fail.
		(Path(tmp) / "plan-archival-lint" / "added.txt").mkdir(parents=True)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 1, output
		assert "outcome=enumerate_failed exit_code=" in output, output
		assert "outcome=enumerate_failed exit_code=3" not in output, output
		assert "rename it" not in output, output
		assert "the enumerate script failed" in output, output
		assert github_output == ""


def test_enumerate_step_fails_closed_on_a_unicode_line_separator() -> None:
	"""The lint script reads the list with str.splitlines(), which also
	splits on U+2028 and U+0085. Such a name under docs/completed/ takes the
	guided exit-3 path instead of reaching the lint as fragments."""
	def _adds_a_plan_with_a_line_separator(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "a\u2028b.md").write_text("new\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_a_plan_with_a_line_separator)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 1, output
		assert "outcome=enumerate_failed exit_code=3" in output, output
		assert github_output == ""


def test_enumerate_step_fails_closed_on_a_non_utf8_completed_path() -> None:
	"""The lint script reads the list as UTF-8. A name that is not valid
	UTF-8 under docs/completed/ takes the guided exit-3 path, not a traceback
	in the lint step; such a name elsewhere is left out of the list."""
	def _adds_a_plan_with_a_non_utf8_name(repo_dir: Path) -> None:
		with open(os.path.join(os.fsencode(repo_dir), b"docs", b"completed", b"bad\xff.md"), "wb") as fh:
			fh.write(b"new\n")

	def _adds_code_with_a_non_utf8_name(repo_dir: Path) -> None:
		with open(os.path.join(os.fsencode(repo_dir), b"code\xff.py"), "wb") as fh:
			fh.write(b"x = 1\n")
		(repo_dir / "docs" / "completed" / "ok.md").write_text("fine\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_a_plan_with_a_non_utf8_name)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 1, output
		assert "outcome=enumerate_failed exit_code=3" in output, output
		assert github_output == ""
	with tempfile.TemporaryDirectory() as tmp:
		repo_dir = _merge_commit_repo(Path(tmp), _adds_code_with_a_non_utf8_name)
		rc, output, github_output = _run_enumerate_step(repo_dir, Path(tmp))
		assert rc == 0, output
		added = (Path(tmp) / "plan-archival-lint" / "added.txt").read_text(encoding="utf-8").splitlines()
		assert added == ["docs/completed/ok.md"], added
		assert github_output == "needed=true\n", github_output


def test_enumerate_step_works_in_a_depth_2_clone() -> None:
	"""The job checks out the merge commit with `fetch-depth: 2`. A clone of
	that depth has the merge commit and both parents, which is all the
	enumerate step reads."""
	def _adds_a_completed_plan(repo_dir: Path) -> None:
		(repo_dir / "docs" / "completed" / "x.md").write_text("new\n", encoding="utf-8")

	with tempfile.TemporaryDirectory() as tmp:
		source_dir = _merge_commit_repo(Path(tmp), _adds_a_completed_plan)
		clone_dir = Path(tmp) / "clone"
		_git(Path(tmp), "clone", "-q", "--depth", "2", "--branch", "main", source_dir.as_uri(), str(clone_dir))
		assert _git(clone_dir, "rev-parse", "--is-shallow-repository") == "true"
		head_sha = _git(source_dir, "rev-parse", "pr-branch")
		rc, output, github_output = _run_enumerate_step(clone_dir, Path(tmp), event_head_sha=head_sha)
		assert rc == 0, output
		assert github_output == "needed=true\n", github_output
		assert "added_files=1 lint_needed=true" in output, output


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
	# The plan-archival group needs depth 2 (see
	# test_checkout_depth_is_the_merge_commit_and_its_parents); the lint's
	# own deepening is then a no-op and still harmless.
	assert checkout["with"]["fetch-depth"] == 2


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
