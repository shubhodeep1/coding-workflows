#!/usr/bin/env python3
"""Contract tests for template-mode routing in scripts/validate_process.sh."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
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
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env["BASH_ENV"] = ""
	env["ENV"] = ""
	subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True, env=env)


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
	assert 'raw on-disk contents with any prior self-heal patches applied' in prompt_text
	assert 'literal `{{SERENA_TOOL_HINTS}}` runtime placeholder' in prompt_text


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
		env["BASH_ENV"] = ""
		env["ENV"] = ""

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


def test_tg_notify_preserves_suffixes_and_fails_open() -> None:
	text = _validate_process_text()
	notification_helpers = "_gh_url() {" + text.split("_gh_url() {", 1)[1].split("\n# gh_retry is provided", 1)[0]

	def _run_case(
		*,
		tracking_issue: int,
		level: str | None = None,
		failing_suffix: bool = False,
	) -> tuple[subprocess.CompletedProcess[str], list[str]]:
		with tempfile.TemporaryDirectory(prefix="validate-tg-notify-") as td:
			capture_path = Path(td) / "capture.bin"
			suffix_override = ""
			if failing_suffix:
				suffix_override = """
_tg_link_suffix()
{
	printf '%s' 'partial suffix that must be discarded'
	return 17
}
"""
			level_argument = "" if level is None else f" {shlex.quote(level)}"
			script = f"""set -euo pipefail
{notification_helpers}
{suffix_override}
tg_send_tracked()
{{
	printf '%s\\0%s\\0%s' "$1" "$2" "$3" > "${{CAPTURE_FILE}}"
}}
tg_send_msg()
{{
	printf '%s\\0%s' "$1" "$2" > "${{CAPTURE_FILE}}"
}}

TRACKING_ISSUE_NUM={tracking_issue}
GITHUB_SERVER_URL=https://github.example
GITHUB_REPOSITORY=octo/demo
GITHUB_RUN_ID=77
tg_notify 'Original alert'{level_argument}
"""
			proc = subprocess.run(
				["bash", "-s"],
				env={
					**os.environ,
					"BASH_ENV": "",
					"ENV": "",
					"CAPTURE_FILE": str(capture_path),
				},
				input=script,
				text=True,
				capture_output=True,
				timeout=60,
			)
			assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			assert capture_path.exists(), (
				f"tg_notify harness did not write capture file.\nstdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
			)
			captured_arguments = capture_path.read_bytes().decode("utf-8").split("\0")
			return proc, captured_arguments

	success_proc, success_arguments = _run_case(tracking_issue=42)
	assert success_proc.returncode == 0, success_proc.stderr
	assert success_proc.stderr == ""
	assert success_arguments == [
		"42",
		"Original alert\nIssue: https://github.example/octo/demo/issues/42\nRun: https://github.example/octo/demo/actions/runs/77",
		"CRITICAL",
	]

	failure_proc, failure_arguments = _run_case(
		tracking_issue=0,
		level="WARNING",
		failing_suffix=True,
	)
	assert failure_proc.returncode == 0, failure_proc.stderr
	assert failure_arguments == ["Original alert", "WARNING"]
	assert "::warning::Validation Telegram link suffix generation failed; sending alert without links." in failure_proc.stderr
	assert "partial suffix" not in failure_arguments[0]

	tracked_failure_proc, tracked_failure_arguments = _run_case(
		tracking_issue=42,
		level="WARNING",
		failing_suffix=True,
	)
	assert tracked_failure_proc.returncode == 0, tracked_failure_proc.stderr
	assert tracked_failure_arguments == ["42", "Original alert", "WARNING"]
	assert "::warning::Validation Telegram link suffix generation failed; sending alert without links." in tracked_failure_proc.stderr
	assert "partial suffix" not in tracked_failure_arguments[1]


def test_write_result_files_emits_failure_summary_only_for_non_pass() -> None:
	text = _validate_process_text()
	assert 'emit_validation_failure_summary()' in text
	write_status_helper = "write_status_file()\n{" + text.split("write_status_file()\n{", 1)[1].split("\n\nwrite_metadata_file()", 1)[0]
	write_metadata_helper = "write_metadata_file()\n{" + text.split("write_metadata_file()\n{", 1)[1].split("\n\ncompact_validation_summary_value()", 1)[0]
	compact_helper = "compact_validation_summary_value()\n{" + text.split("compact_validation_summary_value()\n{", 1)[1].split("\n\nemit_validation_failure_summary()", 1)[0]
	emit_helper = "emit_validation_failure_summary()\n{" + text.split("emit_validation_failure_summary()\n{", 1)[1].split("\n\nwrite_result_files()", 1)[0]
	write_result_helper = "write_result_files()\n{" + text.split("write_result_files()\n{", 1)[1].split("\n\nemit_phase_failure_marker()", 1)[0]

	def _run_case(
		status: str,
		summary: str,
		failure_summary: str,
		raw_status: str,
	) -> tuple[subprocess.CompletedProcess[str], dict[str, object], dict[str, object]]:
		with tempfile.TemporaryDirectory(prefix="validate-write-result-files-") as td:
			tmp_path = Path(td)
			status_file = tmp_path / "status.json"
			metadata_file = tmp_path / "metadata.json"
			null_json_file = tmp_path / "null.json"
			validation_result_file = tmp_path / "validation_result.json"
			diagnose_result_file = tmp_path / "diagnose_result.json"
			runtime_dir = tmp_path / "runtime"
			runtime_dir.mkdir(parents=True, exist_ok=True)
			null_json_file.write_text("null\n", encoding="utf-8")
			validation_result_file.write_text('{"tests": 2}\n', encoding="utf-8")
			diagnose_result_file.write_text('{"status": "needs_fixes"}\n', encoding="utf-8")

			script = f"""set -euo pipefail
{write_status_helper}
{write_metadata_helper}
{compact_helper}
{emit_helper}
{write_result_helper}

STATUS_FILE={shlex.quote(str(status_file))}
METADATA_FILE={shlex.quote(str(metadata_file))}
NULL_JSON_FILE={shlex.quote(str(null_json_file))}
VALIDATION_RESULT_FILE={shlex.quote(str(validation_result_file))}
DIAGNOSE_RESULT_FILE={shlex.quote(str(diagnose_result_file))}
HINTS_SOURCE=cache
HARNESS_MODE=template_generate
HARNESS_GENERATOR_MODE=templates
PRE_FLIGHT_STATUS=failed
GITHUB_REPOSITORY=octo/demo-repo
TRACKING_ISSUE_RAW=1234
VALIDATION_CYCLE=4
SELF_HEAL_ATTEMPT=1
MAX_SELF_HEAL_ATTEMPTS=2
GITHUB_RUN_ID=77
GITHUB_RUN_ATTEMPT=3
RUNTIME_DIR={shlex.quote(str(runtime_dir))}
VALIDATION_COMPOSE_FILE=validation/docker-compose.test.yml
VALIDATION_LOG_FILE=validation/logs/validate.log
GENERATE_LOG_FILE=runtime/validate_generate.log
DIAGNOSE_LOG_FILE=runtime/validate_diagnose.log
GENERATED_VALIDATE_SCRIPT_PATH=validation/generated_validate.sh
CREATED_FIX_ISSUES_JSON='[101,102]'

write_result_files {shlex.quote(status)} {shlex.quote(summary)} {shlex.quote(failure_summary)} {shlex.quote(raw_status)}
"""
			proc = subprocess.run(
				["bash", "-s"],
				cwd=str(tmp_path),
				env={**os.environ, "BASH_ENV": "", "ENV": ""},
				input=script,
				text=True,
				capture_output=True,
				timeout=60,
			)
			status_payload = json.loads(status_file.read_text(encoding="utf-8"))
			metadata_payload = json.loads(metadata_file.read_text(encoding="utf-8"))
			return proc, status_payload, metadata_payload

	fail_proc, fail_status_payload, fail_metadata_payload = _run_case(
		"fail",
		"Validation needs fixes",
		"Line one\nLine two",
		"needs_fixes",
	)
	assert fail_proc.returncode == 0, fail_proc.stderr
	fail_summary_lines = [
		line for line in fail_proc.stdout.splitlines() if line.startswith("VALIDATION_FAILURE_SUMMARY ")
	]
	assert len(fail_summary_lines) == 1
	assert 'repository="octo/demo-repo"' in fail_summary_lines[0]
	assert "tracking_issue=1234" in fail_summary_lines[0]
	assert "status=fail" in fail_summary_lines[0]
	assert "raw_status=needs_fixes" in fail_summary_lines[0]
	assert "cycle=4" in fail_summary_lines[0]
	assert "run_id=77" in fail_summary_lines[0]
	assert "run_attempt=3" in fail_summary_lines[0]
	assert "self_heal_attempt=1/2" in fail_summary_lines[0]
	assert "fix_issues=2" in fail_summary_lines[0]
	assert 'summary="Validation needs fixes"' in fail_summary_lines[0]
	assert 'failure_summary="Line one Line two"' in fail_summary_lines[0]
	assert fail_status_payload == {
		"failure_summary": "Line one\nLine two",
		"raw_status": "needs_fixes",
		"status": "fail",
		"summary": "Validation needs fixes",
		"tracking_issue": "1234",
	}
	assert fail_metadata_payload["status"] == "fail"
	assert fail_metadata_payload["raw_status"] == "needs_fixes"
	assert fail_metadata_payload["summary"] == "Validation needs fixes"
	assert fail_metadata_payload["failure_summary"] == "Line one\nLine two"
	assert fail_metadata_payload["created_fix_issues"] == [101, 102]
	assert fail_metadata_payload["validation_result"] == {"tests": 2}
	assert fail_metadata_payload["diagnosis"] == {"status": "needs_fixes"}

	pass_proc, pass_status_payload, pass_metadata_payload = _run_case(
		"pass",
		"Validation passed",
		"",
		"pass",
	)
	assert pass_proc.returncode == 0, pass_proc.stderr
	assert "VALIDATION_FAILURE_SUMMARY" not in pass_proc.stdout
	assert pass_status_payload["status"] == "pass"
	assert pass_status_payload["raw_status"] == "pass"
	assert pass_status_payload["summary"] == "Validation passed"
	assert pass_status_payload["failure_summary"] is None
	assert pass_metadata_payload["status"] == "pass"
	assert pass_metadata_payload["raw_status"] == "pass"
	assert pass_metadata_payload["summary"] == "Validation passed"
	assert pass_metadata_payload["failure_summary"] is None


def test_phase1_guard_paths_emit_failure_summary_before_exit() -> None:
	text = _validate_process_text()
	assert 'VALIDATION_ARTIFACT_CONTRACT_FAILURE_OUTPUT=""' in text
	assert 'emit_validation_failure_summary "error" "Validation harness artifact contract violation" "${VALIDATION_ARTIFACT_CONTRACT_FAILURE_OUTPUT}" "harness_error"' in text
	compact_helper = "compact_validation_summary_value()\n{" + text.split("compact_validation_summary_value()\n{", 1)[1].split("\n\nemit_validation_failure_summary()", 1)[0]
	emit_helper = "emit_validation_failure_summary()\n{" + text.split("emit_validation_failure_summary()\n{", 1)[1].split("\n\nwrite_result_files()", 1)[0]
	artifact_contract_helper = "enforce_managed_validation_artifact_contract()\n{" + text.split("enforce_managed_validation_artifact_contract()\n{", 1)[1].split("\n\ntrap cleanup_runtime_containers EXIT", 1)[0]
	artifact_contract_block = "VALIDATION_ARTIFACT_CONTRACT_FAILURE_OUTPUT=\"\"\n" + text.split("VALIDATION_ARTIFACT_CONTRACT_FAILURE_OUTPUT=\"\"\n", 1)[1].split("\n\nif [ -L validation ] || { [ -e validation ] && [ ! -d validation ]; }; then", 1)[0]
	non_directory_guard_block = "if [ -L validation ] || { [ -e validation ] && [ ! -d validation ]; }; then\n" + text.split("if [ -L validation ] || { [ -e validation ] && [ ! -d validation ]; }; then\n", 1)[1].split("\n\nif [ \"${VALIDATION_USE_TEMPLATES_ENABLED}\" != \"true\" ]; then", 1)[0]
	ownership_guard_block = "if [ -d validation ] && [ ! -f validation/.ai-validation-owned ]; then\n" + text.split("if [ -d validation ] && [ ! -f validation/.ai-validation-owned ]; then\n", 1)[1].split("\nfi\nrm -rf validation", 1)[0] + "\nfi"

	def _run_guard_case(script_body: str, *, setup=None, git_repo: bool = False) -> subprocess.CompletedProcess[str]:
		with tempfile.TemporaryDirectory(prefix="validate-guard-summary-") as td:
			tmp_path = Path(td)
			if git_repo:
				_bootstrap_git_repo(tmp_path)
			if setup is not None:
				setup(tmp_path)

			script = f"""set -euo pipefail
{compact_helper}
{emit_helper}
{artifact_contract_helper}

TRACKING_ISSUE_RAW=1234
VALIDATION_CYCLE=4
SELF_HEAL_ATTEMPT=1
MAX_SELF_HEAL_ATTEMPTS=2
GITHUB_REPOSITORY=octo/demo-repo
GITHUB_RUN_ID=77
GITHUB_RUN_ATTEMPT=3
CREATED_FIX_ISSUES_JSON='[]'

{script_body}
"""
			proc = subprocess.run(
				["bash", "-s"],
				cwd=str(tmp_path),
				env={
					**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
					"BASH_ENV": "",
					"ENV": "",
				},
				input=script,
				text=True,
				capture_output=True,
				timeout=60,
			)
			return proc

	def _summary_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
		return [
			line for line in proc.stdout.splitlines() if line.startswith("VALIDATION_FAILURE_SUMMARY ")
		]

	def _setup_artifact_contract_violation(tmp_path: Path) -> None:
		validation_dir = tmp_path / "validation"
		validation_dir.mkdir(parents=True, exist_ok=True)
		(validation_dir / "validate.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
		_git(["git", "add", "validation/validate.sh"], cwd=tmp_path)

	artifact_proc = _run_guard_case(
		artifact_contract_block,
		setup=_setup_artifact_contract_violation,
		git_repo=True,
	)
	assert artifact_proc.returncode == 1, artifact_proc.stderr
	artifact_summary_lines = _summary_lines(artifact_proc)
	assert len(artifact_summary_lines) == 1
	assert 'summary="Validation harness artifact contract violation"' in artifact_summary_lines[0]
	assert 'raw_status=harness_error' in artifact_summary_lines[0]
	assert 'fix_issues=0' in artifact_summary_lines[0]
	assert 'failure_summary="Managed validation artifact contract violation detected: - validation/validate.sh is tracked. validation/ artifacts must remain transient and untracked."' in artifact_summary_lines[0]
	assert "Managed validation artifact contract violation detected:" in artifact_proc.stderr

	non_directory_proc = _run_guard_case(
		non_directory_guard_block,
		setup=lambda tmp_path: (tmp_path / "validation").write_text("repo-owned file\n", encoding="utf-8"),
	)
	assert non_directory_proc.returncode == 1, non_directory_proc.stderr
	non_directory_summary_lines = _summary_lines(non_directory_proc)
	assert len(non_directory_summary_lines) == 1
	assert 'summary="Validation harness generation failed"' in non_directory_summary_lines[0]
	assert 'failure_summary="Refusing to use non-directory \'validation\' path."' in non_directory_summary_lines[0]
	assert 'raw_status=harness_error' in non_directory_summary_lines[0]
	assert "Refusing to use non-directory 'validation' path." in non_directory_proc.stderr

	ownership_proc = _run_guard_case(
		ownership_guard_block,
		setup=lambda tmp_path: (tmp_path / "validation").mkdir(parents=True, exist_ok=True),
	)
	assert ownership_proc.returncode == 1, ownership_proc.stderr
	ownership_summary_lines = _summary_lines(ownership_proc)
	assert len(ownership_summary_lines) == 1
	assert 'summary="Validation harness generation failed"' in ownership_summary_lines[0]
	assert 'failure_summary="Refusing to delete existing \'validation\' directory without ownership marker (validation/.ai-validation-owned)."' in ownership_summary_lines[0]
	assert 'raw_status=harness_error' in ownership_summary_lines[0]
	assert "Refusing to delete existing 'validation' directory without ownership marker (validation/.ai-validation-owned)." in ownership_proc.stderr


def test_phase0_guard_paths_emit_failure_summary_before_exit() -> None:
	text = _validate_process_text()
	assert 'emit_validation_failure_summary_precheck()' in text
	precheck_emit_helper = "emit_validation_failure_summary_precheck()\n{" + text.split("emit_validation_failure_summary_precheck()\n{", 1)[1].split("\n\nif ! VALIDATION_PRECHECK_FAILURE_OUTPUT=\"$(enforce_canonical_driver_path 2>&1)\"; then", 1)[0]
	canonical_helper = "enforce_canonical_driver_path()\n{" + text.split("enforce_canonical_driver_path()\n{", 1)[1].split("\n\nensure_validation_harness_not_tracked()", 1)[0]
	renamed_helper = "enforce_no_renamed_driver_artifacts()\n{" + text.split("enforce_no_renamed_driver_artifacts()\n{", 1)[1].split("\n\npost_tracking_comment()", 1)[0]
	canonical_guard_block = "if ! VALIDATION_PRECHECK_FAILURE_OUTPUT=\"$(enforce_canonical_driver_path 2>&1)\"; then\n" + text.split("if ! VALIDATION_PRECHECK_FAILURE_OUTPUT=\"$(enforce_canonical_driver_path 2>&1)\"; then\n", 1)[1].split("\n\nif ! VALIDATION_PRECHECK_FAILURE_OUTPUT=\"$(enforce_no_renamed_driver_artifacts 2>&1)\"; then", 1)[0]
	renamed_guard_block = "if ! VALIDATION_PRECHECK_FAILURE_OUTPUT=\"$(enforce_no_renamed_driver_artifacts 2>&1)\"; then\n" + text.split("if ! VALIDATION_PRECHECK_FAILURE_OUTPUT=\"$(enforce_no_renamed_driver_artifacts 2>&1)\"; then\n", 1)[1].split("\n\nextract_last_json_with_key()", 1)[0]

	def _run_precheck_guard_case(script_body: str, *, setup=None, git_repo: bool = False) -> subprocess.CompletedProcess[str]:
		with tempfile.TemporaryDirectory(prefix="validate-precheck-guard-summary-") as td:
			tmp_path = Path(td)
			if git_repo:
				_bootstrap_git_repo(tmp_path)
			if setup is not None:
				setup(tmp_path)

			runner_path = tmp_path / "runner.sh"
			runner_path.write_text(
				f"""#!/usr/bin/env bash
set -euo pipefail
{precheck_emit_helper}
{canonical_helper}
{renamed_helper}

TRACKING_ISSUE_RAW=1234
VALIDATION_CYCLE=4
SELF_HEAL_ATTEMPT=1
MAX_SELF_HEAL_ATTEMPTS=2
GITHUB_REPOSITORY=octo/demo-repo
GITHUB_RUN_ID=77
GITHUB_RUN_ATTEMPT=3
CREATED_FIX_ISSUES_JSON='[]'
CANONICAL_VALIDATE_DRIVER_REL=scripts/validate_process.sh

{script_body}
""",
				encoding="utf-8",
			)
			runner_path.chmod(0o755)
			proc = subprocess.run(
				["bash", str(runner_path)],
				cwd=str(tmp_path),
				env={
					**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
					"BASH_ENV": "",
					"ENV": "",
				},
				text=True,
				capture_output=True,
				timeout=60,
			)
			return proc

	def _summary_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
		return [
			line for line in proc.stdout.splitlines() if line.startswith("VALIDATION_FAILURE_SUMMARY ")
		]

	canonical_proc = _run_precheck_guard_case(canonical_guard_block)
	assert canonical_proc.returncode == 1, canonical_proc.stderr
	canonical_summary_lines = _summary_lines(canonical_proc)
	assert len(canonical_summary_lines) == 1
	assert 'summary="Validation driver path violation"' in canonical_summary_lines[0]
	assert 'raw_status=harness_error' in canonical_summary_lines[0]
	assert 'fix_issues=0' in canonical_summary_lines[0]
	assert 'failure_summary="Refusing to run validate driver from non-canonical path ' in canonical_summary_lines[0]
	assert 'Expected scripts/validate_process.sh."' in canonical_summary_lines[0]
	assert "Refusing to run validate driver from non-canonical path" in canonical_proc.stderr

	def _setup_renamed_artifact(tmp_path: Path) -> None:
		scripts_dir = tmp_path / "scripts"
		scripts_dir.mkdir(parents=True, exist_ok=True)
		validate_driver_body = "#!/usr/bin/env bash\necho validate\n"
		(scripts_dir / "validate_process.sh").write_text(validate_driver_body, encoding="utf-8")
		(scripts_dir / "validate_copy.sh").write_text(validate_driver_body, encoding="utf-8")

	renamed_proc = _run_precheck_guard_case(
		renamed_guard_block,
		setup=_setup_renamed_artifact,
		git_repo=True,
	)
	assert renamed_proc.returncode == 1, renamed_proc.stderr
	renamed_summary_lines = _summary_lines(renamed_proc)
	assert len(renamed_summary_lines) == 1
	assert 'summary="Validation driver artifact violation"' in renamed_summary_lines[0]
	assert 'raw_status=harness_error' in renamed_summary_lines[0]
	assert 'fix_issues=0' in renamed_summary_lines[0]
	assert 'failure_summary="Found renamed managed validate driver artifacts in scripts/: scripts/validate_copy.sh"' in renamed_summary_lines[0]
	assert "Found renamed managed validate driver artifacts in scripts/:" in renamed_proc.stderr
	assert "scripts/validate_copy.sh" in renamed_proc.stderr


def test_bootstrap_guard_paths_emit_failure_summary_before_exit() -> None:
	text = _validate_process_text()
	assert 'require_validation_env_or_emit()' in text
	assert 'emit_validation_failure_summary_bootstrap()' in text
	compact_helper = "compact_validation_summary_value_bootstrap()\n{" + text.split("compact_validation_summary_value_bootstrap()\n{", 1)[1].split("\n\nquote_validation_summary_json_value_bootstrap()", 1)[0]
	quote_helper = "quote_validation_summary_json_value_bootstrap()\n{" + text.split("quote_validation_summary_json_value_bootstrap()\n{", 1)[1].split("\n\nemit_validation_failure_summary_bootstrap()", 1)[0]
	emit_helper = "emit_validation_failure_summary_bootstrap()\n{" + text.split("emit_validation_failure_summary_bootstrap()\n{", 1)[1].split("\n\nrequire_validation_env_or_emit()", 1)[0]
	require_env_helper = "require_validation_env_or_emit()\n{" + text.split("require_validation_env_or_emit()\n{", 1)[1].split("\n\nrequire_validation_env_or_emit \"RUNTIME_DIR\"", 1)[0]
	bootstrap_guard_block = "require_validation_env_or_emit \"RUNTIME_DIR\"\n" + text.split("require_validation_env_or_emit \"RUNTIME_DIR\"\n", 1)[1].split("\n\n_validate_script_dir=", 1)[0]
	timeout_guard_block = "VALIDATION_TIMEOUT=\"${VALIDATION_TIMEOUT:-15}\"\n" + text.split("VALIDATION_TIMEOUT=\"${VALIDATION_TIMEOUT:-15}\"\n", 1)[1].split("\nTOOL_CALL_BUDGET_VALIDATE=", 1)[0]
	codex_guard_block = "if [ ! -f \"${_validate_script_dir}/codex_helpers.sh\" ]; then\n" + text.split("if [ ! -f \"${_validate_script_dir}/codex_helpers.sh\" ]; then\n", 1)[1].split("\nfi\nsource \"${_validate_script_dir}/codex_helpers.sh\"", 1)[0] + "\nfi"
	watchdog_guard_block = "if [ ! -f \"${_validate_script_dir}/watchdog_helpers.sh\" ]; then\n" + text.split("if [ ! -f \"${_validate_script_dir}/watchdog_helpers.sh\" ]; then\n", 1)[1].split("\nfi\nsource \"${_validate_script_dir}/watchdog_helpers.sh\"", 1)[0] + "\nfi"

	def _run_bootstrap_case(script_body: str, *, extra_env: dict[str, str] | None = None, setup=None) -> subprocess.CompletedProcess[str]:
		with tempfile.TemporaryDirectory(prefix="validate-bootstrap-guard-summary-") as td:
			tmp_path = Path(td)
			if setup is not None:
				setup(tmp_path)

			script = f"""set -euo pipefail
{compact_helper}
{quote_helper}
{emit_helper}
{require_env_helper}

TRACKING_ISSUE=1234
VALIDATION_CYCLE=4
SELF_HEAL_ATTEMPT=1
MAX_SELF_HEAL_ATTEMPTS=2
GITHUB_RUN_ID=77
GITHUB_RUN_ATTEMPT=3

{script_body}
"""
			env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
			env.update({
				"BASH_ENV": "",
				"ENV": "",
				"RUNTIME_DIR": str(tmp_path / "runtime"),
				"GH_TOKEN": "token",
				"OPENROUTER_API_KEY": "secret",
				"GITHUB_REPOSITORY": "octo/demo-repo",
				"_validate_script_dir": str(tmp_path / "scripts"),
			})
			if extra_env is not None:
				env.update(extra_env)
			proc = subprocess.run(
				["bash", "-s"],
				cwd=str(tmp_path),
				env=env,
				input=script,
				text=True,
				capture_output=True,
				timeout=60,
			)
			return proc

	def _summary_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
		return [
			line for line in proc.stdout.splitlines() if line.startswith("VALIDATION_FAILURE_SUMMARY ")
		]

	missing_token_proc = _run_bootstrap_case(
		bootstrap_guard_block,
		extra_env={"GH_TOKEN": ""},
	)
	assert missing_token_proc.returncode == 1, missing_token_proc.stderr
	missing_token_summary_lines = _summary_lines(missing_token_proc)
	assert len(missing_token_summary_lines) == 1
	assert 'summary="Validation bootstrap failure"' in missing_token_summary_lines[0]
	assert 'failure_summary="GH_TOKEN is required"' in missing_token_summary_lines[0]
	assert 'repository="octo/demo-repo"' in missing_token_summary_lines[0]
	assert 'tracking_issue=1234' in missing_token_summary_lines[0]
	assert 'raw_status=harness_error' in missing_token_summary_lines[0]
	assert 'self_heal_attempt=1/2' in missing_token_summary_lines[0]
	assert 'fix_issues=0' in missing_token_summary_lines[0]
	assert "GH_TOKEN is required" in missing_token_proc.stderr

	invalid_repo_proc = _run_bootstrap_case(
		bootstrap_guard_block,
		extra_env={"GITHUB_REPOSITORY": "octo-demo-repo"},
	)
	assert invalid_repo_proc.returncode == 1, invalid_repo_proc.stderr
	invalid_repo_summary_lines = _summary_lines(invalid_repo_proc)
	assert len(invalid_repo_summary_lines) == 1
	assert 'summary="Validation bootstrap failure"' in invalid_repo_summary_lines[0]
	assert 'failure_summary="GITHUB_REPOSITORY must be in owner/repo format"' in invalid_repo_summary_lines[0]
	assert 'repository="octo-demo-repo"' in invalid_repo_summary_lines[0]
	assert 'raw_status=harness_error' in invalid_repo_summary_lines[0]
	assert "GITHUB_REPOSITORY must be in owner/repo format" in invalid_repo_proc.stderr

	timeout_proc = _run_bootstrap_case(
		timeout_guard_block,
		extra_env={"VALIDATION_TIMEOUT": "bogus"},
	)
	assert timeout_proc.returncode == 1, timeout_proc.stderr
	timeout_summary_lines = _summary_lines(timeout_proc)
	assert len(timeout_summary_lines) == 1
	assert 'summary="Validation configuration error"' in timeout_summary_lines[0]
	assert 'failure_summary="VALIDATION_TIMEOUT must be a positive integer (got: bogus)"' in timeout_summary_lines[0]
	assert 'repository="octo/demo-repo"' in timeout_summary_lines[0]
	assert 'raw_status=harness_error' in timeout_summary_lines[0]
	assert "VALIDATION_TIMEOUT must be a positive integer (got: bogus)" in timeout_proc.stderr

	codex_proc = _run_bootstrap_case(
		codex_guard_block,
	)
	assert codex_proc.returncode == 1, codex_proc.stderr
	codex_summary_lines = _summary_lines(codex_proc)
	assert len(codex_summary_lines) == 1
	assert 'summary="Validation bootstrap failure"' in codex_summary_lines[0]
	assert 'failure_summary="Missing required support script ' in codex_summary_lines[0]
	assert '/codex_helpers.sh"' in codex_summary_lines[0]
	assert 'raw_status=harness_error' in codex_summary_lines[0]
	assert "::error::Missing required support script" in codex_proc.stderr
	assert "codex_helpers.sh" in codex_proc.stderr

	watchdog_proc = _run_bootstrap_case(
		watchdog_guard_block,
	)
	assert watchdog_proc.returncode == 1, watchdog_proc.stderr
	watchdog_summary_lines = _summary_lines(watchdog_proc)
	assert len(watchdog_summary_lines) == 1
	assert 'summary="Validation bootstrap failure"' in watchdog_summary_lines[0]
	assert 'failure_summary="Missing required support script ' in watchdog_summary_lines[0]
	assert '/watchdog_helpers.sh"' in watchdog_summary_lines[0]
	assert 'raw_status=harness_error' in watchdog_summary_lines[0]
	assert "::error::Missing required support script" in watchdog_proc.stderr
	assert "watchdog_helpers.sh" in watchdog_proc.stderr


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
			env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
			env.update(extra_env)
			env["BASH_ENV"] = ""
			env["ENV"] = ""
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


def test_clear_stale_serena_codex_config_removes_only_serena_block() -> None:
	text = _validate_process_text()
	cleanup_helper = "clear_stale_serena_codex_config()\n{" + text.split("clear_stale_serena_codex_config()\n{", 1)[1].split('\n\nif [ -z "${SERENA_PROJECT_PREEXISTED}" ]; then', 1)[0]

	with tempfile.TemporaryDirectory(prefix="validate-serena-config-") as td:
		root = Path(td)
		home = root / "home"
		config_path = home / ".codex" / "config.toml"
		config_path.parent.mkdir(parents=True, exist_ok=True)
		config_path.write_text(
			'model_reasoning_effort = "xhigh"\n\n'
			'[mcp_servers.serena] # stale\n'
			'command = "/stale/serena"\n'
			'args = ["start-mcp-server"]\n\n'
			'[existing] # keep\n'
			'value = "keep"\n',
			encoding="utf-8",
		)

		result = subprocess.run(
			["bash", "-s"],
			cwd=str(root),
			env={
				**os.environ,
				"HOME": str(home),
				"PYTHONDONTWRITEBYTECODE": "1",
				"BASH_ENV": "",
				"ENV": "",
			},
			input="set -euo pipefail\n" + cleanup_helper + "\nclear_stale_serena_codex_config\n",
			text=True,
			capture_output=True,
			timeout=60,
		)

		assert result.returncode == 0, result.stderr
		body = config_path.read_text(encoding="utf-8")
		assert "[mcp_servers.serena]" not in body
		assert tomllib.loads(body) == {
			"model_reasoning_effort": "xhigh",
			"existing": {"value": "keep"},
		}


def main() -> int:
	test_template_mode_selection_contract_present()
	test_render_recovery_contract_and_prompt_only_self_heal_scope()
	test_template_mode_missing_manifest_returns_harness_error()
	test_template_mode_harness_contract_accepts_missing_validate_env()
	test_render_recovery_lint_gate_contract_present()
	test_tg_notify_preserves_suffixes_and_fails_open()
	test_write_result_files_emits_failure_summary_only_for_non_pass()
	test_phase1_guard_paths_emit_failure_summary_before_exit()
	test_phase0_guard_paths_emit_failure_summary_before_exit()
	test_bootstrap_guard_paths_emit_failure_summary_before_exit()
	test_serena_runtime_filter_hides_only_unchanged_bootstrap_tree()
	test_clear_stale_serena_codex_config_removes_only_serena_block()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
