#!/usr/bin/env python3
"""Contract: a phase workflow runs its support scripts from one verified source.

docs/plans/remove-unattended-pipeline-stuck-states-plan.md, phase P2a (root
cause 2). The "Stage workflow support files" step of a phase workflow used to
copy a hand-kept list of scripts from `.codex-workflow-src` (SCRIPT_REF) into
the issue checkout's scripts/; every helper missing from the list ran from the
target branch. Heal issues target `stable`, so main's YAML ran stable's
helpers, and every heal plan crashed with
`codex_stall_guard.sh: unknown option: --engine` (run 37406961564).

The step now runs scripts/stage_support_for_phase.sh, which stages a verified
bundle under RUNNER_TEMP (SUPPORT_SCRIPTS_DIR), and every later call uses
"${SUPPORT_SCRIPTS_DIR:-scripts}/<helper>". The legacy copy loop survives only
between the `support-scripts-legacy` markers, behind the kill switch
SUPPORT_SCRIPTS_UNIFIED_ENABLED.

PHASE_WORKFLOWS is parametrized so each phase workflow adds one entry: clarify
(P2a-1), plan (P2a-2), orchestrate_clarify_respond.yml (P2a-3) and
implement.yml (P2b). PHASE_EXPECTATIONS holds the per-phase differences (host
Python directory, checkout sandbox Dockerfile, required call sites).

implement.yml (P2b) also fills its trusted run dir
(IMPLEMENT_STAGED_SUPPORT_RUN_DIR) from the bundle instead of the workspace,
so a self-repo integration branch's tracked helpers are never overwritten.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "stage_support_for_phase.sh"
TG_HELPERS = REPO_ROOT / "scripts" / "tg_helpers.sh"
LEGACY_BEGIN = "# support-scripts-legacy:begin"
LEGACY_END = "# support-scripts-legacy:end"
KILL_SWITCH_BINDING = "SUPPORT_SCRIPTS_UNIFIED_ENABLED: ${{ vars.SUPPORT_SCRIPTS_UNIFIED_ENABLED || 'true' }}"
SUPPORT_PATH = "${SUPPORT_SCRIPTS_DIR:-scripts}/"

# (workflow, staging step name, stage_support_for_phase.sh phase)
PHASE_WORKFLOWS = [
	pytest.param(".github/workflows/clarify.yml", "Stage workflow support files", "clarify", id="clarify"),
	pytest.param(".github/workflows/plan.yml", "Stage workflow support files", "plan", id="plan"),
	pytest.param(
		".github/workflows/orchestrate_clarify_respond.yml", "Stage workflow support files", "clarify_respond",
		id="clarify_respond",
	),
	pytest.param(".github/workflows/implement.yml", "Stage workflow support files", "implement", id="implement"),
]

# Per-phase differences of the staging step.
#   host_python        the phase passes --host-python-dir (CLARIFY_HOST_PYTHON_DIR)
#   checkout_sandbox   unified mode still installs scripts/clarify_sandbox/Dockerfile
#                      into the checkout (clarify's preseed step reads it there)
#   unified_checkout   entries the unified step leaves in the checkout's scripts/
#   call_sites         "${SUPPORT_SCRIPTS_DIR:-scripts}/..." calls the workflow must hold
#   dropped_script     a required script the fail-closed check deletes
#   list_probe         the step probes `stage_support_for_phase.sh <phase> --list`
#   isolation_dir      the helper exports CLARIFY_ISOLATION_SUPPORT_DIR (default True)
#   legacy_sandbox     the legacy branch installs scripts/clarify_sandbox/Dockerfile (default True)
#   runtime_host_python  the step passes --host-python-dir "${RUNTIME_DIR}/host_python"
#   bare_exempt_steps  self-contained steps that install a helper from the support
#                      checkout and source that copy (not a target-branch helper)
PHASE_EXPECTATIONS = {
	"clarify": {
		"host_python": True,
		"checkout_sandbox": True,
		"unified_checkout": {"clarify_sandbox", ".gitignore"},
		"call_sites": (f'source "{SUPPORT_PATH}ai_engine.sh"', f'bash "{SUPPORT_PATH}clarify_isolated_run.sh"'),
		"dropped_script": "auto_decisions.py",
		"list_probe": False,
	},
	"plan": {
		"host_python": False,
		"checkout_sandbox": False,
		"unified_checkout": {".gitignore"},
		"call_sites": (f'source "{SUPPORT_PATH}ai_engine.sh"', f'bash "{SUPPORT_PATH}run_plan_codex.sh"'),
		"dropped_script": "plan_split_dispatch.sh",
		"list_probe": True,
	},
	"clarify_respond": {
		"host_python": False,
		"checkout_sandbox": False,
		"unified_checkout": {".gitignore"},
		"call_sites": (
			f'source "{SUPPORT_PATH}ai_engine.sh"',
			f'bash "{SUPPORT_PATH}clarify_isolated_run.sh"',
			f'bash "{SUPPORT_PATH}orchestrate_parse_and_post_answer.sh"',
		),
		"dropped_script": "clarify_data_provision_guard.py",
		"list_probe": True,
	},
	"implement": {
		"host_python": False,
		"runtime_host_python": True,
		"checkout_sandbox": False,
		"legacy_sandbox": False,
		"isolation_dir": False,
		"unified_checkout": {".gitignore"},
		"call_sites": (
			f'source "{SUPPORT_PATH}ai_engine.sh"',
			f'bash "{SUPPORT_PATH}implement_heal_preflight.sh"',
			f'bash "{SUPPORT_PATH}build_static_context.sh"',
		),
		"dropped_script": "implement_commit_changes.sh",
		"list_probe": True,
		"bare_exempt_steps": ("Telegram duplicate PR notification",),
	},
}

# Scripts that resolve their sibling helpers from their own directory (plan D5).
OWN_DIR_SCRIPTS = ("run_plan_codex.sh", "plan_split_dispatch.sh", "implement_heal_preflight.sh")

# Prompt and instruction assets implement.yml's staging step installs into the
# checkout (prompts stay in the checkout, plan D3).
IMPLEMENT_PROMPT_ASSETS = (
	"prompts/mode-implement-diagnose.txt", "prompts/mode-implement-repair.txt",
	"prompts/mode-implement-repair-continuation.txt", "prompts/mode-implement-diagnose-continuation.txt",
	"prompts/mode-validate-self-heal-continuation.txt", "prompts/contracts/mode-implement-repair-continuation.yml",
	"prompts/contracts/mode-implement-diagnose-continuation.yml",
	"prompts/contracts/mode-validate-self-heal-continuation.yml", "prompts/_prelude_serena.txt",
	"prompts/_templates/mode-implement-diagnose.txt", "prompts/_templates/mode-implement-diagnose-continuation.txt",
	"prompts/_templates/mode-implement-repair.txt", "prompts/_templates/mode-implement-repair-continuation.txt",
	"prompts/_templates/mode-validate-self-heal-continuation.txt",
)

# Real support files the execution fixtures copy; every other name the phase
# stages is a stub.
REAL_SUPPORT_FILES = ("stage_support_for_phase.sh", "ai_engine.sh", "codex_stall_guard.sh", "load_workflow_overlay.py")

# Stands in for the review bundle stager (plan D6): same SHA gate and the same
# GITHUB_ENV exports, no network, and the cwd write the real one makes.
STUB_STAGER = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ ! "${SCRIPT_REF:-}" =~ ^[0-9a-f]{40}$ ]] || [ "$(git -C .codex-workflow-src rev-parse HEAD 2>/dev/null)" != "${SCRIPT_REF}" ]; then
	echo "::error::Unverified workflow support checkout." >&2
	exit 1
fi
root="${RUNNER_TEMP}/coding-workflows-runtime-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
mkdir -p "${root}/scripts" "${root}/prompts" "${root}/ai-memory/schemas"
install -m 0755 .codex-workflow-src/scripts/ai_engine.sh "${root}/scripts/ai_engine.sh"
echo "stager cwd write" > probably_unnecessary_but_read_if_stuck.md
{
	echo "SUPPORT_ROOT_DIR=${root}"
	echo "SUPPORT_SCRIPTS_DIR=${root}/scripts"
	echo "SUPPORT_PROMPTS_DIR=${root}/prompts"
} >> "${GITHUB_ENV}"
"""

# The target branch's guard: older than --engine, as on `stable` in run 37406961564.
OLD_STALL_GUARD = r"""#!/usr/bin/env bash
for arg in "$@"; do
	if [ "${arg}" = "--engine" ]; then
		touch "${OLD_GUARD_MARKER:?}"
		echo "codex_stall_guard.sh: unknown option: --engine" >&2
		exit 2
	fi
done
exit 0
"""

STRIPPED_ENV = (
	"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
	"GIT_ALTERNATE_OBJECT_DIRECTORIES", "SUPPORT_SCRIPTS_DIR", "SUPPORT_ROOT_DIR", "SUPPORT_PROMPTS_DIR",
	"SUPPORT_SOURCE_DIR", "CLARIFY_ISOLATION_SUPPORT_DIR", "SCRIPT_REF", "GITHUB_ENV", "BASH_ENV", "ENV",
	"SUPPORT_SCRIPTS_UNIFIED_ENABLED", "_TG_HELPERS_LOADED",
)


# --- workflow text helpers ----------------------------------------------------


def _workflow_text(workflow: str) -> str:
	return (REPO_ROOT / workflow).read_text(encoding="utf-8")


def _step_lines(text: str, step_name: str) -> list[str]:
	lines = text.splitlines()
	starts = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*- name: " + re.escape(step_name), line)]
	assert starts, f"no step named {step_name!r}"
	start = starts[0]
	indent = len(lines[start]) - len(lines[start].lstrip())
	end = start + 1
	while end < len(lines):
		line = lines[end]
		stripped = line.lstrip()
		if stripped and len(line) - len(stripped) <= indent:
			break
		end += 1
	return lines[start:end]


def _legacy_region(lines: list[str]) -> tuple[int, int]:
	begins = [i for i, line in enumerate(lines) if line.strip() == LEGACY_BEGIN]
	ends = [i for i, line in enumerate(lines) if line.strip() == LEGACY_END]
	assert len(begins) == 1 and len(ends) == 1, "expected exactly one support-scripts-legacy region"
	assert begins[0] < ends[0]
	return begins[0], ends[0]


def _outside_legacy_code(text: str) -> list[str]:
	"""Non-comment lines outside the kill-switch region."""
	lines = text.splitlines()
	begin, end = _legacy_region(lines)
	kept = lines[:begin] + lines[end + 1:]
	return [line for line in kept if not line.strip().startswith("#")]


def _legacy_helpers(text: str) -> list[str]:
	lines = text.splitlines()
	begin, end = _legacy_region(lines)
	for line in lines[begin:end]:
		match = re.fullmatch(r"\s*for f in (gh_helpers\.sh [^;]+); do", line)
		if match:
			return match.group(1).split()
	raise AssertionError("the legacy region no longer holds the `for f in gh_helpers.sh ...` loop")


def _helper_phase_list(phase: str, variable: str) -> list[str]:
	text = HELPER.read_text(encoding="utf-8")
	block = re.search(r"^\t" + re.escape(phase) + r"\)\n(.*?)^\t\t;;", text, re.M | re.S)
	assert block, f"stage_support_for_phase.sh has no {phase} case"
	match = re.search(re.escape(variable) + r"=\((.*?)\)", block.group(1), re.S)
	assert match, f"{phase} case defines no {variable}"
	return match.group(1).split()


# --- static contract ------------------------------------------------------------


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_staging_step_binds_kill_switch(workflow: str, step_name: str, phase: str) -> None:
	step = _step_lines(_workflow_text(workflow), step_name)
	body = "\n".join(step)
	assert KILL_SWITCH_BINDING in body
	assert 'support_unified="${SUPPORT_SCRIPTS_UNIFIED_ENABLED:-true}"' in body
	assert re.search(r"bash \.codex-workflow-src/scripts/stage_support_for_phase\.sh " + re.escape(phase) + r"(?: |$)", body, re.M)
	begin, end = _legacy_region(step)
	assert step[begin - 1].strip() == "else", "the legacy region must be the kill-switch else branch"
	assert step[end + 1].strip() == "fi"
	call = next(i for i, line in enumerate(step) if "stage_support_for_phase.sh " + phase in line and "bash " in line)
	assert step[call - 1].strip() == 'if [ "${support_unified}" = "true" ]; then'


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_legacy_copy_loop_only_inside_kill_switch_region(workflow: str, step_name: str, phase: str) -> None:
	text = _workflow_text(workflow)
	lines = text.splitlines()
	begin, end = _legacy_region(lines)
	region = "\n".join(lines[begin:end])
	assert re.search(r"for f in gh_helpers\.sh [^\n]*; do", region)
	assert 'install -m 0755 "${src}" "scripts/${f}"' in region
	outside = "\n".join(_outside_legacy_code(text))
	copy_loop = re.compile(r"for f in [^\n]*; do\n(?:(?!\n\s*done\b).)*?\binstall\b[^\n]*\"?scripts/\$\{f\}", re.S)
	assert not copy_loop.search(outside), "a scripts/ copy loop runs outside the kill-switch region"


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_no_bare_support_helper_calls(workflow: str, step_name: str, phase: str) -> None:
	text = _workflow_text(workflow)
	helpers = _legacy_helpers(text)
	names = "|".join(re.escape(name) for name in sorted(helpers, key=len, reverse=True))
	bare = re.compile(
		r"(?:(?<![\w/}.-])(?:bash -n|bash|source|python3(?: -I -B)?|-[fL])\s+|(?<!\S)\.\s+|\bgrep\b[^\n|;]*?\s|\")"
		r"scripts/(" + names + r")(?![\w.])"
	)
	# Exempt by position: an identical bare call in another step still counts.
	lines = text.splitlines()
	for exempt_step in PHASE_EXPECTATIONS[phase].get("bare_exempt_steps", ()):
		block = _step_lines(text, exempt_step)
		start = lines.index(block[0])
		lines[start:start + len(block)] = [""] * len(block)
	offenders = [line.strip() for line in _outside_legacy_code("\n".join(lines)) if bare.search(line)]
	assert not offenders, f"bare scripts/<helper> calls outside the kill-switch region: {offenders}"
	for call_site in PHASE_EXPECTATIONS[phase]["call_sites"]:
		assert call_site in text, call_site


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_helper_phase_list_matches_legacy_loop(workflow: str, step_name: str, phase: str) -> None:
	"""The bundle and the kill-switch loop stage the same scripts."""
	assert _helper_phase_list(phase, "stage_support_required_scripts") == _legacy_helpers(_workflow_text(workflow))


def test_helper_parses_and_is_executable() -> None:
	assert HELPER.is_file() and not HELPER.is_symlink()
	assert HELPER.stat().st_mode & 0o111
	result = subprocess.run(["bash", "-n", str(HELPER)], capture_output=True, text=True)
	assert result.returncode == 0, result.stderr


# --- execution fixtures -----------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
		cwd=cwd, env=_base_env(cwd), capture_output=True, text=True, check=True,
	).stdout.strip()


def _base_env(home: Path) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
	env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONDONTWRITEBYTECODE": "1"})
	return env


def _write(path: Path, text: str, mode: int = 0o644) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")
	path.chmod(mode)


def _make_support(workspace: Path, phase: str, workflow: str) -> tuple[Path, str]:
	"""A support checkout at <workspace>/.codex-workflow-src; returns (dir, HEAD)."""
	support = workspace / ".codex-workflow-src"
	scripts = support / "scripts"
	names = set(_helper_phase_list(phase, "stage_support_required_scripts")) | {"render_prompt.py"}
	for name in sorted(names):
		_write(scripts / name, "#!/usr/bin/env bash\nexit 0\n", 0o755)
	for name in REAL_SUPPORT_FILES:
		shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
	_write(scripts / "stage_workflow_support.sh", STUB_STAGER, 0o755)
	_write(scripts / "emit_event.sh", "# emit_event stub\n", 0o755)
	_write(scripts / "codex_model_catalog.json", "{}\n")
	_write(scripts / "clarify_sandbox" / "Dockerfile", "FROM node:22-bookworm-slim\n")
	if "claude_settings.json.tmpl" not in names:
		_write(scripts / "claude_settings.json.tmpl", "{}\n")
	for rel in (
		"unattended_system_instructions.md", "ai_pipeline.md", "prompts/header.txt", "prompts/_identity_recall.txt",
		"prompts/references/security-money-lens.txt", "prompts/mode-clarify-respond.txt",
		"prompts/_prelude_common.txt", "prompts/_prelude_output_contract.txt", "prompts/_prelude_role_persona.txt",
		"prompts/_templates/mode-clarify-respond.txt",
		*IMPLEMENT_PROMPT_ASSETS,
	):
		_write(support / rel, f"{rel}\n")
	schema = "ai-memory/schemas/workflow_overlay.v1.json"
	(support / schema).parent.mkdir(parents=True, exist_ok=True)
	shutil.copy2(REPO_ROOT / schema, support / schema)
	_git(support, "init", "-q")
	_git(support, "add", "-A")
	_git(support, "commit", "-q", "-m", "support")
	return support, _git(support, "rev-parse", "HEAD")


def _make_target(root: Path) -> Path:
	"""The issue checkout: a target branch whose guard predates --engine."""
	target = root / "target"
	_write(target / "README.md", "target\n")
	_write(target / "scripts" / "codex_stall_guard.sh", OLD_STALL_GUARD, 0o755)
	shutil.copy2(REPO_ROOT / "scripts" / "ai_engine.sh", target / "scripts" / "ai_engine.sh")
	_git(target, "init", "-q")
	_git(target, "add", "-A")
	_git(target, "commit", "-q", "-m", "target")
	return target


def _runner_env(root: Path, workspace: Path, script_ref: str) -> dict[str, str]:
	env = _base_env(root)
	runner_temp = root / "runner"
	runner_temp.mkdir(exist_ok=True)
	github_env = root / "github_env"
	github_env.touch()
	runtime_dir = root / "runtime"
	runtime_dir.mkdir(exist_ok=True)
	env.update(
		{
			"RUNTIME_DIR": str(runtime_dir),
			"HOME": str(root),
			"GITHUB_WORKSPACE": str(workspace),
			"GITHUB_REPOSITORY": "owner/consumer",
			"RUNNER_TEMP": str(runner_temp),
			"GITHUB_RUN_ID": "101",
			"GITHUB_RUN_ATTEMPT": "1",
			"GITHUB_ENV": str(github_env),
			"SCRIPT_REF": script_ref,
		}
	)
	return env


def _github_env(env: dict[str, str]) -> dict[str, str]:
	values: dict[str, str] = {}
	for line in Path(env["GITHUB_ENV"]).read_text(encoding="utf-8").splitlines():
		key, sep, value = line.partition("=")
		if sep:
			values[key] = value
	return values


def _run_helper(workspace: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		["bash", str(workspace / ".codex-workflow-src" / "scripts" / "stage_support_for_phase.sh"), *args],
		cwd=workspace, env=env, capture_output=True, text=True, timeout=120,
	)


def _guard_call(target: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
	"""The rendered clarify call: source the engine, then run its stall guard."""
	return subprocess.run(
		[
			"bash", "-c",
			'source "${SUPPORT_SCRIPTS_DIR:-scripts}/ai_engine.sh"; '
			'bash "${_AI_ENGINE_DIR}/codex_stall_guard.sh" --phase clarify --engine claude -- true',
		],
		cwd=target, env=env, capture_output=True, text=True, timeout=120,
	)


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_support_copy_of_stall_guard_wins(tmp_path: Path, workflow: str, step_name: str, phase: str) -> None:
	target = _make_target(tmp_path)
	support, head = _make_support(target, phase, workflow)
	status_before = _git(target, "status", "--porcelain")
	env = _runner_env(tmp_path, target, head)
	host_python = tmp_path / "host-python"
	expect = PHASE_EXPECTATIONS[phase]

	host_args = ("--host-python-dir", str(host_python)) if expect["host_python"] or expect.get("runtime_host_python") else ()
	result = _run_helper(target, env, phase, *host_args)
	assert result.returncode == 0, result.stderr
	assert f"Support scripts staged phase={phase} sha={head} verify=exact" in result.stdout
	exported = _github_env(env)
	bundle = Path(exported["SUPPORT_SCRIPTS_DIR"])
	assert bundle == tmp_path / "runner" / "coding-workflows-runtime-101-1" / "scripts"
	if expect.get("isolation_dir", True):
		assert exported["CLARIFY_ISOLATION_SUPPORT_DIR"] == str(bundle)
		assert (bundle / "clarify_sandbox" / "Dockerfile").is_file()
	else:
		assert "CLARIFY_ISOLATION_SUPPORT_DIR" not in exported
	assert exported["SUPPORT_SCRIPTS_VERIFY"] == "exact"
	for name in _helper_phase_list(phase, "stage_support_required_scripts"):
		assert (bundle / name).is_file(), name
	assert (bundle / "codex_stall_guard.sh").read_bytes() == (REPO_ROOT / "scripts" / "codex_stall_guard.sh").read_bytes()
	assert (bundle / "emit_event.sh").is_file()
	for name in _helper_phase_list(phase, "stage_support_data_files"):
		assert (bundle / name).is_file(), name
	if expect["host_python"] or expect.get("runtime_host_python"):
		assert sorted(p.name for p in host_python.iterdir()) == sorted(
			_helper_phase_list(phase, "stage_support_host_python_scripts")
		)
	else:
		assert not host_python.exists()
	# Read-only with respect to the issue checkout: no executables, and the
	# stager's cwd write stays in its scratch directory.
	assert _git(target, "status", "--porcelain") == status_before
	assert not (target / "probably_unnecessary_but_read_if_stuck.md").exists()
	assert not list((tmp_path / "runner").glob("stage-support-phase-*")), "scratch directory left behind"

	marker = tmp_path / "old-guard-ran"
	guard_env = dict(
		env,
		SUPPORT_SCRIPTS_DIR=str(bundle),
		OLD_GUARD_MARKER=str(marker),
		CODEX_STALL_GUARD_ENABLED="false",
		CODEX_HEARTBEAT_INTERVAL_SECS="1",
		CODEX_STALL_HEARTBEAT_DIR=str(tmp_path / "hb"),
	)
	called = _guard_call(target, guard_env)
	assert called.returncode == 0, called.stderr
	assert "unknown option: --engine" not in called.stderr
	assert not marker.exists(), "the target branch's guard ran"

	# Negative control (kill switch): SUPPORT_SCRIPTS_DIR unset resolves the
	# checkout's copies, which is the reported crash.
	guard_env.pop("SUPPORT_SCRIPTS_DIR")
	legacy = _guard_call(target, guard_env)
	assert legacy.returncode == 2
	assert "codex_stall_guard.sh: unknown option: --engine" in legacy.stderr
	assert marker.exists()
	assert support.is_dir()


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_helper_rejects_mismatched_or_invalid_ref(tmp_path: Path, workflow: str, step_name: str, phase: str) -> None:
	workspace = tmp_path / "ws"
	workspace.mkdir()
	support, head = _make_support(workspace, phase, workflow)

	for bad_ref in ("0" * 40, "bad..ref", "$(touch pwned)", "-x", "/abs"):
		env = _runner_env(tmp_path, workspace, bad_ref)
		result = _run_helper(workspace, env, phase)
		assert result.returncode == 1, (bad_ref, result.stderr)
		assert "SUPPORT_SCRIPTS_DIR" not in _github_env(env)
	assert not (workspace / "pwned").exists()

	# A ref that resolves locally must be the checkout HEAD.
	_git(support, "tag", "stable")
	env = _runner_env(tmp_path, workspace, "stable")
	result = _run_helper(workspace, env, phase)
	assert result.returncode == 0, result.stderr
	assert f"sha={head} verify=ref" in result.stdout
	_write(support / "moved.txt", "moved\n")
	_git(support, "add", "-A")
	_git(support, "commit", "-q", "-m", "moved")
	result = _run_helper(workspace, _runner_env(tmp_path, workspace, "stable"), phase)
	assert result.returncode == 1
	assert "refs/tags/stable" in result.stderr

	# A ref absent from the checkout (the fallback to main) keeps its HEAD.
	head_only_env = _runner_env(tmp_path, workspace, "no-such-ref")
	result = _run_helper(workspace, head_only_env, phase)
	assert result.returncode == 0, result.stderr
	assert "verify=head_only" in result.stdout
	assert "does not resolve in the support checkout" in result.stdout
	assert _github_env(head_only_env)["SUPPORT_SCRIPTS_VERIFY"] == "head_only"

	head_env = _runner_env(tmp_path, workspace, _git(support, "rev-parse", "HEAD"))
	assert _run_helper(workspace, head_env, "unknown-phase").returncode == 2
	assert _run_helper(workspace, head_env, phase, "--host-python-dir", "relative/dir").returncode == 2
	assert _run_helper(workspace, head_env, phase, "--bogus").returncode == 2

	# A required script missing at the support commit fails closed.
	dropped = PHASE_EXPECTATIONS[phase]["dropped_script"]
	assert dropped in _helper_phase_list(phase, "stage_support_required_scripts")
	(support / "scripts" / dropped).unlink()
	_git(support, "add", "-A")
	_git(support, "commit", "-q", "-m", "drop")
	result = _run_helper(workspace, _runner_env(tmp_path, workspace, _git(support, "rev-parse", "HEAD")), phase)
	assert result.returncode == 1
	assert f"Missing required support script {dropped}" in result.stderr


def _staging_step_script(workflow: str, step_name: str) -> str:
	step = _step_lines(_workflow_text(workflow), step_name)
	run = next(i for i, line in enumerate(step) if line.strip() == "run: |")
	indent = len(step[run + 1]) - len(step[run + 1].lstrip())
	body = "\n".join(line[indent:] for line in step[run + 1:])
	body = body.replace("${{ github.repository }}", "owner/consumer")
	return re.sub(r"\$\{\{[^}]*\}\}", "false", body)


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
@pytest.mark.parametrize("unified", ["true", "false"])
def test_staging_step_runs_both_modes(tmp_path: Path, workflow: str, step_name: str, phase: str, unified: str) -> None:
	"""The real step body: the bundle when on, today's copy loop when off."""
	workspace = tmp_path / "ws"
	workspace.mkdir()
	_, head = _make_support(workspace, phase, workflow)
	env = _runner_env(tmp_path, workspace, head)
	env["SUPPORT_SCRIPTS_UNIFIED_ENABLED"] = unified
	script = tmp_path / "step.sh"
	script.write_text(_staging_step_script(workflow, step_name), encoding="utf-8")
	result = subprocess.run(
		["bash", "--noprofile", "--norc", "-e", str(script)],
		cwd=workspace, env=env, capture_output=True, text=True, timeout=180,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	exported = _github_env(env)
	helpers = _legacy_helpers(_workflow_text(workflow))
	staged = {p.name for p in (workspace / "scripts").iterdir()}
	expect = PHASE_EXPECTATIONS[phase]
	sandbox_in_checkout = (workspace / "scripts" / "clarify_sandbox" / "Dockerfile").is_file()
	assert sandbox_in_checkout == (expect["checkout_sandbox"] or (unified == "false" and expect.get("legacy_sandbox", True)))
	if expect["host_python"]:
		host_python = Path(exported["CLARIFY_HOST_PYTHON_DIR"])
		assert sorted(p.name for p in host_python.iterdir()) == sorted(
			_helper_phase_list(phase, "stage_support_host_python_scripts")
		)
	else:
		assert "CLARIFY_HOST_PYTHON_DIR" not in exported
	if expect.get("runtime_host_python"):
		assert (Path(env["RUNTIME_DIR"]) / "host_python" / "load_workflow_overlay.py").is_file()
	if unified == "true":
		assert staged == expect["unified_checkout"], staged
		bundle = Path(exported["SUPPORT_SCRIPTS_DIR"])
		assert all((bundle / name).is_file() for name in helpers)
		if expect.get("isolation_dir", True):
			assert exported["CLARIFY_ISOLATION_SUPPORT_DIR"] == str(bundle)
		else:
			assert "CLARIFY_ISOLATION_SUPPORT_DIR" not in exported
	else:
		assert set(helpers) <= staged
		assert "SUPPORT_SCRIPTS_DIR" not in exported
		assert "CLARIFY_ISOLATION_SUPPORT_DIR" not in exported


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_missing_helper_falls_back_to_legacy_staging(tmp_path: Path, workflow: str, step_name: str, phase: str) -> None:
	"""An older support ref without the helper keeps today's behaviour."""
	workspace = tmp_path / "ws"
	workspace.mkdir()
	support, _ = _make_support(workspace, phase, workflow)
	(support / "scripts" / "stage_support_for_phase.sh").unlink()
	_git(support, "add", "-A")
	_git(support, "commit", "-q", "-m", "old ref")
	env = _runner_env(tmp_path, workspace, _git(support, "rev-parse", "HEAD"))
	script = tmp_path / "step.sh"
	script.write_text(_staging_step_script(workflow, step_name), encoding="utf-8")
	result = subprocess.run(
		["bash", "--noprofile", "--norc", "-e", str(script)],
		cwd=workspace, env=env, capture_output=True, text=True, timeout=180,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "using the legacy support staging" in result.stdout
	assert "SUPPORT_SCRIPTS_DIR" not in _github_env(env)
	assert (workspace / "scripts" / "codex_stall_guard.sh").is_file()


def _run_staging_step(tmp_path: Path, workspace: Path, workflow: str, step_name: str, env: dict[str, str]) -> subprocess.CompletedProcess:
	script = tmp_path / "step.sh"
	script.write_text(_staging_step_script(workflow, step_name), encoding="utf-8")
	return subprocess.run(
		["bash", "--noprofile", "--norc", "-e", str(script)],
		cwd=workspace, env=env, capture_output=True, text=True, timeout=180,
	)


def _gitignore_names(workspace: Path) -> set[str]:
	text = (workspace / "scripts" / ".gitignore").read_text(encoding="utf-8")
	return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


@pytest.mark.parametrize(("workflow", "step_name", "phase"), PHASE_WORKFLOWS)
def test_helper_list_is_read_only(tmp_path: Path, workflow: str, step_name: str, phase: str) -> None:
	"""--list prints the phase's names without SCRIPT_REF or GITHUB_ENV and writes nothing."""
	cwd = tmp_path / "cwd"
	cwd.mkdir()
	env = _base_env(tmp_path)
	env["HOME"] = str(tmp_path)
	result = subprocess.run(["bash", str(HELPER), phase, "--list"], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
	assert result.returncode == 0, result.stderr
	expected = (
		_helper_phase_list(phase, "stage_support_required_scripts")
		+ _helper_phase_list(phase, "stage_support_optional_scripts")
		+ _helper_phase_list(phase, "stage_support_data_files")
	)
	assert result.stdout.splitlines() == expected
	assert not list(cwd.iterdir())
	unknown = subprocess.run(["bash", str(HELPER), "unknown-phase", "--list"], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
	assert unknown.returncode == 2
	mixed = subprocess.run(
		["bash", str(HELPER), phase, "--list", "--host-python-dir", str(tmp_path / "hp")],
		cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
	)
	assert mixed.returncode == 2
	assert not (tmp_path / "hp").exists()


LIST_PROBE_WORKFLOWS = [param for param in PHASE_WORKFLOWS if PHASE_EXPECTATIONS[param.values[2]]["list_probe"]]


@pytest.mark.parametrize(("workflow", "step_name", "phase"), LIST_PROBE_WORKFLOWS)
def test_unified_gitignore_keeps_legacy_manifest(tmp_path: Path, workflow: str, step_name: str, phase: str) -> None:
	"""The consumer scripts/.gitignore (template-owned path guard input) loses no name."""
	names: dict[str, set[str]] = {}
	for unified in ("false", "true"):
		root = tmp_path / unified
		workspace = root / "ws"
		workspace.mkdir(parents=True)
		_, head = _make_support(workspace, phase, workflow)
		env = _runner_env(root, workspace, head)
		env["SUPPORT_SCRIPTS_UNIFIED_ENABLED"] = unified
		result = _run_staging_step(root, workspace, workflow, step_name, env)
		assert result.returncode == 0, result.stdout + result.stderr
		names[unified] = _gitignore_names(workspace)
	assert names["false"] <= names["true"], names["false"] - names["true"]
	assert set(_legacy_helpers(_workflow_text(workflow))) <= names["true"]


@pytest.mark.parametrize(("workflow", "step_name", "phase"), LIST_PROBE_WORKFLOWS)
def test_helper_without_phase_case_falls_back(tmp_path: Path, workflow: str, step_name: str, phase: str) -> None:
	"""An older support ref whose helper only knows clarify degrades to the legacy staging."""
	workspace = tmp_path / "ws"
	workspace.mkdir()
	support, _ = _make_support(workspace, phase, workflow)
	helper = support / "scripts" / "stage_support_for_phase.sh"
	text = helper.read_text(encoding="utf-8")
	stripped = re.sub(r"^\t" + re.escape(phase) + r"\)\n.*?^\t\t;;\n", "", text, count=1, flags=re.M | re.S)
	assert stripped != text
	helper.write_text(stripped, encoding="utf-8")
	_git(support, "add", "-A")
	_git(support, "commit", "-q", "-m", "clarify-only helper")
	env = _runner_env(tmp_path, workspace, _git(support, "rev-parse", "HEAD"))
	result = _run_staging_step(tmp_path, workspace, workflow, step_name, env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"cannot stage the {phase} phase; using the legacy support staging" in result.stdout
	assert "SUPPORT_SCRIPTS_DIR" not in _github_env(env)
	assert set(_legacy_helpers(_workflow_text(workflow))) <= {p.name for p in (workspace / "scripts").iterdir()}


@pytest.mark.parametrize("name", OWN_DIR_SCRIPTS)
def test_own_dir_scripts_have_no_bare_helper_calls(name: str) -> None:
	text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
	bare = re.compile(r"(?:\b(?:bash|source|python3(?: -I -B)?)|-[fL])\s+\"?scripts/[\w.-]+\.(?:sh|py)\b")
	offenders = [line.strip() for line in text.splitlines() if not line.strip().startswith("#") and bare.search(line)]
	assert not offenders, offenders


def test_plan_split_dispatch_sources_its_own_directory(tmp_path: Path) -> None:
	"""Copied into a bundle, plan_split_dispatch.sh sources the bundle's gh_helpers.sh, not the cwd copy."""
	bundle = tmp_path / "bundle"
	cwd = tmp_path / "checkout"
	bundle.mkdir()
	shutil.copy2(REPO_ROOT / "scripts" / "plan_split_dispatch.sh", bundle / "plan_split_dispatch.sh")
	_write(bundle / "gh_helpers.sh", 'touch "${BUNDLE_MARKER:?}"\n')
	_write(cwd / "scripts" / "gh_helpers.sh", 'touch "${CWD_MARKER:?}"\n')
	env = _base_env(tmp_path)
	env.update({"BUNDLE_MARKER": str(tmp_path / "bundle-sourced"), "CWD_MARKER": str(tmp_path / "cwd-sourced")})
	for key in ("REPO", "ISSUE_NUMBER", "ISSUE_JSON_FILE", "GH_TOKEN"):
		env.pop(key, None)
	result = subprocess.run(
		["bash", str(bundle / "plan_split_dispatch.sh")], cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
	)
	# No REPO: a usage error right after the helpers are sourced.
	assert result.returncode == 1, result.stdout + result.stderr
	assert (tmp_path / "bundle-sourced").exists()
	assert not (tmp_path / "cwd-sourced").exists()


def test_tg_helpers_prefers_support_gh_helpers(tmp_path: Path) -> None:
	cwd = tmp_path / "checkout"
	support = tmp_path / "bundle"
	_write(cwd / "scripts" / "gh_helpers.sh", "curl_gh_api() { echo checkout; }\n")
	_write(support / "gh_helpers.sh", "curl_gh_api() { echo bundle; }\n")
	env = _base_env(tmp_path)
	script = f'source {TG_HELPERS}; curl_gh_api'
	with_bundle = subprocess.run(
		["bash", "-c", script], cwd=cwd, env=dict(env, SUPPORT_SCRIPTS_DIR=str(support)),
		capture_output=True, text=True, timeout=30,
	)
	assert with_bundle.stdout.strip() == "bundle", with_bundle.stderr
	legacy = subprocess.run(["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
	assert legacy.stdout.strip() == "checkout", legacy.stderr
	# A bundle without gh_helpers.sh keeps the cwd-relative lookup.
	empty = tmp_path / "empty"
	empty.mkdir()
	fallback = subprocess.run(
		["bash", "-c", script], cwd=cwd, env=dict(env, SUPPORT_SCRIPTS_DIR=str(empty)),
		capture_output=True, text=True, timeout=30,
	)
	assert fallback.stdout.strip() == "checkout", fallback.stderr


IMPLEMENT_WORKFLOW = ".github/workflows/implement.yml"
IMPLEMENT_STEP = "Stage workflow support files"


def _implement_workspace(tmp_path: Path, repository: str, unified: str = "true") -> tuple[Path, Path, dict[str, str]]:
	"""A git workspace with the implement support checkout inside it."""
	workspace = tmp_path / "ws"
	workspace.mkdir()
	support, head = _make_support(workspace, "implement", IMPLEMENT_WORKFLOW)
	_git(workspace, "init", "-q")
	env = _runner_env(tmp_path, workspace, head)
	env["GITHUB_REPOSITORY"] = repository
	env["SUPPORT_SCRIPTS_UNIFIED_ENABLED"] = unified
	return workspace, support, env


def test_implement_run_dir_is_filled_from_bundle(tmp_path: Path) -> None:
	"""Unified: the trusted run dir copies the bundle, never a checkout helper."""
	workspace, _, env = _implement_workspace(tmp_path, "owner/consumer")
	_write(workspace / "scripts" / "gh_helpers.sh", "# consumer decoy\n", 0o755)
	result = _run_staging_step(tmp_path, workspace, IMPLEMENT_WORKFLOW, IMPLEMENT_STEP, env)
	assert result.returncode == 0, result.stdout + result.stderr
	exported = _github_env(env)
	bundle = Path(exported["SUPPORT_SCRIPTS_DIR"])
	run_dir = Path(exported["IMPLEMENT_STAGED_SUPPORT_RUN_DIR"])
	assert run_dir == Path(env["RUNTIME_DIR"]) / "staged_support_run" / "scripts"
	for name in _legacy_helpers(_workflow_text(IMPLEMENT_WORKFLOW)):
		assert (run_dir / name).read_bytes() == (bundle / name).read_bytes(), name
	runtime = Path(env["RUNTIME_DIR"])
	for name in ("implement_handle_guard_block.sh", "gh_helpers.sh"):
		assert (runtime / name).read_bytes() == (bundle / name).read_bytes(), name
	assert (workspace / "scripts" / "gh_helpers.sh").read_text(encoding="utf-8") == "# consumer decoy\n"
	assert {p.name for p in (workspace / "scripts").iterdir()} == {"gh_helpers.sh", ".gitignore"}


@pytest.mark.parametrize("unified", ["true", "false"])
def test_implement_self_repo_keeps_tracked_helpers(tmp_path: Path, unified: str) -> None:
	"""Self-repo: unified staging never writes a tracked helper; the kill switch still ledgers it."""
	workspace, support, env = _implement_workspace(tmp_path, "shubhodeep1/coding-workflows", unified)
	branch_copy = "# integration branch gh_helpers.sh\n"
	_write(workspace / "scripts" / "gh_helpers.sh", branch_copy, 0o755)
	shutil.copytree(support / "prompts", workspace / "prompts")
	shutil.copytree(support / "ai-memory", workspace / "ai-memory")
	for name in ("unattended_system_instructions.md", "ai_pipeline.md"):
		shutil.copy2(support / name, workspace / name)
	_git(workspace, "add", "--", "scripts", "prompts", "ai-memory", "unattended_system_instructions.md", "ai_pipeline.md")
	_git(workspace, "commit", "-q", "-m", "branch")
	result = _run_staging_step(tmp_path, workspace, IMPLEMENT_WORKFLOW, IMPLEMENT_STEP, env)
	assert result.returncode == 0, result.stdout + result.stderr
	ledger = Path(_github_env(env)["STAGED_SUPPORT_LEDGER"])
	if unified == "true":
		assert (workspace / "scripts" / "gh_helpers.sh").read_text(encoding="utf-8") == branch_copy
		assert _git(workspace, "status", "--porcelain", "--", "scripts") == ""
		assert ledger.read_text(encoding="utf-8") == ""
	else:
		assert "scripts/gh_helpers.sh" in ledger.read_text(encoding="utf-8").splitlines()


def test_implement_unified_keeps_caller_serena_template(tmp_path: Path) -> None:
	workspace, _, env = _implement_workspace(tmp_path, "owner/consumer")
	_write(workspace / "scripts" / "templates" / "serena_project.yml.j2", "caller template\n")
	_git(workspace, "add", "--", "scripts")
	_git(workspace, "commit", "-q", "-m", "caller template")
	result = _run_staging_step(tmp_path, workspace, IMPLEMENT_WORKFLOW, IMPLEMENT_STEP, env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "preserving caller-owned Serena template" in result.stdout
	bundle = Path(_github_env(env)["SUPPORT_SCRIPTS_DIR"])
	assert (bundle / "templates" / "serena_project.yml.j2").read_text(encoding="utf-8") == "caller template\n"


def test_implement_heal_preflight_resolves_heal_py_from_own_dir() -> None:
	text = (REPO_ROOT / "scripts" / "implement_heal_preflight.sh").read_text(encoding="utf-8")
	assert "WORKFLOW_HEAL_PY:-scripts/" not in text
	assert 'heal_py="${WORKFLOW_HEAL_PY:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/workflow_failure_heal.py}"' in text


def test_ci_runs_this_file() -> None:
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "python3 -m pytest -q -p no:cacheprovider tests/test_support_script_single_source.py" in ci
