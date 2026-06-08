#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "workspace_init.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"


def _parse_kv_file(path: Path) -> dict[str, str]:
	values: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		if not line:
			continue
		key, sep, value = line.partition("=")
		assert sep == "=", f"Malformed key/value line: {line!r}"
		values[key] = value
	return values


def _run_helper(command: str, tmp_path: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
	output_file = tmp_path / "github_output.txt"
	env_file = tmp_path / "github_env.txt"
	env = os.environ.copy()
	env.update(
		{
			"GITHUB_OUTPUT": str(output_file),
			"GITHUB_ENV": str(env_file),
			"GITHUB_RUN_ID": "12345",
			"GITHUB_RUN_ATTEMPT": "2",
			"RUNNER_TEMP": str(tmp_path / "runner-temp"),
		}
	)
	env.update(env_overrides)
	return subprocess.run(
		["bash", str(HELPER), command],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def _workflow_doc(path: Path) -> dict[str, object]:
	doc = yaml.safe_load(path.read_text(encoding="utf-8"))
	if not isinstance(doc, dict):
		raise AssertionError(f"Workflow did not parse into a mapping: {path}")
	return doc


def _step(path: Path, step_name: str) -> dict[str, object]:
	jobs = _workflow_doc(path).get("jobs")
	if not isinstance(jobs, dict):
		raise AssertionError(f"Workflow jobs mapping missing in {path}")
	for job in jobs.values():
		if not isinstance(job, dict):
			continue
		steps = job.get("steps")
		if not isinstance(steps, list):
			continue
		for step in steps:
			if isinstance(step, dict) and str(step.get("name", "")).strip() == step_name:
				return step
	raise AssertionError(f"Step not found in {path}: {step_name}")


def _step_run_text(path: Path, step_name: str) -> str:
	run = _step(path, step_name).get("run")
	if not isinstance(run, str):
		raise AssertionError(f"Step does not define a run block: {step_name}")
	return run


def test_helper_script_exists_and_is_executable() -> None:
	assert HELPER.exists()
	assert HELPER.stat().st_mode & 0o111


def test_metadata_sanitizes_key_and_derives_workspace_path(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	source_path.mkdir()
	result = _run_helper(
		"metadata",
		tmp_path,
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_ISSUE_IDENTIFIER="Issue 42/alpha",
		WORKSPACE_FINGERPRINT="fingerprint-1",
		WORKSPACE_SOURCE_PATH=str(source_path),
	)
	assert result.returncode == 0, result.stderr
	outputs = _parse_kv_file(tmp_path / "github_output.txt")
	assert outputs["workspace_key"] == "Issue_42_alpha"
	assert outputs["workspace_path"].endswith("/runner-temp/workspaces/Issue_42_alpha")
	assert outputs["workspace_fingerprint"] == "fingerprint-1"
	assert outputs["workspace_cache_key"] == "workspace-v1-Issue_42_alpha-fingerprint-1-12345"


def test_metadata_strips_crlf_from_fingerprint_file(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	source_path.mkdir()
	fingerprint_file = tmp_path / "fingerprint.txt"
	fingerprint_file.write_text("tree-abc\r\n", encoding="utf-8")
	result = _run_helper(
		"metadata",
		tmp_path,
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_ISSUE_IDENTIFIER="issue-7",
		WORKSPACE_FINGERPRINT_FILE=str(fingerprint_file),
		WORKSPACE_SOURCE_PATH=str(source_path),
	)
	assert result.returncode == 0, result.stderr
	outputs = _parse_kv_file(tmp_path / "github_output.txt")
	assert outputs["workspace_fingerprint"] == "tree-abc"
	assert outputs["workspace_cache_key"] == "workspace-v1-issue-7-tree-abc-12345"


def test_metadata_exact_restore_sets_created_now_false(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	source_path.mkdir()
	result = _run_helper(
		"metadata",
		tmp_path,
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_ISSUE_IDENTIFIER="issue-7",
		WORKSPACE_FINGERPRINT="tree-abc",
		WORKSPACE_SOURCE_PATH=str(source_path),
		WORKSPACE_CACHE_MATCHED_KEY="workspace-v1-issue-7-tree-abc-999",
	)
	assert result.returncode == 0, result.stderr
	outputs = _parse_kv_file(tmp_path / "github_output.txt")
	assert outputs["workspace_cache_restore_state"] == "exact"
	assert outputs["created_now"] == "false"


def test_metadata_prefix_restore_sets_created_now_true(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	source_path.mkdir()
	result = _run_helper(
		"metadata",
		tmp_path,
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_ISSUE_IDENTIFIER="issue-7",
		WORKSPACE_FINGERPRINT="tree-abc",
		WORKSPACE_SOURCE_PATH=str(source_path),
		WORKSPACE_CACHE_MATCHED_KEY="workspace-v1-issue-7-tree-old-777",
	)
	assert result.returncode == 0, result.stderr
	outputs = _parse_kv_file(tmp_path / "github_output.txt")
	assert outputs["workspace_cache_restore_state"] == "partial"
	assert outputs["created_now"] == "true"


def test_metadata_miss_sets_created_now_true(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	source_path.mkdir()
	result = _run_helper(
		"metadata",
		tmp_path,
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_ISSUE_IDENTIFIER="issue-7",
		WORKSPACE_FINGERPRINT="tree-abc",
		WORKSPACE_SOURCE_PATH=str(source_path),
	)
	assert result.returncode == 0, result.stderr
	outputs = _parse_kv_file(tmp_path / "github_output.txt")
	assert outputs["workspace_cache_restore_state"] == "miss"
	assert outputs["created_now"] == "true"


def test_metadata_rejects_workspace_escape_key(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	source_path.mkdir()
	result = _run_helper(
		"metadata",
		tmp_path,
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_ISSUE_IDENTIFIER="..",
		WORKSPACE_FINGERPRINT="tree-abc",
		WORKSPACE_SOURCE_PATH=str(source_path),
	)
	assert result.returncode != 0
	assert "escapes" in result.stderr


def test_finalize_refreshes_source_tree_and_preserves_extra_state(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	workspace_path = tmp_path / "runner-temp" / "workspaces" / "issue-7"
	source_path.mkdir(parents=True)
	workspace_path.mkdir(parents=True)

	(source_path / "tracked.txt").write_text("fresh\n", encoding="utf-8")
	(source_path / "scripts").mkdir()
	(source_path / "scripts" / "helper.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

	(workspace_path / "tracked.txt").write_text("stale\n", encoding="utf-8")
	(workspace_path / "stale.txt").write_text("remove me\n", encoding="utf-8")
	(workspace_path / ".cache" / "tool").mkdir(parents=True)
	(workspace_path / ".cache" / "tool" / "state.json").write_text("{}\n", encoding="utf-8")
	(workspace_path / ".ai" / ".workspace_source_manifest.txt").parent.mkdir(parents=True)
	(workspace_path / ".ai" / ".workspace_source_manifest.txt").write_text("tracked.txt\nstale.txt\n", encoding="utf-8")
	(workspace_path / ".ai" / "validate-hints-cache").mkdir(parents=True)
	(workspace_path / ".ai" / "validate-hints-cache" / "hints.yml").write_text("stale: true\n", encoding="utf-8")
	(workspace_path / ".serena").mkdir()
	(workspace_path / ".serena" / "project.yml").write_text("old: true\n", encoding="utf-8")

	result = _run_helper(
		"finalize",
		tmp_path,
		WORKSPACE_SOURCE_PATH=str(source_path),
		WORKSPACE_PATH=str(workspace_path),
		WORKSPACE_REUSE_ENABLED="true",
		WORKSPACE_CACHE_RESTORE_STATE="partial",
	)
	assert result.returncode == 0, result.stderr
	assert (workspace_path / "tracked.txt").read_text(encoding="utf-8") == "fresh\n"
	assert (workspace_path / "scripts" / "helper.sh").exists()
	assert not (workspace_path / "stale.txt").exists()
	assert (workspace_path / ".cache" / "tool" / "state.json").exists()
	assert not (workspace_path / ".ai" / "validate-hints-cache").exists()
	assert not (workspace_path / ".serena").exists()


def test_finalize_ignores_manifest_entries_outside_workspace(tmp_path: Path) -> None:
	source_path = tmp_path / "source"
	workspace_path = tmp_path / "runner-temp" / "workspaces" / "issue-7"
	sibling_path = workspace_path.parent / "sibling.txt"
	source_path.mkdir(parents=True)
	workspace_path.mkdir(parents=True)
	(source_path / "tracked.txt").write_text("fresh\n", encoding="utf-8")
	sibling_path.write_text("keep\n", encoding="utf-8")
	(workspace_path / ".ai").mkdir(parents=True)
	(workspace_path / ".ai" / ".workspace_source_manifest.txt").write_text("../sibling.txt\ntracked.txt\n", encoding="utf-8")

	result = _run_helper("finalize", tmp_path, WORKSPACE_SOURCE_PATH=str(source_path), WORKSPACE_PATH=str(workspace_path), WORKSPACE_REUSE_ENABLED="true")
	assert result.returncode == 0, result.stderr
	assert sibling_path.read_text(encoding="utf-8") == "keep\n"


def test_implement_workflow_stages_workspace_helper_and_orders_restore_keys() -> None:
	stage_block = _step_run_text(IMPLEMENT_WORKFLOW, "Stage workflow support files")
	assert "workspace_init.sh" in stage_block
	cache_step = _step(IMPLEMENT_WORKFLOW, "Restore reusable workspace cache")
	assert cache_step.get("uses") == "actions/cache@v5"
	with_block = cache_step.get("with")
	assert isinstance(with_block, dict)
	assert with_block.get("path") == "${{ steps.workspace_meta.outputs.workspace_path }}"
	assert with_block.get("key") == "${{ steps.workspace_meta.outputs.workspace_cache_key }}"
	assert with_block.get("restore-keys") == (
		"${{ steps.workspace_meta.outputs.workspace_cache_restore_prefix_exact }}\n"
		"${{ steps.workspace_meta.outputs.workspace_cache_restore_prefix_issue }}\n"
	)


def test_validate_workflow_stages_workspace_helper_and_uses_workspace_paths() -> None:
	fetch_block = _step_run_text(VALIDATE_WORKFLOW, "Fetch workflow support files")
	assert "workspace_init.sh" in fetch_block
	cache_step = _step(VALIDATE_WORKFLOW, "Restore reusable workspace cache")
	assert cache_step.get("uses") == "actions/cache@v5"
	with_block = cache_step.get("with")
	assert isinstance(with_block, dict)
	assert with_block.get("restore-keys") == (
		"${{ steps.workspace_meta.outputs.workspace_cache_restore_prefix_exact }}\n"
		"${{ steps.workspace_meta.outputs.workspace_cache_restore_prefix_issue }}\n"
	)
	prepare_runtime_step = _step(VALIDATE_WORKFLOW, "Prepare behavioural smoke runtime cache path")
	assert 'mkdir -p "${{ steps.workspace_state.outputs.workspace_path }}/.ai/review_runtime/"' in _step_run_text(VALIDATE_WORKFLOW, "Prepare behavioural smoke runtime cache path")
	assert prepare_runtime_step.get("if") == "steps.behavioural_smoke_gate.outputs.enabled == 'true' && steps.behavioural_smoke_pr.outputs.pr_number != ''"
	behavioural_smoke_restore_step = _step(VALIDATE_WORKFLOW, "Restore behavioural smoke runtime cache")
	assert behavioural_smoke_restore_step.get("uses") == "actions/cache/restore@v5"
	assert _step(VALIDATE_WORKFLOW, "Restore validate hints cache").get("with", {}).get("path") == "${{ steps.workspace_state.outputs.workspace_path }}/.ai/validate-hints-cache"
	assert behavioural_smoke_restore_step.get("with", {}).get("path") == "${{ steps.workspace_state.outputs.workspace_path }}/.ai/review_runtime/"
	assert "${{ steps.workspace_state.outputs.workspace_path }}/validation/" in str(_step(VALIDATE_WORKFLOW, "Upload validation artifacts").get("with", {}).get("path"))


def test_workspace_shell_context_activates_before_repo_sensitive_steps() -> None:
	implement_text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	validate_text = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	implement_activate = _step_run_text(IMPLEMENT_WORKFLOW, "Activate workspace shell context")
	validate_activate = _step_run_text(VALIDATE_WORKFLOW, "Activate workspace shell context")
	for block in (implement_activate, validate_activate):
		assert 'cd "${WORKSPACE_PATH}"' in block
		assert 'echo "BASH_ENV=${workspace_shell_env}"' in block
		assert 'echo "GIT_WORK_TREE=${WORKSPACE_PATH}"' in block
	assert implement_text.find("- name: Activate workspace shell context") < implement_text.find("- name: Run Codex implementation")
	assert validate_text.find("- name: Activate workspace shell context") < validate_text.find("- name: Run validation process")


def test_validate_workspace_metadata_disables_reuse_without_numeric_tracking_issue() -> None:
	metadata_block = _step_run_text(VALIDATE_WORKFLOW, "Initialize workspace metadata")
	assert 'workspace_reuse_enabled="${WORKSPACE_REUSE_ENABLED:-false}"' in metadata_block
	assert 'if [[ "${{ inputs.tracking_issue }}" =~ ^[0-9]+$ ]] && [ "${{ inputs.tracking_issue }}" -gt 0 ]; then' in metadata_block
	assert 'workspace_reuse_enabled="false"' in metadata_block
	assert 'WORKSPACE_REQUIRE_STABLE_IDENTIFIER_FOR_REUSE="true"' in metadata_block


def _run_tmp_path_case(case_fn) -> None:
	with TemporaryDirectory() as tmp_dir:
		case_fn(Path(tmp_dir))


def main() -> int:
	test_helper_script_exists_and_is_executable()
	for case_fn in (
		test_metadata_sanitizes_key_and_derives_workspace_path,
		test_metadata_strips_crlf_from_fingerprint_file,
		test_metadata_exact_restore_sets_created_now_false,
		test_metadata_prefix_restore_sets_created_now_true,
		test_metadata_miss_sets_created_now_true,
		test_metadata_rejects_workspace_escape_key,
		test_finalize_refreshes_source_tree_and_preserves_extra_state,
	):
		_run_tmp_path_case(case_fn)
	test_implement_workflow_stages_workspace_helper_and_orders_restore_keys()
	test_validate_workflow_stages_workspace_helper_and_uses_workspace_paths()
	test_workspace_shell_context_activates_before_repo_sensitive_steps()
	test_validate_workspace_metadata_disables_reuse_without_numeric_tracking_issue()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
