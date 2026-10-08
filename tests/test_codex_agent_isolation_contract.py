#!/usr/bin/env python3
"""Contract: every Codex agent that reads untrusted text runs isolated.

The plan, implement, validate, orchestrator, log-analysis, triage,
security-audit and failure-heal agents read issue bodies, comments, PR diffs
or CI / workflow logs. A prompt injection there must not be able to read
GH_PAT (GH_TOKEN), the OpenRouter key, or the checkout's .git (whose config
carries the GH_PAT remote URL and checkout extraheader). They therefore run
through scripts/codex_isolated_exec.sh or, for triage, through
scripts/clarify_isolated_run.sh: credential-free, network-isolated containers
that reach the model only through the host-side broker
(scripts/clarify_openrouter_broker.py). These checks pin that wiring; the
behaviour is covered by tests/test_codex_isolated_exec.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
HELPER = SCRIPTS / "codex_isolated_exec.sh"

# Files allowed to start the codex binary directly: the container
# entrypoints (Codex runs inside the isolated container there).
# heal_isolated_implement.sh runs the workflow-heal editor in its own
# credential-free `--network none --read-only --cap-drop ALL` container (#6463).
CONTAINER_ENTRYPOINTS = {"codex_isolated_exec.sh", "clarify_isolated_run.sh", "heal_isolated_implement.sh"}

RAW_CODEX = re.compile(r'''(?:^|[\s;&|(]|--\s)(?<!Usage: )codex\s+(?:--ask-for-approval|-c\s|exec\b|"\$@")''')
PY_RAW_CODEX = re.compile(r'''\[\s*"codex"\s*,''')


def logical_lines(text: str):
	"""Shell-ish lines with backslash continuations joined; comments and echo text dropped."""
	buffer = ""
	for raw in text.splitlines():
		stripped = raw.strip()
		if stripped.startswith("#"):
			continue
		if raw.rstrip().endswith("\\"):
			buffer += raw.rstrip()[:-1] + " "
			continue
		line = buffer + raw
		buffer = ""
		if re.match(r'''\s*(echo|printf)\b''', line):
			continue
		yield line


def raw_codex_sites(path: Path):
	text = path.read_text(encoding="utf-8")
	if path.suffix == ".py":
		return [m.group(0) for m in PY_RAW_CODEX.finditer(text)]
	return [line.strip() for line in logical_lines(text) if RAW_CODEX.search(line)]


def test_no_codex_agent_is_started_outside_the_isolation_helpers():
	offenders = {}
	paths = (list(SCRIPTS.rglob("*.sh")) + list(SCRIPTS.rglob("*.py")) +
		list(WORKFLOWS.rglob("*.yml")) + list(WORKFLOWS.rglob("*.yaml")) +
		list((REPO_ROOT / ".github" / "actions").rglob("*.yml")) +
		list((REPO_ROOT / ".github" / "actions").rglob("*.yaml")))
	for path in sorted(paths):
		if path.parent == SCRIPTS and path.name in CONTAINER_ENTRYPOINTS:
			continue
		sites = raw_codex_sites(path)
		if sites:
			offenders[str(path.relative_to(REPO_ROOT))] = sites
	assert not offenders, (
		"Codex must run through scripts/codex_isolated_exec.sh (credential-free, network-isolated "
		f"container); found direct launches: {offenders}"
	)


def test_thread_reuse_launches_through_the_isolated_launcher():
	text = (SCRIPTS / "codex_thread_reuse.sh").read_text(encoding="utf-8")
	assert 'cmd=("${CODEX_THREAD_REUSE_LAUNCHER[@]}" --ask-for-approval never' in text
	assert 'CODEX_THREAD_REUSE_LAUNCHER=(bash "${CODEX_ISOLATED_EXEC}" run --mode "${CODEX_ISOLATED_MODE:-read-only}")' in text
	assert text.count('"${CODEX_THREAD_REUSE_LAUNCHER[@]}" "${prefix[@]}" exec') == 3


@pytest.mark.parametrize(
	"relative, needle",
	[
		("scripts/run_plan_codex.sh", 'bash "${CODEX_ISOLATED_EXEC}" run --mode read-only --'),
		("scripts/validate_process.sh", 'CODEX_ISOLATED_MODE="read-only"'),
		("scripts/self_heal_validation.sh", 'self_heal_codex_cmd=(bash "${CODEX_ISOLATED_EXEC}" run --mode read-only --)'),
		("scripts/implement_diagnose_post_codex_failure.sh", 'diagnose_codex_cmd=(bash "${CODEX_ISOLATED_EXEC}" run --mode read-only --)'),
		("scripts/workflow_retro_fanout.sh", 'codex_isolated_exec.sh" run --mode read-only --workdir "${REPO_ROOT}"'),
		("scripts/check_failure_triage.sh", 'bash "${ISOLATED_HELPER}" "${PROMPT_FILE}" "${DIAG_FILE}" "${RUNTIME_DIR}/codex_log.txt"'),
		("scripts/security_audit.sh", 'codex_isolated_exec.sh" run --mode read-only ${audit_isolated_args[@]+"${audit_isolated_args[@]}"} --'),
		("scripts/workflow_failure_heal_intake.sh", 'heal_isolated_args=(run --mode read-only)'),
		("scripts/validation_discovery_bootstrap.py", 'CODEX_ISOLATED_EXEC = Path(__file__).resolve().parent / "codex_isolated_exec.sh"'),
		("scripts/orchestrate_poll_process.sh", 'ORCH_CODEX_ISOLATED_EXEC="${ORCH_SCRIPTS_ROOT}/codex_isolated_exec.sh"'),
		(".github/workflows/orchestrate.yml", "bash scripts/codex_isolated_exec.sh run --mode read-only --"),
		(".github/workflows/workflow-log-analysis.yml", 'bash scripts/codex_isolated_exec.sh run --mode read-only ${wla_run_logs_include_args[@]+"${wla_run_logs_include_args[@]}"} --'),
		(".github/workflows/implement.yml", 'codex_isolated_exec.sh" run --mode read-only \\'),
	],
)
def test_each_agent_site_uses_the_helper(relative, needle):
	assert needle in (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_helper_container_has_no_credentials_network_or_host_checkout():
	text = HELPER.read_text(encoding="utf-8")
	run_block = text[text.index('env -i PATH="${PATH}" HOME="${HOME:-/tmp}" docker run --rm -i --init'):]
	run_block = run_block[: run_block.index("docker_pid=$!")]
	for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--user"):
		assert flag in run_block
	docker_flags = run_block[: run_block.index('"${image}" /bin/bash -c')]
	assert '"${engine_env[@]}"' in docker_flags
	# The container environment is the per-engine engine_env array.
	engine_env_blocks = re.findall(r"engine_env=\((.*?)\n\t\)", text, re.S)
	assert len(engine_env_blocks) == 2
	env_flags = re.findall(r'''--env\s+"?([^"\s]+)''', docker_flags + "".join(engine_env_blocks))
	assert env_flags and "--env-file" not in docker_flags and " -e " not in docker_flags
	for secret in ("OPENROUTER_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "TG_BOT_SECRET"):
		assert not any(secret in flag for flag in env_flags), f"{secret} must never be passed to the agent container"
	assert "docker.sock" not in text
	# The key is handed only to the host-side broker, in an otherwise empty env.
	assert 'env -i PATH="${PATH}" OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" CLARIFY_MODEL="${model}"' in text
	# Support files come from the helper's own (trusted) directory, never the cwd.
	assert 'support_dir="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"' in text
	# No switch that turns isolation off and no host fallback.
	assert "CODEX_ISOLATION_DISABLED" not in text and "fallback" not in text.lower().replace("fall back", "")


def test_dependency_containers_require_the_registry_proxy():
	for helper_name in ("codex_isolated_exec.sh", "review_untrusted_sandbox.sh"):
		text = (SCRIPTS / helper_name).read_text(encoding="utf-8")
		assert "dependency_registry_proxy.py" in text
		for line in logical_lines(text):
			if "docker run --rm" in line:
				assert "--network none" in line, helper_name
	stage = (SCRIPTS / "stage_workflow_support.sh").read_text(encoding="utf-8")
	assert "dependency_registry_proxy.py" in stage.split('REQUIRED_BOOTSTRAP_SCRIPTS="', 1)[1].split('"', 1)[0].split()
	implement = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
	assert "dependency_registry_proxy.py" in implement.split("for f in ", 1)[1].split("; do", 1)[0].split()
	for workflow, minimum in (("implement.yml", 2), ("review_autofix.yml", 2)):
		text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
		assert text.count("DEPENDENCY_PROXY_ALLOWED_HOSTS: ${{ vars.DEPENDENCY_PROXY_ALLOWED_HOSTS || '' }}") >= minimum
	assert "Review dependencies skipped: registry proxy support missing" in (SCRIPTS / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert "Review dependencies skipped: registry proxy unavailable" in (SCRIPTS / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert "Review dependencies skipped: registry proxy bridge unavailable" in (SCRIPTS / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")


def step_block(workflow_text: str, name: str) -> str:
	start = workflow_text.index(f"      - name: {name}\n")
	end = workflow_text.find("\n      - name: ", start + 1)
	return workflow_text[start:] if end == -1 else workflow_text[start:end]


def test_implement_runs_privileged_helpers_from_the_trusted_copy():
	text = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
	stage = step_block(text, "Stage workflow support files")
	# The trusted run copy exists in every repo, not only coding-workflows.
	run_dir_line = 'IMPLEMENT_STAGED_SUPPORT_RUN_DIR="${RUNTIME_DIR}/staged_support_run/scripts"'
	assert run_dir_line in stage
	assert stage.index(run_dir_line) > stage.index('if [ "${is_self_repo}" = "true" ]; then\n            STAGED_SUPPORT_LEDGER=')
	assert stage.index(run_dir_line) > stage.index('"STAGED_SUPPORT_EDITOR_HEAD_LEDGER=${RUNTIME_DIR}/staged_support_editor_head.txt"')
	for name in ("codex_isolated_exec.sh", "codex_isolated_workspace.py", "clarify_openrouter_broker.py"):
		assert f" {name}" in stage
	assert '_staged_support_hooks_dest="${RUNTIME_DIR}/staged_support_run/.github/ai/workspace_hooks/implement"' in stage

	codex_index = text.index("      - name: Run Codex implementation\n")
	after_editor = text[codex_index:]
	# No step from the Codex step onward executes a workspace copy of a
	# support script: the agent's transferred edits could be in it.
	prompt_free = re.sub(r"cat > \"\$\{PROMPT_TEMPLATE_FILE\}\" <<'EOF'.*?\n          EOF\n", "", after_editor, flags=re.S)
	executions = re.findall(r'''(?:^|[\s;&|(])(?:bash|source|\.|python3)\s+"?scripts/[A-Za-z0-9_]+\.(?:sh|py)''', prompt_free, flags=re.M)
	assert executions == [], executions
	codex_step = step_block(text, "Run Codex implementation")
	assert 'CODEX_ISOLATED_EXEC="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}/codex_isolated_exec.sh"' in codex_step
	assert 'prepare --workdir "$(pwd)" --deps' in codex_step
	assert 'CODEX_ISOLATED_MODE="workspace"' in codex_step
	repair = step_block(text, "Attempt post-Codex syntax repair")
	assert 'CODEX_ISOLATED_ROOT="${CODEX_ISOLATED_IMPLEMENT_ROOT:-}"' in repair
	assert "CODEX_VERSION: ${{ vars.CODEX_VERSION || 'v0.114.0' }}" in text
	cleanup = step_block(text, "Cleanup temporary artifacts")
	assert 'cleanup --root "${CODEX_ISOLATED_IMPLEMENT_ROOT}"' in cleanup


def test_isolated_runs_carry_no_serena_hints():
	text = (WORKFLOWS / "implement.yml").read_text(encoding="utf-8")
	assert "Serena MCP is available in this run" not in text
	for script in ("validate_process.sh", "self_heal_validation.sh"):
		body = (SCRIPTS / script).read_text(encoding="utf-8")
		assert '[ -n "${CODEX_ISOLATED_EXEC:-}" ] || [ "${SERENA_AVAILABLE:-false}" != "true" ]' in body


@pytest.mark.parametrize(
	"workflow",
	["plan.yml", "implement.yml", "orchestrate.yml", "orchestrate_poll.yml", "check_failure_triage.yml", "validate.yml"],
)
def test_workflows_stage_the_isolation_support_files(workflow):
	text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
	names = (("clarify_isolated_run.sh", "clarify_sandbox/Dockerfile", "clarify_openrouter_broker.py")
		if workflow == "check_failure_triage.yml" else
		("codex_isolated_exec.sh", "codex_isolated_workspace.py", "clarify_openrouter_broker.py"))
	for name in names:
		assert name in text, f"{workflow} must stage {name}"


@pytest.mark.parametrize(
	"workflow",
	[
		"plan.yml", "implement.yml", "orchestrate.yml", "orchestrate_poll.yml", "check_failure_triage.yml",
		"validate.yml", "workflow-log-analysis.yml", "validation-refresh.yml", "security-audit.yml",
		"workflow-failure-heal-intake.yml", "issue_pr_status.yml",
	],
)
def test_workflows_pin_the_sandbox_codex_version(workflow):
	text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
	assert "CODEX_VERSION: ${{ vars.CODEX_VERSION || 'v0.114.0' }}" in text


def test_poller_file_editing_judges_use_worktrees_and_trusted_push():
	text = (SCRIPTS / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	assert 'ORCH_SCRIPTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")"' in text
	assert 'git checkout -B "${FOLLOWUP_BRANCH}"' not in text
	assert 'git checkout -B "${HEAD_REF}"' not in text
	assert 'RB_COMBINED_WORKDIR="${RUNTIME_DIR:-/tmp}/rb-judge-wt-${rb_issue}"' in text
	assert 'pushd "${RB_COMBINED_WORKDIR}" >/dev/null' in text
	assert 'POLLER_JUDGE_ENGINE_LABELS="${RB_JUDGE_ENGINE_LABELS_JSON}" poller_claude_judge RB_JUDGE' in text
	assert 'git -C "${RB_COMBINED_WORKDIR}" remote set-url origin "https://x-access-token:${GH_TOKEN}' not in text
	assert 'push "https://github.com/${GITHUB_REPOSITORY}" "HEAD:${HEAD_REF}"' in text
	assert 'push "https://github.com/${GITHUB_REPOSITORY}" "HEAD:${FOLLOWUP_BRANCH}"' in text
	assert text.count("-c 'credential.helper=!f()") == 3
	# Integration judge diagnoses in a read-only sandbox; only the clean
	# merge path reaches the trusted scope check and push.
	assert 'poller_claude_judge INTEGRATION_JUDGE' in text
	assert text.index('_integration_judge_capture_baseline "${judge_wt}"') < text.index('poller_claude_judge INTEGRATION_JUDGE')
	assert "fetch both branches" not in text
	assert 'the existing review workflow performs the isolated resolution' in text
	assert '_integration_judge_commit_and_push "${judge_wt}"' in text
	assert 'python3 "${ORCH_FINGERPRINT_VERIFIER}" "${fp_file}"' in text
	assert '_integration_judge_commit_and_push "${judge_wt}" "${final_pr}" "${integration_branch}" "${default_branch}" "${baseline_dir}" "${expected_conflict_count}" || {' in text
	commit_block = text[text.index('_integration_judge_commit_and_push() {'):text.index('# _refresh_integration_resolver_tooling')]
	assert commit_block.index('_integration_judge_verify_scope "${wt}" "${baseline_dir}"') < commit_block.index('commit --no-verify')
	assert "rev-parse 'HEAD^{tree}'" in commit_block
	assert 'return 0\n}\n\n# _integration_judge_remove_worktree' in text
	assert 'git -C "${wt}" rev-parse -q --verify MERGE_HEAD' in text
	assert 'if ! jq -ce' in text
	assert '"$(wc -c < "${fp_file}" 2>/dev/null || echo 0)" -gt 3' in text
	assert 'git -C "${wt}" add -A -- .' in text
	assert 'git -C "${wt}" remote set-url origin "https://x-access-token:${GH_TOKEN}' not in text
	assert 'push "https://github.com/${GITHUB_REPOSITORY}" "HEAD:refs/heads/${integration_branch}"' in text


def test_review_blocked_fix_writer_runs_in_the_review_sandbox():
	text = (SCRIPTS / "review_rb_judge.sh").read_text(encoding="utf-8")
	assert 'WORKSPACE_PATH="${rb_fix_workspace_path}" review_rb_opencode_sandbox_prepare' in text
	assert 'GITHUB_WORKSPACE="${RB_OPENCODE_WORKSPACE}"' not in text
	assert '"GITHUB_WORKSPACE=${RB_OPENCODE_WORKSPACE}"' not in text
	assert 'rb_fix_workspace_path=""' in text
	assert 'rb_fix_workspace_path="${RB_OPENCODE_WORKSPACE}"' in text
	assert 'workspace="$(< "${root}/workspace")"' in (SCRIPTS / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/review_untrusted_sandbox.sh" run' in text
	fix_block = text[text.index("rb_fix_opencode_cmd=("):]
	fix_block = fix_block[: fix_block.index(")\n")]
	assert "opencode_run_cmd" not in fix_block


def test_review_sandbox_admits_the_merge_guard_for_ci_repairs():
	import importlib.util

	spec = importlib.util.spec_from_file_location("review_untrusted_workspace", SCRIPTS / "review_untrusted_workspace.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	# The host-executed merge guard must not become editor-controlled output.
	assert not module.allowed(".claude/hooks/pr_merge_status_guard.py")
	assert not module.allowed(".claude/hooks/unrelated.py")


# --- the Claude engine (CLAUDE.md answer Q16 A) ---------------------------------------

# Files allowed to start the Claude Code CLI directly: the container
# entrypoints, where it runs inside the isolated container, and the token
# step's usage probe, which sends the fixed prompt "Reply OK" (no untrusted
# text) from an empty directory with GH_TOKEN / GITHUB_TOKEN unset.
CLAUDE_CONTAINER_ENTRYPOINTS = {"codex_isolated_exec.sh", "clarify_isolated_run.sh", "review_untrusted_sandbox.sh"}
CLAUDE_FIXED_PROMPT_PROBES = {"claude_pool_token.sh"}
RAW_CLAUDE = re.compile(r'''(?:^|[\s;&|(=]|--\s)claude\s+(?:-p\b|--print\b|"\$@")''')


def test_no_claude_cli_is_started_outside_the_isolation_helpers():
	offenders = {}
	paths = list(SCRIPTS.glob("*.sh")) + list(WORKFLOWS.glob("*.yml")) + list((REPO_ROOT / ".github" / "actions").rglob("*.y*ml"))
	for path in sorted(paths):
		if path.name in CLAUDE_CONTAINER_ENTRYPOINTS | CLAUDE_FIXED_PROMPT_PROBES:
			continue
		sites = [line.strip() for line in logical_lines(path.read_text(encoding="utf-8")) if RAW_CLAUDE.search(line)]
		if sites:
			offenders[str(path.relative_to(REPO_ROOT))] = sites
	assert not offenders, (
		"The Claude Code CLI must run through scripts/codex_isolated_exec.sh --engine claude "
		f"(or another container entrypoint); found direct launches: {offenders}"
	)


def test_claude_run_launches_through_the_isolated_helper_without_the_token():
	text = (SCRIPTS / "ai_engine.sh").read_text(encoding="utf-8")
	body = text[text.index("claude_run()"):]
	assert 'local isolated_exec="${_AI_ENGINE_DIR}/codex_isolated_exec.sh"' in body
	assert 'cmd=(bash "${isolated_exec}" "${isolation_args[@]}" --claude-token-file "${token_file}" --' in body
	assert "run --engine claude --mode" in body
	assert 'isolation_mode="read-only"' in body and 'isolation_mode="workspace"' in body
	assert "--hide-claude-md" in body and "--claude-home" in body
	assert "--guard-hook /support/guard.py" in body
	# The token is never read into the job's shell or the CLI's environment.
	assert "CLAUDE_CODE_OAUTH_TOKEN=" not in body
	assert "export CLAUDE_CODE_OAUTH_TOKEN" not in body
	assert 'cat -- "${token_file}"' not in body and '< "${token_file}"' not in body
	assert "ai_engine_fallback \"${role}\" isolation_unavailable" in body


def test_helper_claude_branch_keeps_the_token_on_the_host():
	text = HELPER.read_text(encoding="utf-8")
	assert '"${support_dir}/claude_anthropic_relay.py" broker "${root}/socket/provider.sock" "${claude_token_file}" "${claude_models}"' in text
	assert "--env CLAUDE_CODE_OAUTH_TOKEN=isolated-placeholder" in text
	assert "--env ANTHROPIC_BASE_URL=http://127.0.0.1:8765" in text
	for line in text.splitlines():
		if "--mount" in line:
			assert "claude_token_file" not in line, line
	assert 'export CODEX_ISOLATED_HIDE="CLAUDE.md"' in text
