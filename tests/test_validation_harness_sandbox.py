#!/usr/bin/env python3
"""Contract tests for scripts/validation_harness_sandbox.sh.

Generated validation tests run as a separate unprivileged user with a
rootless Docker daemon; these tests pin the parts that do not need root:
the environment allowlist, fail-closed behaviour without sudo or a sandbox
daemon, all-or-nothing log copy-back, and the validate_process.sh wiring.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "validation_harness_sandbox.sh"
PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"


def _run(args: list[str], env: dict[str, str] | None = None, stdin: bytes | None = None, cwd: Path | None = None):
	base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
	return subprocess.run(
		["bash", str(HELPER), *args], input=stdin, capture_output=True, timeout=60,
		env={**base, **(env or {})}, cwd=cwd,
	)


def test_helper_is_executable_and_syntax_valid() -> None:
	assert os.access(HELPER, os.X_OK)
	assert subprocess.run(["bash", "-n", str(HELPER)], capture_output=True).returncode == 0


def test_env_allowlist_drops_credentials_and_keeps_driver_vars() -> None:
	secrets = {
		"GH_TOKEN": "a", "GH_PAT": "b", "GITHUB_TOKEN": "c", "OPENROUTER_API_KEY": "d", "TG_BOT_SECRET": "e",
		"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader", "ACTIONS_RUNTIME_TOKEN": "f",
		"GITHUB_ENV": "/tmp/x", "GITHUB_PATH": "/tmp/y", "VALIDATION_DEPLOY_TOKEN": "g",
		"VALIDATION_SECRET_VALUE": "h", "VALIDATION_HARNESS_SANDBOX_USER": "ai-validation",
	}
	kept = {
		"APP_SERVICE": "app", "COMPOSE_FILE": "validation/docker-compose.test.yml", "HEALTH_TIMEOUT": "60",
		"VALIDATION_INCLUDE_SYNTHESISED": "true", "TEST_USERNAME": "u", "TEST_PASSWORD": "p",
		"TEST_API_KEY": "k", "VALIDATION_TEST_API_KEY": "k",
	}
	result = _run(["print-env-names"], env={**secrets, **kept})
	assert result.returncode == 0, result.stderr
	names = set(result.stdout.decode().split())
	for name in secrets:
		assert name not in names, name
	for name in kept:
		assert name in names, name
	for name in ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
		assert name in names
	# Names only: never a value.
	assert b"=" not in result.stdout


def _stub_bin(root: Path, sudo_body: str) -> Path:
	bin_dir = root / "bin"
	bin_dir.mkdir()
	sudo = bin_dir / "sudo"
	sudo.write_text(f"#!/bin/sh\n{sudo_body}\n", encoding="utf-8")
	sudo.chmod(0o755)
	return bin_dir


def test_run_and_selfcheck_fail_closed_without_sudo() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		bin_dir = _stub_bin(root, "exit 1")
		status = root / "status"
		entry = root / "entry.sh"
		marker = root / "ran"
		entry.write_text(f"touch {marker}\n", encoding="utf-8")
		env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(status)}
		for args in (["selfcheck", "1"], ["run", str(entry)], ["provision"]):
			result = _run(args, env=env, cwd=root)
			assert result.returncode == 3, (args, result.stderr)
			assert b"VALIDATION_HARNESS_SANDBOX phase=" in result.stderr
			assert b"outcome=fail reason=sudo_unavailable" in result.stderr
			assert status.read_text().startswith("fail ")
		assert not marker.exists(), "entry must never run on the host as a fallback"


def test_run_fails_closed_when_rootless_docker_unavailable() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		marker = root / "ran"
		# sudo works for `true`; every command as the sandbox user fails.
		bin_dir = _stub_bin(root, 'if [ "$1" = -n ] && [ "$2" = true ]; then exit 0; fi\nexit 1')
		entry = root / "entry.sh"
		entry.write_text(f"touch {marker}\n", encoding="utf-8")
		env = {
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"VALIDATION_HARNESS_SANDBOX_USER": "nobody",
			"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(root / "status"),
		}
		result = _run(["run", str(entry)], env=env, cwd=root)
		assert result.returncode == 3, result.stderr
		assert b"rootless_docker_unavailable" in result.stderr or b"sandbox_user_missing" in result.stderr
		assert not marker.exists()


def test_invalid_sandbox_user_is_rejected() -> None:
	for user in ("root", "bad user", "$(id)", "UPPER"):
		result = _run(["print-env-names"], env={"VALIDATION_HARNESS_SANDBOX_USER": user})
		assert result.returncode == 0  # print-env-names does not need the user
		result = _run(["selfcheck"], env={"VALIDATION_HARNESS_SANDBOX_USER": user, "VALIDATION_HARNESS_SANDBOX_STATUS_FILE": "/dev/null"})
		assert result.returncode == 3
		assert b"invalid_sandbox_user" in result.stderr


def _tar(members: list[tuple[str, str, bytes]]) -> bytes:
	buffer = io.BytesIO()
	with tarfile.open(fileobj=buffer, mode="w") as archive:
		for kind, name, payload in members:
			info = tarfile.TarInfo(name)
			if kind == "file":
				info.size = len(payload)
				archive.addfile(info, io.BytesIO(payload))
			elif kind == "symlink":
				info.type = tarfile.SYMTYPE
				info.linkname = payload.decode()
				archive.addfile(info)
			elif kind == "fifo":
				info.type = tarfile.FIFOTYPE
				archive.addfile(info)
	return buffer.getvalue()


def test_copyback_accepts_regular_logs() -> None:
	with tempfile.TemporaryDirectory() as td:
		dest = Path(td) / "logs"
		data = _tar([("file", "validation/logs/compose.log", b"compose"), ("file", "validation/logs/sub/t.log", b"t")])
		result = _run(["ingest-logs", str(dest)], stdin=data)
		assert result.returncode == 0, result.stderr
		assert (dest / "compose.log").read_bytes() == b"compose"
		assert (dest / "sub" / "t.log").read_bytes() == b"t"


def test_copyback_refuses_unsafe_members_without_partial_writes() -> None:
	cases = [
		([("file", "validation/logs/a.log", b"a"), ("symlink", "validation/logs/evil", b"/etc/passwd")], b"copyback_unsafe", {}),
		([("file", "validation/logs/a.log", b"a"), ("fifo", "validation/logs/pipe", b"")], b"copyback_unsafe", {}),
		([("file", "validation/logs/a.log", b"a"), ("file", "validation/logs/../../escape", b"x")], b"copyback_unsafe", {}),
		([("file", "validation/logs/a.log", b"a"), ("file", "other/b.log", b"x")], b"copyback_unsafe", {}),
		([("file", "validation/logs/a.log", b"a" * 200)], b"copyback_oversize", {"VALIDATION_HARNESS_SANDBOX_MAX_COPYBACK_BYTES": "100"}),
		([("file", "validation/logs/a.log", b"a"), ("file", "validation/logs/b.log", b"b")], b"copyback_too_many_files", {"VALIDATION_HARNESS_SANDBOX_MAX_FILES": "1"}),
	]
	for members, reason, env in cases:
		with tempfile.TemporaryDirectory() as td:
			dest = Path(td) / "logs"
			result = _run(["ingest-logs", str(dest)], env={**env, "VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(Path(td) / "s")}, stdin=_tar(members))
			assert result.returncode == 3, (reason, result.stderr)
			assert reason in result.stderr
			assert not dest.exists() or not any(dest.rglob("*")), reason


def test_copyback_refuses_symlinked_destination() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		outside = root / "outside"
		outside.mkdir()
		dest = root / "logs"
		dest.mkdir()
		(dest / "a.log").symlink_to(outside / "target")
		result = _run(["ingest-logs", str(dest)], env={"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(root / "s")},
			stdin=_tar([("file", "validation/logs/a.log", b"pwn")]))
		assert result.returncode == 3
		assert not (outside / "target").exists()


def test_selfcheck_probe_covers_required_isolation_checks() -> None:
	text = HELPER.read_text(encoding="utf-8")
	probe = text.split("<<'PROBE'", 1)[1].split("\nPROBE\n", 1)[0]
	for token in (
		"proc_environ_readable", "host_docker_socket_usable", "sudo_available", "credential_env_present",
		"git_credential_config_present", "runner_dir_listable", "rootless_docker_unavailable",
	):
		assert f"fail {token}" in probe, token
	# Selfcheck and the harness run under `env -i` as the sandbox user.
	assert 'sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- env -i "${SANDBOX_ENV_ARGS[@]}" "$@"' in text
	assert "setsid --wait" in text


def test_validate_process_phase3_launches_only_through_the_sandbox() -> None:
	text = PROCESS.read_text(encoding="utf-8")
	phase3 = text.split("# Phase 3: Execute validation harness", 1)[1].split("tail -n 200 \"${VALIDATION_LOG_FILE}\"", 1)[0]
	assert not re.search(r'^\s*bash validation/validate\.sh\s*>', phase3, re.M)
	assert not re.search(r'^\s*"\$\{VALIDATION_RUNNER_FILE\}"\s*>', phase3, re.M)
	assert 'bash "${VALIDATION_HARNESS_SANDBOX_SCRIPT}" run "${GENERATED_VALIDATE_SCRIPT_PATH}" > "${VALIDATION_LOG_FILE}" 2>&1 &' in phase3
	assert 'bash "${VALIDATION_HARNESS_SANDBOX_SCRIPT}" provision' in phase3
	assert 'bash "${VALIDATION_HARNESS_SANDBOX_SCRIPT}" selfcheck "$$"' in phase3
	assert 'bash "${VALIDATION_HARNESS_SANDBOX_SCRIPT}" cleanup' in phase3
	assert '"harness_error"' in phase3
	assert "fail_closed_validation_sandbox" in phase3
	# Provision/selfcheck precede the launch.
	assert phase3.index("selfcheck") < phase3.index(" run \"${GENERATED_VALIDATE_SCRIPT_PATH}\"")


def test_fail_closed_handler_writes_harness_error_without_running_tests() -> None:
	if shutil.which("jq") is None:
		pytest.skip("jq unavailable")
	text = PROCESS.read_text(encoding="utf-8")
	handler = re.search(r"^fail_closed_validation_sandbox\(\)\n\{\n.*?^\}\n", text, re.M | re.S)
	assert handler is not None
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		diag = root / "diag.json"
		calls = root / "calls"
		script = (
			"set -euo pipefail\n"
			f"DIAGNOSE_RESULT_FILE={diag}\n"
			f"post_tracking_comment() {{ echo comment >> {calls}; }}\n"
			f"set_tracking_phase_label() {{ echo \"label $1\" >> {calls}; }}\n"
			f"write_result_files() {{ echo \"result $1 $4\" >> {calls}; }}\n"
			f"tg_notify() {{ echo \"tg $2\" >> {calls}; }}\n"
			+ handler.group(0)
			+ "fail_closed_validation_sandbox 'selfcheck proc_environ_readable'\necho unreachable\n"
		)
		result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
		assert result.returncode == 0, result.stderr
		assert "unreachable" not in result.stdout
		assert '"status": "harness_error"' in diag.read_text()
		recorded = calls.read_text()
		assert "label ai:validation-failed" in recorded
		assert "result fail harness_error" in recorded
		assert "tg ERROR" in recorded


def test_validate_workflow_stages_sandbox_helper() -> None:
	assert '"scripts/validation_harness_sandbox.sh",' in WORKFLOW.read_text(encoding="utf-8")
