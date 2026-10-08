"""Claude engine cutover of the utility roles (plan unattended-claude-pipeline-completion, phase 3).

Item 3a: the validation roles VALIDATE, VALIDATE_SELF_HEAL and
VALIDATION_REFRESH resolve to Claude by default, `AI_ENGINE_<ROLE>=codex` and
the `ai:codex` label keep the unchanged codex command line, and every call
site reaches Claude only through `claude_run_selected` sourced from the
trusted engine root, never from the checkout. Items 3c and 3d append their
own rows.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
AI_ENGINE = SCRIPTS / "ai_engine.sh"

VALIDATE_ROLES = ("VALIDATE", "VALIDATE_SELF_HEAL", "VALIDATION_REFRESH")


def _resolve(role: str, **extra_env: str) -> subprocess.CompletedProcess:
	env = {
		key: value
		for key, value in os.environ.items()
		# GITHUB_EVENT_PATH carries the PR labels on a pull_request run, and an
		# ai:engine-claude label there beats the per-role variable under test.
		if not key.startswith(("AI_ENGINE", "SUPPORT_", "CLAUDE_FIXER", "GITHUB_WORKSPACE", "GITHUB_EVENT"))
	}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(extra_env)
	return subprocess.run(
		["bash", "-c", 'source "$0"; ai_engine_for_role "$1"', str(AI_ENGINE), role],
		capture_output=True,
		text=True,
		env=env,
		timeout=60,
		check=False,
	)


@pytest.mark.parametrize("role", VALIDATE_ROLES)
def test_validate_role_defaults_to_claude(role: str) -> None:
	result = _resolve(role)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "claude"
	assert f"AI_ENGINE_SELECTED role={role} engine=claude" in result.stderr


@pytest.mark.parametrize("role", VALIDATE_ROLES)
def test_validate_role_variable_rolls_back_to_codex(role: str) -> None:
	result = _resolve(role, **{f"AI_ENGINE_{role}": "codex"})
	assert result.stdout.strip() == "codex"
	assert f"source=var:AI_ENGINE_{role}" in result.stderr


@pytest.mark.parametrize("role", VALIDATE_ROLES)
def test_validate_role_codex_label_wins(role: str) -> None:
	result = _resolve(role, AI_ENGINE_LABELS='["ai:codex"]', **{f"AI_ENGINE_{role}": "claude"})
	assert result.stdout.strip() == "codex"
	assert "source=label:ai:codex" in result.stderr


# --- call sites --------------------------------------------------------------------


def _function_body(text: str, name: str) -> str:
	match = re.search(rf"^{re.escape(name)}\(\)\s*\{{\n(.*?)^\}}\n", text, re.S | re.M)
	assert match, name
	return match.group(1)


def test_validate_process_routes_discover_and_diagnose_through_the_selector() -> None:
	text = (SCRIPTS / "validate_process.sh").read_text(encoding="utf-8")
	assert 'run_validate_engine_attempt "VALIDATE" "validate_discover"' in text
	assert 'run_validate_engine_attempt "VALIDATE" "validate_diagnose"' in text
	body = _function_body(text, "run_validate_engine_attempt")
	assert 'claude_run_selected "${role}"' in body
	assert "-- \\\n      run_validate_codex_attempt " in body
	assert "${CLAUDE_ENGINE_SUPPORT_DIR:-${RUNNER_TEMP:-/tmp}/claude-engine-support}" in body
	assert "reason=engine_support_missing" in body
	# The unchanged codex launch stays in run_validate_codex_attempt.
	codex = _function_body(text, "run_validate_codex_attempt")
	assert 'bash "${CODEX_ISOLATED_EXEC}" run --mode read-only --' in codex


def test_self_heal_routes_through_the_selector() -> None:
	text = (SCRIPTS / "self_heal_validation.sh").read_text(encoding="utf-8")
	body = _function_body(text, "run_self_heal_codex")
	assert "claude_run_selected VALIDATE_SELF_HEAL" in body
	assert "run_self_heal_codex_direct" in body
	assert "reason=engine_support_missing" in body
	direct = _function_body(text, "run_self_heal_codex_direct")
	assert '"${self_heal_codex_cmd[@]}"' in direct


def test_discovery_bootstrap_uses_the_refresh_role_with_codex_stdio() -> None:
	text = (SCRIPTS / "validation_discovery_bootstrap.py").read_text(encoding="utf-8")
	assert 'DISCOVERY_ENGINE_ROLE = "VALIDATION_REFRESH"' in text
	assert '\'source "$0"; claude_run_selected "$@"\'' in text
	assert '"--codex-stdio"' in text
	assert '"AI_ENGINE_READ_ONLY": "true"' in text


@pytest.mark.parametrize(
	"path",
	["scripts/validate_process.sh", "scripts/self_heal_validation.sh", "scripts/validation_discovery_bootstrap.py"],
)
def test_no_call_site_sources_ai_engine_from_the_checkout(path: str) -> None:
	text = (REPO_ROOT / path).read_text(encoding="utf-8")
	assert not re.search(r"source\s+\"?(\./)?scripts/ai_engine\.sh", text)
	# Only under the trusted engine root.
	if path.endswith(".py"):
		assert '_claude_engine_support_root() / "scripts" / "ai_engine.sh"' in text
	else:
		assert 'engine_script="${engine_root}/scripts/ai_engine.sh"' in text


def _step_index(text: str, name: str) -> int:
	index = text.find(f"      - name: {name}\n")
	assert index >= 0, name
	return index


def test_validate_workflow_installs_claude_before_the_workspace_copy() -> None:
	text = (WORKFLOWS / "validate.yml").read_text(encoding="utf-8")
	init = _step_index(text, "Initialize workspace metadata")
	for name in (
		"Stage trusted Claude engine support",
		"Resolve AI engine",
		"Stage trusted Claude engine actions",
		"Install Claude Code CLI",
		"Resolve Claude credential",
	):
		assert _step_index(text, name) < init, name
	assert _step_index(text, "Fetch workflow support files") < _step_index(text, "Stage trusted Claude engine support")
	assert "uses: ./.codex-workflow-src/.github/actions/install-claude" in text
	assert "uses: ./.codex-workflow-src/.github/actions/claude-pool-token" in text
	assert text.count("if: steps.ai_engine.outputs.engine == 'claude' && steps.claude_engine_actions.outcome == 'success'") == 2
	assert 'ai_engine_stage_support "$1" "$2"' in text
	assert "AI_ENGINE_VALIDATE: ${{ vars.AI_ENGINE_VALIDATE || '' }}" in text
	assert "AI_ENGINE_VALIDATE_SELF_HEAL: ${{ vars.AI_ENGINE_VALIDATE_SELF_HEAL || '' }}" in text
	assert 'echo "AI_ENGINE_LABELS=${labels_json}" >> "$GITHUB_ENV"' in text


def test_workspace_copy_excludes_the_staged_action_directory() -> None:
	# The pool-token post step needs its action directory until the job ends,
	# so the copies live where workspace_init.sh never materializes them.
	text = (SCRIPTS / "workspace_init.sh").read_text(encoding="utf-8")
	assert "excluded_roots = {'.git', '.codex-workflow-src', '.codex-workflow-src-main'}" in text


def test_validation_refresh_gates_claude_steps_on_the_engine() -> None:
	text = (WORKFLOWS / "validation-refresh.yml").read_text(encoding="utf-8")
	run = _step_index(text, "Run validation refresh")
	for name in ("Resolve AI engine", "Install Claude Code CLI", "Resolve Claude credential"):
		assert _step_index(text, name) < run, name
	assert text.count("if: steps.ai_engine.outputs.engine == 'claude'") == 2
	assert "uses: ./.github/actions/install-claude" in text
	assert "uses: ./.github/actions/claude-pool-token" in text
	assert "ai_engine_for_role VALIDATION_REFRESH" in text
	assert "uses: ./.github/actions/install-codex" in text  # codex stays as the fallback
	# Only a schedule run or a default-branch dispatch may stage Claude support.
	assert 'if [ "${trusted_ref}" = "true" ] && [ -n "${checkout_head}" ]' in text
	assert '[ "${GITHUB_REF}" = "refs/heads/${DEFAULT_BRANCH}" ]' in text


# --- discovery bootstrap Claude branch --------------------------------------------


def _load_discovery():
	spec = importlib.util.spec_from_file_location("validation_discovery_bootstrap_3a", SCRIPTS / "validation_discovery_bootstrap.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


class _RecordingExecutor:
	def __init__(self, output: str) -> None:
		self.output = output
		self.seen: list[tuple[list[str], dict, str | None]] = []

	def run(self, command, *, cwd=None, check=True, env_overrides=None, input_text=None, timeout=600):
		self.seen.append((list(command), dict(env_overrides or {}), input_text))
		Path(command[6]).write_text(self.output, encoding="utf-8")
		assert Path(command[5]).read_text(encoding="utf-8")  # the prompt file
		return subprocess.CompletedProcess(args=command, returncode=0, stdout="ignored", stderr="")


def test_discovery_claude_branch_reads_the_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	discovery = _load_discovery()
	root = tmp_path / "engine-root"
	(root / "scripts").mkdir(parents=True)
	(root / "scripts" / "ai_engine.sh").write_text("# trusted\n", encoding="utf-8")
	monkeypatch.setenv("CLAUDE_ENGINE_SUPPORT_DIR", str(root))
	clone = tmp_path / "clone"
	clone.mkdir()
	executor = _RecordingExecutor("not: a manifest\n")
	result = discovery.discover_manifest_via_codex(
		clone_dir=clone,
		prompt_path=REPO_ROOT / "prompts" / "mode-validate-discover.txt",
		schema_path=SCRIPTS / "templates" / "slot_manifest.schema.json",
		model="openai/gpt-6-sol",
		reasoning_effort="high",
		attempts=1,
		executor=executor,
		sleep_fn=lambda _seconds: None,
		engine_resolver=lambda: "claude",
	)
	command, env, input_text = executor.seen[0]
	assert command[:5] == ["bash", "-c", 'source "$0"; claude_run_selected "$@"', str(root / "scripts" / "ai_engine.sh"), "VALIDATION_REFRESH"]
	assert command[7] == str(clone)
	assert command[8:10] == ["--codex-stdio", "--"]
	assert command[10:12] == ["bash", str(SCRIPTS / "codex_isolated_exec.sh")]
	assert not Path(command[5]).is_relative_to(clone)
	assert not Path(command[5]).parent.exists(), "the private prompt directory is removed"
	assert env["AI_ENGINE_READ_ONLY"] == "true" and env["AI_ENGINE_EFFORT_HINT"] == "high"
	assert input_text is None
	# The output file, not stdout, is the candidate text.
	assert result.outcome == "failed"
	assert "ignored" not in (result.failure_reason or "")


def test_discovery_codex_branch_keeps_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	discovery = _load_discovery()
	monkeypatch.setenv("CLAUDE_ENGINE_SUPPORT_DIR", str(tmp_path / "absent"))
	seen: list[list[str]] = []

	class _Codex:
		def run(self, command, **kwargs):
			seen.append(list(command))
			assert kwargs["input_text"]
			return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="")

	discovery.discover_manifest_via_codex(
		clone_dir=tmp_path,
		prompt_path=REPO_ROOT / "prompts" / "mode-validate-discover.txt",
		schema_path=SCRIPTS / "templates" / "slot_manifest.schema.json",
		model="openai/gpt-6-sol",
		reasoning_effort="high",
		attempts=1,
		executor=_Codex(),
		sleep_fn=lambda _seconds: None,
	)
	assert seen[0][:5] == ["bash", str(SCRIPTS / "codex_isolated_exec.sh"), "run", "--mode", "read-only"]


# --- Item 3c: workflow heal, check triage, activation verify, unblock judge ---

HEAL_TRIAGE_ROLES = ("WORKFLOW_HEAL", "CHECK_TRIAGE", "ACTIVATION_VERIFY", "UNBLOCK_JUDGE")
HEAL_INTAKE = SCRIPTS / "workflow_failure_heal_intake.sh"
ACTIVATION_VERIFY = SCRIPTS / "activation_verify.sh"
CHECK_TRIAGE = SCRIPTS / "check_failure_triage.sh"
CLARIFY_ISOLATED = SCRIPTS / "clarify_isolated_run.sh"
UNBLOCK_JUDGE = SCRIPTS / "unblock_judge.sh"


@pytest.mark.parametrize("role", HEAL_TRIAGE_ROLES)
def test_heal_triage_role_defaults_to_claude(role: str) -> None:
	result = _resolve(role)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "claude"
	assert f"AI_ENGINE_SELECTED role={role} engine=claude" in result.stderr


@pytest.mark.parametrize("role", HEAL_TRIAGE_ROLES)
def test_heal_triage_role_variable_rolls_back_to_codex(role: str) -> None:
	result = _resolve(role, **{f"AI_ENGINE_{role}": "codex"})
	assert result.stdout.strip() == "codex"
	assert f"source=var:AI_ENGINE_{role}" in result.stderr


@pytest.mark.parametrize("role", HEAL_TRIAGE_ROLES)
def test_heal_triage_role_codex_label_wins(role: str) -> None:
	result = _resolve(role, AI_ENGINE_LABELS="ai:engine-claude,ai:codex")
	assert result.stdout.strip() == "codex"
	assert "source=label:ai:codex" in result.stderr


def test_heal_intake_routes_through_the_selector_from_its_own_directory() -> None:
	text = HEAL_INTAKE.read_text(encoding="utf-8")
	assert 'heal_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in text
	assert 'heal_engine_helper="${heal_script_dir}/ai_engine.sh"' in text
	assert "bash -c 'source \"$0\"; claude_run_selected \"$@\"' \"${heal_engine_helper}\"" in text
	assert 'WORKFLOW_HEAL "${PROMPT_FILE}" "${DIAG_FILE}" "${PWD}" --codex-stdio -- "${heal_codex_cmd[@]}"' in text
	assert 'AI_ENGINE_INCLUDE_PATHS="${heal_include_paths}"' in text
	assert "AI_ENGINE_FALLBACK role=WORKFLOW_HEAL reason=engine_support_missing" in text
	# The codex command line is unchanged: the isolated launcher and its arguments.
	assert 'heal_codex_cmd=(bash "${heal_script_dir}/codex_isolated_exec.sh" "${heal_isolated_args[@]}" --' in text
	for needle in (
		"--ask-for-approval never",
		"-c include_apply_patch_tool=false",
		"-c 'shell_environment_policy.filters.OPENROUTER_API_KEY=\"exclude\"'",
		'--model "${MODEL_EDITOR:-openai/gpt-6-sol}"',
		"--sandbox read-only)",
	):
		assert needle in text
	assert "source scripts/ai_engine.sh" not in text
	assert text.count("env -u GH_TOKEN -u GITHUB_TOKEN -u TG_BOT_SECRET -u TG_ADMIN_CHAT_ID -u TG_CHAT_ID") == 2


def test_activation_verify_routes_through_the_trusted_selector() -> None:
	text = ACTIVATION_VERIFY.read_text(encoding="utf-8")
	assert 'local activation_engine_helper="${SUPPORT_DIR}/scripts/ai_engine.sh"' in text
	assert 'ACTIVATION_VERIFY "${prompt_file}" "${output_file}" "${TARGET_DIR}" --codex-stdio --' in text
	assert "AI_ENGINE_FALLBACK role=ACTIVATION_VERIFY reason=engine_support_missing" in text
	# Project mode reads the tracking issue's labels so ai:codex keeps codex.
	assert 'repos/${REPOSITORY}/issues/${TRACKING_NUM}/labels?per_page=100' in text
	assert 'AI_ENGINE_LABELS="${activation_engine_labels:-${AI_ENGINE_LABELS:-}}"' in text
	assert text.count(
		'bash "${SUPPORT_DIR}/scripts/codex_isolated_exec.sh" run --mode read-only --workdir "${TARGET_DIR}" --reasoning "${reasoning}" --'
	) == 2
	# The model key redaction of GitHub-bound text is unchanged.
	assert 'api_key = os.environ.get("OPENROUTER_API_KEY", "")' in text
	assert 'text = text.replace(api_key, "[redacted]")' in text


def test_check_triage_uses_the_isolated_helpers_claude_branch() -> None:
	text = CHECK_TRIAGE.read_text(encoding="utf-8")
	assert 'triage_engine_helper="${TRUSTED_SUPPORT_DIR}/scripts/ai_engine.sh"' in text
	assert "ai_engine_for_role CHECK_TRIAGE" in text
	assert '"${RUNTIME_DIR}/codex_log.txt" claude CHECK_TRIAGE) || triage_rc=$?' in text
	assert 'if [ "${triage_rc}" -eq 75 ]; then' in text
	# The unchanged three-argument codex call still runs on codex and after exit 75.
	assert 'bash "${ISOLATED_HELPER}" "${PROMPT_FILE}" "${DIAG_FILE}" "${RUNTIME_DIR}/codex_log.txt") || triage_rc=$?' in text
	assert "SOURCE_ROOT}/scripts/ai_engine.sh" not in text
	assert "log \"warn isolation_unavailable\"" in text
	# The PR's labels come from the already fetched payload, so ai:codex keeps codex.
	assert '"${RUNTIME_DIR}/pr_payload.json"' in text
	assert 'AI_ENGINE_LABELS="${triage_engine_labels:-${AI_ENGINE_LABELS:-}}"' in text
	helper = CLARIFY_ISOLATED.read_text(encoding="utf-8")
	assert helper.count("^(CLARIFY|CLARIFY_RESPOND|PLAN|UNBLOCK_JUDGE|CHECK_TRIAGE)$") == 2


def test_unblock_judge_engine_paths_are_unchanged() -> None:
	text = UNBLOCK_JUDGE.read_text(encoding="utf-8")
	assert "ai_engine_for_role UNBLOCK_JUDGE" in text
	assert "claude UNBLOCK_JUDGE" in text
	assert "codex UNBLOCK_JUDGE" in text


def _job_steps(workflow: str, job: str) -> list[dict]:
	yaml = pytest.importorskip("yaml")

	data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
	return data["jobs"][job]["steps"]


@pytest.mark.parametrize(
	"workflow, job, action_prefix",
	[
		("workflow-failure-heal-intake.yml", "intake", "./.github/actions/"),
		("check_failure_triage.yml", "triage", "./.codex-workflow-src/.github/actions/"),
		("issue_pr_status.yml", "activation-verify", "./.codex-workflow-src/.github/actions/"),
	],
)
def test_heal_triage_workflows_gate_claude_steps_on_the_engine(workflow: str, job: str, action_prefix: str) -> None:
	steps = _job_steps(workflow, job)
	names = [step.get("name", "") for step in steps]
	resolve = names.index("Resolve AI engine")
	for name, action in (("Install Claude Code CLI", "install-claude"), ("Resolve Claude credential", "claude-pool-token")):
		index = names.index(name)
		assert index > resolve
		step = steps[index]
		assert step["uses"] == action_prefix + action
		assert step["if"] == "steps.ai_engine.outputs.engine == 'claude'"
		assert step["continue-on-error"] is True


def test_triage_job_permissions_and_staging() -> None:
	yaml = pytest.importorskip("yaml")

	data = yaml.safe_load((WORKFLOWS / "check_failure_triage.yml").read_text(encoding="utf-8"))
	job = data["jobs"]["triage"]
	assert job["permissions"] == {"contents": "read", "id-token": "write"}
	names = [step.get("name", "") for step in job["steps"]]
	assert names.index("Resolve Claude credential") < names.index("Stage workflow support files")
	stage = job["steps"][names.index("Stage workflow support files")]["run"]
	assert ".claude/hooks/gh_api_write_guard.py" in stage
	assert "scripts/claude_anthropic_relay.py" in stage
	assert "! -name claude-pool-token" in stage
	# The engine preflight sees the PR labels the diagnosis stage reads, taken
	# from the prerequisite's existing PR payload (no extra API call).
	derive = data["jobs"]["derive_check_name_key"]
	assert derive["outputs"]["pr_labels"] == "${{ steps.hash_check_name.outputs.pr_labels }}"
	resolve = job["steps"][names.index("Resolve AI engine")]
	assert resolve["env"]["AI_ENGINE_LABELS"] == "${{ needs.derive_check_name_key.outputs.pr_labels || '' }}"


def test_activation_verify_job_adds_no_permissions_and_poller_resolves_the_role() -> None:
	yaml = pytest.importorskip("yaml")

	data = yaml.safe_load((WORKFLOWS / "issue_pr_status.yml").read_text(encoding="utf-8"))
	assert "permissions" not in data["jobs"]["activation-verify"]
	# The merged PR's labels reach both the preflight resolve and the verifier.
	labels_expr = "${{ toJSON(github.event.pull_request.labels.*.name) }}"
	for step in data["jobs"]["activation-verify"]["steps"]:
		if step.get("name") in ("Resolve AI engine", "Verify activation"):
			assert step["env"]["AI_ENGINE_LABELS"] == labels_expr
	poll = (WORKFLOWS / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert "SECURITY_AUDIT ACTIVATION_VERIFY; do" in poll
	assert poll.count("AI_ENGINE_ACTIVATION_VERIFY: ${{ vars.AI_ENGINE_ACTIVATION_VERIFY || '' }}") == 2


# --- item 3d: log analysis and utility roles -----------------------------------------

LOG_UTILITY_ROLES = ("LOG_ANALYSIS", "LOG_AUDIT", "LOG_SUMMARY", "RETRO", "MATERIALITY", "SUMMARISER", "BEHAVIOURAL_SMOKE")
SONNET_ROLES = ("LOG_SUMMARY", "RETRO", "MATERIALITY", "SUMMARISER", "BEHAVIOURAL_SMOKE")
LOG_ANALYSIS_WORKFLOW = WORKFLOWS / "workflow-log-analysis.yml"
RETRO_FANOUT = SCRIPTS / "workflow_retro_fanout.sh"
SUMMARISER_SCRIPT = SCRIPTS / "summarize_reviewer_consensus.sh"
SMOKE_SCRIPT = SCRIPTS / "review_synthesise_smoke.sh"
REVIEW_SANDBOX = SCRIPTS / "review_untrusted_sandbox.sh"


@pytest.mark.parametrize("role", LOG_UTILITY_ROLES)
def test_log_utility_role_defaults_to_claude(role: str) -> None:
	result = _resolve(role)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "claude"
	assert f"AI_ENGINE_SELECTED role={role} engine=claude" in result.stderr


@pytest.mark.parametrize("role", LOG_UTILITY_ROLES)
def test_log_utility_role_model(role: str) -> None:
	env = {key: value for key, value in os.environ.items() if not key.startswith(("AI_ENGINE", "SUPPORT_", "GITHUB_WORKSPACE", "GITHUB_EVENT"))}
	result = subprocess.run(
		["bash", "-c", 'source "$0"; ai_engine_model "$1" openai/gpt-6-luna', str(AI_ENGINE), role],
		capture_output=True,
		text=True,
		env=env,
		timeout=60,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	expected = "claude-sonnet-5-5" if role in SONNET_ROLES else "claude-opus-5-5"
	assert result.stdout.strip() == expected


@pytest.mark.parametrize("role", LOG_UTILITY_ROLES)
def test_log_utility_role_variable_rolls_back_to_codex(role: str) -> None:
	result = _resolve(role, **{f"AI_ENGINE_{role}": "codex"})
	assert result.stdout.strip() == "codex"
	assert f"source=var:AI_ENGINE_{role}" in result.stderr


@pytest.mark.parametrize("role", LOG_UTILITY_ROLES)
def test_log_utility_role_codex_label_wins(role: str) -> None:
	result = _resolve(role, AI_ENGINE_LABELS='["ai:codex"]', **{f"AI_ENGINE_{role}": "claude"})
	assert result.stdout.strip() == "codex"
	assert "source=label:ai:codex" in result.stderr


LOG_ANALYSIS_JOBS = {
	"weekly-retro": ("RETRO",),
	"analyze-commit-notify": ("LOG_SUMMARY", "LOG_ANALYSIS"),
	"deep-audit": ("LOG_AUDIT",),
	"api-redundancy": ("LOG_ANALYSIS",),
}


@pytest.mark.parametrize("job", sorted(LOG_ANALYSIS_JOBS))
def test_log_analysis_jobs_gate_claude_steps_on_the_engine(job: str) -> None:
	yaml = pytest.importorskip("yaml")

	data = yaml.safe_load(LOG_ANALYSIS_WORKFLOW.read_text(encoding="utf-8"))
	job_data = data["jobs"][job]
	steps = job_data["steps"]
	names = [step.get("name") for step in steps]
	resolve = names.index("Resolve AI engine")
	assert names[resolve - 1] == "Checkout repository"
	assert steps[resolve]["id"] == "ai_engine"
	assert steps[resolve]["env"]["WLA_ENGINE_ROLES"] == " ".join(LOG_ANALYSIS_JOBS[job])
	run = steps[resolve]["run"]
	assert '"${checkout_head}" = "${GITHUB_SHA}"' in run
	assert 'ai_engine_stage_support "${GITHUB_WORKSPACE}" "${CLAUDE_ENGINE_SUPPORT_DEST}"' in run
	for name, action in (("Install Claude Code CLI", "install-claude"), ("Resolve Claude credential", "claude-pool-token")):
		step = steps[names.index(name)]
		assert step["if"] == "steps.ai_engine.outputs.any_claude == 'true'"
		assert step["continue-on-error"] is True
		assert step["uses"] == f"./.github/actions/{action}"
	assert job_data["env"]["AI_ENGINE"] == "${{ vars.AI_ENGINE || '' }}"
	for role in LOG_ANALYSIS_JOBS[job]:
		assert job_data["env"][f"AI_ENGINE_{role}"] == f"${{{{ vars.AI_ENGINE_{role} || '' }}}}"


def test_log_analysis_passes_route_through_the_selector() -> None:
	text = LOG_ANALYSIS_WORKFLOW.read_text(encoding="utf-8")
	assert 'claude_run_selected RETRO "${CODEX_PROMPT_FILE}" "${RETRO_BODY_FILE}" "${PWD}" -- wla_codex_retro' in text
	assert 'claude_run_selected LOG_ANALYSIS "${CODEX_PROMPT_FILE}" "${REPORT_FILE}" "${PWD}" -- wla_codex_analyze' in text
	assert 'claude_run_selected LOG_AUDIT "${CODEX_PROMPT_FILE}" "${SECTION_FILE}" "${PWD}" -- wla_codex_audit' in text
	assert 'claude_run_selected LOG_ANALYSIS "${CODEX_PROMPT_FILE}" "${SECTION_FILE}" "${PWD}" -- wla_codex_api_redundancy' in text
	# The engine root is the staged support, never the checkout.
	assert text.count('source "${CLAUDE_ENGINE_SUPPORT_DIR}/scripts/ai_engine.sh"') == 4
	# The codex commands are unchanged; xhigh stays a codex-only setting.
	assert text.count("bash scripts/codex_heartbeat.sh") == 4
	assert text.count("--reasoning xhigh") == 2
	assert 'AI_ENGINE_MODEL_HINT="${WORKFLOW_EDITOR_MODEL}" AI_ENGINE_EFFORT_HINT=""' in text
	assert "WLA_API_REDUNDANCY_EFFORT_HINT: ${{ vars.THINKING_LEVEL_ANALYSIS || 'high' }}" in text


def test_retro_fanout_routes_through_the_trusted_selector() -> None:
	text = RETRO_FANOUT.read_text(encoding="utf-8")
	body = _function_body(text, "retro_fanout_model_call")
	assert '${CLAUDE_ENGINE_SUPPORT_DIR:-${RUNNER_TEMP:-/tmp}/claude-engine-support}/scripts/ai_engine.sh' in body
	assert "claude_run_selected RETRO" in body
	assert "-- retro_fanout_codex_call" in body
	assert "unset GH_TOKEN GITHUB_TOKEN" in body
	assert "SCRIPT_DIR}/ai_engine.sh" not in text
	codex_body = _function_body(text, "retro_fanout_codex_call")
	assert '-- bash "${SCRIPT_DIR}/codex_isolated_exec.sh" run --mode read-only --workdir "${REPO_ROOT}"' in codex_body
	assert "WORKFLOW_RETRO_FANOUT_V1: repo=${target_repo} week=${week_label} status=posted" in text


def _load_unselected_summarizer():
	spec = importlib.util.spec_from_file_location("_summarize_unselected_runs_3d", SCRIPTS / "summarize_unselected_runs.py")
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _fake_engine_root(tmp_path: Path, engine: str, claude_rc: int) -> Path:
	root = tmp_path / "engine"
	(root / "scripts").mkdir(parents=True)
	(root / "scripts" / "ai_engine.sh").write_text(
		"ai_engine_for_role() { printf '%s\\n' '" + engine + "'; }\n"
		"claude_run()\n{\n"
		"\tprintf 'role=%s gh=%s or=%s ro=%s\\n' \"$1\" \"${GH_TOKEN:-}\" \"${OPENROUTER_API_KEY:-}\" \"${AI_ENGINE_READ_ONLY:-}\" > \"$3\"\n"
		"\treturn " + str(claude_rc) + "\n}\n",
		encoding="utf-8",
	)
	return root


def test_unselected_summaries_resolve_the_engine_from_trusted_support(tmp_path: Path) -> None:
	module = _load_unselected_summarizer()
	assert module.resolve_engine_script("") is None
	assert module.resolve_engine_script(str(tmp_path / "missing")) is None
	claude_root = _fake_engine_root(tmp_path / "c", "claude", 0)
	script = module.resolve_engine_script(str(claude_root))
	assert script == claude_root / "scripts" / "ai_engine.sh"
	assert module.resolve_log_summary_engine(script, "openai/gpt-6-luna") == "claude"
	assert module.resolve_log_summary_engine(None, "openai/gpt-6-luna") == "codex"
	codex_root = _fake_engine_root(tmp_path / "x", "codex", 0)
	assert module.resolve_log_summary_engine(module.resolve_engine_script(str(codex_root)), "m") == "codex"
	link_root = tmp_path / "link"
	(link_root / "scripts").mkdir(parents=True)
	(link_root / "scripts" / "ai_engine.sh").symlink_to(script)
	assert module.resolve_engine_script(str(link_root)) is None


def test_unselected_summaries_claude_run_is_read_only_and_credential_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	module = _load_unselected_summarizer()
	monkeypatch.setenv("GH_TOKEN", "ghs_secret")
	monkeypatch.setenv("OPENROUTER_API_KEY", "or_secret")
	script = module.resolve_engine_script(str(_fake_engine_root(tmp_path, "claude", 0)))
	summarizer = module.ClaudeEngineSummarizer(script, model="openai/gpt-6-luna", timeout_seconds=5)
	assert summarizer.timeout_seconds == module.CLAUDE_ENGINE_MIN_TIMEOUT_SECONDS
	text, tokens = summarizer.summarize({"repository": "o/r", "run_id": 1}, "step log")
	assert text == "role=LOG_SUMMARY gh= or= ro=true"
	assert tokens > 0
	unavailable = module.ClaudeEngineSummarizer(
		module.resolve_engine_script(str(_fake_engine_root(tmp_path / "u", "claude", 75))), model="m", timeout_seconds=5
	)
	with pytest.raises(module.ClaudeEngineUnavailable):
		unavailable.summarize({"repository": "o/r", "run_id": 1}, "step log")


def _run_unselected_main(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str, claude_rc: int) -> tuple[dict, list[str]]:
	import json as _json

	report = tmp_path / "report.json"
	report.write_text(_json.dumps({"runs": [{"repository": "o/r", "run_id": 7, "created_at": "2026-10-01T00:00:00Z"}]}), encoding="utf-8")

	class _Collector:
		@staticmethod
		def _fetch_run_log_archive(repository, run_id, token, cache):
			return b""

		@staticmethod
		def extract_full_logs(_archive):
			return [{"step_name": "build", "content": "error: boom"}]

	calls: list[str] = []

	class _OpenRouter:
		def __init__(self, *args, **kwargs) -> None:
			calls.append("openrouter")

		def summarize(self, run, logs_text):
			return "- openrouter summary", 10

	monkeypatch.setattr(module, "_load_collector_module", lambda: _Collector)
	monkeypatch.setattr(module, "OpenRouterSummarizer", _OpenRouter)
	monkeypatch.setenv("CLAUDE_ENGINE_SUPPORT_DIR", str(_fake_engine_root(tmp_path / engine, engine, claude_rc)))
	monkeypatch.setenv("OPENROUTER_API_KEY", "orkey")
	monkeypatch.setenv("GH_TOKEN", "ghs_test")
	assert module.main(["--report", str(report)]) == 0
	run = _json.loads(report.read_text(encoding="utf-8"))["runs"][0]
	return run, calls


def test_unselected_summaries_use_claude_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	module = _load_unselected_summarizer()
	run, calls = _run_unselected_main(module, tmp_path, monkeypatch, "claude", 0)
	assert run["log_summary"].startswith("role=LOG_SUMMARY")
	assert calls == []


def test_unselected_summaries_fall_back_to_openrouter_on_exit_75(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
	module = _load_unselected_summarizer()
	run, calls = _run_unselected_main(module, tmp_path, monkeypatch, "claude", 75)
	assert run["log_summary"] == "- openrouter summary"
	assert calls == ["openrouter"]
	assert "AI_ENGINE_FALLBACK role=LOG_SUMMARY reason=claude_unavailable" in capsys.readouterr().err


def test_unselected_summaries_codex_selection_never_runs_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	module = _load_unselected_summarizer()
	run, calls = _run_unselected_main(module, tmp_path, monkeypatch, "codex", 0)
	assert run["log_summary"] == "- openrouter summary"
	assert calls == ["openrouter"]


@pytest.mark.parametrize(
	"path, role, host_command",
	[
		(SUMMARISER_SCRIPT, "SUMMARISER", "summariser_opencode_cmd"),
		(SMOKE_SCRIPT, "BEHAVIOURAL_SMOKE", "behavioural_smoke_opencode_cmd"),
	],
)
def test_review_utility_roles_use_the_review_sandbox(path: Path, role: str, host_command: str) -> None:
	text = path.read_text(encoding="utf-8")
	assert f"ai_engine_for_role {role}" in text
	assert "prepare-ephemeral \"${sandbox_engine}\"" in text
	assert f'"${{sandbox_engine}}" {role} read' in text
	assert "summariser_sandbox_attempt claude" in text or "behavioural_smoke_sandbox_attempt claude" in text
	assert "summariser_sandbox_attempt codex" in text or "behavioural_smoke_sandbox_attempt codex" in text
	# Finding review-summarizer-host-fallback: no path runs OpenCode on the
	# host; an unavailable sandbox is refused with REVIEW_UTILITY_ISOLATION.
	assert host_command not in text
	assert "opencode_run_cmd" not in text
	assert f"AI_ENGINE_FALLBACK role={role} reason=sandbox_unavailable" not in text
	assert f"REVIEW_UTILITY_ISOLATION role={role} engine=" in text
	assert "outcome=refused reason=" in text
	assert '_engine_state="legacy"' not in text
	# Never a host claude_run.
	assert not re.search(r"^\s*(AI_ENGINE_[A-Z_]+=\S+\s+)*claude_run(_selected)?\s", text, re.M)


def test_review_sandbox_admits_utility_roles_read_only() -> None:
	text = REVIEW_SANDBOX.read_text(encoding="utf-8")
	allow = re.search(r'case "\$\{claude_role\}" in ([A-Z_|]+)\) ;; \*\) exit 2 ;; esac', text)
	assert allow and {"SUMMARISER", "BEHAVIOURAL_SMOKE"} <= set(allow.group(1).split("|"))
	assert "WAVE_JUDGE|STALL_JUDGE|INTEGRATION_JUDGE|SECURITY_JUDGE|SUMMARISER|BEHAVIOURAL_SMOKE)\n\t\t[ \"${claude_access}\" = read ] || exit 2 ;;" in text
	assert '"SUMMARISER", "BEHAVIOURAL_SMOKE"}' in text
	assert "RB_JUDGE|REVIEW_CONSOLIDATOR|SUMMARISER|BEHAVIOURAL_SMOKE)" in text
	engine = AI_ENGINE.read_text(encoding="utf-8")
	sandbox_only = re.search(r"_AI_ENGINE_SANDBOX_ONLY_ROLES=\(([^)]*)\)", engine)
	assert sandbox_only and "SUMMARISER" not in sandbox_only.group(1)


def test_materiality_makes_no_model_call() -> None:
	text = (SCRIPTS / "review_agents_md_materiality.sh").read_text(encoding="utf-8")
	for marker in ("opencode_run_cmd", "codex exec", "claude_run", "codex_isolated_exec.sh"):
		assert marker not in text


def test_implement_issue_summary_routes_through_the_selector() -> None:
	text = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
	resolve = text[_step_index(text, "Resolve AI engine"):_step_index(text, "Install Claude Code CLI")]
	assert "AI_ENGINE_SUMMARISER: ${{ vars.AI_ENGINE_SUMMARISER || '' }}" in resolve
	assert 'summariser_engine="$(ai_engine_for_role SUMMARISER || echo codex)"' in resolve
	assert 'ai_engine_stage_support "${GITHUB_WORKSPACE}/.codex-workflow-src" "${claude_engine_support_dest}"' in resolve
	assert 'echo "AI_ENGINE_RESOLVED_SUMMARISER=${summariser_engine}" >> "$GITHUB_ENV"' in resolve
	summary = text[_step_index(text, "Generate AI issue summary for PR comment"):_step_index(text, "Post AI issue summary as first PR comment")]
	assert 'claude_run_selected SUMMARISER "${_summary_combined_prompt}" "${ISSUE_SUMMARY_FILE}" "${PWD}" -- implement_summary_codex_call' in summary
	assert 'source "${CLAUDE_ENGINE_SUPPORT_DIR}/scripts/ai_engine.sh"' in summary
	assert 'AI_ENGINE_INCLUDE_PATHS="${RUNTIME_DIR}/issue_context_for_summary.txt"' in summary
	assert "Start with this exact heading: \"### AI Issue Summary\"" in summary
	assert "grep -q '^### AI Issue Summary' \"${ISSUE_SUMMARY_FILE}\"" in summary
	assert '--include "${RUNTIME_DIR}/issue_context_for_summary.txt"' in summary
