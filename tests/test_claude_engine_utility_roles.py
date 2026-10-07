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
		if not key.startswith(("AI_ENGINE", "SUPPORT_", "CLAUDE_FIXER", "GITHUB_WORKSPACE"))
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
