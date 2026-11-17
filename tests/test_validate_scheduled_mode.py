"""Contract for the opt-in weekly scheduled validation (issue #7553).

The consumer wrapper gains a fixed Monday cron gated by the repository
variable AI_VALIDATE_SCHEDULE_ENABLED. The reusable validate.yml detects a
scheduled run from the caller event (no new workflow_call input), runs
scripts/validate_process.sh with VALIDATE_DETERMINISTIC_ONLY (no model phase,
no self-heal, no per-phase alerts) and reports the result through
scripts/validate_scheduled_sink.sh from the trusted support checkout.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "workflow-templates" / "ai-validate.yml"
REUSABLE = ROOT / ".github" / "workflows" / "validate.yml"
PROCESS = ROOT / "scripts" / "validate_process.sh"

SCHEDULED_MODE_EXPR = (
	"VALIDATE_SCHEDULED_MODE: ${{ github.event_name == 'schedule' && "
	"(inputs.tracking_issue == '' || inputs.tracking_issue == '0') && "
	"inputs.target_ref == '' && 'true' || 'false' }}"
)


def _step_block(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	end = text.find("\n      - name: ", start + 1)
	return text[start:] if end == -1 else text[start:end]


def _gate_block(label: str) -> str:
	text = PROCESS.read_text(encoding="utf-8")
	begin = f"# BEGIN deterministic-only gate (#7553): {label}\n"
	end = f"# END deterministic-only gate (#7553): {label}\n"
	assert text.count(begin) == 1 and text.count(end) == 1
	return text[text.index(begin):text.index(end) + len(end)]


def test_template_adds_gated_weekly_schedule() -> None:
	text = TEMPLATE.read_text(encoding="utf-8")
	assert "  schedule:\n    - cron: \"17 4 * * 1\"\n" in text
	assert "  workflow_dispatch:\n" in text
	assert (
		"    if: github.event_name != 'schedule' || vars.AI_VALIDATE_SCHEDULE_ENABLED == 'true'\n"
		"    uses: shubhodeep1/coding-workflows/.github/workflows/validate.yml@stable\n"
	) in text
	for fallback in (
		"tracking_issue: ${{ inputs.tracking_issue || '0' }}",
		"compose_file: ${{ inputs.compose_file || 'docker-compose.yml' }}",
		"validation_timeout: ${{ inputs.validation_timeout || '15' }}",
		"alert_msg_level: ${{ inputs.alert_msg_level || '' }}",
		"target_ref: ${{ inputs.target_ref || '' }}",
	):
		assert fallback in text
	assert "run-name: \"${{ format('AI Validate [tracking:{0}]', inputs.tracking_issue || '0') }}\"" in text


def test_reusable_detects_scheduled_mode_without_new_input() -> None:
	text = REUSABLE.read_text(encoding="utf-8")
	assert "      " + SCHEDULED_MODE_EXPR + "\n" in text
	assert "scheduled:" not in text.split("    secrets:", 1)[0]


def test_run_step_disables_model_phases_and_alerts_in_scheduled_mode() -> None:
	step = _step_block(REUSABLE.read_text(encoding="utf-8"), "Run validation process")
	assert "MAX_SELF_HEAL_ATTEMPTS: ${{ env.VALIDATE_SCHEDULED_MODE == 'true' && '0' || vars.MAX_SELF_HEAL_ATTEMPTS || '2' }}" in step
	assert "VALIDATE_DETERMINISTIC_ONLY: ${{ env.VALIDATE_SCHEDULED_MODE }}" in step
	assert "ALERT_MSG_LEVEL: ${{ env.VALIDATE_SCHEDULED_MODE == 'true' && 'SILENT' || inputs.alert_msg_level || vars.ALERT_MSG_LEVEL || 'WARNING' }}" in step


def test_sink_step_runs_trusted_copy_before_outcome() -> None:
	text = REUSABLE.read_text(encoding="utf-8")
	step = _step_block(text, "Report scheduled validation result")
	assert "if: \"!cancelled() && env.VALIDATE_SCHEDULED_MODE == 'true' && env.CLAUDE_CAPACITY_WAIT != 'true'\"" in step
	assert "continue-on-error: true" in step
	run = step.split("        run: |\n", 1)[1]
	assert "${{" not in run
	assert 'sink_script="${sink_support_dir}/scripts/validate_scheduled_sink.sh"' in run
	assert '[ -L "${sink_script}" ]' in run
	assert "VALIDATE_SCHEDULED_SINK outcome=skip reason=support_missing" in run
	assert text.index("- name: Record validation run end") < text.index("- name: Report scheduled validation result")
	assert text.index("- name: Report scheduled validation result") < text.index("- name: Enforce validation outcome")


def test_engine_resolution_stays_codex_in_scheduled_mode() -> None:
	step = _step_block(REUSABLE.read_text(encoding="utf-8"), "Resolve AI engine")
	run = step.split("        run: |\n", 1)[1]
	guard = run.index('if [ "${VALIDATE_SCHEDULED_MODE:-false}" = "true" ]; then')
	assert guard < run.index('source "${engine_script}"')
	assert guard < run.index("labels_json=")


def _run_gate(script: str, cwd: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
	env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env_extra}
	return subprocess.run(["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_switch_parsing(tmp_path: Path) -> None:
	block = _gate_block("switch")
	script = "MAX_SELF_HEAL_ATTEMPTS=2\n" + block + 'echo "STATE ${VALIDATE_DETERMINISTIC_ONLY_ACTIVE} ${MAX_SELF_HEAL_ATTEMPTS}"\n'
	cases = {
		None: ("false", "2", False),
		"": ("false", "2", False),
		"false": ("false", "2", False),
		"TRUE": ("true", "0", False),
		"true": ("true", "0", False),
		"maybe": ("false", "2", True),
	}
	for value, (active, budget, warns) in cases.items():
		env = {} if value is None else {"VALIDATE_DETERMINISTIC_ONLY": value}
		proc = _run_gate(script, tmp_path, env)
		assert proc.returncode == 0, proc.stderr
		assert f"STATE {active} {budget}" in proc.stdout
		assert ("::warning::VALIDATE_DETERMINISTIC_ONLY" in proc.stdout) is warns
		assert ("phase=self_heal outcome=skip" in proc.stdout) is (active == "true")


STUBS = (
	'write_result_files() { printf "%s|%s|%s|%s\\n" "$1" "$2" "$3" "$4" > "${RESULT_LOG}"; }\n'
	'run_validate_engine_attempt() { echo ENGINE >> "${RESULT_LOG}"; }\n'
)


def test_discover_gate_reports_harness_missing_without_model(tmp_path: Path) -> None:
	block = _gate_block("discover")
	result = tmp_path / "result.log"
	script = STUBS + block + 'run_validate_engine_attempt "VALIDATE" "validate_discover"\necho CONTINUED\n'
	proc = _run_gate(script, tmp_path, {"RESULT_LOG": str(result), "VALIDATE_DETERMINISTIC_ONLY_ACTIVE": "true"})
	assert proc.returncode == 0, proc.stderr
	assert "CONTINUED" not in proc.stdout
	assert "phase=discover outcome=skip reason=no_committed_manifest" in proc.stdout
	logged = result.read_text(encoding="utf-8")
	assert logged.startswith("error|Validation harness missing|")
	assert logged.strip().endswith("|harness_missing")
	assert "ENGINE" not in logged

	(tmp_path / ".ai").mkdir()
	(tmp_path / ".ai" / "validate.yml").write_text("slots: {}\n", encoding="utf-8")
	result.unlink()
	proc = _run_gate(script, tmp_path, {"RESULT_LOG": str(result), "VALIDATE_DETERMINISTIC_ONLY_ACTIVE": "true"})
	assert "CONTINUED" in proc.stdout

	proc = _run_gate(script, tmp_path / ".ai", {"RESULT_LOG": str(result), "VALIDATE_DETERMINISTIC_ONLY_ACTIVE": "false"})
	assert "CONTINUED" in proc.stdout


def test_diagnose_gate_reports_tests_failed_without_model(tmp_path: Path) -> None:
	block = _gate_block("diagnose")
	result = tmp_path / "result.log"
	script = STUBS + block + 'run_validate_engine_attempt "VALIDATE" "validate_diagnose"\necho CONTINUED\n'
	env = {"RESULT_LOG": str(result), "VALIDATE_DETERMINISTIC_ONLY_ACTIVE": "true", "FAILED_TESTS": "2", "FIRST_FAILURE": "boom"}
	proc = _run_gate(script, tmp_path, env)
	assert proc.returncode == 0, proc.stderr
	assert "CONTINUED" not in proc.stdout
	assert "phase=diagnose outcome=skip reason=deterministic_only" in proc.stdout
	assert result.read_text(encoding="utf-8").strip() == "fail|Runtime validation failed|2 failing test(s): boom|tests_failed"

	env["VALIDATE_DETERMINISTIC_ONLY_ACTIVE"] = "false"
	proc = _run_gate(script, tmp_path, env)
	assert "CONTINUED" in proc.stdout


def test_gates_precede_model_calls() -> None:
	text = PROCESS.read_text(encoding="utf-8")
	discover_gate = text.index("# BEGIN deterministic-only gate (#7553): discover")
	diagnose_gate = text.index("# BEGIN deterministic-only gate (#7553): diagnose")
	assert discover_gate < text.index('run_validate_engine_attempt "VALIDATE" "validate_discover"')
	assert discover_gate < text.index("if [ -f .ai/validate.yml ]; then")
	assert diagnose_gate < text.index('run_validate_engine_attempt "VALIDATE" "validate_diagnose"')
	assert diagnose_gate < text.index('ensure_serena_bootstrap "diagnose"')
	assert text.index("# BEGIN deterministic-only gate (#7553): switch") < discover_gate
	# The diagnose gate also keeps scheduled runs from creating fix-up issues.
	assert diagnose_gate < text.index("FIX_URL_OUTPUT=\"$(gh_retry gh issue create")
	assert re.search(r"write_result_files \"pass\"", text)
