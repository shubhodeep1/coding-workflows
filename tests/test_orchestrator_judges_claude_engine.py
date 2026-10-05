"""Phase 5c (replace-claude-sessions plan): the orchestrator judges on Claude.

scripts/orchestrate_poll_process.sh runs the wave, stall, integration,
security-pass and review-blocked judges through ``poller_claude_judge``. On
Claude the judge goes through ``claude_run``; when the role is on codex or
Claude is unavailable (exit 75) the unchanged codex command runs (plan D1).
These tests run the helper against a stand-in ai_engine.sh and pin each call
site's codex command (G4).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
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
ai_engine_fallback() {
  printf 'AI_ENGINE_FALLBACK role=%s reason=%s\n' "$1" "$2" >&2
}
claude_run() {
  printf '%s|%s|%s|%s|%s|%s|%s\n' "$1" "$(basename "$2")" "$(basename "$3")" "$4" "${AI_ENGINE_LABELS-unset}" "${AI_ENGINE_MODEL_HINT-}" "${AI_ENGINE_EFFORT_HINT-}" >> "${CALLS}.claude"
  case "${FAKE_CLAUDE_MODE}" in
    success) printf 'claude verdict\n' > "$3"; return 0 ;;
    unavailable) echo "AI_ENGINE_FALLBACK role=$1 reason=no_credential" >&2; return 75 ;;
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
	role: str = "WAVE_JUDGE",
	combined_mode: str = "false",
	log_file: str = "judge_log.txt",
) -> tuple[subprocess.CompletedProcess[str], Path]:
	scripts = tmp_path / "scripts"
	scripts.mkdir(parents=True, exist_ok=True)
	if with_engine:
		(scripts / "ai_engine.sh").write_text(FAKE_AI_ENGINE, encoding="utf-8")
	if role == "RB_JUDGE" and combined_mode == "true":
		subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
		(tmp_path / ".gitignore").write_text("rb-claude-untracked-*\nreview_sandbox_transfer_failed\nout.txt\ncalls.*\njudge_log.txt\n", encoding="utf-8")
	prompt = tmp_path / "prompt.txt"
	prompt.write_text("judge this\n", encoding="utf-8")
	calls = tmp_path / "calls"
	labels_line = f"TRACKING_LABELS='{tracking_labels}'\n" if tracking_labels is not None else "unset TRACKING_LABELS\n"
	script = (
		"set -euo pipefail\n"
		f"_POLLER_AI_ENGINE_SH={scripts / 'ai_engine.sh'}\n"
		+ _helper_source()
		+ labels_line
		+ f"MODEL_EDITOR=openai/gpt-6-sol\nMODEL_REASONING_EFFORT_JUDGE=xhigh\nRB_COMBINED_MODE={combined_mode}\n"
		+ "rc=0\n"
		+ f'poller_claude_judge {role} prompt.txt out.txt {log_file} ' + extra + ' || rc=$?\n'
		+ 'echo "rc=${rc}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), FAKE_ENGINE=engine, FAKE_CLAUDE_MODE=claude_mode,
		GITHUB_WORKSPACE=str(tmp_path), RUNTIME_DIR=str(tmp_path), FAKE_SANDBOX_ROOT=str(tmp_path / "sandbox-root"))
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
	assert "AI_ENGINE_SELECTED role=WAVE_JUDGE engine=claude" in _read(tmp_path / "judge_log.txt")


def test_judge_on_codex_returns_75_without_running_claude(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="codex")
	assert "rc=75" in proc.stdout, proc.stderr
	assert _read(Path(f"{calls}.claude")) == ""


def test_dev_null_suppresses_judge_selection_stderr(tmp_path: Path) -> None:
	proc, _calls = _run_helper(tmp_path, engine="codex", log_file="/dev/null")
	assert "rc=75" in proc.stdout
	assert proc.stderr == ""


def test_claude_unavailable_returns_75_so_codex_runs(tmp_path: Path) -> None:
	proc, calls = _run_helper(tmp_path, engine="claude", claude_mode="unavailable")
	assert "rc=75" in proc.stdout, proc.stderr
	assert len(_read(Path(f"{calls}.claude")).splitlines()) == 1


def test_claude_crash_is_not_a_fallback(tmp_path: Path) -> None:
	proc, _calls = _run_helper(tmp_path, engine="claude", claude_mode="crash")
	assert "rc=1" in proc.stdout, proc.stderr


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


FAKE_RB_SANDBOX = r"""#!/usr/bin/env bash
case "$1" in
  prepare-ephemeral)
    [ "$FAKE_CLAUDE_MODE" != prepare_failed ] || exit 1
    printf '%s\n' "$FAKE_SANDBOX_ROOT" ;;
  run)
    printf '%s|%s|%s|%s\n' "$8" "$9" "$AI_ENGINE_LABELS" "$AI_ENGINE_EFFORT_HINT" >> "${CALLS}.sandbox"
    case "$FAKE_CLAUDE_MODE" in
      success) printf 'claude verdict\n' > "$3" ;;
      unavailable) exit 75 ;;
      transfer_failed) printf 'unusable verdict\n' > "$3"; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      transfer_failed_dirty) printf 'unusable verdict\n' > "$3"; mkdir -p leaked; printf 'partial\n' > 'leaked/partial.py'; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      transfer_failed_modified) printf 'unusable verdict\n' > "$3"; printf 'partial\n' > 'preexisting.py'; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      transfer_failed_chmod) printf 'unusable verdict\n' > "$3"; chmod 755 preexisting.py; : > "${RUNTIME_DIR}/review_sandbox_transfer_failed"; exit 1 ;;
      cleanup_failed) printf 'claude verdict\n' > "$3"; printf 'partial\n' > 'new-file.py' ;;
      *) printf 'unusable verdict\n' > "$3"; exit 1 ;;
    esac ;;
  cleanup) printf 'cleanup\n' >> "${CALLS}.sandbox"; [ "$FAKE_CLAUDE_MODE" != cleanup_failed ] ;;
esac
"""


def test_poller_rb_judge_runs_only_in_isolated_sandbox(tmp_path: Path) -> None:
	support = tmp_path / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True)
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_RB_SANDBOX, encoding="utf-8")
	for combined, expected in (("false", "read"), ("true", "write")):
		proc, calls = _run_helper(tmp_path, engine="claude", role="RB_JUDGE", combined_mode=combined)
		assert "rc=0" in proc.stdout, proc.stderr
		assert _read(tmp_path / "out.txt") == "claude verdict\n"
		assert _read(Path(f"{calls}.claude")) == ""
		assert _read(Path(f"{calls}.sandbox")).splitlines()[-2:] == [
			f'RB_JUDGE|{expected}|["bug","ai:engine-claude"]|xhigh', "cleanup",
		]


def test_poller_rb_judge_falls_back_only_when_sandbox_unavailable(tmp_path: Path) -> None:
	support = tmp_path / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True)
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_RB_SANDBOX, encoding="utf-8")
	for mode, expected in (("prepare_failed", 75), ("unavailable", 75), ("crash", 1), ("transfer_failed", 76)):
		proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
		assert f"rc={expected}" in proc.stdout, proc.stderr
		if mode == "prepare_failed":
			assert "AI_ENGINE_FALLBACK role=RB_JUDGE reason=sandbox_prepare_failed" in proc.stderr
		else:
			assert _read(tmp_path / "out.txt") == ""
			assert _read(Path(f"{calls}.sandbox")).splitlines()[-1] == "cleanup"
	assert not (tmp_path / "review_sandbox_transfer_failed").exists()
	(support / "review_untrusted_sandbox.sh").unlink()
	proc, calls = _run_helper(tmp_path, engine="claude", role="RB_JUDGE")
	assert "rc=75" in proc.stdout, proc.stderr
	assert "AI_ENGINE_FALLBACK role=RB_JUDGE reason=sandbox_unavailable" in proc.stderr
	assert _read(Path(f"{calls}.claude")) == ""


def test_rejected_poller_transfer_removes_only_new_untracked_files(tmp_path: Path) -> None:
	assert 'LC_ALL=C comm -z -13 <(LC_ALL=C sort -z "${rb_untracked_before_file}") <(LC_ALL=C sort -z "${rb_untracked_after_file}")' in _helper_source()
	support = tmp_path / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True)
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_RB_SANDBOX, encoding="utf-8")
	preexisting = tmp_path / "preexisting.py"
	preexisting.write_text("keep\n", encoding="utf-8")
	for mode in ("transfer_failed_dirty", "cleanup_failed"):
		proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
		if mode == "cleanup_failed":
			assert proc.returncode == 1 and "stopping the poller" in proc.stderr
			assert "rc=" not in proc.stdout
		else:
			assert "rc=76" in proc.stdout, proc.stderr
			assert _read(tmp_path / "out.txt") == ""
		assert not (tmp_path / "leaked" / "partial.py").exists()
		assert not (tmp_path / "new-file.py").exists()
		assert preexisting.read_text(encoding="utf-8") == "keep\n"
		assert _read(Path(f"{calls}.sandbox")).splitlines()[-1] == "cleanup"
		assert not (tmp_path / "review_sandbox_transfer_failed").exists()


@pytest.mark.parametrize("mode", ("transfer_failed_modified", "transfer_failed_chmod"))
def test_rejected_poller_transfer_stops_if_existing_untracked_file_was_modified(tmp_path: Path, mode: str) -> None:
	support = tmp_path / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True)
	(support / "review_untrusted_sandbox.sh").write_text(FAKE_RB_SANDBOX, encoding="utf-8")
	preexisting = tmp_path / "preexisting.py"
	preexisting.write_text("keep\n", encoding="utf-8")
	proc, calls = _run_helper(tmp_path, engine="claude", claude_mode=mode, role="RB_JUDGE", combined_mode="true")
	assert proc.returncode == 1
	assert "stopping this poll tick" in proc.stderr
	assert "rc=" not in proc.stdout
	if mode == "transfer_failed_modified":
		assert preexisting.read_text(encoding="utf-8") == "partial\n"
	else:
		assert preexisting.stat().st_mode & 0o111
	assert _read(Path(f"{calls}.sandbox")).splitlines()[-1] == "cleanup"


def test_poller_rb_prepare_preserves_engine_selection_log(tmp_path: Path) -> None:
	support = tmp_path / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True)
	(support / "review_untrusted_sandbox.sh").write_text(
		FAKE_RB_SANDBOX.replace('  prepare-ephemeral)\n', '  prepare-ephemeral)\n    printf "sandbox preparation started\\n" >&2\n'),
		encoding="utf-8",
	)
	proc, _calls = _run_helper(tmp_path, engine="claude", role="RB_JUDGE", log_file="judge_log.txt")
	assert "rc=0" in proc.stdout, proc.stderr
	log = _read(tmp_path / "judge_log.txt")
	assert "AI_ENGINE_SELECTED role=RB_JUDGE engine=claude" in log
	assert "sandbox preparation started" in log


# Each judge call site: the role, then the unchanged codex command that runs
# only on exit 75.
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
		'cat "${judge_effective_prompt_file}" | codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access > "${JUDGE_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/judge_log.txt" >&2) || true',
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
	assert '[ "${RB_JUDGE_ENGINE_RC}" -ne 76 ] || break' in text


def _poll_steps() -> list[dict]:
	data = yaml.safe_load(POLL_WORKFLOW.read_text(encoding="utf-8"))
	return data["jobs"]["poll"]["steps"]


def test_poll_job_stages_the_engine_and_fetches_the_pool_only_when_needed() -> None:
	steps = _poll_steps()
	names = [step.get("name") for step in steps]
	stage = steps[names.index("Stage workflow support files")]["run"]
	assert "security_dependency.py ai_engine.sh claude_engine.py claude_settings.json.tmpl codex_stall_guard.sh; do" in stage
	resolve = steps[names.index("Resolve AI engine")]
	assert resolve["id"] == "ai_engine"
	assert resolve["env"]["AI_ENGINE_LABELS"] == ""
	assert '--json number,title,labels' in steps[names.index("Find active tracking issues")]["run"]
	assert 'any(.[]; any(.labels[]?; .name == "ai:engine-claude"))' in resolve["run"]
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
	assert process_env["CLAUDE_FIXER_ENABLED"] == "${{ vars.CLAUDE_FIXER_ENABLED || 'true' }}"


def test_poll_preflight_installs_for_label_even_with_global_codex(tmp_path: Path) -> None:
	steps = _poll_steps()
	preflight = next(step["run"] for step in steps if step.get("name") == "Resolve AI engine")
	issue_file = tmp_path / "tracking_issues.json"
	output_file = tmp_path / "github_output"
	env = {**os.environ, "RUNTIME_DIR": str(tmp_path), "GITHUB_OUTPUT": str(output_file), "AI_ENGINE": "codex", "AI_ENGINE_LABELS": ""}
	for role in ("WAVE_JUDGE", "STALL_JUDGE", "INTEGRATION_JUDGE", "SECURITY_JUDGE", "RB_JUDGE"):
		env[f"AI_ENGINE_{role}"] = ""
	for labels, expected in ((["ai:engine-claude"], "true"), (["ai:codex"], "false")):
		issue_file.write_text(json.dumps([{"number": 1, "labels": [{"name": label} for label in labels]}]), encoding="utf-8")
		output_file.write_text("", encoding="utf-8")
		result = subprocess.run(["bash", "-c", preflight], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
		assert result.returncode == 0, result.stderr
		assert output_file.read_text(encoding="utf-8").strip() == f"any_claude={expected}"


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
	assert "write_codex_config.sh ai_engine.sh claude_engine.py claude_settings.json.tmpl; do" in steps["Stage workflow support files"]["run"]
