"""Phase 5d (replace-claude-sessions plan): the remaining roles on Claude.

VALIDATE, VALIDATE_SELF_HEAL, VALIDATION_REFRESH, SECURITY_AUDIT,
CHECK_TRIAGE, WORKFLOW_HEAL, LOG_ANALYSIS, LOG_AUDIT, RETRO, SUMMARISER and
BEHAVIOURAL_SMOKE run through `claude_run_selected` (scripts/ai_engine.sh)
when their job's "Resolve AI engine" step exported
AI_ENGINE_RESOLVED_<ROLE>=claude. Exit 75 runs each site's unchanged codex or
OpenCode command (plan D1, G4).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


# script -> (role, claude call fragment, gate fragment, unchanged command fragment)
SITES = {
	"workflow_failure_heal_intake.sh": (
		"WORKFLOW_HEAL",
		"bash -c 'source \"$1\" && claude_run_selected WORKFLOW_HEAL \"$2\" \"$3\" \"$4\"'",
		'elif command -v codex >/dev/null 2>&1; then',
		"\t\t--sandbox read-only \\\n\t\t< \"${PROMPT_FILE}\" \\\n\t\t> \"${DIAG_FILE}\" 2> >(tee -a \"${RUNTIME_DIR}/codex_log.txt\" >&2); then",
	),
	"check_failure_triage.sh": (
		"CHECK_TRIAGE",
		"bash -c 'source \"$1\" && claude_run_selected CHECK_TRIAGE \"$2\" \"$3\" \"$4\"'",
		'elif command -v codex >/dev/null 2>&1; then',
		"\t\t--sandbox danger-full-access \\\n\t\t< \"${PROMPT_FILE}\" \\\n\t\t> \"${DIAG_FILE}\" 2> >(tee -a \"${RUNTIME_DIR}/codex_log.txt\" >&2); then",
	),
	"security_audit.sh": (
		"SECURITY_AUDIT",
		"bash -c 'source \"$1\" && claude_run_selected SECURITY_AUDIT \"$2\" \"$3\" \"$4\"'",
		'elif codex --ask-for-approval never \\',
		'\t\t--sandbox read-only < "${RENDERED_PROMPT_FILE}" \\\n\t\t> "${CODEX_OUTPUT_FILE}" 2> "${CODEX_ERROR_FILE}"; then',
	),
	"validate_process.sh": (
		"VALIDATE",
		"bash -c 'source \"$1\" && claude_run_selected VALIDATE \"$2\" \"$3\" \"$4\"'",
		'[ "${validate_claude_rc}" -eq 75 ] || return "${validate_claude_rc}"',
		'  codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${prompt_file}" > "${output_file}" 2> >(tee -a "${log_file}" >&2)',
	),
	"self_heal_validation.sh": (
		"VALIDATE_SELF_HEAL",
		"bash -c 'source \"$1\" && claude_run_selected VALIDATE_SELF_HEAL \"$2\" \"$3\" \"$4\"'",
		'[ "${rc}" -eq 75 ] || return "${rc}"',
		'\t\tcodex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${SELF_HEAL_PROMPT_FILE}" > "${SELF_HEAL_OUTPUT_FILE}" 2> "${stderr_tmp}"',
	),
	"workflow_retro_fanout.sh": (
		"RETRO",
		"bash -c 'source \"$1\" && claude_run_selected RETRO \"$2\" \"$3\" \"$4\"'",
		'if [ "${retro_engine_rc}" -eq 75 ]; then',
		'-- codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${WORKFLOW_RETRO_MODEL}" --sandbox danger-full-access < "${prompt_file}"',
	),
	"summarize_reviewer_consensus.sh": (
		"SUMMARISER",
		"bash -c 'source \"$1\" && claude_run_selected SUMMARISER \"$2\" \"$3\" \"$4\"'",
		'if [ "${summariser_engine_rc}" -eq 75 ]; then',
		'\t\t\t"${summariser_opencode_cmd[@]}" < "${prompt_file}" \\',
	),
	"review_synthesise_smoke.sh": (
		"BEHAVIOURAL_SMOKE",
		"bash -c 'source \"$1\" && claude_run_selected BEHAVIOURAL_SMOKE \"$2\" \"$3\" \"$4\"'",
		'elif timeout --signal=TERM --kill-after=30s -- "${BEHAVIOURAL_SMOKE_TIMEOUT_S}" \\',
		'\t"${behavioural_smoke_opencode_cmd[@]}" \\\n\t< "${PROMPT_FILE}" > "${RAW_OUTPUT_FILE}" 2> "${STDERR_FILE}"; then',
	),
}


@pytest.mark.parametrize("script", sorted(SITES))
def test_site_tries_claude_then_the_unchanged_command(script: str) -> None:
	_role, claude_call, gate, unchanged = SITES[script]
	text = _read(SCRIPTS / script)
	assert text.count(claude_call) == 1
	start = text.index(claude_call)
	gate_at = text.index(gate, start)
	assert text.index(unchanged, gate_at) > gate_at


def test_read_only_utility_roles_narrow_the_tool_profile() -> None:
	for script in ("summarize_reviewer_consensus.sh", "review_synthesise_smoke.sh"):
		assert "AI_ENGINE_READ_ONLY=true AI_ENGINE_MODEL_HINT=" in _read(SCRIPTS / script), script


def test_credential_stripping_covers_the_claude_call() -> None:
	for script in ("workflow_failure_heal_intake.sh", "check_failure_triage.sh", "security_audit.sh"):
		text = _read(SCRIPTS / script)
		claude_at = text.index("bash -c 'source \"$1\" && claude_run_selected")
		env_line = text.rindex("env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET", 0, claude_at)
		assert claude_at - env_line < 400, script
		assert "-u OPENROUTER_API_KEY" in text[env_line:claude_at], script
		if script == "security_audit.sh":
			assert "-u GH_PAT" in text[env_line:claude_at]
			assert "-u GITHUB_ENV -u GITHUB_PATH" in text[env_line:claude_at]


def test_untrusted_checkouts_use_the_staged_engine_root_only() -> None:
	for script in ("check_failure_triage.sh", "validate_process.sh", "self_heal_validation.sh"):
		text = _read(SCRIPTS / script)
		assert '"${SUPPORT_ROOT_DIR:-}/scripts/ai_engine.sh"' in text, script
		assert '[ -n "${SUPPORT_ROOT_DIR:-}" ] && [ -f ' in text, script
		assert "scripts/ai_engine.sh\" \"${" not in text.replace('"${SUPPORT_ROOT_DIR:-}/scripts/ai_engine.sh"', ""), script


# ---- the Claude branch of a script site, run against a stand-in engine ----

FAKE_ENGINE = r"""
claude_run_selected() {
  local resolved_var="AI_ENGINE_RESOLVED_$1"
  printf '%s|%s|%s|%s|%s|%s|%s\n' "$1" "$(basename "$2")" "$(basename "$3")" "$4" "${!resolved_var:-codex}" "${AI_ENGINE_READ_ONLY:-}" "${GH_TOKEN-unset}" >> "${CALLS}"
  [ "${!resolved_var:-codex}" = "claude" ] || return 75
  case "${MODE}" in
    success) printf '%s\n' "${CLAUDE_ANSWER:-claude answer}" > "$3"; return 0 ;;
    unavailable) return 75 ;;
    *) return 1 ;;
  esac
}
"""


def _heal_block() -> str:
	text = _read(SCRIPTS / "workflow_failure_heal_intake.sh")
	start = text.index("heal_claude_rc=75\n")
	end = text.index('if [ ! -s "${DIAG_FILE}" ]; then', start)
	return text[start:end]


def _run_heal_block(tmp_path: Path, *, resolved: str, mode: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts" / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	codex = bin_dir / "codex"
	codex.write_text('#!/usr/bin/env bash\necho "codex $*" >> "${CALLS}.codex"\necho "codex answer"\n', encoding="utf-8")
	codex.chmod(0o755)
	(tmp_path / "prompt.txt").write_text("diagnose\n", encoding="utf-8")
	calls = tmp_path / "calls"
	script = (
		"set -euo pipefail\n"
		"log() { echo \"LOG $*\"; }\n"
		"PROMPT_FILE=prompt.txt\nDIAG_FILE=diag.md\nRUNTIME_DIR=.\nDIAGNOSIS_FALLBACK_REASON=none\n"
		+ _heal_block()
		+ 'echo "reason=${DIAGNOSIS_FALLBACK_REASON}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), MODE=mode, GH_TOKEN="secret", AI_ENGINE_RESOLVED_WORKFLOW_HEAL=resolved,
		PATH=f"{bin_dir}:{os.environ['PATH']}")
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, (calls.read_text(encoding="utf-8") if calls.exists() else ""), (Path(f"{calls}.codex").read_text(encoding="utf-8") if Path(f"{calls}.codex").exists() else "")


def test_heal_on_claude_runs_claude_without_credentials(tmp_path: Path) -> None:
	proc, calls, codex_calls = _run_heal_block(tmp_path, resolved="claude", mode="success")
	assert proc.returncode == 0, proc.stderr
	assert calls.splitlines() == [f"WORKFLOW_HEAL|prompt.txt|diag.md|{tmp_path}|claude||unset"]
	assert codex_calls == ""
	assert (tmp_path / "diag.md").read_text(encoding="utf-8") == "claude answer\n"


def test_heal_on_codex_or_unavailable_runs_the_codex_call(tmp_path: Path) -> None:
	for index, (resolved, mode) in enumerate((("codex", "success"), ("claude", "unavailable"))):
		work = tmp_path / str(index)
		work.mkdir()
		proc, _calls, codex_calls = _run_heal_block(work, resolved=resolved, mode=mode)
		assert proc.returncode == 0, proc.stderr
		assert "exec --skip-git-repo-check" in codex_calls and "--sandbox read-only" in codex_calls
		assert (work / "diag.md").read_text(encoding="utf-8") == "codex answer\n"


def test_heal_claude_crash_files_the_fallback_reason(tmp_path: Path) -> None:
	proc, _calls, codex_calls = _run_heal_block(tmp_path, resolved="claude", mode="crash")
	assert proc.returncode == 0, proc.stderr
	assert codex_calls == ""
	assert "reason=failed (Claude exited non-zero)" in proc.stdout
	assert (tmp_path / "diag.md").read_text(encoding="utf-8") == ""


# ---- validation refresh discovery (Python) ----

MODULE_PATH = SCRIPTS / "validation_discovery_bootstrap.py"
_spec = importlib.util.spec_from_file_location("validation_discovery_bootstrap_5d", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
discovery = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = discovery
_spec.loader.exec_module(discovery)

SCHEMA_PATH = SCRIPTS / "templates" / "slot_manifest.schema.json"
PROMPT_PATH = REPO_ROOT / "prompts" / "mode-validate-discover.txt"
MANIFEST = """type: node-runtime
entry: npm
slots:
  project_name: demo-node
  canary_tools:
    - bash
    - node
    - npm
  tap_plan: 3
"""


class _CodexExecutor:
	def __init__(self, stdout: str) -> None:
		self.stdout = stdout
		self.commands: list[list[str]] = []

	def run(self, command, **_kwargs):
		self.commands.append(list(command))
		return subprocess.CompletedProcess(args=command, returncode=0, stdout=self.stdout, stderr="")


def _discover(tmp_path: Path, monkeypatch, *, resolved: str, mode: str, executor: _CodexExecutor):
	engine = tmp_path / "ai_engine.sh"
	engine.write_text(FAKE_ENGINE, encoding="utf-8")
	clone = tmp_path / "clone"
	clone.mkdir()
	monkeypatch.setattr(discovery, "_AI_ENGINE_SH", engine)
	monkeypatch.setenv("AI_ENGINE_RESOLVED_VALIDATION_REFRESH", resolved)
	monkeypatch.setenv("MODE", mode)
	monkeypatch.setenv("CALLS", str(tmp_path / "calls"))
	monkeypatch.setenv("CLAUDE_ANSWER", MANIFEST)
	return discovery.discover_manifest_via_codex(
		clone_dir=clone,
		prompt_path=PROMPT_PATH,
		schema_path=SCHEMA_PATH,
		model="openai/gpt-6-sol",
		reasoning_effort="high",
		attempts=2,
		executor=executor,
		per_call_timeout_secs=30,
		sleep_fn=lambda _seconds: None,
	)


def test_discovery_on_claude_never_runs_codex(tmp_path: Path, monkeypatch) -> None:
	executor = _CodexExecutor("not yaml")
	result = _discover(tmp_path, monkeypatch, resolved="claude", mode="success", executor=executor)
	assert result.outcome == "success" and result.parsed_manifest["type"] == "node-runtime"
	assert executor.commands == []
	assert _read(tmp_path / "calls").startswith("VALIDATION_REFRESH|mode-validate-discover.txt|")
	assert list(tmp_path.glob(".claude-discovery-*")) == []


def test_discovery_falls_back_to_the_unchanged_codex_command(tmp_path: Path, monkeypatch) -> None:
	executor = _CodexExecutor(MANIFEST)
	result = _discover(tmp_path, monkeypatch, resolved="claude", mode="unavailable", executor=executor)
	assert result.outcome == "success" and result.attempts_used == 1
	assert len(executor.commands) == 1
	assert executor.commands[0][:2] == ["codex", "--ask-for-approval"]
	assert executor.commands[0][-4:] == ["--model", "openai/gpt-6-sol", "--sandbox", "danger-full-access"]


def test_discovery_claude_crash_is_an_attempt_failure(tmp_path: Path, monkeypatch) -> None:
	executor = _CodexExecutor(MANIFEST)
	result = _discover(tmp_path, monkeypatch, resolved="claude", mode="crash", executor=executor)
	assert result.outcome == "failed" and result.failure_reason == "claude_rc_nonzero(rc=1)"
	assert executor.commands == []


def test_discovery_on_codex_never_touches_claude(tmp_path: Path, monkeypatch) -> None:
	executor = _CodexExecutor(MANIFEST)
	result = _discover(tmp_path, monkeypatch, resolved="codex", mode="success", executor=executor)
	assert result.outcome == "success"
	assert not (tmp_path / "calls").exists()


# ---- workflow wiring ----

# workflow -> (job, roles, action prefix)
WORKFLOW_JOBS = {
	"workflow-failure-heal-intake.yml": [("intake", ["WORKFLOW_HEAL"], "./.github/actions/")],
	"check_failure_triage.yml": [(None, ["CHECK_TRIAGE"], "./.codex-workflow-src/.github/actions/")],
	"security-audit.yml": [("security-audit", ["SECURITY_AUDIT"], "./.github/actions/")],
	"validate.yml": [("validate", ["VALIDATE", "VALIDATE_SELF_HEAL"], "shubhodeep1/coding-workflows/.github/actions/")],
	"validation-refresh.yml": [("refresh", ["VALIDATION_REFRESH"], "./.github/actions/")],
	"workflow-log-analysis.yml": [
		("weekly-retro", ["RETRO"], "./.github/actions/"),
		("analyze-commit-notify", ["LOG_ANALYSIS"], "./.github/actions/"),
		("deep-audit", ["LOG_AUDIT"], "./.github/actions/"),
		("api-redundancy", ["LOG_ANALYSIS"], "./.github/actions/"),
	],
}


def _job_steps(workflow: str, job: str | None) -> list[dict]:
	data = yaml.safe_load(_read(WORKFLOWS / workflow))
	jobs = data["jobs"]
	if job is None:
		job = next(name for name, spec in jobs.items() if any(step.get("name") == "Resolve AI engine" for step in spec.get("steps", [])))
	return jobs[job]["steps"]


@pytest.mark.parametrize("workflow", sorted(WORKFLOW_JOBS))
def test_workflow_resolves_the_roles_and_fetches_the_pool_only_when_needed(workflow: str) -> None:
	for job, roles, prefix in WORKFLOW_JOBS[workflow]:
		steps = _job_steps(workflow, job)
		names = [step.get("name") for step in steps]
		resolve = steps[names.index("Resolve AI engine")]
		assert f"for role in {' '.join(roles)}; do" in resolve["run"]
		for role in roles:
			assert resolve["env"][f"AI_ENGINE_{role}"] == f"${{{{ vars.AI_ENGINE_{role} || '' }}}}"
		for name, action in (("Install Claude Code CLI", "install-claude"), ("Resolve Claude credential", "claude-pool-token")):
			step = steps[names.index(name)]
			assert step["uses"].startswith(prefix + action), (workflow, job, step["uses"])
			assert step["continue-on-error"] is True
			assert step["if"].endswith("steps.ai_engine.outputs.any_claude == 'true'")


def test_triage_and_validate_stage_a_trusted_engine_root() -> None:
	for workflow, job in (("check_failure_triage.yml", None), ("validate.yml", "validate")):
		runs = "\n".join(step.get("run", "") for step in _job_steps(workflow, job))
		assert 'engine_root="${RUNNER_TEMP}/claude-engine-support"' in runs, workflow
		assert 'echo "SUPPORT_ROOT_DIR=${engine_root}" >> "$GITHUB_ENV"' in runs, workflow
		assert ".github/ai/claude_engine.json .claude/hooks/gh_api_write_guard.py" in runs, workflow
	triage = "\n".join(step.get("run", "") for step in _job_steps("check_failure_triage.yml", None))
	assert triage.index('echo "SUPPORT_ROOT_DIR=${engine_root}"') < triage.index("rm -rf .codex-workflow-src")


def test_log_analysis_sites_keep_their_codex_heartbeat_calls() -> None:
	text = _read(WORKFLOWS / "workflow-log-analysis.yml")
	assert text.count("bash scripts/codex_heartbeat.sh") == 4
	for role in ("RETRO", "LOG_ANALYSIS", "LOG_AUDIT"):
		assert f"claude_run_selected {role} " in text
	assert len(re.findall(r"_ENGINE_RC\}\" -eq 75 \]; then", text)) == 4


def test_poll_job_resolves_the_security_pass_audit() -> None:
	run = next(step for step in yaml.safe_load(_read(WORKFLOWS / "orchestrate_poll.yml"))["jobs"]["poll"]["steps"] if step.get("name") == "Resolve AI engine")
	assert 'echo "AI_ENGINE_RESOLVED_SECURITY_AUDIT=${security_audit_engine}" >> "$GITHUB_ENV"' in run["run"]
	assert run["env"]["AI_ENGINE_SECURITY_AUDIT"] == "${{ vars.AI_ENGINE_SECURITY_AUDIT || '' }}"


def test_review_job_resolves_the_utility_roles() -> None:
	steps = yaml.safe_load(_read(WORKFLOWS / "review_autofix.yml"))["jobs"]["codex-agent"]["steps"]
	resolve = next(step for step in steps if step.get("name") == "Resolve AI engine")
	assert "for role in REVIEW_EDITOR REVIEW_CONSOLIDATOR CONFLICT_RESOLVER RB_JUDGE SUMMARISER BEHAVIOURAL_SMOKE; do" in resolve["run"]
