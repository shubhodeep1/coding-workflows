#!/usr/bin/env python3
"""Integration-style guards for render-phase preflight recovery wiring."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"
SELF_HEAL_HELPER_PATH = REPO_ROOT / "scripts" / "self_heal_validation.sh"
SELF_HEAL_PROMPT_PATH = REPO_ROOT / "prompts" / "mode-validate-self-heal.txt"


def _validate_process_text() -> str:
	return VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")


def _extract_recovery_helper() -> str:
	text = _validate_process_text()
	match = re.search(r"attempt_preflight_render_recovery\(\)\n\{(?:.|\n)*?\n\}\n", text)
	assert match is not None, "missing attempt_preflight_render_recovery helper in validate_process.sh"
	return match.group(0)


def _run_helper_case(
	*,
	harness_mode: str,
	failure_kind: str,
	render_rc: int,
	recheck_rc: int,
	recheck_kind: str,
) -> tuple[dict[str, str], str]:
	helper = _extract_recovery_helper()
	with tempfile.TemporaryDirectory(prefix="validate-render-recovery-") as td:
		root = Path(td)
		preflight_log = root / "validation_preflight.log"
		script_path = root / "run_case.sh"
		(root / "validation/tests").mkdir(parents=True, exist_ok=True)
		(root / "validation/tests/00_canary.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

		script = f"""#!/usr/bin/env bash
set -euo pipefail

{helper}

render_calls=0
recheck_calls=0

run_template_validation_harness_renderer() {{
	render_calls=$((render_calls + 1))
	return {render_rc}
}}

run_preflight_checks() {{
	recheck_calls=$((recheck_calls + 1))
	PRE_FLIGHT_FAILURE_KIND=\"{recheck_kind}\"
	return {recheck_rc}
}}

HARNESS_MODE=\"{harness_mode}\"
PRE_FLIGHT_FAILURE_KIND=\"{failure_kind}\"
PRE_FLIGHT_RECOVERY_STATUS=\"not_attempted\"
PRE_FLIGHT_LOG_FILE=\"{preflight_log}\"

if attempt_preflight_render_recovery; then
	attempt_rc=0
else
	attempt_rc=$?
fi

printf 'attempt_rc=%s\\n' \"${{attempt_rc}}\"
printf 'recovery_status=%s\\n' \"${{PRE_FLIGHT_RECOVERY_STATUS}}\"
printf 'failure_kind=%s\\n' \"${{PRE_FLIGHT_FAILURE_KIND}}\"
printf 'render_calls=%s\\n' \"${{render_calls}}\"
printf 'recheck_calls=%s\\n' \"${{recheck_calls}}\"
"""
		script_path.write_text(script, encoding="utf-8")
		script_path.chmod(0o755)

		proc = subprocess.run(
			["bash", str(script_path)],
			cwd=str(root),
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"

		payload: dict[str, str] = {}
		for line in proc.stdout.splitlines():
			if "=" in line:
				k, v = line.strip().split("=", 1)
				payload[k] = v

		log_text = preflight_log.read_text(encoding="utf-8") if preflight_log.exists() else ""
		return payload, log_text


def test_render_recovery_metadata_contract_present() -> None:
	text = _validate_process_text()
	assert 'PRE_FLIGHT_FAILURE_KIND="none"' in text
	assert 'PRE_FLIGHT_RECOVERY_STATUS="not_attempted"' in text
	assert '--arg pre_flight_failure_kind "${PRE_FLIGHT_FAILURE_KIND}" \\' in text
	assert '--arg pre_flight_recovery_status "${PRE_FLIGHT_RECOVERY_STATUS}" \\' in text
	assert "pre_flight_failure_kind: $pre_flight_failure_kind" in text
	assert "pre_flight_recovery_status: $pre_flight_recovery_status" in text


def test_render_recovery_succeeds_after_rerender_and_relint() -> None:
	payload, log_text = _run_helper_case(
		harness_mode="template_generate",
		failure_kind="lint",
		render_rc=0,
		recheck_rc=0,
		recheck_kind="none",
	)
	assert payload["attempt_rc"] == "0"
	assert payload["recovery_status"] == "recovered"
	assert payload["render_calls"] == "1"
	assert payload["recheck_calls"] == "1"
	assert "deterministic re-render + re-lint succeeded" in log_text


def test_render_recovery_fail_open_when_renderer_retry_fails() -> None:
	payload, log_text = _run_helper_case(
		harness_mode="template_generate",
		failure_kind="lint",
		render_rc=14,
		recheck_rc=0,
		recheck_kind="none",
	)
	assert payload["attempt_rc"] == "1"
	assert payload["recovery_status"] == "render_failed"
	assert payload["render_calls"] == "1"
	assert payload["recheck_calls"] == "0"
	assert "continuing with original preflight failure path" in log_text


def test_render_recovery_fail_open_when_relint_still_fails() -> None:
	payload, log_text = _run_helper_case(
		harness_mode="template_generate",
		failure_kind="lint",
		render_rc=0,
		recheck_rc=1,
		recheck_kind="lint",
	)
	assert payload["attempt_rc"] == "1"
	assert payload["recovery_status"] == "recheck_failed"
	assert payload["render_calls"] == "1"
	assert payload["recheck_calls"] == "1"
	assert "continuing with terminal preflight failure path" in log_text


def test_render_recovery_not_applicable_outside_lint_template_case() -> None:
	payload, _ = _run_helper_case(
		harness_mode="template_generate",
		failure_kind="structural",
		render_rc=0,
		recheck_rc=0,
		recheck_kind="none",
	)
	assert payload["attempt_rc"] == "1"
	assert payload["recovery_status"] == "not_applicable"
	assert payload["render_calls"] == "0"
	assert payload["recheck_calls"] == "0"


def test_preflight_failure_branch_switches_to_render_phase_tagging() -> None:
	text = _validate_process_text()
	needle = "if ! run_preflight_checks; then"
	idx = text.find(needle)
	assert idx != -1, "missing preflight failure branch"
	slice_text = text[idx : idx + 3000]
	assert "if attempt_preflight_render_recovery; then" in slice_text
	assert 'self_heal_phase="preflight"' in slice_text
	assert 'self_heal_phase="render"' in slice_text
	assert 'attempt_self_heal_and_reexec "${self_heal_phase}"' in slice_text


def test_render_recovery_helper_contains_no_freehand_generation_path() -> None:
	helper = _extract_recovery_helper()
	assert "codex exec" not in helper
	assert "mode-validate-generate.txt" not in helper
	assert "mode-validate-fix-harness.txt" not in helper


def test_self_heal_allow_list_and_prompt_guardrails_preserved() -> None:
	helper_text = SELF_HEAL_HELPER_PATH.read_text(encoding="utf-8")
	for target in (
		'\"mode-validate-discover.txt\"',
		'\"mode-validate-generate.txt\"',
		'\"mode-validate-fix-harness.txt\"',
		'\"mode-validate-diagnose.txt\"',
	):
		assert target in helper_text
	assert '\"mode-validate-self-heal.txt\"' not in helper_text
	assert 'SELF_HEAL_FAILURE_PHASE    — string tag (\"generate\"|\"preflight\"|\"render\"' in helper_text

	prompt_text = SELF_HEAL_PROMPT_PATH.read_text(encoding="utf-8")
	assert "Render-phase harness recovery is handled mechanically by `scripts/validate_process.sh`" in prompt_text
	assert "You may ONLY edit these four files" in prompt_text


def main() -> int:
	failed = 0
	for name in sorted(n for n in globals() if n.startswith("test_")):
		try:
			globals()[name]()
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
