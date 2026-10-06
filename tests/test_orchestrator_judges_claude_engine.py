"""Phase 5c (replace-claude-sessions plan): the orchestrator judges on Claude.

scripts/orchestrate_poll_process.sh runs the wave, stall, integration,
security-pass and review-blocked judges through ``poller_claude_judge``. On
Claude the judge goes through ``claude_run``; when the role is on codex or
Claude is unavailable (exit 75), the wave judge uses isolated Codex and
the other judges use their existing Codex commands (plan D1). These tests
run the helper against a stand-in ai_engine.sh and pin each fallback (G4).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"

# A stand-in for scripts/ai_engine.sh: the engine comes from FAKE_ENGINE, and
# claude_run records its role, files, labels and hints, then plays
# FAKE_CLAUDE_MODE (success writes "claude verdict", unavailable returns 75,
# crash returns 1).
FAKE_AI_ENGINE = r"""
ai_engine_for_role() {
  printf '%s|%s\n' "$1" "${AI_ENGINE_LABELS-unset}" >> "${CALLS}.resolve"
  echo "AI_ENGINE_SELECTED role=$1 engine=${FAKE_ENGINE}" >&2
  printf '%s\n' "${FAKE_ENGINE}"
}
claude_run() {
  printf '%s|%s|%s|%s|%s|%s|%s\n' "$1" "$(basename "$2")" "$(basename "$3")" "$4" "${AI_ENGINE_LABELS-unset}" "${AI_ENGINE_MODEL_HINT-}" "${AI_ENGINE_EFFORT_HINT-}" >> "${CALLS}.claude"
  printf '%s\n' "${AI_ENGINE_READ_ONLY-unset}" >> "${CALLS}.profile"
  case "${FAKE_CLAUDE_MODE}" in
    success) printf 'claude verdict\n' > "$3"; return 0 ;;
    unavailable) echo "AI_ENGINE_FALLBACK role=$1 reason=no_credential" >&2; return 75 ;;
    tampered) return 86 ;;
    *) return 1 ;;
  esac
}
"""


def _helper_source() -> str:
	text = POLLER.read_text(encoding="utf-8")
	match = re.search(r"^poller_claude_judge\(\)\n\{\n.*?^\}\n", text, re.M | re.S)
	assert match, "poller_claude_judge() not found"
	return match.group(0)


def _run_helper(
	tmp_path: Path,
	*,
	engine: str,
	claude_mode: str = "success",
	with_engine: bool = True,
	extra: str = "",
	tracking_labels: str | None = '["bug","ai:engine-claude"]',
) -> tuple[subprocess.CompletedProcess[str], Path]:
	scripts = tmp_path / "scripts"
	scripts.mkdir(parents=True, exist_ok=True)
	if with_engine:
		(scripts / "ai_engine.sh").write_text(FAKE_AI_ENGINE, encoding="utf-8")
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("judge this\n", encoding="utf-8")
	calls = tmp_path / "calls"
	labels_line = f"TRACKING_LABELS='{tracking_labels}'\n" if tracking_labels is not None else "unset TRACKING_LABELS\n"
	script = (
		"set -euo pipefail\n"
		f"_POLLER_AI_ENGINE_SH={scripts / 'ai_engine.sh'}\n"
		+ _helper_source()
		+ labels_line
		+ "MODEL_EDITOR=openai/gpt-6-sol\nMODEL_REASONING_EFFORT_JUDGE=xhigh\n"
		+ "rc=0\n"
		+ 'poller_claude_judge WAVE_JUDGE prompt.txt out.txt judge_log.txt ' + extra + ' || rc=$?\n'
		+ 'echo "rc=${rc}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), FAKE_ENGINE=engine, FAKE_CLAUDE_MODE=claude_mode)
	for key in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(key, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, calls


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8") if path.exists() else ""


def test_judge_on_claude_runs_claude_run_with_the_tracking_labels(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude")
	assert "rc=0" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "claude verdict\n"
	assert _read(Path(f"{calls}.claude")).splitlines() == [
		f'WAVE_JUDGE|prompt.txt|out.txt|{tmp_path}|["bug","ai:engine-claude"]|openai/gpt-6-sol|xhigh',
	]
	assert _read(Path(f"{calls}.resolve")).splitlines() == ['WAVE_JUDGE|["bug","ai:engine-claude"]']
	assert _read(Path(f"{calls}.profile")).splitlines() == ["true"]
	assert "AI_ENGINE_SELECTED role=WAVE_JUDGE engine=claude" in _read(tmp_path / "judge_log.txt")


def test_judge_on_codex_returns_75_without_running_claude(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="codex")
	assert "rc=75" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.claude")) == ""


def test_claude_unavailable_returns_75_so_codex_runs(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude", claude_mode="unavailable")
	assert "rc=75" in proc.stdout, proc.stderr
	assert len(_read(Path(f"{calls}.claude")).splitlines()) == 1


def test_claude_crash_is_not_a_fallback(tmp_path: Path) -> None:
	proc, _calls = _run_helper(tmp_path, engine="claude", claude_mode="crash")
	assert "rc=1" in proc.stdout, proc.stderr


def test_tampered_support_is_terminal_for_judge(tmp_path: Path) -> None:
	proc, _calls = _run_helper(tmp_path, engine="claude", claude_mode="tampered")
	assert proc.returncode == 86
	assert "Trusted support changed" in proc.stderr


def test_poll_job_verifies_support_before_running_followup_steps() -> None:
	workflow = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["poll"]["steps"]
	by_name = {step["name"]: step for step in steps}
	names = list(by_name)
	assert names.index("Record trusted support integrity") < names.index("Process each tracking issue")
	assert names.index("Release staged-support latches without active projects") < names.index("Verify trusted support integrity") < names.index("Run worktree registry GC")
	assert by_name["Verify trusted support integrity"]["id"] == "verify_support_integrity"
	assert by_name["Verify trusted support integrity"]["if"] == "always()"
	for name in ("Run worktree registry GC", "Build state snapshot", "Publish state snapshot branch", "Write run summary", "Record poll run end", "Notify Telegram on job failure"):
		assert "steps.verify_support_integrity.outcome != 'failure'" in by_name[name]["if"]
	assert 'if [ "${security_audit_run_rc}" -eq 86 ]' in POLLER.read_text(encoding="utf-8")


def test_security_pass_codex_config_failure_only_blocks_codex_selected_audit(tmp_path: Path) -> None:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index('  security_audit_selected_engine="codex"\n')
	end = text.index('  local security_audit_run_rc=0\n', start)
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts" / "write_codex_config.sh").write_text("exit 1\n", encoding="utf-8")
	(tmp_path / "scripts" / "ai_engine.sh").write_text(
		'ai_engine_for_role() {\n'
		'  if [[ "${AI_ENGINE_LABELS}" == *ai:codex* ]] || [ "${AI_ENGINE_SECURITY_AUDIT:-}" = codex ]; then echo codex; else echo claude; fi\n'
		'}\n', encoding="utf-8")
	script = (
		"set -euo pipefail\n"
		"security_pass_fail_closed() { echo \"$1\" > failure.txt; }\n"
		"_POLLER_AI_ENGINE_SH=scripts/ai_engine.sh\n"
		"check_audit_setup() {\nprior_security_status=pending\naudit_error_file=config.err\n"
		+ text[start:end]
		+ "}\ncheck_audit_setup\n"
	)
	for labels, override, expected_engine, expected_rc in (
		('["ai:engine-claude"]', "", "claude", 0),
		('["ai:codex","ai:engine-claude"]', "", "codex", 1),
		('["ai:engine-claude"]', "codex", "codex", 1),
	):
		proc = subprocess.run(["bash", "-c", script], cwd=tmp_path,
			env={**{key: value for key, value in os.environ.items() if key not in {"BASH_ENV", "ENV", "WORKSPACE_PATH"}},
				"TRACKING_LABELS": labels, "AI_ENGINE_SECURITY_AUDIT": override,
				"AI_ENGINE_RESOLVED_SECURITY_AUDIT": "claude"},
			capture_output=True, text=True, check=False)
		assert proc.returncode == expected_rc, proc.stderr
		if expected_engine == "claude":
			assert "Codex fallback will fail preflight" in proc.stdout
			assert not (tmp_path / "failure.txt").exists()
		else:
			assert (tmp_path / "failure.txt").read_text(encoding="utf-8").strip() == "engine_unavailable"
	assert 'AI_ENGINE_RESOLVED_SECURITY_AUDIT="${security_audit_selected_engine}" \\' in text
	assert 'AI_ENGINE_LABELS="${TRACKING_LABELS:-[]}" \\' in text


def test_missing_ai_engine_returns_75(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude", with_engine=False)
	assert "rc=75" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.resolve")) == ""


def test_no_tracking_labels_means_an_explicit_empty_list(tmp_path: Path) -> None:
	_proc, calls = _run_helper(tmp_path, engine="codex", tracking_labels=None)
	assert _read(Path(f"{calls}.resolve")).splitlines() == ["WAVE_JUDGE|[]"]


def test_model_hint_argument_overrides_model_editor(tmp_path: Path) -> None:
	_proc, calls = _run_helper(tmp_path, engine="claude", extra="claude-sonnet-5-5")
	assert _read(Path(f"{calls}.claude")).split("|")[5] == "claude-sonnet-5-5"


# Each judge call site: the role, then its Codex fallback that runs on exit 75.
SITES = {
	"SECURITY_JUDGE": (
		'poller_claude_judge SECURITY_JUDGE "${prompt_file}" "${output_file}" "${error_file}" "${effective_judge_model}" || security_judge_rc=$?',
		'if [ "${security_judge_rc}" -eq 75 ]; then',
		'-- codex --ask-for-approval never -c model_verbosity=low exec --skip-git-repo-check \\\n            --model "${effective_judge_model}" --sandbox read-only < "${prompt_file}" || true',
	),
	"INTEGRATION_JUDGE": (
		'poller_claude_judge INTEGRATION_JUDGE "${prompt_file}" "${output_file}" "${RUNTIME_DIR}/integration_judge.log" "${MODEL_EDITOR:-openai/gpt-6-sol}" || integration_judge_rc=$?',
		'if [ "${integration_judge_rc}" -eq 75 ]; then',
		'cat "${prompt_file}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR:-openai/gpt-6-sol}" --sandbox danger-full-access > "${output_file}" 2>> "${RUNTIME_DIR}/integration_judge.log" || integration_judge_rc=$?',
	),
	"STALL_JUDGE": (
		'poller_claude_judge STALL_JUDGE "${stall_judge_prompt_file}" "${stall_judge_output_file}" "${RUNTIME_DIR}/stall_judge.log" || stall_judge_rc=$?',
		'if [ "${stall_judge_rc}" -eq 75 ]; then',
		'codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${stall_judge_prompt_file}" > "${stall_judge_output_file}" 2>> "${RUNTIME_DIR}/stall_judge.log" || true',
	),
	"RB_JUDGE": (
		'poller_claude_judge RB_JUDGE "${RB_JUDGE_PROMPT_FILE}" "${RB_JUDGE_OUTPUT_FILE}" /dev/null || RB_JUDGE_ENGINE_RC=$?',
		'if [ "${RB_JUDGE_ENGINE_RC}" -eq 75 ]; then',
		'cat "${RB_JUDGE_PROMPT_FILE}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access > "${RB_JUDGE_OUTPUT_FILE}" 2>/dev/null || true',
	),
	"WAVE_JUDGE": (
		'poller_claude_judge WAVE_JUDGE "${judge_effective_prompt_file}" "${JUDGE_OUTPUT_FILE}" "${RUNTIME_DIR}/judge_log.txt" || wave_judge_rc=$?',
		'if [ "${wave_judge_rc}" -eq 75 ]; then',
		'bash scripts/clarify_isolated_run.sh "${judge_effective_prompt_file}" "${JUDGE_OUTPUT_FILE}" "${RUNTIME_DIR}/judge_log.txt" codex WAVE_JUDGE || {',
	),
}


def test_each_judge_tries_claude_then_runs_the_unchanged_codex_command() -> None:
	text = POLLER.read_text(encoding="utf-8")
	for role, (claude_call, gate, codex_call) in SITES.items():
		assert text.count(claude_call) == 1, role
		start = text.index(claude_call)
		assert text.index(gate, start) > start, role
		assert text.index(codex_call, start) > text.index(gate, start), role
		assert text.count(codex_call) == 1, role
	assert len(re.findall(r"^\s+poller_claude_judge [A-Z_]+ ", text, re.M)) == len(SITES)
	assert "codex --ask-for-approval" not in SITES["WAVE_JUDGE"][2]
	assert 'MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT_JUDGE:-high}"' in text
	fallback = REPO_ROOT / "scripts" / "clarify_isolated_run.sh"
	isolation = fallback.read_text(encoding="utf-8")
	assert 'CLARIFY|CLARIFY_RESPOND|WAVE_JUDGE' in isolation
	assert 'env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}"' in isolation
	assert '--network none --read-only --cap-drop ALL' in isolation
	assert '--mount "type=bind,src=${run_root}/source,dst=/source,readonly"' in isolation
	assert '--env CLARIFY_PROXY_KEY=isolated-placeholder' in isolation
	assert '"id_ed25519"' in isolation


def test_wave_judge_fallback_never_starts_host_codex(tmp_path: Path) -> None:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("    wave_judge_rc=0\n")
	end = text.index('    rm -f "${judge_attempt_prompt_file}"', start)
	scripts = tmp_path / "scripts"
	scripts.mkdir()
	(scripts / "clarify_isolated_run.sh").write_text("# staged support\n", encoding="utf-8")
	for available in (True, False):
		(tmp_path / "verdict.txt").write_text("stale verdict\n", encoding="utf-8")
		if not available:
			(scripts / "clarify_isolated_run.sh").unlink()
		script = (
			"set -euo pipefail\n"
			"poller_claude_judge() { return 75; }\n"
			"bash() { printf '%s|%s|%s|%s\\n' \"$1\" \"$5\" \"$6\" \"${MODEL_REASONING_EFFORT}\" > call.txt; return 1; }\n"
			"codex() { echo 'UNSAFE HOST CODEX' > host-codex.txt; }\n"
			"MODEL_REASONING_EFFORT_JUDGE=xhigh\n"
			"MODEL_EDITOR=openai/gpt-6-sol\n"
			"TRACKING_NUM=42\n"
			"RUNTIME_DIR=.\n"
			"judge_effective_prompt_file=prompt.txt\n"
			"JUDGE_OUTPUT_FILE=verdict.txt\n"
			+ text[start:end]
		)
		proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False)
		assert proc.returncode == 0, proc.stderr
		assert not (tmp_path / "host-codex.txt").exists()
		assert (tmp_path / "verdict.txt").read_text(encoding="utf-8") == ""
		if available:
			assert (tmp_path / "call.txt").read_text(encoding="utf-8").strip() == "scripts/clarify_isolated_run.sh|codex|WAVE_JUDGE|xhigh"
			assert "Isolated wave-judge fallback failed for tracking issue #42" in proc.stderr
		else:
			assert "refusing host Codex" in proc.stderr


def _poll_steps() -> list[dict]:
	data = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	return data["jobs"]["poll"]["steps"]


def test_poll_job_stages_the_engine_and_fetches_the_pool_only_when_needed() -> None:
	steps = _poll_steps()
	names = [step.get("name") for step in steps]
	stage = steps[names.index("Stage workflow support files")]["run"]
	assert "security_dependency.py ai_engine.sh claude_engine.py claude_read_isolated_run.sh claude_read_snapshot.py claude_anthropic_relay.py review_untrusted_workspace.py claude_settings.json.tmpl codex_stall_guard.sh clarify_isolated_run.sh clarify_openrouter_broker.py codex_model_catalog.json; do" in stage
	assert 'install -m 0644 "${sandbox_src}" scripts/clarify_sandbox/Dockerfile' in stage
	resolve = steps[names.index("Resolve AI engine")]
	assert resolve["id"] == "ai_engine"
	assert resolve["env"]["AI_ENGINE_LABELS"] == ""
	assert "for role in WAVE_JUDGE STALL_JUDGE INTEGRATION_JUDGE SECURITY_JUDGE RB_JUDGE; do" in resolve["run"]
	for name, uses in (
		("Install Claude Code CLI", "./.codex-workflow-src/.github/actions/install-claude"),
		("Resolve Claude credential", "./.codex-workflow-src/.github/actions/claude-pool-token"),
	):
		step = steps[names.index(name)]
		assert step["uses"] == uses
		assert step["continue-on-error"] is True
		assert step["if"] == "steps.find_tracking.outputs.has_work == 'true' && steps.ai_engine.outputs.any_claude == 'true'"
		assert names.index(name) < names.index("Process each tracking issue")
	process_env = steps[names.index("Process each tracking issue")]["env"]
	for role in ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "RB_JUDGE"):
		assert process_env[f"AI_ENGINE_{role}"] == f"${{{{ vars.AI_ENGINE_{role} || '' }}}}"
	assert process_env["AI_ENGINE_SECURITY_AUDIT"] == "${{ vars.AI_ENGINE_SECURITY_AUDIT || '' }}"
	assert process_env["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"


ORCHESTRATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate.yml"
DECOMPOSER_CODEX = (
	'timeout --signal=TERM --kill-after=30s -- "${ORCHESTRATE_DECOMPOSER_PER_ATTEMPT_TIMEOUT_SECS}" \\\n'
	'       codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${CODEX_PROMPT_FILE}" > "${CODEX_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2) || decomposer_rc=$?'
)


def _orchestrate_steps() -> dict[str, dict]:
	data = yaml.safe_load(ORCHESTRATE_WORKFLOW.read_text(encoding="utf-8"))
	return {step.get("name"): step for step in data["jobs"]["orchestrate"]["steps"]}


def _decomposer_engine_block() -> str:
	run = _orchestrate_steps()["Run Codex (decomposer)"]["run"]
	start = run.index("  decomposer_rc=75\n")
	end = run.index('  if [ "${decomposer_rc}" -eq 0 ]; then\n')
	return run[start:end]


def _run_decomposer_block(tmp_path: Path, engine: str, claude_mode: str) -> tuple[subprocess.CompletedProcess[str], Path]:
	scripts = tmp_path / "scripts"
	scripts.mkdir(parents=True, exist_ok=True)
	(scripts / "ai_engine.sh").write_text(FAKE_AI_ENGINE, encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	codex = bin_dir / "codex"
	codex.write_text('#!/usr/bin/env bash\nprintf "codex %s\\n" "$*" >> "${CALLS}.codex"\nprintf "codex plan\\n"\n', encoding="utf-8")
	codex.chmod(0o755)
	(tmp_path / "prompt.txt").write_text("decompose\n", encoding="utf-8")
	calls = tmp_path / "calls"
	script = (
		"set -euo pipefail\n"
		"CODEX_PROMPT_FILE=prompt.txt\nCODEX_OUTPUT_FILE=out.txt\nRUNTIME_DIR=.\n"
		"ORCHESTRATE_DECOMPOSER_PER_ATTEMPT_TIMEOUT_SECS=30\nMODEL_EDITOR=openai/gpt-6-sol\nMODEL_REASONING_EFFORT=high\n"
		f"ORCHESTRATE_ENGINE={engine}\n"
		+ _decomposer_engine_block()
		+ 'echo "rc=${decomposer_rc} engine=${ORCHESTRATE_ENGINE}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), FAKE_ENGINE="claude", FAKE_CLAUDE_MODE=claude_mode, PATH=f"{bin_dir}:{os.environ['PATH']}")
	for key in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(key, None)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, calls


def test_decomposer_on_claude_runs_claude_run_only(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "claude", "success")
	assert "rc=0 engine=claude" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "claude verdict\n"
	assert _read(Path(f"{calls}.claude")).split("|")[:3] == ["ORCHESTRATE", "prompt.txt", "out.txt"]
	assert _read(Path(f"{calls}.codex")) == ""


def test_decomposer_falls_back_to_codex_and_stays_there(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "claude", "unavailable")
	assert "rc=0 engine=codex" in proc.stdout, proc.stderr
	assert _read(tmp_path / "out.txt") == "codex plan\n"
	assert "exec --skip-git-repo-check --model openai/gpt-6-sol --sandbox danger-full-access" in _read(Path(f"{calls}.codex"))


def test_decomposer_claude_crash_keeps_its_status(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "claude", "crash")
	assert "rc=1 engine=claude" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.codex")) == ""


def test_decomposer_on_codex_never_touches_claude(tmp_path: Path) -> None:
	proc, calls = _run_decomposer_block(tmp_path, "codex", "success")
	assert "rc=0 engine=codex" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.claude")) == ""
	assert _read(tmp_path / "out.txt") == "codex plan\n"


def test_decomposer_codex_command_and_failure_status_are_kept() -> None:
	run = _orchestrate_steps()["Run Codex (decomposer)"]["run"]
	assert run.count(DECOMPOSER_CODEX) == 1
	assert 'rc="${decomposer_rc}"' in run
	assert "    rc=$?\n" not in run


def test_orchestrate_job_resolves_the_engine_from_the_engine_input() -> None:
	steps = _orchestrate_steps()
	names = list(steps)
	labels = "${{ inputs.engine == 'claude' && 'ai:engine-claude' || (inputs.engine == 'codex' && 'ai:codex' || '') }}"
	resolve = steps["Resolve AI engine"]
	assert resolve["env"]["AI_ENGINE_LABELS"] == labels
	assert 'engine="$(ai_engine_for_role ORCHESTRATE || echo codex)"' in resolve["run"]
	assert steps["Run Codex (decomposer)"]["env"]["AI_ENGINE_LABELS"] == labels
	assert steps["Run Codex (decomposer)"]["env"]["ORCHESTRATE_ENGINE"] == "${{ steps.ai_engine.outputs.engine || 'codex' }}"
	for name in ("Install Claude Code CLI", "Resolve Claude credential"):
		assert steps[name]["if"] == "steps.ai_engine.outputs.engine == 'claude'"
		assert names.index("Resolve AI engine") < names.index(name) < names.index("Run Codex (decomposer)")
	assert "write_codex_config.sh ai_engine.sh claude_engine.py; do" in steps["Stage workflow support files"]["run"]
