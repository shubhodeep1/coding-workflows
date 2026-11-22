"""Docs-only final CI (run-volume plan item 4, docs/plans/reduce-actions-run-volume-plan.md).

Pins:
  * the path list in scripts/ci_docs_only_paths.sh (`classify`);
  * `verify` against real merge commits: it passes only for a merge whose
    second parent is the run's head and whose changes are all on the list;
  * ci.yml: the `docs_only` dispatch input, the seven gated test jobs, the
    `lint` job's check steps, and the aggregate's decision matrix (`skipped`
    counts only for the gated jobs, only after the check passed);
  * `_pr_ensure_final_ci` (scripts/pr_checks_lib.sh): it passes
    docs_only=true only for an all-docs PR, counts anything unreadable as
    code, and retries once without the input when the dispatch is refused.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from test_review_final_ci import LIB_PRELUDE, REPO, RUNS_KEY, SHA, _dispatches, _ensure, fake_gh  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parent.parent
CLASSIFIER = REPO_ROOT / "scripts" / "ci_docs_only_paths.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GATED_JOBS = (
	"tests-hooks-and-orchestrator",
	"tests-heal-plan-and-validation",
	"tests-promote-stall-and-review",
	"tests-release-and-log-analysis",
	"orchestrate-poll",
	"tests-system-python",
	"tests-sandbox-python",
)
GATED_IF = "inputs.mode != 'quick' && inputs.docs_only != 'true' && inputs.sync_quick != 'true'"
AGGREGATE_STEP_NAME = "Require every CI job to succeed"


def _clean_env() -> dict:
	env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "CI_DOCS_ONLY"))}
	env.pop("BASH_ENV", None)
	return env


def _classify(*paths: str) -> str:
	payload = "".join(path + "\0" for path in paths)
	result = subprocess.run(["bash", str(CLASSIFIER), "classify"], input=payload, capture_output=True, text=True, env=_clean_env(), check=False)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


@pytest.mark.parametrize("path", [
	"analysis/workflow-optimization-2026-11-15.md",
	"analysis/sub/dir/x.json",
	"docs/plans/reduce-actions-run-volume-plan.md",
	"docs/postmortems/2026-05-18-project-2734-stall.md",
	"docs/completed/old-plan.md",
	"docs/deploy-activation/runbook.md",
	"docs/triage/pr-1-note.md",
	"docs/how-it-works.md",
	"docs/hardenaireviewprompt.md",
	"docs/changelog-style.md",
	"docs/review-future-improvements.md",
])
def test_listed_paths_are_docs_only(path: str) -> None:
	assert _classify(path) == "docs_only"


@pytest.mark.parametrize("path", [
	"README.md",
	"agents.md",
	"CLAUDE.md",
	"docs/INVENTORY.md",
	"docs/scripts-pending-removal.md",
	"docs/sub/x-future-improvements.md",
	"docs/-future-improvements.md",
	"docs/plans",
	"analysis",
	"analysis/",
	"scripts/pr_checks_lib.sh",
	".github/workflows/ci.yml",
	"tests/test_x.py",
	"changelog.d/x.md",
	"agents.d/x.md",
	"docs/plans/../../scripts/x.sh",
	"./docs/plans/x.md",
	"/docs/plans/x.md",
	"",
])
def test_unlisted_paths_are_code(path: str) -> None:
	assert _classify(path).startswith("code"), path


def test_one_unlisted_path_makes_the_whole_set_code() -> None:
	assert _classify("docs/plans/a.md", "scripts/x.sh", "analysis/b.md") == "code scripts/x.sh"


def test_no_paths_is_code() -> None:
	assert _classify() == "code empty"


def test_path_with_newline_is_one_path() -> None:
	assert _classify("docs/plans/a\nscripts/x.sh") == "docs_only"
	assert _classify("scripts/x\ndocs/plans/a.md").startswith("code")


def test_code_verdict_stays_on_one_line() -> None:
	"""A code path whose name ends in a newline and `docs_only` must not
	leave `docs_only` as a line of its own for a reader to pick up."""
	verdict = _classify("scripts/x\ndocs_only")
	assert "\n" not in verdict
	assert verdict.startswith("code ")


def test_last_path_without_a_trailing_nul_is_classified() -> None:
	result = subprocess.run(["bash", str(CLASSIFIER), "classify"], input="docs/plans/a.md\0scripts/x.sh", capture_output=True, text=True, env=_clean_env(), check=False)
	assert result.stdout.strip() == "code scripts/x.sh"


# --- verify: real merge commits ------------------------------------------


def _git(cwd: Path, *args: str) -> str:
	env = _clean_env()
	env.update({
		"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
		"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
	})
	return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
	target = repo / rel
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(text, encoding="utf-8")


def _merge_repo(tmp_path: Path, change) -> tuple[Path, str]:
	"""A repo whose HEAD merges a PR head (made by `change`) into a moved base.

	Returns (repo, pr_head_sha)."""
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_write(repo, "scripts/x.sh", "echo x\n")
	_write(repo, "docs/plans/p.md", "plan\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	_git(repo, "checkout", "-q", "-b", "pr")
	change(repo)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "pr change")
	pr_head = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "-q", "main")
	_write(repo, "lib/base_moved.sh", "echo moved\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base moved")
	_git(repo, "merge", "-q", "--no-ff", "--no-edit", "pr")
	return repo, pr_head


def _verify(repo: Path, head_sha: str) -> subprocess.CompletedProcess:
	env = _clean_env()
	env["CI_DOCS_ONLY_HEAD_SHA"] = head_sha
	return subprocess.run(["bash", str(CLASSIFIER), "verify"], cwd=repo, env=env, capture_output=True, text=True, check=False)


def test_verify_passes_for_a_docs_only_merge(tmp_path: Path) -> None:
	repo, pr_head = _merge_repo(tmp_path, lambda r: _write(r, "docs/plans/p.md", "plan v2\n"))
	result = _verify(repo, pr_head)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "CI_DOCS_ONLY_GATE mode=verify outcome=pass reason=all_listed" in result.stdout


def test_verify_ignores_what_the_base_changed(tmp_path: Path) -> None:
	"""The base moved with lib/base_moved.sh; only the PR side counts."""
	repo, pr_head = _merge_repo(tmp_path, lambda r: _write(r, "analysis/report.md", "r\n"))
	assert _verify(repo, pr_head).returncode == 0


def test_verify_fails_when_the_pr_changes_code(tmp_path: Path) -> None:
	def change(r: Path) -> None:
		_write(r, "docs/plans/p.md", "plan v2\n")
		_write(r, "scripts/x.sh", "echo y\n")
	repo, pr_head = _merge_repo(tmp_path, change)
	result = _verify(repo, pr_head)
	assert result.returncode == 1
	assert "reason=code_changed first=scripts/x.sh" in result.stdout


def test_verify_fails_for_a_rename_out_of_a_code_path(tmp_path: Path) -> None:
	repo, pr_head = _merge_repo(tmp_path, lambda r: _git(r, "mv", "scripts/x.sh", "docs/plans/x.sh"))
	result = _verify(repo, pr_head)
	assert result.returncode == 1
	assert "first=scripts/x.sh" in result.stdout


def test_verify_fails_when_the_head_moved(tmp_path: Path) -> None:
	repo, _ = _merge_repo(tmp_path, lambda r: _write(r, "docs/plans/p.md", "plan v2\n"))
	result = _verify(repo, "c" * 40)
	assert result.returncode == 1
	assert "reason=head_moved" in result.stdout


def test_verify_fails_on_a_non_merge_head(tmp_path: Path) -> None:
	repo, pr_head = _merge_repo(tmp_path, lambda r: _write(r, "docs/plans/p.md", "plan v2\n"))
	_git(repo, "reset", "-q", "--hard", pr_head)
	result = _verify(repo, pr_head)
	assert result.returncode == 1
	assert "reason=not_a_merge" in result.stdout


@pytest.mark.parametrize("head", ["", "abc", "A" * 40])
def test_verify_fails_on_an_unusable_head_sha(tmp_path: Path, head: str) -> None:
	repo, _ = _merge_repo(tmp_path, lambda r: _write(r, "docs/plans/p.md", "plan v2\n"))
	result = _verify(repo, head)
	assert result.returncode == 1
	assert "reason=head_sha_invalid" in result.stdout


def test_verify_fails_outside_a_git_checkout(tmp_path: Path) -> None:
	result = _verify(tmp_path, SHA)
	assert result.returncode == 1
	assert "reason=merge_unreadable" in result.stdout


# --- ci.yml structure -----------------------------------------------------


def _ci() -> dict:
	return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
	return _ci()["jobs"]


def _lint_steps() -> dict:
	return {step.get("name"): step for step in _jobs()["lint"]["steps"]}


def test_dispatch_input_defaults_to_the_full_suite() -> None:
	on = _ci().get("on") or _ci().get(True)
	spec = on["workflow_dispatch"]["inputs"]["docs_only"]
	assert spec["required"] is False
	assert spec["default"] == "false"
	assert spec["type"] == "string"
	# The quick caller never passes it; pushes have no inputs at all.
	assert "docs_only" not in on["workflow_call"]["inputs"]


def test_exactly_the_test_jobs_are_gated() -> None:
	jobs = _jobs()
	gated = sorted(job_id for job_id, job in jobs.items() if "inputs.docs_only" in str(job.get("if", "")))
	assert gated == sorted(GATED_JOBS)
	for job_id in GATED_JOBS:
		assert jobs[job_id]["if"] == GATED_IF, job_id
	assert "if" not in jobs["static-checks"]


def test_aggregate_names_the_same_gated_jobs() -> None:
	env = _lint_steps()[AGGREGATE_STEP_NAME]["env"]
	assert env["CI_DOCS_ONLY_GATED_JOBS"].split() == list(GATED_JOBS)
	assert env["CI_DOCS_ONLY_VERIFIED"] == "${{ steps.docs_only_verify.outcome == 'success' }}"


def test_lint_checks_the_merged_tree_before_the_aggregate() -> None:
	steps = _jobs()["lint"]["steps"]
	names = [step.get("name") for step in steps]
	checkout = steps[names.index("Checkout the merged tree for the docs-only check")]
	verify = steps[names.index("Verify every changed file is docs-only")]
	assert names.index("Verify every changed file is docs-only") < names.index(AGGREGATE_STEP_NAME)
	assert checkout["if"] == "inputs.docs_only == 'true'"
	assert checkout["uses"] == "actions/checkout@v5"
	assert checkout["with"]["ref"] == "${{ format('refs/pull/{0}/merge', inputs.pr_number) }}"
	assert checkout["with"]["fetch-depth"] == 2
	assert checkout["with"]["persist-credentials"] is False
	assert verify["id"] == "docs_only_verify"
	assert verify["if"] == "inputs.docs_only == 'true'"
	assert verify["run"] == "bash scripts/ci_docs_only_paths.sh verify"
	assert verify["env"]["CI_DOCS_ONLY_HEAD_SHA"] == "${{ github.sha }}"
	assert "continue-on-error" not in verify


def test_this_module_is_wired_into_ci() -> None:
	assert "tests/test_ci_docs_only_final_ci.py" in CI_WORKFLOW.read_text(encoding="utf-8")


# --- the aggregate's decision matrix -------------------------------------


def _aggregate(results: dict, verified: str) -> subprocess.CompletedProcess:
	step = _lint_steps()[AGGREGATE_STEP_NAME]
	env = _clean_env()
	env["CI_NEEDS_JSON"] = json.dumps({job_id: {"result": result, "outputs": {}} for job_id, result in results.items()})
	env["CI_DOCS_ONLY_VERIFIED"] = verified
	env["CI_DOCS_ONLY_GATED_JOBS"] = step["env"]["CI_DOCS_ONLY_GATED_JOBS"]
	return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, check=False)


def _results(gated: str, static: str = "success") -> dict:
	return {"static-checks": static, **{job_id: gated for job_id in GATED_JOBS}}


@pytest.mark.parametrize("verified,gated,static,passes", [
	("true", "skipped", "success", True),
	("true", "success", "success", True),
	("true", "failure", "success", False),
	("true", "cancelled", "success", False),
	("true", "skipped", "failure", False),
	("true", "skipped", "skipped", False),
	("false", "skipped", "success", False),
	("false", "success", "success", True),
	("", "skipped", "success", False),
	("True", "skipped", "success", False),
])
def test_aggregate_matrix(verified: str, gated: str, static: str, passes: bool) -> None:
	result = _aggregate(_results(gated, static), verified)
	assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_skip_is_only_accepted_for_the_named_jobs() -> None:
	results = _results("skipped")
	results["new-unlisted-job"] = "skipped"
	result = _aggregate(results, "true")
	assert result.returncode == 1
	assert "CI jobs that did not succeed: new-unlisted-job" in result.stdout


def test_one_failed_gated_job_is_named_in_a_docs_only_run() -> None:
	results = _results("skipped")
	results["orchestrate-poll"] = "failure"
	result = _aggregate(results, "true")
	assert result.returncode == 1
	assert "CI jobs that did not succeed: orchestrate-poll" in result.stdout


# --- the dispatcher -------------------------------------------------------


FILES_KEY = f"repos/{REPO}/pulls/7/files"
NO_RUN = {RUNS_KEY: {"body": {"workflow_runs": []}}}


def _files(*names: str, previous: dict | None = None) -> list[dict]:
	entries = []
	for name in names:
		entry = {"filename": name}
		if previous and name in previous:
			entry["previous_filename"] = previous[name]
		entries.append(entry)
	return entries


def _base_dispatch(**extra: str) -> list[str]:
	call = ["workflow", "run", "ci.yml", "--repo", REPO, "--ref", "ai/issue-1", "-f", "pr_number=7", "-f", f"head_sha={SHA}"]
	for key, value in extra.items():
		call += ["-f", f"{key}={value}"]
	return call


def test_docs_only_pr_dispatches_with_the_input(fake_gh) -> None:
	result, calls = _ensure(fake_gh, {**NO_RUN, FILES_KEY: {"body": _files("docs/plans/a.md", "analysis/b.md")}})
	assert "rc=0 outcome=dispatched" in result.stdout, result.stdout + result.stderr
	assert f"REVIEW_FINAL_CI_DOCS_ONLY pr=7 head={SHA} docs_only=true reason=docs_only" in result.stdout
	assert _dispatches(calls) == [_base_dispatch(docs_only="true")]


@pytest.mark.parametrize("files", [
	_files("docs/plans/a.md", "scripts/x.sh"),
	_files("docs/plans/x.sh", previous={"docs/plans/x.sh": "scripts/x.sh"}),
	[],
	[{"status": "added"}],
	{"message": "Not Found"},
	[{"filename": "docs/plans/a.md"}, "docs/plans/b.md"],
	[{"filename": "docs/plans/a.md"}, {"filename": 5}],
	[{"filename": "docs/plans/a.md", "previous_filename": ["scripts/x.sh"]}],
	[{"filename": ""}],
	_files("scripts/x\ndocs_only"),
	_files(*[f"docs/plans/{i}.md" for i in range(100)]),
])
def test_anything_else_dispatches_the_full_suite(fake_gh, files) -> None:
	result, calls = _ensure(fake_gh, {**NO_RUN, FILES_KEY: {"body": files}})
	assert "rc=0 outcome=dispatched" in result.stdout, result.stdout + result.stderr
	assert "docs_only=false" in result.stdout
	assert _dispatches(calls) == [_base_dispatch()]


def test_unreadable_files_dispatch_the_full_suite(fake_gh) -> None:
	result, calls = _ensure(fake_gh, {**NO_RUN, FILES_KEY: {"rc": 1}})
	assert f"REVIEW_FINAL_CI_DOCS_ONLY pr=7 head={SHA} docs_only=false reason=files_unreadable" in result.stdout
	assert _dispatches(calls) == [_base_dispatch()]


def test_refused_docs_only_dispatch_retries_once_without_it(fake_gh, tmp_path: Path) -> None:
	# Refuse only the dispatch that carries docs_only (a branch whose ci.yml
	# predates the input).
	gh = tmp_path / "bin" / "gh"
	source = gh.read_text(encoding="utf-8").replace(
		'if args[:2] == ["workflow", "run"]:\n',
		'if args[:2] == ["workflow", "run"] and "docs_only=true" in args:\n\tsys.exit(1)\nif args[:2] == ["workflow", "run"]:\n',
		1,
	)
	gh.write_text(source, encoding="utf-8")
	result, calls = _ensure(fake_gh, {**NO_RUN, FILES_KEY: {"body": _files("docs/plans/a.md")}})
	assert "rc=0 outcome=dispatched" in result.stdout, result.stdout + result.stderr
	assert "docs_only=dispatch_refused" in result.stdout + result.stderr
	assert _dispatches(calls) == [_base_dispatch(docs_only="true"), _base_dispatch()]


def test_existing_run_reads_no_files(fake_gh) -> None:
	runs = {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_sha": SHA, "status": "in_progress", "conclusion": None}]}
	result, calls = _ensure(fake_gh, {RUNS_KEY: {"body": runs}, FILES_KEY: {"body": _files("docs/plans/a.md")}})
	assert "outcome=exists" in result.stdout
	assert not any("pulls/7/files" in " ".join(call) for call in calls)
	assert _dispatches(calls) == []


def test_missing_classifier_dispatches_the_full_suite(fake_gh, tmp_path: Path) -> None:
	lib_copy = tmp_path / "lib"
	lib_copy.mkdir()
	for name in ("gh_helpers.sh", "pr_checks_lib.sh"):
		(lib_copy / name).write_text((REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8")
	script = (
		f'set -uo pipefail\nsource "{lib_copy}/gh_helpers.sh"\nsource "{lib_copy}/pr_checks_lib.sh"\n'
		f'_pr_ensure_final_ci "{REPO}" 7 "{SHA}" "ai/issue-1"; echo "rc=$? outcome=${{PR_FINAL_CI_OUTCOME}}"'
	)
	result, calls = fake_gh(script, {**NO_RUN, FILES_KEY: {"body": _files("docs/plans/a.md")}}, {"REVIEW_FINAL_CI_WORKFLOW": "ci.yml"})
	assert "docs_only=false reason=classifier_missing" in result.stdout, result.stdout + result.stderr
	assert not any("pulls/7/files" in " ".join(call) for call in calls)
	assert _dispatches(calls) == [_base_dispatch()]


def test_lib_prelude_is_shared() -> None:
	"""The dispatcher tests reuse test_review_final_ci's harness unchanged."""
	assert "pr_checks_lib.sh" in LIB_PRELUDE
