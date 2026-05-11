#!/usr/bin/env python3
"""Contract tests for template-mode routing in scripts/validate_process.sh."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"
SELF_HEAL_SCRIPT_PATH = REPO_ROOT / "scripts" / "self_heal_validation.sh"
SELF_HEAL_PROMPT_PATH = REPO_ROOT / "prompts" / "mode-validate-self-heal.txt"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")


def _self_heal_script_text() -> str:
	return SELF_HEAL_SCRIPT_PATH.read_text(encoding="utf-8")


def _self_heal_prompt_text() -> str:
	return SELF_HEAL_PROMPT_PATH.read_text(encoding="utf-8")


def _git(cmd: list[str], *, cwd: Path) -> None:
	subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _bootstrap_git_repo(repo_dir: Path) -> None:
	repo_dir.mkdir(parents=True, exist_ok=True)
	_git(["git", "init"], cwd=repo_dir)
	_git(["git", "config", "user.name", "tests"], cwd=repo_dir)
	_git(["git", "config", "user.email", "tests@example.com"], cwd=repo_dir)
	(repo_dir / "README.md").write_text("test\n", encoding="utf-8")
	_git(["git", "add", "README.md"], cwd=repo_dir)
	_git(["git", "commit", "-m", "init"], cwd=repo_dir)


def test_template_mode_selection_contract_present() -> None:
	text = _validate_process_text()
	assert 'VALIDATION_USE_TEMPLATES="${VALIDATION_USE_TEMPLATES:-true}"' in text
	assert 'VALIDATION_USE_TEMPLATES_ENABLED="false"' in text
	assert "case \"$(printf '%s' \"${VALIDATION_USE_TEMPLATES}\" | tr '[:upper:]' '[:lower:]')\" in" in text
	assert 'if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" != "true" ]; then' in text
	assert 'Freehand harness generation has been removed. Set VALIDATION_USE_TEMPLATES=true (or leave it unset) to use template rendering.' in text
	assert 'HARNESS_MODE="template_generate"' in text
	assert 'HARNESS_GENERATOR_MODE="templates"' in text
	assert 'PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"' in text
	assert 'attempt_render_recovery_after_preflight_failure()' in text
	assert 'if attempt_render_recovery_after_preflight_failure; then' in text
	assert 'attempt_self_heal_and_reexec "render"' in text
	assert "python3_bin=\"$(command -v python3 2>/dev/null || printf '%s' 'python3')\"" in text
	assert "if ! \"${python3_bin}\" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then" in text
	assert 'Template renderer requires python3 >= 3.9' in text
	assert 'Template rendering is now the only supported harness generation path.' in text


def test_render_recovery_contract_and_prompt_only_self_heal_scope() -> None:
	validate_text = _validate_process_text()
	assert 'Render recovery: deterministic template rerender triggered after pre-flight failure.' in validate_text
	assert 'Render recovery: rerender completed; re-running pre-flight checks.' in validate_text
	assert 'Render recovery: pre-flight checks still failing after deterministic rerender (kind=${PRE_FLIGHT_FAILURE_KIND} reason=${PRE_FLIGHT_FAILURE_REASON}).' in validate_text
	assert 'PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="true"' in validate_text
	assert 'if [ "${PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED:-false}" = "true" ]; then' in validate_text
	assert 'if [ "${render_recovery_exit}" -eq 2 ]; then' in validate_text

	script_text = _self_heal_script_text()
	assert '"mode-validate-discover.txt"' in script_text
	assert '"mode-validate-generate.txt"' in script_text
	assert '"mode-validate-fix-harness.txt"' in script_text
	assert '"mode-validate-diagnose.txt"' in script_text
	assert "self-heal: refusing — target_prompt '" in script_text
	assert 'self-heal: refusing — patch contains deletion lines; self-heal patches must be additive-only' in script_text

	prompt_text = _self_heal_prompt_text()
	assert 'For `failing_phase=render` or deterministic template rerender/lint recovery failures, keep self-heal scope prompt-only:' in prompt_text
	assert 'Do not propose harness-file edits, renderer-script edits, or workflow changes.' in prompt_text


def test_template_mode_missing_manifest_returns_harness_error() -> None:
	with tempfile.TemporaryDirectory(prefix="validate-template-mode-") as td:
		tmp_path = Path(td)
		status_file = tmp_path / "status.json"
		metadata_file = tmp_path / "metadata.json"

		harness_script = """#!/usr/bin/env bash
set -euo pipefail

run_template_validation_harness_renderer() {
	local manifest_path=".ai/validate.yml"
	local renderer_script="scripts/render_validation_templates.py"
	local schema_path="scripts/templates/slot_manifest.schema.json"
	local templates_root="workflow-templates/validation-harness"
	local renderer_summary=""

	HARNESS_GENERATOR_MODE="templates"

	if [ ! -f "${manifest_path}" ]; then
		return 10
	fi
	if [ ! -f "${renderer_script}" ]; then
		return 11
	fi
	if [ ! -f "${schema_path}" ]; then
		return 12
	fi
	if [ ! -d "${templates_root}" ]; then
		return 13
	fi
	if [ ! -f "${templates_root}/_shared/_lib/tap_helpers.sh.j2" ] \
		|| [ ! -f "${templates_root}/_shared/tests/00_canary.sh.j2" ] \
		|| [ ! -f "${templates_root}/_shared/tests/90_tap_report.sh.j2" ]; then
		return 15
	fi

	if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
		printf '%s\n' "Template renderer requires python3 >= 3.9 (detected: $(python3 -V 2>&1 || echo unknown))." >> "${GENERATE_LOG_FILE}"
		return 14
	fi

	if ! renderer_summary="$(python3 "${renderer_script}" \
		--manifest "${manifest_path}" \
		--schema "${schema_path}" \
		--templates-root "${templates_root}" \
		--output-root validation 2>&1)"; then
		printf '%s\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
		return 14
	fi

	if [ -n "${renderer_summary}" ]; then
		printf '%s\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
	fi

	if [ -d validation/tests ]; then
		find validation/tests -type f -name '*.sh' -exec chmod +x {} +
	fi

	return 0
}

write_result_files() {
	local status="$1"
	local summary="$2"
	local failure_summary="$3"
	local raw_status="${4:-${status}}"

	jq -n \
		--arg status "${status}" \
		--arg raw_status "${raw_status}" \
		--arg summary "${summary}" \
		--arg failure_summary "${failure_summary}" \
		'{status: $status, raw_status: $raw_status, summary: $summary, failure_summary: $failure_summary}' > "${STATUS_FILE}"

	jq -n \
		--arg harness_mode "${HARNESS_MODE}" \
		--arg harness_generator_mode "${HARNESS_GENERATOR_MODE}" \
		'{harness_mode: $harness_mode, harness_generator_mode: $harness_generator_mode}' > "${METADATA_FILE}"
}

post_tracking_comment() { :; }
set_tracking_phase_label() { :; }
tg_notify() { :; }

HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
GENERATE_LOG_FILE="${RUNTIME_DIR}/validate_generate.log"

		if run_template_validation_harness_renderer; then
			renderer_exit=0
		else
			renderer_exit=$?
		fi

case "${renderer_exit}" in
	0)
		exit 0
		;;
	10)
		local_failure_summary="Template mode requires ${PWD}/.ai/validate.yml but it is missing. Create the required manifest to proceed with template-based validation."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed" "ERROR"
		exit 1
		;;
	*)
		local_failure_summary="Template renderer failed while generating validation assets."
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		exit 1
		;;
esac
"""

		script_path = tmp_path / "template_missing_manifest_harness.sh"
		script_path.write_text(harness_script, encoding="utf-8")
		script_path.chmod(0o755)

		runtime_dir = tmp_path / "runtime"
		runtime_dir.mkdir(parents=True, exist_ok=True)

		env = os.environ.copy()
		env["RUNTIME_DIR"] = str(runtime_dir)
		env["STATUS_FILE"] = str(status_file)
		env["METADATA_FILE"] = str(metadata_file)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(tmp_path),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert proc.returncode == 1, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert status_file.exists()
		assert metadata_file.exists()
		status_payload = status_file.read_text(encoding="utf-8")
		metadata_payload = metadata_file.read_text(encoding="utf-8")
		assert '"raw_status": "harness_error"' in status_payload
		assert '.ai/validate.yml but it is missing' in status_payload
		assert '"harness_mode": "template_generate"' in metadata_payload
		assert '"harness_generator_mode": "templates"' in metadata_payload


def test_template_mode_harness_contract_accepts_missing_validate_env() -> None:
	text = _validate_process_text()
	assert 'if [ -f validation/docker-compose.test.yml ] \\' in text
	assert '&& [ -f validation/tests/00_canary.sh ] \\' in text
	assert 'if [ ! -f validation/tests/00_canary.sh ]; then' in text
	assert 'echo "Missing validation/validate.env" >> "${PRE_FLIGHT_LOG_FILE}"' not in text
	assert 'Template renderer completed but produced non-runnable validation assets (validation/docker-compose.test.yml and validation/tests/00_canary.sh at minimum).' in text


def test_render_recovery_lint_gate_contract_present() -> None:
	text = _validate_process_text()
	assert 'if [ "${PRE_FLIGHT_FAILURE_CLASS:-non_lint}" != "lint" ]; then' in text
	assert 'Render recovery: skipping deterministic rerender because pre-flight failure class=${PRE_FLIGHT_FAILURE_CLASS:-unknown}.' in text


def test_serena_runtime_filter_hides_only_unchanged_bootstrap_tree() -> None:
	text = _validate_process_text()
	filter_helper = "filter_runtime_status_noise()\n{" + text.split("filter_runtime_status_noise()\n{", 1)[1].split("\n\nbuild_validate_serena_tool_hints()", 1)[0]

	with tempfile.TemporaryDirectory(prefix="validate-serena-runtime-") as td:
		repo_dir = Path(td)
		_bootstrap_git_repo(repo_dir)

		def _write_serena_tree(project_body: str) -> str:
			project_path = repo_dir / ".serena" / "project.yml"
			cache_path = repo_dir / ".serena" / "cache" / "state.json"
			cache_path.parent.mkdir(parents=True, exist_ok=True)
			project_path.write_text(project_body, encoding="utf-8")
			cache_path.write_text('{"runtime": true}\n', encoding="utf-8")
			return hashlib.sha256(project_path.read_bytes()).hexdigest()

		def _run_inline_bash(script: str, *, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
			env = os.environ.copy()
			env.update(extra_env)
			return subprocess.run(
				["bash", "-s"],
				cwd=str(repo_dir),
				env=env,
				input=script,
				text=True,
				capture_output=True,
				timeout=60,
			)

		filter_script = "set -euo pipefail\n" + filter_helper + "\ngit status --porcelain -uall | filter_runtime_status_noise\n"

		bootstrap_hash = _write_serena_tree("project_name: bootstrap\n")
		filter_result = _run_inline_bash(
			filter_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert filter_result.returncode == 0, filter_result.stderr
		assert ".serena/" not in filter_result.stdout, (
			"Unchanged bootstrap-owned .serena state should be filtered out of validate path-constraint bookkeeping"
		)

		(repo_dir / ".serena" / "project.yml").write_text("project_name: mutated\n", encoding="utf-8")
		mutated_filter_result = _run_inline_bash(
			filter_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "false",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert mutated_filter_result.returncode == 0, mutated_filter_result.stderr
		assert ".serena/project.yml" in mutated_filter_result.stdout, (
			"Changing project.yml must stop the validate filter from hiding .serena/project.yml"
		)
		assert ".serena/cache/state.json" in mutated_filter_result.stdout, (
			"Changing project.yml must stop the validate filter from hiding sibling .serena runtime files"
		)

		shutil.rmtree(repo_dir / ".serena")
		bootstrap_hash = _write_serena_tree("project_name: preexisting\n")
		preexisting_filter_result = _run_inline_bash(
			filter_script,
			extra_env={
				"SERENA_PROJECT_PREEXISTED": "true",
				"SERENA_PROJECT_BOOTSTRAP_HASH": bootstrap_hash,
			},
		)
		assert preexisting_filter_result.returncode == 0, preexisting_filter_result.stderr
		assert ".serena/project.yml" in preexisting_filter_result.stdout, (
			"Repo-owned Serena state must stay visible to validate bookkeeping even when its content matches the bootstrap hash"
		)


def main() -> int:
	test_template_mode_selection_contract_present()
	test_render_recovery_contract_and_prompt_only_self_heal_scope()
	test_template_mode_missing_manifest_returns_harness_error()
	test_template_mode_harness_contract_accepts_missing_validate_env()
	test_render_recovery_lint_gate_contract_present()
	test_serena_runtime_filter_hides_only_unchanged_bootstrap_tree()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
