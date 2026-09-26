"""Contract tests for the resolver sandbox-home preflight.

Every review_autofix conflict-resolver run on the orchestrator/project-3965
lineage since f03b8d6 (#4071) died at the same line of
scripts/review_conflict_resolve.sh:

    mkdir: cannot create directory '<RUNTIME_DIR>/resolver-agent-home': Permission denied

(runs 34746616712 on PR #4077; 34960984494 and 34992257788 on PR #4088),
even though review_conflict_prepare.sh had just written into the same
RUNTIME_DIR and the editor path creates its own sandbox there. The bare
coreutils error never named the process identity or the directory state, so
the resolver now runs ``_resolver_agent_home_preflight`` first: it logs one
``RESOLVER_AGENT_HOME_PREFLIGHT`` line with uid/euid/user, owner, mode, ACLs
and mount of RUNTIME_DIR plus the state of an existing resolver home, and fails closed with
``::error::RESOLVER_AGENT_HOME_PREFLIGHT_DENIED`` when the directory is not a
writable, searchable directory for the current identity or an existing
resolver home is unusable.

These tests pin (a) the placement contract in the script text and (b) the
helper's behaviour by executing the extracted function under bash.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
PREFLIGHT_FUNCTION_NAME = "_resolver_agent_home_preflight"
PREFLIGHT_LOG_PREFIX = "RESOLVER_AGENT_HOME_PREFLIGHT"
PREFLIGHT_DENIED_PREFIX = "RESOLVER_AGENT_HOME_PREFLIGHT_DENIED"
AGENT_HOME_MKDIR = 'mkdir -p "${MODEL_PROVIDER_BROKER_AGENT_HOME}/tmp" "${MODEL_PROVIDER_BROKER_AGENT_HOME}/.cache"'


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def _preflight_function_source(text: str) -> str:
	match = re.search(
		rf"^{re.escape(PREFLIGHT_FUNCTION_NAME)}\(\)\n\{{\n.*?^\}}\n",
		text,
		flags=re.MULTILINE | re.DOTALL,
	)
	assert match, f"{PREFLIGHT_FUNCTION_NAME} must be defined as a top-level bash function"
	return match.group(0)


def _run_preflight(function_source: str, target: Path) -> subprocess.CompletedProcess[str]:
	agent_home = target / "resolver-agent-home"
	script = (
		f"MODEL_PROVIDER_BROKER_AGENT_HOME={str(agent_home)!r}\n"
		f"{function_source}\n{PREFLIGHT_FUNCTION_NAME} {str(target)!r}\n"
	)
	return subprocess.run(
		["bash", "-c", script],
		capture_output=True,
		text=True,
		check=False,
	)


def test_preflight_runs_before_the_agent_home_mkdir_and_fails_closed() -> None:
	text = _resolve_script_text()
	definition = text.index(f"{PREFLIGHT_FUNCTION_NAME}()\n{{")
	invocation = text.index(f'{PREFLIGHT_FUNCTION_NAME} "${{RUNTIME_DIR}}" || exit 1')
	mkdir_index = text.index(AGENT_HOME_MKDIR)
	broker_start_index = text.index("model_provider_broker_start", mkdir_index)
	ownership_transfer_index = text.index('sudo -n chown -R "${RESOLVER_ISOLATION_USER}"', mkdir_index)
	opencode_launch_index = text.index('model_provider_broker_unprivileged_argv_into resolver_unprivileged_cmd "${RESOLVER_ISOLATION_USER}"')
	assert definition < invocation < mkdir_index < broker_start_index < ownership_transfer_index < opencode_launch_index, (
		"the runner must preflight and start the broker before transferring the agent home, "
		"and ownership transfer must precede the unprivileged OpenCode launch"
	)
	assert text.count(AGENT_HOME_MKDIR) == 1
	function_source = _preflight_function_source(text)
	assert f'echo "{PREFLIGHT_LOG_PREFIX} uid=' in function_source
	assert f'echo "::error::{PREFLIGHT_DENIED_PREFIX} runtime_dir=' in function_source
	assert "return 1" in function_source


def test_preflight_passes_and_logs_identity_for_a_writable_runtime_dir(tmp_path: Path) -> None:
	function_source = _preflight_function_source(_resolve_script_text())
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir()
	completed = _run_preflight(function_source, runtime_dir)
	assert completed.returncode == 0, completed.stderr
	lines = [line for line in completed.stdout.splitlines() if line.startswith(f"{PREFLIGHT_LOG_PREFIX} ")]
	assert len(lines) == 1, completed.stdout
	line = lines[0]
	assert f"uid={os.getuid()} euid={os.geteuid()} " in line
	assert f"runtime_dir={runtime_dir} " in line
	assert " owner=" in line and " mode=" in line and " type=directory" in line
	assert " parent_dir=owner=" in line
	assert " acl=" in line and " mount=" in line
	assert " agent_home_exists=false " in line
	assert " agent_home_symlink=false " in line
	assert " agent_home_acl=not-applicable" in line
	assert PREFLIGHT_DENIED_PREFIX not in completed.stderr


def test_preflight_fails_closed_when_runtime_dir_is_missing(tmp_path: Path) -> None:
	function_source = _preflight_function_source(_resolve_script_text())
	missing_dir = tmp_path / "does-not-exist"
	completed = _run_preflight(function_source, missing_dir)
	assert completed.returncode == 1
	assert f"::error::{PREFLIGHT_DENIED_PREFIX} runtime_dir={missing_dir} exists=false writable=false searchable=false" in completed.stderr
	assert f"uid={os.getuid()} euid={os.geteuid()} " in completed.stderr
	assert re.search(r" user=\S+ ", completed.stderr)
	assert completed.stdout.startswith(f"{PREFLIGHT_LOG_PREFIX} uid=")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
def test_preflight_fails_closed_when_runtime_dir_is_not_writable(tmp_path: Path) -> None:
	function_source = _preflight_function_source(_resolve_script_text())
	locked_dir = tmp_path / "locked"
	locked_dir.mkdir()
	locked_dir.chmod(0o500)
	try:
		completed = _run_preflight(function_source, locked_dir)
	finally:
		locked_dir.chmod(0o700)
	assert completed.returncode == 1
	assert f"::error::{PREFLIGHT_DENIED_PREFIX} runtime_dir={locked_dir} exists=true writable=false searchable=true" in completed.stderr
	assert f"uid={os.getuid()} euid={os.geteuid()} " in completed.stderr
	assert re.search(r" user=\S+ ", completed.stderr)
	assert " mode=500 " in completed.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
def test_preflight_fails_closed_when_existing_agent_home_is_not_writable(tmp_path: Path) -> None:
	function_source = _preflight_function_source(_resolve_script_text())
	runtime_dir = tmp_path / "runtime"
	agent_home = runtime_dir / "resolver-agent-home"
	agent_home.mkdir(parents=True)
	agent_home.chmod(0o500)
	try:
		completed = _run_preflight(function_source, runtime_dir)
	finally:
		agent_home.chmod(0o700)
	assert completed.returncode == 1
	assert "exists=true writable=true searchable=true" in completed.stderr
	assert "agent_home_exists=true agent_home_symlink=false agent_home_directory=true" in completed.stderr
	assert "agent_home_writable=false agent_home_searchable=true" in completed.stderr
	assert "agent_home_stat=owner=" in completed.stderr
	assert " mode=500 type=directory" in completed.stderr
	assert f"uid={os.getuid()} euid={os.geteuid()} " in completed.stderr
	assert re.search(r" user=\S+ ", completed.stderr)


def test_preflight_fails_closed_when_existing_agent_home_is_a_symlink(tmp_path: Path) -> None:
	function_source = _preflight_function_source(_resolve_script_text())
	runtime_dir = tmp_path / "runtime"
	external_target = tmp_path / "external-target"
	runtime_dir.mkdir()
	external_target.mkdir()
	(runtime_dir / "resolver-agent-home").symlink_to(external_target, target_is_directory=True)
	completed = _run_preflight(function_source, runtime_dir)
	assert completed.returncode == 1
	assert "agent_home_exists=true agent_home_symlink=true agent_home_directory=true" in completed.stderr
	assert "agent_home_writable=true agent_home_searchable=true" in completed.stderr
	assert "agent_home_stat=owner=" in completed.stderr
	assert " type=symbolic link" in completed.stderr
	assert f"uid={os.getuid()} euid={os.geteuid()} " in completed.stderr
	assert re.search(r" user=\S+ ", completed.stderr)
