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

PHASE_WORKFLOWS is parametrized so plan.yml, orchestrate_clarify_respond.yml
and implement.yml (later P2a/P2b issues) each add one entry.
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
]

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
	assert f"bash .codex-workflow-src/scripts/stage_support_for_phase.sh {phase} " in body
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
	offenders = [line.strip() for line in _outside_legacy_code(text) if bare.search(line)]
	assert not offenders, f"bare scripts/<helper> calls outside the kill-switch region: {offenders}"
	assert f'source "{SUPPORT_PATH}ai_engine.sh"' in text
	assert f'bash "{SUPPORT_PATH}clarify_isolated_run.sh"' in text


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
	for rel in ("unattended_system_instructions.md", "ai_pipeline.md", "prompts/header.txt", "prompts/_identity_recall.txt"):
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
	env.update(
		{
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

	result = _run_helper(target, env, phase, "--host-python-dir", str(host_python))
	assert result.returncode == 0, result.stderr
	assert f"Support scripts staged phase={phase} sha={head} verify=exact" in result.stdout
	exported = _github_env(env)
	bundle = Path(exported["SUPPORT_SCRIPTS_DIR"])
	assert bundle == tmp_path / "runner" / "coding-workflows-runtime-101-1" / "scripts"
	assert exported["CLARIFY_ISOLATION_SUPPORT_DIR"] == str(bundle)
	assert exported["SUPPORT_SCRIPTS_VERIFY"] == "exact"
	for name in _helper_phase_list(phase, "stage_support_required_scripts"):
		assert (bundle / name).is_file(), name
	assert (bundle / "codex_stall_guard.sh").read_bytes() == (REPO_ROOT / "scripts" / "codex_stall_guard.sh").read_bytes()
	assert (bundle / "clarify_sandbox" / "Dockerfile").is_file()
	assert (bundle / "emit_event.sh").is_file()
	assert sorted(p.name for p in host_python.iterdir()) == sorted(
		_helper_phase_list(phase, "stage_support_host_python_scripts")
	)
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
	(support / "scripts" / "auto_decisions.py").unlink()
	_git(support, "add", "-A")
	_git(support, "commit", "-q", "-m", "drop")
	result = _run_helper(workspace, _runner_env(tmp_path, workspace, _git(support, "rev-parse", "HEAD")), phase)
	assert result.returncode == 1
	assert "Missing required support script auto_decisions.py" in result.stderr


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
	assert (workspace / "scripts" / "clarify_sandbox" / "Dockerfile").is_file()
	host_python = Path(exported["CLARIFY_HOST_PYTHON_DIR"])
	assert sorted(p.name for p in host_python.iterdir()) == sorted(
		_helper_phase_list(phase, "stage_support_host_python_scripts")
	)
	if unified == "true":
		assert staged == {"clarify_sandbox", ".gitignore"}, staged
		bundle = Path(exported["SUPPORT_SCRIPTS_DIR"])
		assert all((bundle / name).is_file() for name in helpers)
		assert exported["CLARIFY_ISOLATION_SUPPORT_DIR"] == str(bundle)
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


def test_ci_runs_this_file() -> None:
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "python3 -m pytest -q -p no:cacheprovider tests/test_support_script_single_source.py" in ci
