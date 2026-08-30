#!/usr/bin/env python3
"""Security contracts for dispatch inputs consumed by workflow shell blocks."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SCOPED_WORKFLOWS = (
	"validation-refresh.yml",
	"workflow-log-analysis.yml",
	"mark-stable.yml",
	"test-and-mark-stable.yml",
	"promote-main-to-stable.yml",
	"comprehensive-test-and-release.yml",
	"validate.yml",
)
UNTRUSTED_EXPRESSION = re.compile(r"\$\{\{\s*(?:inputs|github\.event\.inputs)\.")


def _workflow(name: str) -> dict[str, object]:
	document = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
	assert isinstance(document, dict)
	return document


def _step(name: str, job_id: str, step_name: str) -> dict[str, object]:
	jobs = _workflow(name).get("jobs")
	assert isinstance(jobs, dict)
	job = jobs.get(job_id)
	assert isinstance(job, dict)
	for candidate in job.get("steps", []):
		if isinstance(candidate, dict) and candidate.get("name") == step_name:
			return candidate
	raise AssertionError(f"Missing step {name}:{job_id}:{step_name}")


def _run_step_prefix(
	name: str,
	job_id: str,
	step_name: str,
	marker: str,
	env_overrides: dict[str, str],
) -> subprocess.CompletedProcess[str]:
	run = _step(name, job_id, step_name).get("run")
	assert isinstance(run, str)
	prefix, separator, _remainder = run.partition(marker)
	assert separator, f"Missing test marker in {name}:{step_name}: {marker}"
	env = os.environ.copy()
	env.pop("BASH_ENV", None)
	env.pop("ENV", None)
	env.update(env_overrides)
	return subprocess.run(
		["bash", "-c", prefix],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_scoped_run_blocks_do_not_interpolate_untrusted_inputs() -> None:
	for workflow_name in SCOPED_WORKFLOWS:
		jobs = _workflow(workflow_name).get("jobs")
		assert isinstance(jobs, dict)
		for job_id, job in jobs.items():
			if not isinstance(job, dict):
				continue
			for step in job.get("steps", []):
				if not isinstance(step, dict) or not isinstance(step.get("run"), str):
					continue
				assert not UNTRUSTED_EXPRESSION.search(step["run"]), (
					f"{workflow_name}:{job_id}:{step.get('name')} interpolates an input into shell source"
				)


def test_dispatch_inputs_are_bound_through_step_environments() -> None:
	expected = {
		("validation-refresh.yml", "refresh", "Run validation refresh"): {
			"VALIDATION_REFRESH_REPOS_FILE_INPUT",
			"VALIDATION_REFRESH_BRANCH_NAME_INPUT",
		},
		("workflow-log-analysis.yml", "collect-logs", "Resolve repositories"): {"OVERRIDE_INPUT"},
		("workflow-log-analysis.yml", "collect-logs", "Collect workflow logs"): {
			"SINCE_INPUT",
			"LOOKBACK_DAYS_INPUT",
		},
		("mark-stable.yml", "resolve-version", "Resolve version tag"): {"INPUT_VERSION"},
		("test-and-mark-stable.yml", "resolve-version", "Resolve version tag"): {"INPUT_VERSION"},
		("promote-main-to-stable.yml", "promote", "Dispatch test-and-mark-stable on stable"): {
			"TEST_REPO_INPUT",
			"SKIP_E2E_INPUT",
			"DRY_RUN_INPUT",
			"PHASE_TIMEOUT_INPUT",
			"REVIEW_TIMEOUT_INPUT",
		},
		("validate.yml", "validate", "Initialize workspace metadata"): {"TRACKING_ISSUE_INPUT"},
	}
	for location, names in expected.items():
		environment = _step(*location).get("env")
		assert isinstance(environment, dict)
		assert names <= environment.keys()


def test_shell_metacharacters_remain_literal_at_validation_boundaries() -> None:
	with TemporaryDirectory(prefix="workflow-input-contract-") as temp_dir:
		temp_path = Path(temp_dir)
		sentinel = temp_path / "executed"
		payload = f"$(touch {sentinel})"
		cases = (
			(
				"validation-refresh.yml",
				"refresh",
				"Run validation refresh",
				"# Wire git",
				{
					"GH_TOKEN": "token",
					"GITHUB_WORKSPACE": str(REPO_ROOT),
					"VALIDATION_REFRESH_REPOS_FILE_INPUT": payload,
					"VALIDATION_REFRESH_BRANCH_NAME_INPUT": "ai/validation-refresh",
				},
			),
			(
				"mark-stable.yml",
				"resolve-version",
				"Resolve version tag",
				"# Refresh tags",
				{"INPUT_VERSION": payload, "GITHUB_OUTPUT": str(temp_path / "out-mark")},
			),
			(
				"test-and-mark-stable.yml",
				"resolve-version",
				"Resolve version tag",
				"# Refresh tags",
				{"INPUT_VERSION": payload, "GITHUB_OUTPUT": str(temp_path / "out-test")},
			),
			(
				"workflow-log-analysis.yml",
				"collect-logs",
				"Resolve repositories",
				"declare -A seen=()",
				{"OVERRIDE_INPUT": payload, "GITHUB_OUTPUT": str(temp_path / "out-repos")},
			),
			(
				"test-and-mark-stable.yml",
				"e2e-smoke-test",
				"Validate prerequisites",
				"# Validate REVIEW_WORKFLOW_FILE",
				{
					"GH_TOKEN": "token",
					"TEST_REPO": payload,
					"PHASE_TIMEOUT": "30",
					"PLAN_PHASE_TIMEOUT": "60",
					"REVIEW_TIMEOUT": "60",
					"REVIEW_STEP_TIMEOUT": "75",
					"REVIEW_WORKFLOW_FILE": "internal-review.yml",
				},
			),
			(
				"promote-main-to-stable.yml",
				"promote",
				"Dispatch test-and-mark-stable on stable",
				"# test-and-mark-stable.yml hard-rejects",
				{
					"TEST_REPO_INPUT": payload,
					"SKIP_E2E_INPUT": "false",
					"DRY_RUN_INPUT": "false",
					"PHASE_TIMEOUT_INPUT": "30",
					"REVIEW_TIMEOUT_INPUT": "60",
				},
			),
			(
				"comprehensive-test-and-release.yml",
				"phase2-collect-and-analyze-logs",
				"Dispatch, monitor, and persist analysis state",
				"LAST_COLLECTION_TS=",
				{
					"PHASE_TIMEOUT": payload,
					"LOOKBACK_DAYS_FALLBACK": "7",
					"GITHUB_REF_NAME": "main",
				},
			),
			(
				"validate.yml",
				"validate",
				"Initialize workspace metadata",
				"WORKSPACE_REUSE_ENABLED=",
				{"TRACKING_ISSUE_INPUT": payload},
			),
		)
		for case in cases:
			result = _run_step_prefix(*case)
			assert result.returncode != 0, case[:3]
			assert not sentinel.exists(), case[:3]


def test_validation_refresh_checks_path_containment_and_git_ref() -> None:
	run = _step("validation-refresh.yml", "refresh", "Run validation refresh").get("run")
	assert isinstance(run, str)
	assert '[[ "${REPOS_FILE}" = /* ]]' in run
	assert '[[ "/${REPOS_FILE}/" = *"/../"* ]]' in run
	assert 'realpath -e "${GITHUB_WORKSPACE}/${REPOS_FILE}"' in run
	assert '[[ "${REPOS_FILE_REAL}" != "${WORKSPACE_REAL}/"* ]]' in run
	assert 'git check-ref-format --branch "${BRANCH_NAME}"' in run


def test_numeric_repository_sha_and_tag_validators_are_present() -> None:
	workflow_log = (WORKFLOW_DIR / "workflow-log-analysis.yml").read_text(encoding="utf-8")
	test_release = (WORKFLOW_DIR / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	comprehensive = (WORKFLOW_DIR / "comprehensive-test-and-release.yml").read_text(encoding="utf-8")
	assert "lookback_days must be a positive integer" in workflow_log
	assert workflow_log.count("tracking_issue must be 0 or a positive integer") >= 3
	assert "test_repo must match owner/repo" in test_release
	assert "^[0-9a-fA-F]{40}$" in test_release
	assert "must match vX.Y.Z" in test_release
	assert "positive numeric run id" in comprehensive
	assert "Resolved tracking issue must be a positive integer" in comprehensive


def test_every_release_target_repo_job_validates_before_api_use() -> None:
	workflow = _workflow("test-and-mark-stable.yml")
	jobs = workflow.get("jobs")
	assert isinstance(jobs, dict)
	validated_jobs = 0
	for job_id, job in jobs.items():
		if not isinstance(job, dict) or "TEST_REPO" not in (job.get("env") or {}):
			continue
		preflight = _step("test-and-mark-stable.yml", job_id, "Validate prerequisites").get("run")
		assert isinstance(preflight, str)
		assert '[[ "${TEST_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]' in preflight
		validated_jobs += 1
	assert validated_jobs == 7


def test_release_dispatch_uses_quoted_fields_without_textual_json() -> None:
	workflow = (WORKFLOW_DIR / "test-and-mark-stable.yml").read_text(encoding="utf-8")
	assert "INPUTS: '{" not in workflow
	assert '--field "repos_override=${TEST_REPO}"' in workflow


def run_all_contract_tests() -> None:
	for name, test_func in sorted(globals().items()):
		if name.startswith("test_") and callable(test_func):
			test_func()


if __name__ == "__main__":
	run_all_contract_tests()
