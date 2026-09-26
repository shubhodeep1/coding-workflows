"""Contract tests for the conflict resolver's supervisor nesting.

After the sandbox-home fix (8015993, #4095) every review_autofix conflict
resolver attempt on the orchestrator/project-3965 lineage still died before
OpenCode started (run 35182160034 on PR #4115, the probe for PR #4088):

    File "<string>", line 67, in _open_output
    PermissionError: [Errno 13] Permission denied: '/tmp/tmp.NRwTT9Zrsi'

scripts/review_conflict_resolve.sh created ``tmp_output`` and
``_stall_status_file`` with ``mktemp`` as the runner (mode 0600) and then
launched ``model_provider_broker_exec_unprivileged nobody ... codex_stall_guard.sh
--stdout-file "$tmp_output" ...``, so the stall guard itself ran as ``nobody``
and could not open the runner-owned capture file. The guard is designed the
other way round (see ``_isolated_user_from_command`` in
scripts/codex_stall_guard.sh and the editor launch in
scripts/review_apply_fixes.sh): it runs as the runner and wraps the
``sudo -n -u <user> -- env -i ...`` argv as its child.

These tests pin (a) the nesting in the resolver script text, (b) the argv
builder ``model_provider_broker_unprivileged_argv_into`` in
scripts/codex_helpers.sh, and (c) the whole chain end to end under bash with a
fake ``sudo``: the guard, running as the test user, writes the child's output
into a mode-0600 ``mktemp`` file exactly as the resolver does.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
HELPERS_SCRIPT = REPO_ROOT / "scripts" / "codex_helpers.sh"
STALL_GUARD_SCRIPT = REPO_ROOT / "scripts" / "codex_stall_guard.sh"
ARGV_BUILDER = "model_provider_broker_unprivileged_argv_into"
EXEC_UNPRIVILEGED = "model_provider_broker_exec_unprivileged"
RESOLVER_ARGV_ARRAY = "resolver_unprivileged_cmd"


def _resolve_script_text() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def _helpers_text() -> str:
	return HELPERS_SCRIPT.read_text(encoding="utf-8")


def _bash_function_source(text: str, name: str) -> str:
	match = re.search(
		rf"^{re.escape(name)}\(\)\n\{{\n.*?^\}}\n",
		text,
		flags=re.MULTILINE | re.DOTALL,
	)
	assert match, f"{name} must be defined as a top-level bash function"
	return match.group(0)


def _launch_block(text: str) -> str:
	start = text.index(f"{RESOLVER_ARGV_ARRAY}=()")
	end = text.index('resolver_clean_output="${tmp_output}.ansi-clean"', start)
	return text[start:end]


def test_resolver_builds_the_unprivileged_argv_once_and_wraps_it_with_the_guard() -> None:
	text = _resolve_script_text()
	block = _launch_block(text)
	build_call = (
		f'{ARGV_BUILDER} {RESOLVER_ARGV_ARRAY} "${{RESOLVER_ISOLATION_USER}}" \\\n'
		'      timeout --signal=TERM --kill-after=30s -- "${CONFLICT_RESOLVER_PER_ATTEMPT_TIMEOUT_SECS}" \\\n'
		'      "${resolver_opencode_cmd[@]}"'
	)
	assert build_call in block, "the resolver must build the sudo/env argv from the shared helper, timeout included"
	assert block.count(f'-- "${{{RESOLVER_ARGV_ARRAY}[@]}}" < "${{_effective_prompt_file}}"') == 2, (
		"both the stall-guard and heartbeat branches must hand the unprivileged argv to the wrapper as its child"
	)
	guard_call_index = block.index('"${CODEX_STALL_GUARD_HELPER}" \\\n        --phase review_conflict_resolve')
	heartbeat_call_index = block.index('"${CODEX_HEARTBEAT_HELPER}" \\\n        --phase review_conflict_resolve')
	for call_index in (guard_call_index, heartbeat_call_index):
		call_line_start = block.rfind("\n", 0, call_index) + 1
		call_line = block[call_line_start:call_index]
		assert EXEC_UNPRIVILEGED not in call_line and "sudo" not in call_line, (
			"the wrapper must be launched directly as the runner, never nested inside the unprivileged sudo"
		)
	# The wrapper-less fallback keeps the direct unprivileged launch with the
	# runner-side stdout redirect, which never had the permission problem.
	bare_branch = block[block.index("    else\n", heartbeat_call_index):]
	assert f'{EXEC_UNPRIVILEGED} "${{RESOLVER_ISOLATION_USER}}" timeout --signal=TERM --kill-after=30s' in bare_branch
	assert '> "${tmp_output}"' in bare_branch
	# An argv-builder failure (broker not ready) must count as a failed attempt
	# rather than launching anything.
	assert "_codex_exit=1\n    _run_codex=false" in block


def test_argv_builder_is_the_single_source_of_the_unprivileged_launch() -> None:
	text = _helpers_text()
	builder = _bash_function_source(text, ARGV_BUILDER)
	exec_fn = _bash_function_source(text, EXEC_UNPRIVILEGED)
	assert 'local -n unprivileged_argv_target="${unprivileged_argv_target_name}"' in builder
	assert "sudo -n -u \"${isolation_user}\" -- env -i" in builder
	assert "return 1" in builder
	assert f"{ARGV_BUILDER} exec_unprivileged_argv \"$@\" || return 1" in exec_fn
	assert '"${exec_unprivileged_argv[@]}"' in exec_fn
	assert "sudo" not in exec_fn, "exec_unprivileged must execute the builder's argv, not a second copy of the sudo line"


def _run_argv_builder(*command: str, env_overrides: dict[str, str | None] | None = None) -> subprocess.CompletedProcess[str]:
	text = _helpers_text()
	functions = _bash_function_source(text, ARGV_BUILDER) + _bash_function_source(text, EXEC_UNPRIVILEGED)
	script = (
		"set -euo pipefail\n"
		f"{functions}\n"
		"probe_argv=()\n"
		f'{ARGV_BUILDER} probe_argv "$@"\n'
		"printf '%s\\0' \"${probe_argv[@]}\"\n"
	)
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"MODEL_PROVIDER_BROKER_TOKEN": "broker-token-for-test",
		"MODEL_PROVIDER_BROKER_BASE_URL": "http://127.0.0.1:1/v1",
		"MODEL_PROVIDER_BROKER_AGENT_HOME": "/tmp/agent-home-for-test",
	}
	if env_overrides:
		for key, value in env_overrides.items():
			if value is None:
				env.pop(key, None)
			else:
				env[key] = value
	return subprocess.run(
		["bash", "-c", script, "argv-builder", *command],
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def test_argv_builder_emits_the_guard_recognised_sudo_prefix_and_isolated_environment() -> None:
	completed = _run_argv_builder("nobody", "timeout", "--signal=TERM", "--", "5", "bash", "-c", "echo hi")
	assert completed.returncode == 0, completed.stderr
	argv = completed.stdout.split("\0")[:-1]
	# Shape codex_stall_guard.sh's _isolated_user_from_command requires to
	# enter privileged process-group signalling mode.
	assert argv[:3] == ["sudo", "-n", "-u"]
	assert argv[3] == "nobody"
	assert argv[4] == "--"
	assert argv[5:7] == ["env", "-i"]
	env_assignments = argv[7 : argv.index("timeout")]
	assignments = dict(item.split("=", 1) for item in env_assignments)
	assert assignments["HOME"] == "/tmp/agent-home-for-test"
	assert assignments["TMPDIR"] == "/tmp/agent-home-for-test/tmp"
	assert assignments["XDG_CACHE_HOME"] == "/tmp/agent-home-for-test/.cache"
	assert assignments["CODEX_HOME"] == "/tmp/agent-home-for-test/.codex"
	assert assignments["OPENROUTER_API_KEY"] == "broker-token-for-test"
	assert assignments["NO_PROXY"] == "127.0.0.1,localhost"
	assert "PATH" in assignments
	assert argv[argv.index("timeout") :] == ["timeout", "--signal=TERM", "--", "5", "bash", "-c", "echo hi"]


def test_argv_builder_refuses_when_the_broker_is_not_ready() -> None:
	completed = _run_argv_builder("nobody", "true", env_overrides={"MODEL_PROVIDER_BROKER_TOKEN": None})
	assert completed.returncode == 1
	assert "model provider broker is not ready" in completed.stderr
	assert completed.stdout == ""


def _write_fake_sudo(path: Path) -> None:
	"""A ``sudo`` that drops privileges by doing nothing: it execs the
	command after ``sudo -n -u <user> --`` as the current user, which is
	exactly what lets the nesting be exercised without root."""
	path.write_text(
		"#!/usr/bin/env python3\n"
		"import os, sys\n"
		"args = sys.argv[1:]\n"
		'if len(args) >= 5 and args[:2] == ["-n", "-u"] and args[3] == "--":\n'
		"\tos.execvp(args[4], args[4:])\n"
		"raise SystemExit(64)\n",
		encoding="utf-8",
	)
	path.chmod(0o755)


def test_guard_as_runner_wrapping_the_unprivileged_argv_writes_the_runner_owned_capture_file() -> None:
	"""End to end: the argv builder's output as the stall guard's child.

	The stdout capture file is created the way the resolver creates it
	(mktemp, mode 0600, owned by the caller). Before the fix the guard ran
	inside the sudo and hit EACCES on this open; now it opens the file as
	the runner and the unprivileged child's stdout lands in it.
	"""
	helpers_text = _helpers_text()
	functions = _bash_function_source(helpers_text, ARGV_BUILDER) + _bash_function_source(helpers_text, EXEC_UNPRIVILEGED)
	with tempfile.TemporaryDirectory(prefix="resolver-guard-nesting-") as td:
		tmp = Path(td)
		fake_bin = tmp / "bin"
		fake_bin.mkdir()
		_write_fake_sudo(fake_bin / "sudo")
		agent_home = tmp / "agent-home"
		(agent_home / "tmp").mkdir(parents=True)
		heartbeat_dir = tmp / "heartbeats"
		status_file = tmp / "guard.status"
		marker_file = tmp / "child-ran.txt"
		script = (
			"set -euo pipefail\n"
			f"{functions}\n"
			'tmp_output="$(mktemp)"\n'
			'printf \'%s\\n\' "${tmp_output}" > "$1"\n'
			f"{RESOLVER_ARGV_ARRAY}=()\n"
			f'{ARGV_BUILDER} {RESOLVER_ARGV_ARRAY} nobody \\\n'
			'  timeout --signal=TERM --kill-after=5s -- 30 \\\n'
			'  bash -c \'printf "hi from %s\\n" "$HOME"; printf "%s" "$TMPDIR" > "$1"\' resolver-child "$2"\n'
			f'"{STALL_GUARD_SCRIPT}" --phase review_conflict_resolve --stdout-file "${{tmp_output}}" --status-file "$3" \\\n'
			f'  -- "${{{RESOLVER_ARGV_ARRAY}[@]}}" < /dev/null\n'
		)
		output_path_file = tmp / "tmp-output-path.txt"
		env = {
			"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
			"MODEL_PROVIDER_BROKER_TOKEN": "broker-token-for-test",
			"MODEL_PROVIDER_BROKER_BASE_URL": "http://127.0.0.1:1/v1",
			"MODEL_PROVIDER_BROKER_AGENT_HOME": str(agent_home),
			"TMPDIR": str(tmp),
			"PYTHONDONTWRITEBYTECODE": "1",
			"CODEX_HEARTBEAT_ENABLED": "1",
			"CODEX_HEARTBEAT_INTERVAL_SECS": "1",
			"CODEX_STALL_GUARD_ENABLED": "true",
			"CODEX_STALL_TIMEOUT_SECONDS": "20",
			"CODEX_STALL_KILL_GRACE_SECONDS": "2",
			"CODEX_STALL_HEARTBEAT_DIR": str(heartbeat_dir),
			"GITHUB_RUN_ID": "35182160034",
			"PR_NUMBER": "4088",
		}
		completed = subprocess.run(
			["bash", "-c", script, "resolver-launch", str(output_path_file), str(marker_file), str(status_file)],
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
			check=False,
		)
		assert completed.returncode == 0, completed.stderr
		assert "PermissionError" not in completed.stderr
		tmp_output = Path(output_path_file.read_text(encoding="utf-8").strip())
		try:
			assert stat.S_IMODE(tmp_output.stat().st_mode) == 0o600, "mktemp must have produced the resolver's 0600 capture file"
			assert tmp_output.read_text(encoding="utf-8") == f"hi from {agent_home}\n"
		finally:
			tmp_output.unlink(missing_ok=True)
		assert marker_file.read_text(encoding="utf-8") == str(agent_home / "tmp"), "the child must see the isolated TMPDIR"
		# The guard only writes its status file on an observed stall or a
		# kill; a child that exits promptly leaves it absent. Either way the
		# guard must not have killed the unprivileged child.
		if status_file.exists():
			assert "state=killed" not in status_file.read_text(encoding="utf-8")
		assert "codex_stall_killed" not in completed.stderr


if __name__ == "__main__":
	failures = 0
	for name, value in sorted(globals().items()):
		if name.startswith("test_") and callable(value):
			try:
				value()
			except AssertionError as exc:
				failures += 1
				print(f"FAIL {name}: {exc}", file=sys.stderr)
			else:
				print(f"ok {name}")
	raise SystemExit(1 if failures else 0)
