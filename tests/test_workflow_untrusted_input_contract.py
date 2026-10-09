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


RELEASE_WORKFLOWS = ("mark-stable.yml", "test-and-mark-stable.yml")
REF_EXPRESSION = re.compile(r"\$\{\{[^}]*github\.(?:ref|ref_name|head_ref)\b")


def _run_step_body(
	name: str,
	job_id: str,
	step_name: str,
	env_overrides: dict[str, str],
	path_prefix: str | None = None,
) -> subprocess.CompletedProcess[str]:
	run = _step(name, job_id, step_name).get("run")
	assert isinstance(run, str)
	env = os.environ.copy()
	for key in ("BASH_ENV", "ENV", "GATE_ONLY", "DISPATCH_REF", "DISPATCH_REF_NAME", "DISPATCH_SHA"):
		env.pop(key, None)
	env.update(env_overrides)
	if path_prefix is not None:
		env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
	return subprocess.run(
		["bash", "-c", run],
		cwd=REPO_ROOT,
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_release_source_jobs_do_not_interpolate_ref_expressions() -> None:
	for workflow_name in RELEASE_WORKFLOWS:
		jobs = _workflow(workflow_name).get("jobs")
		assert isinstance(jobs, dict)
		for job_id, job in jobs.items():
			if not isinstance(job, dict):
				continue
			for step in job.get("steps", []):
				if isinstance(step, dict) and isinstance(step.get("run"), str):
					assert not REF_EXPRESSION.search(step["run"]), (
						f"{workflow_name}:{job_id}:{step.get('name')} interpolates a ref into shell source"
					)
		detect_env = _step(workflow_name, "source", "Validate dispatch ref").get("env")
		assert isinstance(detect_env, dict)
		assert detect_env.get("DISPATCH_REF") == "${{ github.ref }}"


def test_release_source_ref_check_is_exact_and_literal() -> None:
	with TemporaryDirectory(prefix="release-ref-contract-") as temp_dir:
		temp_path = Path(temp_dir)
		sentinel = temp_path / "executed"
		hostile_names = (f"$(touch {sentinel})", "$(echo${IFS}stable)", f"`touch {sentinel}`")
		for workflow_name in RELEASE_WORKFLOWS:
			output = temp_path / f"out-{workflow_name}"

			def run(ref: str, ref_name: str, gate_only: str = "false") -> subprocess.CompletedProcess[str]:
				output.write_text("", encoding="utf-8")
				return _run_step_body(
					workflow_name,
					"source",
					"Validate dispatch ref",
					{
						"DISPATCH_REF": ref,
						"DISPATCH_REF_NAME": ref_name,
						"GATE_ONLY": gate_only,
						"GITHUB_OUTPUT": str(output),
					},
				)

			result = run("refs/heads/stable", "stable")
			assert result.returncode == 0, result.stderr
			assert output.read_text(encoding="utf-8") == "branch=stable\n"

			for hostile in hostile_names:
				result = run(f"refs/heads/{hostile}", hostile)
				assert result.returncode != 0, (workflow_name, hostile)
				assert not sentinel.exists(), (workflow_name, hostile)
				assert "branch=" not in output.read_text(encoding="utf-8")

			for rejected in ("refs/tags/stable", "refs/heads/main", "stable", "refs/heads/stable-x"):
				result = run(rejected, rejected.rsplit("/", 1)[-1])
				assert result.returncode != 0, (workflow_name, rejected)
				assert "branch=" not in output.read_text(encoding="utf-8")

		hostile = f"$(touch {sentinel})"
		output = temp_path / "out-gate-only"
		output.write_text("", encoding="utf-8")
		result = _run_step_body(
			"test-and-mark-stable.yml",
			"source",
			"Validate dispatch ref",
			{
				"DISPATCH_REF": f"refs/heads/{hostile}",
				"DISPATCH_REF_NAME": hostile,
				"GATE_ONLY": "true",
				"GITHUB_OUTPUT": str(output),
			},
		)
		assert result.returncode == 0, result.stderr
		assert not sentinel.exists()
		assert output.read_text(encoding="utf-8") == f"branch={hostile}\n"


def _verify_commit_case(
	workflow_name: str,
	temp_path: Path,
	dispatch_sha: str,
	tip: str,
	compare_status: str,
	fail_all: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	bin_dir = temp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	calls = temp_path / "gh_calls.txt"
	calls.write_text("", encoding="utf-8")
	stub = bin_dir / "gh"
	stub.write_text(
		"#!/usr/bin/env bash\n"
		'printf "%s\\n" "$*" >> "${STUB_GH_CALLS}"\n'
		'if [ "${STUB_GH_FAIL_ALL}" = "true" ]; then echo "HTTP 502 error body"; exit 1; fi\n'
		'case "$2" in\n'
		'  */git/ref/heads/stable) printf "%s\\n" "${STUB_GH_TIP}" ;;\n'
		'  */compare/*) printf "%s\\n" "${STUB_GH_COMPARE}" ;;\n'
		"  *) exit 1 ;;\n"
		"esac\n",
		encoding="utf-8",
	)
	stub.chmod(0o755)
	result = _run_step_body(
		workflow_name,
		"source",
		"Verify dispatched commit is on stable",
		{
			"GH_TOKEN": "token",
			"GITHUB_REPOSITORY": "owner/repo",
			"DISPATCH_SHA": dispatch_sha,
			"RELEASE_SOURCE_VERIFY_BACKOFF_SECONDS": "0",
			"STUB_GH_CALLS": str(calls),
			"STUB_GH_TIP": tip,
			"STUB_GH_COMPARE": compare_status,
			"STUB_GH_FAIL_ALL": "true" if fail_all else "false",
		},
		path_prefix=str(bin_dir),
	)
	return result, [line for line in calls.read_text(encoding="utf-8").splitlines() if line]


def test_release_source_verifies_dispatched_sha() -> None:
	sha = "a" * 40
	tip = "b" * 40
	for workflow_name in RELEASE_WORKFLOWS:
		step = _step(workflow_name, "source", "Verify dispatched commit is on stable")
		assert step.get("id") == "verify_commit"
		run = step.get("run")
		assert isinstance(run, str)
		assert "git/ref/heads/stable" in run
		step_env = step.get("env")
		assert isinstance(step_env, dict)
		assert step_env.get("GH_TOKEN") == "${{ github.token }}"
		assert step_env.get("DISPATCH_SHA") == "${{ github.sha }}"
		assert "GH_PAT" not in str(step_env)
		steps = _workflow(workflow_name)["jobs"]["source"]["steps"]
		assert all("uses" not in candidate for candidate in steps), "source job must not check out code"
		if workflow_name == "test-and-mark-stable.yml":
			assert step.get("if") == "${{ !inputs.gate_only }}"
		else:
			assert "if" not in step

		with TemporaryDirectory(prefix="release-sha-contract-") as temp_dir:
			temp_path = Path(temp_dir)
			result, calls = _verify_commit_case(workflow_name, temp_path, sha, sha, "")
			assert result.returncode == 0, result.stdout + result.stderr
			assert len(calls) == 1

			for status in ("ahead", "identical"):
				result, calls = _verify_commit_case(workflow_name, temp_path, sha, tip, status)
				assert result.returncode == 0, (status, result.stdout + result.stderr)
				assert len(calls) == 2
				assert f"compare/{sha}...{tip}" in calls[1]

			for status in ("behind", "diverged", ""):
				result, _calls = _verify_commit_case(workflow_name, temp_path, sha, tip, status)
				assert result.returncode != 0, status
				assert "RELEASE_UNTESTED_HEAD" in result.stdout

			result, calls = _verify_commit_case(workflow_name, temp_path, sha, tip, "ahead", fail_all=True)
			assert result.returncode != 0
			assert len(calls) == 3

			result, calls = _verify_commit_case(workflow_name, temp_path, sha, "not-a-sha", "ahead")
			assert result.returncode != 0

			for bad_sha in ("$(touch x)", "A" * 40, "a" * 39, ""):
				result, calls = _verify_commit_case(workflow_name, temp_path, bad_sha, tip, "ahead")
				assert result.returncode != 0, bad_sha
				assert calls == []


def test_release_dispatchers_pass_fully_qualified_stable_branch() -> None:
	promote = (WORKFLOW_DIR / "promote-main-to-stable.yml").read_text(encoding="utf-8")
	assert "--ref refs/heads/stable \\" in promote
	assert "--ref stable \\" not in promote
	auto_release = (REPO_ROOT / "scripts" / "auto_release_stable.sh").read_text(encoding="utf-8")
	assert '--ref "refs/heads/${AUTO_RELEASE_STABLE_BRANCH}"' in auto_release


def test_heal_evidence_is_fenced_and_editors_are_isolated() -> None:
	clarify = _step("clarify.yml", "clarify", "Run Codex")["run"]
	plan = (REPO_ROOT / "scripts/run_plan_codex.sh").read_text(encoding="utf-8")
	implement = _step("implement.yml", "implement", "Run Codex implementation")["run"]
	assert isinstance(clarify, str) and 'cat "${RUNTIME_DIR}/heal_evidence.md" >> "${CODEX_PROMPT_FILE}"' in clarify
	assert 'cat "${HEAL_EVIDENCE_FILE:-${RUNTIME_DIR}/heal_evidence.md}" >> "${CODEX_PROMPT_FILE}"' in plan
	assert '"${PLAN_ENGINE}" PLAN' in plan and '"${HEAL_ROUTE:-false}" != true' in plan
	assert isinstance(implement, str) and 'cat "${RUNTIME_DIR}/heal_evidence.md" >> "${CODEX_PROMPT_FILE}"' in implement
	assert 'heal_isolated_implement.sh' in implement and '"${HEAL_ROUTE:-false}" = true' in implement
	evidence = (REPO_ROOT / "scripts/workflow_failure_heal_evidence.sh").read_text(encoding="utf-8")
	assert "=== BEGIN UNTRUSTED WORKFLOW FAILURE EVIDENCE ===" in evidence
	assert "=== END UNTRUSTED WORKFLOW FAILURE EVIDENCE ===" in evidence


def run_all_contract_tests() -> None:
	for name, test_func in sorted(globals().items()):
		if name.startswith("test_") and callable(test_func):
			test_func()


if __name__ == "__main__":
	run_all_contract_tests()
