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
		'claude_run_selected SECURITY_AUDIT "$2" "$3" "$4" || audit_claude_call_rc=$?',
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
		claude_at = text.index('claude_run_selected SECURITY_AUDIT') if script == "security_audit.sh" else text.index("bash -c 'source \"$1\" && claude_run_selected")
		env_line = text.rindex("env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET", 0, claude_at)
		assert claude_at - env_line < 700, script
		assert "-u OPENROUTER_API_KEY" in text[env_line:claude_at], script
		if script == "check_failure_triage.sh":
			assert "AI_ENGINE_READ_ONLY=true" in text[env_line:claude_at]
		if script == "security_audit.sh":
			assert "-u GH_PAT" in text[env_line:claude_at]
			assert "-u GITHUB_ENV -u GITHUB_PATH" in text[env_line:claude_at]


def test_untrusted_checkouts_use_the_staged_engine_root_only() -> None:
	for script in ("check_failure_triage.sh", "validate_process.sh", "self_heal_validation.sh"):
		text = _read(SCRIPTS / script)
		assert '"${SUPPORT_ROOT_DIR:-}/scripts/ai_engine.sh"' in text, script
		assert '[ -n "${SUPPORT_ROOT_DIR:-}" ] && [ -f ' in text, script
		assert "scripts/ai_engine.sh\" \"${" not in text.replace('"${SUPPORT_ROOT_DIR:-}/scripts/ai_engine.sh"', ""), script


def test_triage_stops_when_read_profile_support_is_tampered(tmp_path: Path) -> None:
	text = _read(SCRIPTS / "check_failure_triage.sh")
	triage_block = text[text.index("triage_claude_rc=75\n"):text.index("# Fallback body if the model produced nothing usable.")]
	support_scripts = tmp_path / "support" / "scripts"
	support_scripts.mkdir(parents=True)
	(support_scripts / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
	(tmp_path / "prompt.txt").write_text("diagnose\n", encoding="utf-8")
	triage_script = (
		"set -euo pipefail\nlog() { echo \"CHECK_TRIAGE $*\"; }\n"
		f"RUNTIME_DIR={tmp_path}\nPROMPT_FILE={tmp_path / 'prompt.txt'}\nDIAG_FILE={tmp_path / 'diagnosis.md'}\n"
		+ triage_block + "echo 'issue filing reached'\n"
	)
	proc = subprocess.run(["bash", "-c", triage_script], cwd=tmp_path,
		env={**os.environ, "MODE": "support_failure", "CALLS": str(tmp_path / "calls"),
			"SUPPORT_ROOT_DIR": str(tmp_path / "support"), "AI_ENGINE_RESOLVED_CHECK_TRIAGE": "claude"},
		capture_output=True, text=True, check=False)
	assert proc.returncode == 86, proc.stderr
	assert "CHECK_TRIAGE error support_tampered" in proc.stdout
	assert "issue filing reached" not in proc.stdout
	assert (tmp_path / "calls").read_text(encoding="utf-8").split("|")[5] == "true"


# ---- the Claude branch of a script site, run against a stand-in engine ----

FAKE_ENGINE = r"""
claude_run_selected() {
  local resolved_var="AI_ENGINE_RESOLVED_$1"
  printf '%s|%s|%s|%s|%s|%s|%s\n' "$1" "$(basename "$2")" "$(basename "$3")" "$4" "${!resolved_var:-codex}" "${AI_ENGINE_READ_ONLY:-}" "${GH_TOKEN-unset}" >> "${CALLS}"
  [ -z "${READ_PATHS_CALLS:-}" ] || printf '%s\n' "${RUNTIME_DIR:-unset}|${AI_ENGINE_ISOLATED_READ_PATHS:-}" > "${READ_PATHS_CALLS}"
  [ "${!resolved_var:-codex}" = "claude" ] || return 75
  case "${MODE}" in
    success) printf '%s\n' "${CLAUDE_ANSWER:-claude answer}" > "$3"; return 0 ;;
    tamper) printf '%s\n' "${CLAUDE_ANSWER:-claude answer}" > "$3"; touch support-tampered; return 0 ;;
    support_failure) return 86 ;;
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


def _run_heal_block(tmp_path: Path, *, resolved: str, mode: str, with_worktrees: bool = False, checkouts_succeeded: bool = True) -> tuple[subprocess.CompletedProcess[str], str, str]:
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts" / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	codex = bin_dir / "codex"
	codex.write_text('#!/usr/bin/env bash\necho "codex $*" >> "${CALLS}.codex"\necho "codex answer"\n', encoding="utf-8")
	codex.chmod(0o755)
	(tmp_path / "prompt.txt").write_text("diagnose\n", encoding="utf-8")
	(tmp_path / ".gitignore").write_text("calls*\ndiag.md\ncodex_log.txt\nread_paths\n", encoding="utf-8")
	subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
	subprocess.run(["git", "-C", str(tmp_path), "add", "scripts", "bin", "prompt.txt", ".gitignore"], check=True)
	subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "support"], check=True)
	heal_support_sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
	calls = tmp_path / "calls"
	heal_paths = {}
	if with_worktrees:
		for name, key in (("heal_src", "HEAL_SOURCE_DIR"), ("heal_branch_tip", "HEAL_BRANCH_TIP_DIR")):
			(tmp_path / name).mkdir()
			heal_paths[key] = str(tmp_path / name)
		if checkouts_succeeded:
			heal_paths["HEAL_SOURCE_NOTE"] = f"{tmp_path / 'heal_src'} (coding-workflows at test-ref)"
			heal_paths["HEAL_BRANCH_TIP_NOTE"] = f"{tmp_path / 'heal_branch_tip'} (coding-workflows at test-ref)"
	script = (
		"set -euo pipefail\n"
		"log() { echo \"LOG $*\"; }\n"
		f"PROMPT_FILE=prompt.txt\nDIAG_FILE=diag.md\nRUNTIME_DIR={tmp_path}\nDIAGNOSIS_FALLBACK_REASON=none\n"
		+ _heal_block()
		+ 'echo "reason=${DIAGNOSIS_FALLBACK_REASON}"\n'
	)
	env = dict(os.environ, CALLS=str(calls), MODE=mode, GH_TOKEN="secret", HEAL_SUPPORT_SHA=heal_support_sha, AI_ENGINE_RESOLVED_WORKFLOW_HEAL=resolved,
		READ_PATHS_CALLS=str(tmp_path / "read_paths"), PATH=f"{bin_dir}:{os.environ['PATH']}", **heal_paths)
	proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, (calls.read_text(encoding="utf-8") if calls.exists() else ""), (Path(f"{calls}.codex").read_text(encoding="utf-8") if Path(f"{calls}.codex").exists() else "")


def test_heal_on_claude_runs_claude_without_credentials(tmp_path: Path) -> None:
	proc, calls, codex_calls = _run_heal_block(tmp_path, resolved="claude", mode="success", with_worktrees=True)
	assert proc.returncode == 0, proc.stderr
	assert calls.splitlines() == [f"WORKFLOW_HEAL|prompt.txt|diag.md|{tmp_path}|claude||unset"]
	assert codex_calls == ""
	assert (tmp_path / "diag.md").read_text(encoding="utf-8") == "claude answer\n"
	assert (tmp_path / "read_paths").read_text(encoding="utf-8").strip() == f"{tmp_path}|{tmp_path / 'heal_src'}:{tmp_path / 'heal_branch_tip'}"


def test_heal_does_not_mount_failed_worktree_checkouts(tmp_path: Path) -> None:
	proc, _calls, _codex_calls = _run_heal_block(tmp_path, resolved="claude", mode="success", with_worktrees=True, checkouts_succeeded=False)
	assert proc.returncode == 0, proc.stderr
	assert (tmp_path / "read_paths").read_text(encoding="utf-8").strip() == f"{tmp_path}|"


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


def test_heal_claude_tampered_support_is_rejected(tmp_path: Path) -> None:
	proc, _calls, codex_calls = _run_heal_block(tmp_path, resolved="claude", mode="tamper")
	assert proc.returncode == 86
	assert "Workflow heal support checkout changed after the model run." in proc.stderr
	assert codex_calls == ""


@pytest.mark.parametrize("invalid_support", ["wrong_sha", "missing_sha", "dirty_checkout"])
def test_heal_claude_rejects_invalid_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_support: str) -> None:
	original_run = subprocess.run

	def run_with_invalid_support(command, **kwargs):
		if command[:2] == ["bash", "-c"]:
			if invalid_support == "dirty_checkout":
				(tmp_path / "scripts" / "untracked.sh").write_text("untrusted\n", encoding="utf-8")
			else:
				kwargs["env"] = dict(kwargs["env"])
				if invalid_support == "wrong_sha":
					kwargs["env"]["HEAL_SUPPORT_SHA"] = "0" * 40
				else:
					kwargs["env"].pop("HEAL_SUPPORT_SHA", None)
					kwargs["env"].pop("GITHUB_SHA", None)
		return original_run(command, **kwargs)

	monkeypatch.setattr(subprocess, "run", run_with_invalid_support)
	proc, calls, codex_calls = _run_heal_block(tmp_path, resolved="claude", mode="success")
	assert proc.returncode == 86, proc.stderr
	assert "Workflow heal support checkout changed after the model run" in proc.stderr
	assert "reason=" not in proc.stdout
	assert calls.startswith("WORKFLOW_HEAL|")
	assert codex_calls == ""


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
		("analyze-commit-notify", ["LOG_ANALYSIS", "LOG_SUMMARY"], "./.github/actions/"),
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
	assert 'mkdir -p "${engine_root}/scripts/clarify_sandbox"' in triage
	assert 'scripts/claude_anthropic_relay.py scripts/clarify_sandbox/Dockerfile' in triage
	assert '.codex-workflow-src/${engine_file}' in triage
	assert '"profile": "read"' in _read(WORKFLOWS.parent / "ai" / "claude_engine.json").split('"CHECK_TRIAGE":', 1)[1].split('"WORKFLOW_HEAL":', 1)[0]


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
	assert "for role in REVIEW_EDITOR REVIEW_CONSOLIDATOR CONFLICT_RESOLVER RB_JUDGE SUMMARISER BEHAVIOURAL_SMOKE MATERIALITY; do" in resolve["run"]


# ---- implement issue summary (Q43: SUMMARISER) ----

def _implement_job_steps() -> list[dict]:
	jobs = yaml.safe_load(_read(WORKFLOWS / "implement.yml"))["jobs"]
	return next(spec["steps"] for spec in jobs.values() if any(step.get("name") == "Generate AI issue summary for PR comment" for step in spec.get("steps", [])))


def test_implement_job_resolves_the_summariser() -> None:
	steps = _implement_job_steps()
	resolve = next(step for step in steps if step.get("name") == "Resolve AI engine")
	assert "for role in IMPLEMENT IMPLEMENT_REPAIR IMPLEMENT_DIAGNOSE SUMMARISER; do" in resolve["run"]
	assert resolve["env"]["AI_ENGINE_SUMMARISER"] == "${{ vars.AI_ENGINE_SUMMARISER || '' }}"


def _run_issue_summary(tmp_path: Path, *, resolved: str, mode: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
	run = next(step for step in _implement_job_steps() if step.get("name") == "Generate AI issue summary for PR comment")["run"]
	support = tmp_path / "support"
	support.mkdir()
	(support / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
	(support / "workspace_safety_check.sh").write_text("exit 0\n", encoding="utf-8")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	codex = bin_dir / "codex"
	codex.write_text('#!/usr/bin/env bash\necho "codex $*" >> "${CALLS}.codex"\nprintf "### AI Issue Summary\\n- codex\\n"\n', encoding="utf-8")
	codex.chmod(0o755)
	sleep = bin_dir / "sleep"
	sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
	sleep.chmod(0o755)
	(tmp_path / "issue.json").write_text('{"title": "T", "html_url": "u", "body": "B", "labels": []}', encoding="utf-8")
	(tmp_path / "plan.md").write_text("plan\n", encoding="utf-8")
	calls = tmp_path / "calls"
	base_env = {key: value for key, value in os.environ.items() if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "OPENROUTER_API_KEY"}}
	env = dict(
		base_env,
		CALLS=str(calls),
		MODE=mode,
		CLAUDE_ANSWER="### AI Issue Summary\n- claude",
		AI_ENGINE_RESOLVED_SUMMARISER=resolved,
		IMPLEMENT_STAGED_SUPPORT_RUN_DIR=str(support),
		ISSUE_SUMMARY_PROMPT_FILE=str(tmp_path / "summary_prompt.txt"),
		ISSUE_SUMMARY_FILE=str(tmp_path / "summary.md"),
		ISSUE_META_FILE=str(tmp_path / "issue.json"),
		PLAN_FILE=str(tmp_path / "plan.md"),
		RUNTIME_DIR=str(tmp_path),
		MODEL_EDITOR="gpt-test",
		PATH=f"{bin_dir}:{os.environ['PATH']}",
	)
	proc = subprocess.run(["bash", "-c", run], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	return proc, (calls.read_text(encoding="utf-8") if calls.exists() else ""), (Path(f"{calls}.codex").read_text(encoding="utf-8") if Path(f"{calls}.codex").exists() else "")


def test_issue_summary_on_claude_runs_read_only_and_never_codex(tmp_path: Path) -> None:
	proc, calls, codex_calls = _run_issue_summary(tmp_path, resolved="claude", mode="success")
	assert proc.returncode == 0, proc.stderr
	assert calls.splitlines() == [f"SUMMARISER|issue_summary_combined_prompt.txt|summary.md|{tmp_path}|claude|true|unset"]
	assert codex_calls == ""
	assert (tmp_path / "summary.md").read_text(encoding="utf-8") == "### AI Issue Summary\n- claude\n"
	assert "Summary generated on attempt 1." in proc.stdout


def test_issue_summary_on_codex_or_unavailable_runs_the_unchanged_codex_call(tmp_path: Path) -> None:
	for index, (resolved, mode) in enumerate((("codex", "success"), ("claude", "unavailable"))):
		work = tmp_path / str(index)
		work.mkdir()
		proc, _calls, codex_calls = _run_issue_summary(work, resolved=resolved, mode=mode)
		assert proc.returncode == 0, proc.stderr
		assert codex_calls.splitlines() == ["codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model gpt-test --sandbox danger-full-access"]
		assert (work / "summary.md").read_text(encoding="utf-8") == "### AI Issue Summary\n- codex\n"


def test_issue_summary_claude_crash_retries_without_codex(tmp_path: Path) -> None:
	proc, calls, codex_calls = _run_issue_summary(tmp_path, resolved="claude", mode="crash")
	assert proc.returncode == 0, proc.stderr
	assert len(calls.splitlines()) == 3
	assert codex_calls == ""
	assert proc.stdout.count("::warning::Summary generation attempt") == 3


# ---- AGENTS.md materiality on Claude (Q42) ----

MATERIALITY_SCRIPT = SCRIPTS / "review_agents_md_materiality.sh"


def _run_materiality(tmp_path: Path, *, paths: list[str], resolved: str = "claude", mode: str = "success", answer: str = "", flag: str = "1", agents_md_changed: bool = False) -> tuple[dict, str, str]:
	support = tmp_path / "support"
	support.mkdir()
	(support / "ai_engine.sh").write_text(FAKE_ENGINE, encoding="utf-8")
	workspace = tmp_path / "workspace"
	workspace.mkdir()
	(workspace / "agents.md").write_text("# agents\n", encoding="utf-8")
	if agents_md_changed:
		paths = [*paths, "agents.md"]
	diff = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n" for p in paths)
	(tmp_path / "pr.diff").write_text(diff, encoding="utf-8")
	(tmp_path / "changed.txt").write_text("\n".join(paths) + "\n", encoding="utf-8")
	calls = tmp_path / "calls"
	base_env = {key: value for key, value in os.environ.items() if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "OPENROUTER_API_KEY"}}
	env = dict(
		base_env,
		CALLS=str(calls),
		MODE=mode,
		CLAUDE_ANSWER=answer,
		AI_ENGINE_RESOLVED_MATERIALITY=resolved,
		SUPPORT_SCRIPTS_DIR=str(support),
		AGENTS_MD_MATERIALITY_ENABLED="1",
		AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED=flag,
		AGENTS_MD_MATERIALITY_RESULT_FILE=str(tmp_path / "result.json"),
		AGENTS_MD_MATERIALITY_COMMENT_FILE=str(tmp_path / "comment.md"),
		PR_CHANGED_FILES_FILE=str(tmp_path / "changed.txt"),
		PR_DIFF_FILE=str(tmp_path / "pr.diff"),
		GITHUB_WORKSPACE=str(workspace),
		REPOSITORY="octo/example",
		PR_NUMBER="7",
		GITHUB_RUN_ID="9",
		BASE_BRANCH="",
	)
	proc = subprocess.run(["bash", str(MATERIALITY_SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=True)
	import json as _json
	result = _json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
	comment = (tmp_path / "comment.md").read_text(encoding="utf-8") if (tmp_path / "comment.md").exists() else ""
	return result, comment, (calls.read_text(encoding="utf-8") if calls.exists() else "") + proc.stdout


def test_materiality_claude_raises_a_low_rating(tmp_path: Path) -> None:
	result, comment, log = _run_materiality(tmp_path, paths=["src/app.py"], answer='{"materiality": "high", "reason": "Adds env var FOO_TIMEOUT, ping @someone <!-- x -->"}')
	assert f"MATERIALITY|prompt.txt|verdict.txt|{tmp_path / 'workspace'}|claude|true|unset" in log
	assert (result["materiality"], result["advisory_required"], result["llm_fallback_used"], result["llm_fallback_status"]) == ("high", True, True, "ok")
	assert "after a Claude review of the diff" in comment
	assert "- Claude: Adds env var FOO_TIMEOUT, ping someone  x" in comment
	assert "@someone" not in comment and "<!-- x" not in comment


def test_materiality_claude_low_or_unavailable_keeps_the_rules_result(tmp_path: Path) -> None:
	for index, (mode, answer, status) in enumerate((("success", '{"materiality": "low", "reason": "tests only"}', "ok"), ("unavailable", "", "unavailable"), ("crash", "", "failed_rc_1"), ("success", "not json", "unparseable"))):
		work = tmp_path / str(index)
		work.mkdir()
		result, comment, _log = _run_materiality(work, paths=["src/app.py"], mode=mode, answer=answer)
		assert (result["materiality"], result["advisory_required"], result["llm_fallback_used"], result["llm_fallback_status"]) == ("low", False, False, status), status
		assert comment == ""


def test_materiality_claude_runs_only_when_needed(tmp_path: Path) -> None:
	cases = {
		"flag_off": {"flag": "0"},
		"role_on_codex": {"resolved": "codex"},
		"agents_md_changed": {"agents_md_changed": True},
		"rules_already_high": {"paths": ["package.json"]},
	}
	for name, kwargs in cases.items():
		work = tmp_path / name
		work.mkdir()
		params = {"paths": ["src/app.py"], "answer": '{"materiality": "high", "reason": "x"}', **kwargs}
		result, _comment, log = _run_materiality(work, **params)
		assert "MATERIALITY|" not in log, name
		assert result["llm_fallback_used"] is False, name
		if name == "role_on_codex":
			assert result["llm_fallback_status"] == "not_selected"
		else:
			assert "llm_fallback_status" not in result, name


def test_review_job_resolves_materiality_and_defaults_the_check_on() -> None:
	text = _read(WORKFLOWS / "review_autofix.yml")
	assert "for role in REVIEW_EDITOR REVIEW_CONSOLIDATOR CONFLICT_RESOLVER RB_JUDGE SUMMARISER BEHAVIOURAL_SMOKE MATERIALITY; do" in text
	assert "AI_ENGINE_MATERIALITY: ${{ vars.AI_ENGINE_MATERIALITY || '' }}" in text
	assert text.count("AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED: ${{ vars.AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED || '1' }}") == 2


def test_log_summary_engine_is_resolved_before_the_summary_step() -> None:
	steps = _job_steps("workflow-log-analysis.yml", "analyze-commit-notify")
	names = [step.get("name") for step in steps]
	summary_at = names.index("Summarize unselected runs (gpt-6-luna)")
	for name in ("Resolve AI engine", "Install Claude Code CLI", "Resolve Claude credential"):
		assert names.index(name) < summary_at, name
	env = steps[summary_at]["env"]
	assert env["WORKFLOW_LOG_SUMMARY_TIME_BUDGET_SECS"] == "${{ vars.WORKFLOW_LOG_SUMMARY_TIME_BUDGET_SECS || '900' }}"
	assert env["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"
