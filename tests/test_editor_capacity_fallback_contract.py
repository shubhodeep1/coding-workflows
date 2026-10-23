"""Contract test for the editor capacity-fallback wiring.

Issue #3515 / run 28640359211: a sustained OpenRouter/OpenAI per-model TPM
saturation on the primary editor model (gpt-5.4 at the time; the primary
default is now gpt-6-sol) burned every retry attempt because all attempts used
the same model. The fix lets the editor retry loops switch to a fallback
editor model (MODEL_EDITOR_FALLBACK, default openai/gpt-5.6-sol — a different
capacity bucket) on their FINAL attempt.

This test pins the three moving parts so a future edit cannot silently drop
the fallback:
  1. openai/gpt-5.6-sol is declared in the model catalog (so codex can resolve
     apply_patch / verbosity for it when the loop switches --model).
  2. Each editor-driven workflow exposes MODEL_EDITOR_FALLBACK.
  3. Each retry loop actually switches to the fallback on the final attempt.
"""

import json
import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG = REPO_ROOT / "scripts" / "codex_model_catalog.json"
PLAN = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
IMPLEMENT = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"

FALLBACK_SLUG = "openai/gpt-5.6-sol"
FALLBACK_ENV_DEFAULT = (
	"MODEL_EDITOR_FALLBACK: ${{ vars.WORKFLOW_EDITOR_FALLBACK_MODEL "
	"|| 'openai/gpt-5.6-sol' }}"
)


def _catalog_entry(slug: str) -> dict:
	payload = json.loads(CATALOG.read_text(encoding="utf-8"))
	for entry in payload["models"]:
		if entry.get("slug") == slug:
			return entry
	raise AssertionError(f"{slug} not declared in {CATALOG}")


def test_fallback_model_declared_in_catalog() -> None:
	entry = _catalog_entry(FALLBACK_SLUG)
	# The loops switch --model to this slug while the config still points
	# model_catalog_json at the catalog file. If apply_patch resolution or
	# xhigh support is missing, the switched attempt regresses relative to
	# the primary. These mirror the primary (same OpenAI family).
	assert entry.get("apply_patch_tool_type") == "function"
	assert entry.get("support_verbosity") is True
	assert entry.get("supported_in_api") is True
	efforts = {
		level.get("effort")
		for level in entry.get("supported_reasoning_levels", [])
	}
	assert "xhigh" in efforts, (
		f"{FALLBACK_SLUG} must advertise xhigh (the repo-wide editor "
		f"reasoning level); got {sorted(efforts)}"
	)


def test_workflows_declare_fallback_env() -> None:
	for path in (PLAN, IMPLEMENT, REVIEW):
		text = path.read_text(encoding="utf-8")
		assert FALLBACK_ENV_DEFAULT in text, (
			f"{path} must declare MODEL_EDITOR_FALLBACK defaulting to "
			f"{FALLBACK_SLUG} via WORKFLOW_EDITOR_FALLBACK_MODEL"
		)


def test_plan_loop_switches_model_on_final_attempt() -> None:
	text = PLAN_RUNNER.read_text(encoding="utf-8")
	assert 'attempt_model="${MODEL_EDITOR}"' in text
	assert (
		'[ "${attempt}" -eq "${max_attempts}" ] '
		'&& [ -n "${MODEL_EDITOR_FALLBACK:-}" ]'
	) in text
	assert 'attempt_model="${MODEL_EDITOR_FALLBACK}"' in text
	# The codex invocation must consume the per-attempt model, not the raw
	# primary — otherwise the switch is computed but never applied.
	assert '--model "${attempt_model}"' in text


def test_implement_loop_switches_model_on_final_attempt() -> None:
	text = IMPLEMENT.read_text(encoding="utf-8")
	assert 'attempt_model="${MODEL_EDITOR}"' in text
	assert (
		'[ "${attempt}" -eq "${max_attempts}" ] '
		'&& [ -n "${MODEL_EDITOR_FALLBACK:-}" ]'
	) in text
	assert 'CODEX_THREAD_REUSE_MODEL="${attempt_model}"' in text
	# The MAIN implement pass switches to the per-attempt model. The
	# secondary implement_repair (syntax-repair) pass intentionally keeps the
	# primary MODEL_EDITOR — it reuses a codex thread with a replace-prefix
	# transform where a mid-continuation model swap is both riskier and
	# unnecessary (it is not the token-heavy pass that hits TPM saturation).
	# Pin that boundary: exactly one raw-primary CODEX_THREAD_REUSE_MODEL
	# assignment remains (the repair pass).
	assert text.count('CODEX_THREAD_REUSE_MODEL="${MODEL_EDITOR}"') == 1


def test_review_apply_fixes_switches_model_on_final_attempt() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert 'editor_max_attempts=3' in text
	assert 'while [ "${attempt}" -le "${editor_max_attempts}" ]' in text
	assert '[ "${attempt}" -eq "${editor_max_attempts}" ]' in text
	# The OpenCode config and command consume the per-attempt model, so the
	# fallback receives matching limits and role permissions.
	assert '--role writer' in text
	assert '--model "${editor_attempt_model}"' in text
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_untrusted_sandbox.sh" run' in text
	assert '"${stdout_file}"\n    "${editor_attempt_model}"' in text
	assert '"${EDITOR_REASONING_EFFORT}"' in text
	# The loop sets the fallback on the final attempt.
	assert 'EDITOR_ATTEMPT_MODEL="${MODEL_EDITOR_FALLBACK}"' in text
	assert 'local editor_attempt_model="${EDITOR_ATTEMPT_MODEL:-${MODEL_EDITOR}}"' in text


FINAL_ATTEMPT_IF = (
	'if [ "${attempt}" -eq "${editor_max_attempts}" ] '
	'&& [ -n "${MODEL_EDITOR_FALLBACK:-}" ] '
	'&& [ "${MODEL_EDITOR_FALLBACK}" != "${MODEL_EDITOR}" ]; then'
)
FINAL_ATTEMPT_ECHO = (
	'echo "Final editor attempt: switching model to fallback '
	'${EDITOR_ATTEMPT_MODEL} (primary ${MODEL_EDITOR} capacity-limited)."'
)
STALL_ELIF = (
	'elif [ "${editor_stall_fallback_active}" -eq 1 ] '
	'|| editor_stall_fallback_applies "${editor_prev_attempt_stall_killed}"; then'
)


def _function_body(text: str, name: str) -> str:
	match = re.search(
		rf"^{re.escape(name)}\(\)\n\{{\n.*?^\}}\n",
		text,
		re.MULTILINE | re.DOTALL,
	)
	assert match, f"{name} not defined in {REVIEW_APPLY_FIXES}"
	return match.group(0)


def test_review_apply_fixes_final_attempt_condition_unchanged() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert FINAL_ATTEMPT_IF in text
	assert FINAL_ATTEMPT_ECHO in text
	# The stall switch is an elif after the unchanged final-attempt arm.
	if_pos = text.index(FINAL_ATTEMPT_IF)
	elif_pos = text.index(STALL_ELIF)
	assert if_pos < text.index(FINAL_ATTEMPT_ECHO) < elif_pos


def test_review_apply_fixes_stall_fallback_condition_text() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert STALL_ELIF in text
	assert (
		'if editor_stall_fallback_switch_enabled '
		'"${EDITOR_STALL_FALLBACK_ENABLED-}"; then '
		'editor_stall_fallback_switch_on=1; fi'
	) in text
	assert 'editor_prev_attempt_stall_killed=0\n  case "${stall_state}" in' in text
	killed_arm = re.search(r"    killed\)\n(.*?)\n      ;;", text, re.DOTALL)
	assert killed_arm, "killed) arm not found"
	assert "editor_prev_attempt_stall_killed=1" in killed_arm.group(1)
	assert (
		'echo "Editor attempt ${attempt}: previous attempt was stall-killed; '
		'switching model to fallback ${MODEL_EDITOR_FALLBACK}."'
	) in text
	assert "editor_stall_fallback_active=1" in text
	# No required-variable guard: the switch defaults on inside the helper.
	assert "${EDITOR_STALL_FALLBACK_ENABLED:?" not in text


def test_review_apply_fixes_stall_fallback_requires_engine_and_switch() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	body = _function_body(text, "editor_stall_fallback_applies")
	assert '"${AI_ENGINE_RESOLVED_REVIEW_EDITOR:-codex}" != "claude"' in body
	assert "editor_stall_fallback_switch_on" in body
	assert '"${MODEL_EDITOR_FALLBACK}" != "${MODEL_EDITOR:-}"' in body


def _run_shell(script: str, env: dict) -> subprocess.CompletedProcess:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	defs = (
		_function_body(text, "editor_stall_fallback_switch_enabled")
		+ _function_body(text, "editor_stall_fallback_applies")
	)
	base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
	base_env.update(env)
	return subprocess.run(
		["bash", "-c", defs + script],
		env=base_env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_review_apply_fixes_stall_fallback_switch_behaviour() -> None:
	unset = _run_shell(
		'editor_stall_fallback_switch_enabled "${EDITOR_STALL_FALLBACK_ENABLED-}"',
		{},
	)
	assert unset.returncode == 0 and unset.stdout == ""
	for value, expected_rc in (
		("", 0),
		("true", 0),
		("TRUE", 0),
		("false", 1),
		("FALSE", 1),
	):
		result = _run_shell(
			'editor_stall_fallback_switch_enabled "${EDITOR_STALL_FALLBACK_ENABLED-}"',
			{"EDITOR_STALL_FALLBACK_ENABLED": value},
		)
		assert result.returncode == expected_rc, (value, result)
		assert "::warning::" not in result.stdout, value
	bad = _run_shell(
		'editor_stall_fallback_switch_enabled "${EDITOR_STALL_FALLBACK_ENABLED-}"',
		{"EDITOR_STALL_FALLBACK_ENABLED": "maybe::set-output x"},
	)
	assert bad.returncode == 1
	assert bad.stdout.count("::warning::") == 1
	# The printed value is reduced to a safe character set.
	assert "::set-output" not in bad.stdout


def test_review_apply_fixes_stall_fallback_predicate_behaviour() -> None:
	base = {
		"MODEL_EDITOR": "openai/gpt-6-sol",
		"MODEL_EDITOR_FALLBACK": "openai/gpt-5.6-sol",
		"editor_stall_fallback_switch_on": "1",
	}

	def applies(prev: str, **overrides: str) -> bool:
		env = dict(base)
		env.update(overrides)
		env = {k: v for k, v in env.items() if v is not None}
		result = _run_shell(f'editor_stall_fallback_applies "{prev}"', env)
		return result.returncode == 0

	assert applies("1")
	assert applies("1", AI_ENGINE_RESOLVED_REVIEW_EDITOR="codex")
	assert not applies("1", AI_ENGINE_RESOLVED_REVIEW_EDITOR="claude")
	assert not applies("1", MODEL_EDITOR_FALLBACK="")
	assert not applies("1", MODEL_EDITOR_FALLBACK="openai/gpt-6-sol")
	assert not applies("0")
	assert not applies("1", editor_stall_fallback_switch_on="0")



SELECTION_START = '  EDITOR_ATTEMPT_MODEL="${MODEL_EDITOR}"\n'
SELECTION_END = '  emit_editor_substate "PreparingWorkspace" "${attempt}"\n'


def _attempt_model_selection_block() -> str:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert text.count(SELECTION_START) == 1, "attempt model selection start not unique"
	start = text.index(SELECTION_START)
	end = text.index(SELECTION_END, start)
	return text[start:end]


def _run_attempt_loop(max_attempts: int, stall_killed: str, env: dict) -> list:
	"""Run the real per-attempt model selection of review_apply_fixes.sh.

	stall_killed holds one 0/1 flag per attempt: whether that attempt was
	stall-killed, which feeds editor_prev_attempt_stall_killed for the next
	attempt the way the loop's stall_state case does.
	"""
	script = (
		"set -euo pipefail\n"
		f"editor_max_attempts={max_attempts}\n"
		"editor_stall_fallback_switch_on=0\n"
		'if editor_stall_fallback_switch_enabled "${EDITOR_STALL_FALLBACK_ENABLED-}"; '
		"then editor_stall_fallback_switch_on=1; fi\n"
		"editor_prev_attempt_stall_killed=0\n"
		"editor_stall_fallback_active=0\n"
		f"read -r -a stall_flags <<< '{stall_killed}'\n"
		"attempt=1\n"
		'while [ "${attempt}" -le "${editor_max_attempts}" ]; do\n'
		+ _attempt_model_selection_block()
		+ '  echo "SELECTED attempt=${attempt} model=${EDITOR_ATTEMPT_MODEL}"\n'
		'  editor_prev_attempt_stall_killed="${stall_flags[$((attempt - 1))]:-0}"\n'
		"  attempt=$((attempt + 1))\n"
		"done\n"
	)
	base = {
		"MODEL_EDITOR": "openai/gpt-6-sol",
		"MODEL_EDITOR_FALLBACK": "openai/gpt-5.6-sol",
	}
	base.update(env)
	result = _run_shell(script, base)
	assert result.returncode == 0, result
	return result.stdout.splitlines()


def _selected_models(lines: list) -> list:
	return [
		line.split("model=", 1)[1]
		for line in lines
		if line.startswith("SELECTED attempt=")
	]


def test_review_apply_fixes_attempt_loop_without_stall_uses_final_capacity_fallback() -> None:
	lines = _run_attempt_loop(3, "0 0 0", {})
	assert _selected_models(lines) == [
		"openai/gpt-6-sol",
		"openai/gpt-6-sol",
		"openai/gpt-5.6-sol",
	]
	assert any(line.startswith("Final editor attempt: switching model") for line in lines)
	assert not any("stall-killed" in line for line in lines)


def test_review_apply_fixes_attempt_loop_stall_switch_is_sticky() -> None:
	lines = _run_attempt_loop(4, "1 0 0 0", {})
	assert _selected_models(lines) == [
		"openai/gpt-6-sol",
		"openai/gpt-5.6-sol",
		"openai/gpt-5.6-sol",
		"openai/gpt-5.6-sol",
	]
	# The switch is logged once, on the attempt after the stall; the sticky
	# attempts and the final attempt do not log it again or claim capacity.
	assert sum("stall-killed" in line for line in lines) == 1
	assert "Editor attempt 2: previous attempt was stall-killed" in "\n".join(lines)
	assert not any(line.startswith("Final editor attempt") for line in lines)


def test_review_apply_fixes_attempt_loop_stall_before_final_names_stall() -> None:
	lines = _run_attempt_loop(3, "0 1 0", {})
	assert _selected_models(lines) == [
		"openai/gpt-6-sol",
		"openai/gpt-6-sol",
		"openai/gpt-5.6-sol",
	]
	assert "Editor attempt 3: previous attempt was stall-killed" in "\n".join(lines)
	assert not any(line.startswith("Final editor attempt") for line in lines)


def test_review_apply_fixes_attempt_loop_stall_ignored_when_off_or_claude() -> None:
	for env in (
		{"EDITOR_STALL_FALLBACK_ENABLED": "false"},
		{"AI_ENGINE_RESOLVED_REVIEW_EDITOR": "claude"},
		{"MODEL_EDITOR_FALLBACK": "openai/gpt-6-sol"},
	):
		lines = _run_attempt_loop(3, "1 0 0", env)
		models = _selected_models(lines)
		assert models[:2] == ["openai/gpt-6-sol", "openai/gpt-6-sol"], env
		assert not any("stall-killed" in line for line in lines), env


ATTEMPT_RESULT_TOKENS = {
	"ok",
	"empty_output",
	"stall_killed",
	"workspace_safety_violation",
	"config_bootstrap",
	"claude_capacity",
	"timeout",
	"exit_nonzero",
}
ATTEMPT_RESULT_PRINTF = "printf 'EDITOR_ATTEMPT_RESULT attempt="


def _attempt_result_block() -> str:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	start = text.index("  # EDITOR_ATTEMPT_RESULT: one structured line per attempt")
	end = text.index('  if [ "${cmd_rc}" -eq 79 ]; then', start)
	return text[start:end]


def test_editor_attempt_result_line_precedes_exit_branches() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	assert text.count(ATTEMPT_RESULT_PRINTF) == 1
	line_pos = text.index(ATTEMPT_RESULT_PRINTF)
	started = text.index('editor_attempt_started_at="$(date +%s')
	launch = text.index('run_editor_codex_attempt "${attempt_prompt_file}"')
	assert started < launch
	stall_parse = text.index('read_codex_stall_guard_state "${stall_status_file}"')
	assert started < stall_parse < line_pos
	for branch in (
		'if [ "${cmd_rc}" -eq 79 ]; then',
		'if [ "${cmd_rc}" -eq 80 ]; then',
		'if [ "${cmd_rc}" -eq 78 ]; then',
		'if [ "${cmd_rc}" -eq 0 ]; then',
	):
		assert line_pos < text.index(branch, stall_parse), branch
	assert (
		"EDITOR_ATTEMPT_RESULT attempt=%s/%s model=%s engine=%s duration_secs=%s "
		"exit_code=%s stall_state=%s stdout_bytes=%s stderr_bytes=%s "
		"summary_present=%s failure_class=%s\\n"
	) in text


def test_editor_attempt_result_failure_class_tokens_closed() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	body = _function_body(text, "editor_attempt_result_failure_class")
	assert set(re.findall(r'echo "([a-z_]+)"', body)) == ATTEMPT_RESULT_TOKENS
	cases = [
		(("0", "none", "10"), "ok"),
		(("0", "none", "0"), "empty_output"),
		(("0", "killed", "0"), "empty_output"),
		(("0", "none", ""), "empty_output"),
		(("0", "none", "abc"), "empty_output"),
		(("1", "killed", "5"), "stall_killed"),
		(("78", "none", "5"), "workspace_safety_violation"),
		(("79", "none", "0"), "config_bootstrap"),
		(("80", "observed", "0"), "claude_capacity"),
		(("124", "none", "0"), "timeout"),
		(("137", "none", "0"), "timeout"),
		(("2", "none", "0"), "exit_nonzero"),
		(("226", "none", "9"), "exit_nonzero"),
		(("abc", "none", "9"), "exit_nonzero"),
	]
	for args, expected in cases:
		quoted = " ".join(f"'{a}'" for a in args)
		result = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + body + f"editor_attempt_result_failure_class {quoted}"],
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, (args, result)
		assert result.stdout.strip() == expected, (args, result.stdout)


def test_editor_attempt_result_model_sanitizer() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	body = _function_body(text, "editor_attempt_result_sanitize_model")
	for value, expected in (
		("openai/gpt-6-sol", "openai/gpt-6-sol"),
		("vendor/model:free", "vendor/model:free"),
		("a b", "unknown"),
		("x$(id)", "unknown"),
		("::error::x y", "unknown"),
		("ab\ncd", "unknown"),
		("", "unknown"),
	):
		result = subprocess.run(
			["bash", "-c", body + 'editor_attempt_result_sanitize_model "$1"', "_", value],
			capture_output=True,
			text=True,
			check=False,
		)
		assert result.returncode == 0, (value, result)
		assert result.stdout == expected + "\n", (value, result.stdout)


def test_editor_attempt_result_prints_no_output_content() -> None:
	block = _attempt_result_block()
	printf_stmt = block[block.index(ATTEMPT_RESULT_PRINTF):]
	for forbidden in ("tmp_output", "tmp_err", "$(cat", "$(<", "cat ", "head ", "tail "):
		assert forbidden not in printf_stmt, forbidden
	assert "cat " not in block and "head " not in block and "tail " not in block
	# The only uses of the output files are byte counts and a header probe.
	refs = re.findall(r".*\$\{tmp_(?:output|err)\}.*", block)
	assert len(refs) == 3, refs
	for ref in refs:
		assert ('wc -c 2>/dev/null <' in ref and '|| echo 0' in ref) or "grep -q '^Changes made:'" in ref, ref
	assert "editor_attempt_result_sanitize_model" in block


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	for func in test_funcs:
		func()
	print(f"OK: editor capacity-fallback contract holds ({len(test_funcs)} cases)")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
