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
import stat
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
		# validate_process.sh always sets these; the handler reads them under set -u.
		result = subprocess.run(
			["bash", "-c", script], capture_output=True, text=True, timeout=30,
			env={**os.environ, "GITHUB_REPOSITORY": "test-owner/test-repo", "TRACKING_ISSUE_RAW": "42"},
		)
		assert result.returncode == 0, result.stderr
		assert "unreachable" not in result.stdout
		assert '"status": "harness_error"' in diag.read_text()
		recorded = calls.read_text()
		assert "label ai:validation-failed" in recorded
		assert "result fail harness_error" in recorded
		assert "tg ERROR" in recorded


def test_validate_workflow_stages_sandbox_helper() -> None:
	assert '"scripts/validation_harness_sandbox.sh",' in WORKFLOW.read_text(encoding="utf-8")


def test_checked_run_rejects_bad_pid_and_never_runs_entry() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		marker = root / "ran"
		entry = root / "entry.sh"
		entry.write_text(f"touch {marker}\n", encoding="utf-8")
		env = {"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(root / "status")}
		for pid in ("", "abc", "1;id"):
			result = _run(["checked-run", pid, str(entry)], env=env, cwd=root)
			assert result.returncode == 3, result.stderr
			assert b"invalid_caller_pid" in result.stderr
		# A valid pid without sudo fails closed in selfcheck before `run`.
		bin_dir = _stub_bin(root, "exit 1")
		result = _run(["checked-run", "1", str(entry)], env={**env, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, cwd=root)
		assert result.returncode == 3
		assert b"phase=selfcheck outcome=fail reason=sudo_unavailable" in result.stderr
		assert not marker.exists()


def test_copyback_destination_override_is_used_and_must_be_absolute() -> None:
	text = HELPER.read_text(encoding="utf-8")
	assert 'copyback_dest="${VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST:-${workspace}/validation/logs}"' in text
	assert '| cmd_ingest_logs "${copyback_dest}" "validation/logs"' in text
	assert "sandbox_fail run invalid_copyback_dest" in text
	# The override never enters the sandbox environment.
	result = _run(["print-env-names"], env={"VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST": "/tmp/x"})
	assert b"VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST" not in result.stdout


def test_validation_refresh_self_test_runs_only_through_sandbox() -> None:
	runner = (REPO_ROOT / "scripts" / "validation_refresh_runner.py").read_text(encoding="utf-8")
	assert '"checked-run",' in runner
	assert "str(os.getpid())" in runner
	assert '("self_test", self_test_command)' not in runner


def _function_body(name: str) -> str:
	text = HELPER.read_text(encoding="utf-8")
	match = re.search(rf"^{name}\(\)\n\{{\n.*?^\}}\n", text, re.M | re.S)
	assert match is not None, name
	return match.group(0)


def test_cleanup_never_kills_the_sandbox_written_pgid_as_root() -> None:
	# ${work}.pgid is written by the untrusted sandbox user; a forged value
	# must not reach a root-privileged kill (it could target runner processes).
	# Root kills only the `unshare` pid root recorded in the root-owned state
	# directory, after checking the file and the process (#7349).
	body = _function_body("cmd_cleanup")
	assert 'pidns_kill_recorded "${state_base}.pid"' in body
	assert 'sudo -n cat "${work}.pgid"' not in body
	assert "pgid=" not in body and '"-${pgid}"' not in body
	assert 'as_sandbox rm -rf -- "${work}" "${work}.pgid"' in body
	kill = _function_body("pidns_kill_recorded")
	assert '[ "${meta}" = "0 regular file" ]' in kill
	assert '[ "${comm}" = "unshare" ] && [ "${owner}" = "0" ]' in kill
	assert '[ "${pid}" -gt 1 ]' in kill
	assert "pgid" not in kill
	assert 'state_base="$(pidns_state_base)"' in body
	assert '"${VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR}"' in _function_body("pidns_state_base")


# --- rootless package install on runners without Docker's apt source ---------

DOCKER_FPR = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"


def _install_env(tmp: Path, *, plain_install_works: bool = False, fingerprint: str = DOCKER_FPR, pinned_version_exists: bool = True) -> dict[str, str]:
	"""Fake sudo/apt-get/curl/gpg/dpkg on PATH; apt-get only finds the rootless
	package once our Docker source list exists (or always, when plain_install_works)."""
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	sources = tmp / "sources.list.d"
	keyrings = tmp / "keyrings"
	sources.mkdir()
	(tmp / "os-release").write_text('ID=ubuntu\nVERSION_CODENAME=noble\n', encoding="utf-8")
	log = tmp / "calls.log"
	scripts = {
		"sudo": '#!/bin/bash\n[ "$1" = "-n" ] && shift\nexec "$@"\n',
		"apt-get": f'''#!/bin/bash
echo "apt-get $*" >> "{log}"
case "$*" in
	*update*) exit 0 ;;
esac
want=""
for a in "$@"; do case "$a" in docker-ce-rootless-extras*) want="$a" ;; esac; done
[ -z "$want" ] && exit 0
{"exit 0" if plain_install_works else ""}
[ -f "{sources}/ai-validation-docker.list" ] || exit 100
case "$want" in
	docker-ce-rootless-extras=*) {"exit 0" if pinned_version_exists else "exit 100"} ;;
esac
exit 0
''',
		"curl": f'''#!/bin/bash
echo "curl $*" >> "{log}"
out=""
while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
echo "-----BEGIN PGP PUBLIC KEY BLOCK-----" > "$out"
''',
		"gpg": f'#!/bin/bash\necho "fpr:::::::::{fingerprint}:"\n',
		"dpkg": '#!/bin/bash\necho amd64\n',
		"dpkg-query": '#!/bin/bash\necho "5:28.4.0-1~ubuntu.24.04~noble"\n',
	}
	for name, body in scripts.items():
		path = bin_dir / name
		path.write_text(body, encoding="utf-8")
		path.chmod(0o755)
	return {
		"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
		"SANDBOX_DOCKER_APT_KEYRING_DIR": str(keyrings),
		"SANDBOX_DOCKER_APT_SOURCES_DIR": str(sources),
		"SANDBOX_OS_RELEASE_FILE": str(tmp / "os-release"),
		"CALLS_LOG": str(log),
	}


def _install(env: dict[str, str]) -> subprocess.CompletedProcess:
	return subprocess.run(
		["bash", "-c", f'source "{HELPER}"; install_rootless_packages'],
		capture_output=True, text=True, timeout=60, env={"HOME": os.environ.get("HOME", "/tmp"), **env},
	)


def test_rootless_packages_come_from_a_temporary_docker_repo_when_the_runner_has_none(tmp_path: Path) -> None:
	env = _install_env(tmp_path)
	result = _install(env)
	assert result.returncode == 0, result.stderr
	calls = Path(env["CALLS_LOG"]).read_text(encoding="utf-8").splitlines()
	assert "curl -fsSL --retry 3 --max-time 60 -o" in " ".join(calls)
	assert any(c.endswith("https://download.docker.com/linux/ubuntu/gpg") for c in calls)
	# The installed docker-ce version is preferred.
	assert "apt-get install -y -q docker-ce-rootless-extras=5:28.4.0-1~ubuntu.24.04~noble" in calls
	assert "VALIDATION_HARNESS_SANDBOX phase=provision outcome=ok reason=rootless_packages_from_docker_repo" in result.stderr
	# The temporary source and key are removed again.
	assert not (tmp_path / "sources.list.d" / "ai-validation-docker.list").exists()
	assert not (tmp_path / "keyrings" / "ai-validation-docker.asc").exists()


def test_the_temporary_source_is_signed_by_the_pinned_key(tmp_path: Path) -> None:
	env = _install_env(tmp_path)
	sources = tmp_path / "sources.list.d"
	# Keep a copy of the list apt saw by having apt-get copy it on the pinned install.
	apt = tmp_path / "bin" / "apt-get"
	apt.write_text(apt.read_text(encoding="utf-8").replace(
		'case "$want" in', f'cp "{sources}/ai-validation-docker.list" "{tmp_path}/seen.list"\ncase "$want" in', 1,
	), encoding="utf-8")
	assert _install(env).returncode == 0
	assert (tmp_path / "seen.list").read_text(encoding="utf-8") == (
		f"deb [arch=amd64 signed-by={tmp_path}/keyrings/ai-validation-docker.asc] https://download.docker.com/linux/ubuntu noble stable\n"
	)


def test_plain_install_needs_no_docker_repo(tmp_path: Path) -> None:
	env = _install_env(tmp_path, plain_install_works=True)
	result = _install(env)
	assert result.returncode == 0, result.stderr
	assert "curl" not in Path(env["CALLS_LOG"]).read_text(encoding="utf-8")


def test_unpinned_install_when_the_installed_version_is_not_in_the_repo(tmp_path: Path) -> None:
	env = _install_env(tmp_path, pinned_version_exists=False)
	result = _install(env)
	assert result.returncode == 0, result.stderr
	assert Path(env["CALLS_LOG"]).read_text(encoding="utf-8").splitlines()[-1] == "apt-get install -y -q docker-ce-rootless-extras"


def test_a_wrong_key_fingerprint_is_refused_and_nothing_is_trusted(tmp_path: Path) -> None:
	env = _install_env(tmp_path, fingerprint="0" * 40)
	result = _install(env)
	assert result.returncode == 1
	assert "reason=docker_repo_key_fingerprint_mismatch" in result.stderr
	assert not list((tmp_path / "sources.list.d").iterdir())
	assert not (tmp_path / "keyrings").exists()


def test_a_failed_repo_install_names_its_reason_and_cleans_up(tmp_path: Path) -> None:
	env = _install_env(tmp_path, pinned_version_exists=False)
	apt = tmp_path / "bin" / "apt-get"
	apt.write_text(apt.read_text(encoding="utf-8").replace("\nexit 0\n", "\nexit 100\n"), encoding="utf-8")
	result = _install(env)
	assert result.returncode == 1
	assert "reason=docker_repo_install_failed" in result.stderr
	assert not (tmp_path / "sources.list.d" / "ai-validation-docker.list").exists()
	assert not (tmp_path / "keyrings" / "ai-validation-docker.asc").exists()


def test_provision_still_fails_closed_when_the_packages_cannot_be_installed() -> None:
	text = HELPER.read_text(encoding="utf-8")
	assert "install_rootless_packages || sandbox_fail provision rootless_packages_unavailable" in text
	assert 'SANDBOX_DOCKER_APT_KEY_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"' in text
	assert 'key_tmp="$(mktemp)" || { sandbox_log provision fail docker_repo_tmpfile_failed; return 1; }' in text


def test_ci_runs_this_file() -> None:
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_validation_harness_sandbox.py" in ci


# Daily sandbox check (Q39: C, item 9): smoke validations skip the sandbox, so
# a runner-image change that broke provisioning showed up only when every
# project validation failed (#6959). The nightly self-test workflow now runs
# provision, the isolation self-check, a staged run and log copy-back.

NIGHTLY = REPO_ROOT / ".github" / "workflows" / "nightly-validation-selftest.yml"

# Stand-in for the sandbox helper: `checked-run` runs the entry in a scratch
# copy and copies validation/logs back, the way the real helper does.
FAKE_HELPER = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_CALLS}"
case "$1" in
	provision) exit "${FAKE_PROVISION_RC:-0}" ;;
	checked-run)
		[[ "$2" =~ ^[0-9]+$ ]] || exit 3
		work="$(mktemp -d)"
		(cd "${work}" && bash "$3") || exit $?
		dest="${VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST:?}"
		mkdir -p "${dest}"
		cp -R "${work}/validation/logs/." "${dest}/"
		;;
	cleanup) exit 0 ;;
esac
"""


def _nightly_step(job: str, name: str) -> str:
	import yaml

	steps = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"][job]["steps"]
	return next(step for step in steps if step.get("name") == name)["run"]


def _run_nightly_check(tmp: Path, *, docker_body: str, provision_rc: int = 0) -> subprocess.CompletedProcess:
	(tmp / "scripts").mkdir()
	helper = tmp / "scripts" / "validation_harness_sandbox.sh"
	helper.write_text(FAKE_HELPER, encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	docker = bin_dir / "docker"
	docker.write_text(f"#!/bin/sh\n{docker_body}\n", encoding="utf-8")
	docker.chmod(0o755)
	runner_temp = tmp / "runner-temp"
	runner_temp.mkdir()
	env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp), "RUNNER_TEMP": str(runner_temp),
		"FAKE_CALLS": str(tmp / "calls"), "FAKE_PROVISION_RC": str(provision_rc)}
	return subprocess.run(["bash", "-c", _nightly_step("harness-sandbox-check", "Provision and exercise the validation harness sandbox")],
		cwd=tmp, env=env, capture_output=True, text=True, timeout=60)


def test_nightly_sandbox_check_is_wired_on_a_fresh_read_only_runner() -> None:
	import yaml

	workflow = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
	on = workflow.get("on", workflow.get(True))
	assert on["schedule"] == [{"cron": "15 2 * * *"}]
	job = workflow["jobs"]["harness-sandbox-check"]
	assert job["permissions"] == {"contents": "read"}
	assert "needs" not in job and job["timeout-minutes"] <= 20
	checkout = job["steps"][0]
	assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["persist-credentials"] is False
	run = _nightly_step("harness-sandbox-check", "Provision and exercise the validation harness sandbox")
	assert "bash scripts/validation_harness_sandbox.sh provision" in run
	assert 'bash scripts/validation_harness_sandbox.sh checked-run "$$" "${probe}"' in run
	assert 'VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST="${logs}"' in run
	cleanup = next(step for step in job["steps"] if step.get("name") == "Clean up the validation harness sandbox")
	assert cleanup["if"] == "always()" and "validation_harness_sandbox.sh cleanup" in cleanup["run"]


def test_nightly_sandbox_check_passes_when_rootless_docker_answers() -> None:
	with tempfile.TemporaryDirectory() as td:
		result = _run_nightly_check(Path(td), docker_body="echo 'server=27.0.0 rootless=[\"name=rootless\"]'")
		assert result.returncode == 0, result.stderr + result.stdout
		assert "VALIDATION_HARNESS_SANDBOX_DAILY outcome=ok server=27.0.0" in result.stdout
		calls = (Path(td) / "calls").read_text().splitlines()
		assert calls[0] == "provision" and calls[1].startswith("checked-run ") and calls[1].endswith("/harness_sandbox_probe.sh")


@pytest.mark.parametrize("docker_body,provision_rc,reason", (("exit 1", 0, "checked_run_failed"), ("echo ok", 3, "provision_failed")))
def test_nightly_sandbox_check_fails_when_docker_or_provision_fails(docker_body, provision_rc, reason) -> None:
	with tempfile.TemporaryDirectory() as td:
		result = _run_nightly_check(Path(td), docker_body=docker_body, provision_rc=provision_rc)
		assert result.returncode != 0
		assert "VALIDATION_HARNESS_SANDBOX_DAILY outcome=ok" not in result.stdout
		assert f"VALIDATION_HARNESS_SANDBOX_DAILY outcome=fail reason={reason}" in result.stdout


def test_nightly_sandbox_check_fails_when_no_log_copies_back() -> None:
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		result = _run_nightly_check(tmp, docker_body="echo ok")
		assert result.returncode == 0
		# A copy-back that delivers nothing is a failure, not a silent pass.
		helper = tmp / "scripts" / "validation_harness_sandbox.sh"
		helper.write_text(FAKE_HELPER.replace('cp -R "${work}/validation/logs/." "${dest}/"', ": copy-back lost"), encoding="utf-8")
		(tmp / "runner-temp" / "harness-sandbox-probe-logs" / "harness_sandbox_probe.log").unlink()
		env = {"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(tmp), "RUNNER_TEMP": str(tmp / "runner-temp"), "FAKE_CALLS": str(tmp / "calls")}
		again = subprocess.run(["bash", "-c", _nightly_step("harness-sandbox-check", "Provision and exercise the validation harness sandbox")],
			cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
		assert again.returncode == 1
		assert "VALIDATION_HARNESS_SANDBOX_DAILY outcome=fail reason=probe_log_missing" in again.stdout


# --- cgroup driver for the sandbox's rootless dockerd (#7063, Q57) -----------
# Rootless dockerd picks the systemd cgroup driver on a systemd cgroup v2 host;
# without a user session for the sandbox user every container start failed with
# "open /sys/fs/cgroup/user.slice/user-<uid>.slice/cgroup.controllers"
# (#6902, #6664). Provision now sets up the session, proves a container starts,
# and falls back to cgroupfs before failing closed. Since #7380 that systemd
# path is opt-in (`auto`); the default `cgroupfs` releases any leftover user
# manager, and every path that ends in cgroupfs releases it first.

def _driver(tmp: Path, *, session: bool, probe: dict[str, int], mode: str | None = "auto", ready: bool = False) -> tuple[subprocess.CompletedProcess, list[str]]:
	"""Run sandbox_start_verified_dockerd with its collaborators stubbed out.

	probe maps the daemon's driver (systemd/cgroupfs/reused) to the probe's exit code.
	mode None leaves VALIDATION_HARNESS_SANDBOX_CGROUP_MODE unset (the default).
	"""
	calls = tmp / "calls"
	status = tmp / "status"
	probes = " ".join(f"[{key}]={value}" for key, value in probe.items())
	script = f'''
source "{HELPER}"
sudo() {{ :; }}
build_sandbox_env() {{ :; }}
declare -A PROBE=({probes})
DAEMON="{"reused" if ready else ""}"
sandbox_systemd_session() {{ echo "session $*" >> "{calls}"; {"return 0" if session else "return 1"}; }}
sandbox_release_systemd_session() {{ echo "release $*" >> "{calls}"; }}
rootless_docker_ready() {{ [ -n "${{DAEMON}}" ]; }}
start_rootless_dockerd() {{ echo "start $1" >> "{calls}"; DAEMON="$1"; }}
stop_rootless_dockerd() {{ echo "stop" >> "{calls}"; DAEMON=""; }}
rootless_docker_container_probe() {{ echo "probe ${{DAEMON}}" >> "{calls}"; return "${{PROBE[$DAEMON]:-1}}"; }}
sandbox_start_verified_dockerd ai-validation 1002 /home/ai-validation /run/user/1002
'''
	env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp), "VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(status)}
	if mode is not None:
		env["VALIDATION_HARNESS_SANDBOX_CGROUP_MODE"] = mode
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
	return result, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def test_a_working_systemd_session_is_used_once_a_container_starts(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"systemd": 0})
	assert result.returncode == 0, result.stderr
	assert calls == ["session ai-validation 1002", "start systemd", "probe systemd"]
	assert "outcome=ok reason=systemd_user_session" in result.stderr
	assert "outcome=ok reason=container_probe_systemd" in result.stderr


def test_a_container_that_fails_under_systemd_restarts_the_daemon_with_cgroupfs(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"systemd": 1, "cgroupfs": 0})
	assert result.returncode == 0, result.stderr
	assert calls == ["session ai-validation 1002", "start systemd", "probe systemd", "stop", "release ai-validation 1002", "start cgroupfs", "probe cgroupfs"]
	assert "outcome=retry reason=container_start_failed_systemd" in result.stderr
	assert "outcome=ok reason=container_probe_cgroupfs" in result.stderr


def test_no_systemd_session_goes_straight_to_cgroupfs(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=False, probe={"cgroupfs": 0})
	assert result.returncode == 0, result.stderr
	assert calls == ["session ai-validation 1002", "release ai-validation 1002", "start cgroupfs", "probe cgroupfs"]
	assert "outcome=ok reason=systemd_user_session_unavailable" in result.stderr


def test_a_container_that_fails_under_both_drivers_fails_provision_closed(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"systemd": 1, "cgroupfs": 1})
	assert result.returncode == 3
	assert calls[-2:] == ["start cgroupfs", "probe cgroupfs"]
	assert "outcome=fail reason=container_start_failed" in result.stderr
	assert (tmp_path / "status").read_text(encoding="utf-8") == "fail provision container_start_failed\n"


def test_cgroupfs_mode_never_touches_the_systemd_session(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"cgroupfs": 0}, mode="cgroupfs")
	assert result.returncode == 0, result.stderr
	assert calls == ["release ai-validation 1002", "start cgroupfs", "probe cgroupfs"]


def test_cgroupfs_is_the_default_cgroup_mode(tmp_path: Path) -> None:
	result = subprocess.run(["bash", "-c", f'source "{HELPER}"; printf "%s" "${{VALIDATION_HARNESS_SANDBOX_CGROUP_MODE}}"'],
		capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
	assert result.returncode == 0, result.stderr
	assert result.stdout == "cgroupfs"


def test_the_default_mode_never_starts_a_systemd_session(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"cgroupfs": 0}, mode=None)
	assert result.returncode == 0, result.stderr
	assert calls == ["release ai-validation 1002", "start cgroupfs", "probe cgroupfs"]
	assert "reason=cgroup_mode_unrecognized" not in result.stderr


def test_an_inconclusive_probe_keeps_the_daemon_and_says_so(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"systemd": 2})
	assert result.returncode == 0, result.stderr
	assert "stop" not in calls
	assert "outcome=ok reason=container_probe_inconclusive" in result.stderr


def test_a_reused_daemon_is_tested_too_and_replaced_when_containers_fail(tmp_path: Path) -> None:
	result, calls = _driver(tmp_path, session=True, probe={"reused": 1, "cgroupfs": 0}, ready=True)
	assert result.returncode == 0, result.stderr
	assert calls == ["session ai-validation 1002", "probe reused", "stop", "release ai-validation 1002", "start cgroupfs", "probe cgroupfs"]


CGROUP_ERR = "docker: Error response from daemon: open /sys/fs/cgroup/user.slice/user-1002.slice/cgroup.controllers: no such file or directory"


def _probe(tmp: Path, run_rc: int, message: str = CGROUP_ERR) -> subprocess.CompletedProcess:
	script = f'''
source "{HELPER}"
as_sandbox() {{
	case "$*" in
		"docker image inspect"*) return 0 ;;
		"docker run"*) echo "{message}" >&2; return {run_rc} ;;
	esac
}}
rootless_docker_container_probe
'''
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp)})


# The Docker CLI (runStartContainerErr) exits 127 for a start error containing
# "no such file or directory", so the #7063 cgroup error arrives as 127.
NOT_FOUND_ERR = "docker: Error response from daemon: exec: /usr/bin/true: executable file not found in PATH"


@pytest.mark.parametrize("run_rc,message,expected", (
	(0, CGROUP_ERR, 0), (125, CGROUP_ERR, 1), (127, CGROUP_ERR, 1), (126, CGROUP_ERR, 1),
	(126, NOT_FOUND_ERR, 2), (127, NOT_FOUND_ERR, 2),
))
def test_only_a_daemon_refusal_counts_as_a_failed_container_start(tmp_path: Path, run_rc: int, message: str, expected: int) -> None:
	result = _probe(tmp_path, run_rc, message)
	assert result.returncode == expected, result.stderr
	if run_rc:
		assert f"probe_exit={run_rc} detail={message[:60]}" in result.stderr


@pytest.mark.skipif(shutil.which("ldd") is None or not Path("/usr/bin/true").exists(), reason="needs ldd and /usr/bin/true")
def test_the_probe_image_is_built_locally_from_true_and_its_libraries(tmp_path: Path) -> None:
	captured = tmp_path / "image.tar"
	script = f'''
source "{HELPER}"
as_sandbox() {{
	case "$*" in
		"docker image inspect"*) return 1 ;;
		"docker import"*) printf '%s\\n' "$*" > "{tmp_path}/import_args"; cat > "{captured}" ;;
	esac
}}
build_probe_image
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
	assert result.returncode == 0, result.stderr
	assert (tmp_path / "import_args").read_text(encoding="utf-8").strip() == 'docker import --change CMD ["/usr/bin/true"] - ai-validation-probe:local'
	with tarfile.open(captured) as archive:
		names = archive.getnames()
		assert "usr/bin/true" in names
		assert any(name.endswith("libc.so.6") for name in names) or len(names) == 1
		assert all(archive.getmember(name).isfile() for name in names), "symlinks are dereferenced"
		assert not any(name.startswith("/") or ".." in name for name in names)


def _session(tmp: Path, *, systemd: bool = True, linger_rc: int = 0, bus: bool = True) -> tuple[subprocess.CompletedProcess, str]:
	run_dir = tmp / "run-systemd"
	if systemd:
		run_dir.mkdir()
	cgroup = tmp / "cgroup"
	(cgroup / "user.slice" / "user-1002.slice").mkdir(parents=True)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "loginctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	(bin_dir / "loginctl").chmod(0o755)
	log = tmp / "sudo.log"
	script = f'''
source "{HELPER}"
sudo() {{
	shift
	echo "$*" >> "{log}"
	case "$1" in
		loginctl) return {linger_rc} ;;
		test) {"return 0" if bus else "return 1"} ;;
	esac
	return 0
}}
sleep() {{ :; }}
sandbox_systemd_session ai-validation 1002
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "HOME": str(tmp), "SANDBOX_SYSTEMD_RUN_DIR": str(run_dir),
			"SANDBOX_CGROUP_ROOT": str(cgroup), "SANDBOX_SESSION_WAIT_SECS": "2"})
	return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_the_systemd_session_is_lingered_and_waits_for_the_user_bus(tmp_path: Path) -> None:
	result, sudo_log = _session(tmp_path)
	assert result.returncode == 0, result.stderr
	assert "loginctl enable-linger ai-validation" in sudo_log
	assert "systemctl start user@1002.service" in sudo_log
	assert "test -S /run/user/1002/bus" in sudo_log


@pytest.mark.parametrize("kwargs", ({"systemd": False}, {"linger_rc": 1}, {"bus": False}))
def test_the_systemd_session_reports_unavailable(tmp_path: Path, kwargs: dict) -> None:
	result, _ = _session(tmp_path, **kwargs)
	assert result.returncode == 1


def test_dockerd_gets_the_user_bus_or_the_cgroupfs_driver(tmp_path: Path) -> None:
	log = tmp_path / "sudo.log"
	script = f'''
source "{HELPER}"
sudo() {{ printf '%s\\n' "$*" >> "{log}"; }}
rootless_docker_ready() {{ return 0; }}
start_rootless_dockerd systemd /home/ai-validation /run/user/1002
start_rootless_dockerd cgroupfs /home/ai-validation /run/user/1002
wait
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
	assert result.returncode == 0, result.stderr
	systemd_line, cgroupfs_line = sorted(log.read_text(encoding="utf-8").splitlines(), key=lambda line: "cgroupfs" in line)
	assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1002/bus" in systemd_line
	assert "native.cgroupdriver" not in systemd_line
	assert "XDG_RUNTIME_DIR=/run/user/1002" in cgroupfs_line
	assert cgroupfs_line.endswith("setsid dockerd-rootless.sh --exec-opt native.cgroupdriver=cgroupfs")
	assert "DBUS_SESSION_BUS_ADDRESS" not in cgroupfs_line


def test_stopping_the_daemon_signals_only_as_the_sandbox_user() -> None:
	text = HELPER.read_text(encoding="utf-8")
	body = text.split("stop_rootless_dockerd()", 1)[1].split("\n}\n", 1)[0]
	for line in body.splitlines():
		if "pkill" in line or "pgrep" in line or "rm -rf" in line:
			assert 'sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" --' in line, line


def test_a_refused_probe_image_import_counts_as_a_daemon_refusal(tmp_path: Path) -> None:
	script = f'''
source "{HELPER}"
as_sandbox() {{
	case "$*" in
		"docker image inspect"*) return 1 ;;
		"docker import"*) cat >/dev/null; echo "Error response from daemon: import refused by test" >&2; return 1 ;;
		"docker run"*) echo "docker run must not be reached" >&2; return 0 ;;
	esac
}}
rootless_docker_container_probe
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
	if shutil.which("ldd") is None or not Path("/usr/bin/true").exists():
		assert result.returncode == 2, result.stderr
		return
	assert result.returncode == 1, result.stderr
	assert "probe_exit=1 stage=import detail=Error response from daemon: import refused by test" in result.stderr
	assert "docker run must not be reached" not in result.stderr


def test_docker_info_ok_but_container_start_refused_fails_provision_before_any_harness(tmp_path: Path) -> None:
	"""#7063: `docker info` answers, `docker run` hits the cgroup error under both drivers.

	Uses the real probe (only the docker CLI is stubbed) and asserts that nothing after
	provisioning, standing in for the harness launch, ever runs.
	"""
	calls = tmp_path / "calls"
	status = tmp_path / "status"
	script = f'''
source "{HELPER}"
sudo() {{ :; }}
build_sandbox_env() {{ :; }}
sandbox_systemd_session() {{ return 0; }}
sandbox_release_systemd_session() {{ :; }}
DAEMON=""
start_rootless_dockerd() {{ echo "start $1" >> "{calls}"; DAEMON="$1"; }}
stop_rootless_dockerd() {{ echo "stop" >> "{calls}"; DAEMON=""; }}
as_sandbox() {{
	case "$*" in
		"docker info"*) [ -n "${{DAEMON}}" ] ;;
		"docker image inspect"*) return 0 ;;
		"docker run"*) echo "run ${{DAEMON}}" >> "{calls}"; echo "docker: Error response from daemon: open /sys/fs/cgroup/user.slice/user-1002.slice/cgroup.controllers: no such file or directory" >&2; return 127 ;;
	esac
}}
sandbox_start_verified_dockerd ai-validation 1002 /home/ai-validation /run/user/1002
echo "HARNESS_LAUNCHED" >> "{calls}"
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(status),
			"VALIDATION_HARNESS_SANDBOX_CGROUP_MODE": "auto"})
	assert result.returncode == 3, result.stderr
	lines = calls.read_text(encoding="utf-8").splitlines()
	assert lines == ["start systemd", "run systemd", "stop", "start cgroupfs", "run cgroupfs"]
	assert "HARNESS_LAUNCHED" not in lines
	assert status.read_text(encoding="utf-8") == "fail provision container_start_failed\n"


def test_a_non_integer_session_wait_falls_back_to_the_default(tmp_path: Path) -> None:
	result = subprocess.run(["bash", "-c", f'source "{HELPER}"; printf "%s" "${{SANDBOX_SESSION_WAIT_SECS}}"'],
		capture_output=True, text=True, timeout=60,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SANDBOX_SESSION_WAIT_SECS": "15s"})
	assert result.returncode == 0, result.stderr
	assert result.stdout == "15"


def test_an_unrecognized_cgroup_mode_is_logged_and_uses_cgroupfs(tmp_path: Path) -> None:
	# A typo must never opt into the systemd user session (#7380).
	result, calls = _driver(tmp_path, session=True, probe={"cgroupfs": 0}, mode="autoo")
	assert result.returncode == 0, result.stderr
	assert "outcome=ok reason=cgroup_mode_unrecognized_using_cgroupfs" in result.stderr
	assert calls == ["release ai-validation 1002", "start cgroupfs", "probe cgroupfs"]


def _release(tmp: Path, *, manager: bool = True, linger: bool = True, loginctl_rc: int = 0) -> tuple[subprocess.CompletedProcess, str]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "loginctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
	(bin_dir / "loginctl").chmod(0o755)
	log = tmp / "sudo.log"
	stopped = tmp / "stopped"
	script = f'''
source "{HELPER}"
sudo() {{
	shift
	echo "$*" >> "{log}"
	case "$1" in
		loginctl) return {loginctl_rc} ;;
		systemctl) : > "{stopped}" ;;
		pgrep) {"[ -e '" + str(stopped) + "' ] || echo 77" if manager else ":"} ;;
		test) {"return 0" if linger else "return 1"} ;;
	esac
	return 0
}}
sleep() {{ :; }}
sandbox_release_systemd_session ai-validation 1002
echo "rc=$?"
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
		env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "HOME": str(tmp), "SANDBOX_SESSION_WAIT_SECS": "2"})
	return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_the_release_disables_linger_and_stops_the_user_manager(tmp_path: Path) -> None:
	result, sudo_log = _release(tmp_path)
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "loginctl disable-linger ai-validation" in sudo_log
	assert "systemctl stop user@1002.service" in sudo_log
	assert "outcome=ok reason=systemd_user_session_released" in result.stderr


def test_the_release_returns_zero_when_loginctl_fails(tmp_path: Path) -> None:
	result, sudo_log = _release(tmp_path, loginctl_rc=1)
	assert result.returncode == 0, result.stderr
	assert "rc=0" in result.stdout
	assert "systemctl stop user@1002.service" in sudo_log


def test_the_release_does_nothing_when_no_manager_or_linger_exists(tmp_path: Path) -> None:
	result, sudo_log = _release(tmp_path, manager=False, linger=False)
	assert "rc=0" in result.stdout
	assert "loginctl" not in sudo_log and "systemctl" not in sudo_log
	assert "systemd_user_session_released" not in result.stderr

# --- PID namespace for the harness (#7349, finding validation-harness-credential-inheritance) ---
# The harness and the namespace self-check start in a new PID and mount
# namespace created by root, so a test cannot list runner processes or read
# their /proc entries. No namespace means fail closed, never a host launch.


def test_the_harness_launches_only_inside_a_pid_namespace() -> None:
	launcher = _function_body("as_sandbox_in_pidns")
	assert "exec unshare --pid --fork --mount-proc --propagation private --kill-child -- " in launcher
	assert "unshare --pid --fork --mount-proc --propagation private --kill-child -- true" in _function_body("pidns_supported")
	assert 'sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- env -i "${SANDBOX_ENV_ARGS[@]}" "$@"' in launcher
	run = _function_body("cmd_run")
	assert 'as_sandbox_in_pidns "${pidfile}" "${started}" setsid --wait bash -c' in run
	assert "as_sandbox setsid" not in run
	assert ".pgid" not in run
	assert 'sudo -n test -f "${started}" || sandbox_fail run pid_namespace_launch_failed' in run
	assert run.index("pidns_supported run") < run.index("python3 -I -c \"${STAGE_PY}\"")
	# as_sandbox itself is unchanged and is no longer used to start the harness.
	assert _function_body("as_sandbox").count("sudo -n -u") == 1


def test_provision_and_selfcheck_require_a_pid_namespace() -> None:
	provision = _function_body("cmd_provision")
	assert provision.index("pidns_supported provision") < provision.index("sandbox_log provision ok")
	assert provision.index("ensure_pidns_state_dir provision") < provision.index("sandbox_log provision ok")
	selfcheck = _function_body("cmd_selfcheck")
	assert "pidns_supported selfcheck" in selfcheck
	assert 'as_sandbox_in_pidns - - bash -c "${SELFCHECK_PIDNS_PROBE}" pidns-selfcheck "${caller_pid}" "${host_ns}"' in selfcheck
	assert selfcheck.index("pidns_supported selfcheck") < selfcheck.index("sandbox_log selfcheck ok")


def _pidns_probe() -> str:
	text = HELPER.read_text(encoding="utf-8")
	return text.split("<<'PIDNS_PROBE' || true\n", 1)[1].split("\nPIDNS_PROBE\n", 1)[0]


def test_the_namespace_probe_names_each_isolation_failure() -> None:
	probe = _pidns_probe()
	for token in ("pid_namespace_shared", "caller_pid_visible", "host_init_visible", "user_manager_reachable"):
		assert f"fail {token}" in probe, token


def _run_probe(*args: str) -> str:
	result = subprocess.run(["bash", "-c", _pidns_probe(), "probe", *args], capture_output=True, text=True, timeout=30)
	return result.stdout.strip().splitlines()[-1]


def test_the_namespace_probe_fails_in_the_host_namespace() -> None:
	host_ns = os.readlink(f"/proc/{os.getpid()}/ns/pid")
	assert _run_probe("", host_ns) == "pid_namespace_shared"
	assert _run_probe("", "") == "pid_namespace_shared"


def test_the_namespace_probe_fails_when_the_caller_is_visible() -> None:
	# A fake host namespace id passes the first check; a separate "caller"
	# process is visible and readable in the shared /proc. (The probe skips
	# its own pid and its parent's, which belong to the namespace.)
	caller = subprocess.Popen(["sleep", "30"])
	try:
		assert _run_probe(str(caller.pid), "pid:[0]") == "caller_pid_visible"
	finally:
		caller.kill()
		caller.wait()


def _sourced(tmp: Path, body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
	script = f'''
source "{HELPER}"
id() {{ case "$1" in -un) echo someone-else ;; *) echo 4242 ;; esac; }}
sandbox_home() {{ echo "{tmp}/home"; }}
{body}
'''
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=tmp,
		env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp),
			"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(tmp / "status"),
			"VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR": str(tmp / "state"), **(env or {})})


def test_selfcheck_fails_closed_when_no_pid_namespace_can_be_created(tmp_path: Path) -> None:
	result = _sourced(tmp_path, '''
as_sandbox() { echo ok; }
sudo() { shift; case "$1" in true) return 0 ;; unshare) return 1 ;; esac; return 0; }
cmd_selfcheck 1
echo REACHED
''')
	assert result.returncode == 3, result.stderr
	assert "phase=selfcheck outcome=fail reason=pid_namespace_unavailable" in result.stderr
	assert "REACHED" not in result.stdout
	assert (tmp_path / "status").read_text(encoding="utf-8") == "fail selfcheck pid_namespace_unavailable\n"


def test_selfcheck_fails_when_the_namespace_probe_does(tmp_path: Path) -> None:
	result = _sourced(tmp_path, '''
as_sandbox() { echo ok; }
as_sandbox_in_pidns() { echo caller_pid_visible; }
sudo() { shift; return 0; }
cmd_selfcheck 1
''')
	assert result.returncode == 3, result.stderr
	assert "reason=caller_pid_visible" in result.stderr


@pytest.mark.parametrize("state_dir", ("relative/state", "/tmp/a/../b", "/tmp/bad dir", "/run", "/tmp", "/run//", "/run/x/", "//run/x"))
def test_an_invalid_state_dir_is_refused(state_dir: str) -> None:
	result = _run(["selfcheck"], env={"VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR": state_dir,
		"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": "/dev/null"})
	assert result.returncode == 3
	assert b"reason=invalid_pidns_state_dir" in result.stderr


def test_a_state_dir_under_the_sandbox_home_is_refused(tmp_path: Path) -> None:
	result = _sourced(tmp_path, "validate_config run", env={"VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR": str(tmp_path / "home" / "state")})
	assert result.returncode == 3
	assert "reason=invalid_pidns_state_dir" in result.stderr


@pytest.mark.parametrize("meta", ("0 755 directory", "1000 700 directory", "0 700 regular file", ""))
def test_an_existing_state_dir_that_is_not_root_0700_is_refused(tmp_path: Path, meta: str) -> None:
	# `install -d` would chmod/chown an existing directory (e.g. /run/user).
	calls = tmp_path / "calls"
	result = _sourced(tmp_path, f'''
sudo() {{
	shift
	case "$1" in
		test) [ "$2" = -e ] && return 0; return 1 ;;
		stat) echo "{meta}" ;;
		install) echo install >> "{calls}" ;;
	esac
	return 0
}}
ensure_pidns_state_dir run
echo REACHED
''')
	assert result.returncode == 3, result.stderr
	assert "reason=pidns_state_dir_failed" in result.stderr
	assert "REACHED" not in result.stdout
	assert not calls.exists(), "an existing directory must never be passed to install"


def test_an_existing_root_0700_state_dir_is_reused_without_install(tmp_path: Path) -> None:
	calls = tmp_path / "calls"
	result = _sourced(tmp_path, f'''
sudo() {{
	shift
	case "$1" in
		test) [ "$2" = -e ] && return 0; return 1 ;;
		stat) echo "0 700 directory" ;;
		install) echo install >> "{calls}" ;;
	esac
	return 0
}}
ensure_pidns_state_dir run
echo REACHED
''')
	assert result.returncode == 0, result.stderr
	assert "REACHED" in result.stdout
	assert not calls.exists()


# The rootless daemon gets its own PID namespace (rootlesskit --pidns), so a
# `--pid=host` container cannot list runner processes; selfcheck verifies it.
def test_the_rootless_daemon_starts_in_its_own_pid_namespace() -> None:
	body = _function_body("start_rootless_dockerd")
	assert '"DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--pidns"' in body
	selfcheck = _function_body("cmd_selfcheck")
	assert 'docker_pidns_isolated "${host_ns}" || sandbox_fail selfcheck docker_pid_namespace_shared' in selfcheck
	assert selfcheck.index("docker_pidns_isolated") < selfcheck.index("sandbox_log selfcheck ok")


def _docker_ns(tmp: Path, pids: str, ns: dict[str, str], host: str = "pid:[1]") -> int:
	cases = "".join(f"\t\t\t/proc/{pid}/ns/pid) echo '{value}' ;;\n" for pid, value in ns.items())
	result = _sourced(tmp, f'''
sudo() {{
	shift
	case "$1" in
		pgrep) printf '%b' '{pids}' ;;
		readlink)
			case "$2" in
{cases}			esac ;;
	esac
	return 0
}}
docker_pidns_isolated '{host}'
''')
	return result.returncode


def test_docker_pidns_isolated_accepts_only_namespaced_daemons(tmp_path: Path) -> None:
	assert _docker_ns(tmp_path, "55\\n", {"55": "pid:[2]"}) == 0
	assert _docker_ns(tmp_path, "55\\n", {"55": "pid:[1]"}) == 1
	assert _docker_ns(tmp_path, "", {}) == 1, "no daemon is not proof of isolation"
	assert _docker_ns(tmp_path, "55\\n56\\n", {"55": "pid:[2]", "56": "pid:[1]"}) == 1
	assert _docker_ns(tmp_path, "55\\n", {}) == 1, "an unreadable namespace fails closed"


def test_selfcheck_fails_when_the_daemon_shares_the_host_pid_namespace(tmp_path: Path) -> None:
	result = _sourced(tmp_path, '''
as_sandbox() { echo ok; }
as_sandbox_in_pidns() { echo ok; }
sudo() {
	shift
	case "$1" in
		pgrep) echo 55 ;;
		readlink) readlink "/proc/$$/ns/pid" ;;
	esac
	return 0
}
cmd_selfcheck 1
echo REACHED
''')
	assert result.returncode == 3, result.stderr
	assert "phase=selfcheck outcome=fail reason=docker_pid_namespace_shared" in result.stderr
	assert "REACHED" not in result.stdout


# --- No reachable systemd user manager (#7380) ---------------------------------
# A container started through the rootless daemon is outside the harness's
# mount namespace; a user manager for the sandbox uid would let it start a
# transient unit in the host PID namespace. selfcheck refuses while one exists.

def _route_selfcheck(tmp: Path, *, process: str = "", bus: bool = False, socket: bool = False) -> subprocess.CompletedProcess:
	return _sourced(tmp, f'''
as_sandbox() {{ echo ok; }}
as_sandbox_in_pidns() {{ echo ok; }}
sudo() {{
	shift
	case "$1" in
		pgrep)
			case "$*" in
				*"-x dockerd"*) echo 55 ;;
				*"-x {process or 'none'}") echo 77 ;;
			esac ;;
		readlink) case "$2" in /proc/55/ns/pid) echo "pid:[999]" ;; *) readlink "$2" ;; esac ;;
		test) case "$*" in "test -S "*/bus) {"return 0" if bus else "return 1"} ;; esac ;;
		find) {"echo /run/user/4242/systemd/private" if socket else ":"} ;;
	esac
	return 0
}}
cmd_selfcheck 1
echo REACHED
''')


@pytest.mark.parametrize("kwargs", ({"process": "systemd"}, {"process": "dbus-daemon"}, {"process": "dbus-broker"}, {"bus": True}, {"socket": True}))
def test_selfcheck_fails_while_a_user_manager_is_reachable(tmp_path: Path, kwargs: dict) -> None:
	result = _route_selfcheck(tmp_path, **kwargs)
	assert result.returncode == 3, result.stderr
	assert "phase=selfcheck outcome=fail reason=user_manager_reachable_from_container" in result.stderr
	assert "REACHED" not in result.stdout
	assert (tmp_path / "status").read_text(encoding="utf-8") == "fail selfcheck user_manager_reachable_from_container\n"


def test_selfcheck_passes_without_a_user_manager(tmp_path: Path) -> None:
	result = _route_selfcheck(tmp_path)
	assert result.returncode == 0, result.stderr
	assert "phase=selfcheck outcome=ok" in result.stderr
	assert "REACHED" in result.stdout


def test_an_unresolvable_uid_fails_the_route_check_closed(tmp_path: Path) -> None:
	result = _sourced(tmp_path, '''
sandbox_uid() { return 1; }
rc=0; user_manager_route_open || rc=$?
echo "rc=${rc}"
''')
	assert "rc=1" in result.stdout


def test_a_denied_test_command_fails_the_route_check_closed(tmp_path: Path) -> None:
	# sudo denying `test` exits 1, like "no such socket"; it must not read as closed.
	result = _sourced(tmp_path, '''
sandbox_uid() { echo 4242; }
sudo() { shift; case "$1" in pgrep) return 1 ;; test) return 1 ;; esac; return 0; }
rc=0; user_manager_route_open || rc=$?
echo "rc=${rc}"
''')
	assert "rc=1" in result.stdout


@pytest.mark.parametrize("failing", ("pgrep", "find"))
def test_a_failed_inspection_fails_the_route_check_closed(tmp_path: Path, failing: str) -> None:
	# An inspection error must not read as "no user manager" (#7380 review).
	result = _sourced(tmp_path, f'''
sandbox_uid() {{ echo 4242; }}
sudo() {{
	shift
	case "$1" in
		pgrep) {"return 3" if failing == "pgrep" else "return 1"} ;;
		test) case "$*" in "test -S "*) return 1 ;; esac; return 0 ;;
		find) {"return 1" if failing == "find" else "return 0"} ;;
	esac
	return 0
}}
rc=0; user_manager_route_open || rc=$?
echo "rc=${{rc}}"
''')
	assert "rc=1" in result.stdout


def test_the_route_check_runs_after_the_docker_check_and_before_ok() -> None:
	selfcheck = _function_body("cmd_selfcheck")
	assert "user_manager_route_open" in selfcheck
	assert 'sandbox_fail selfcheck user_manager_reachable_from_container' in selfcheck
	assert selfcheck.index("docker_pidns_isolated") < selfcheck.index("user_manager_route_open") < selfcheck.index("sandbox_log selfcheck ok")


def _cleanup(tmp: Path, *, meta: str = "0 regular file", pid: str = "4321", comm: str = "unshare", owner: str = "0") -> tuple[int, list[str]]:
	calls = tmp / "calls"
	result = _sourced(tmp, f'''
as_sandbox() {{ return 0; }}
sudo() {{
	shift
	local last="${{@: -1}}"
	case "$1" in
		true) return 0 ;;
		stat) case "$last" in *.pid) echo "{meta}" ;; /proc/*) echo "{owner}" ;; esac ;;
		cat) case "$last" in *.pid) echo "{pid}" ;; */comm) echo "{comm}" ;; esac ;;
		pgrep) echo 4322 ;;
		kill) echo "kill $*" >> "{calls}" ;;
		test) return 1 ;;
	esac
	return 0
}}
cmd_cleanup
''')
	assert "outcome=ok" in result.stderr, result.stderr
	return result.returncode, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def test_cleanup_kills_the_recorded_namespace(tmp_path: Path) -> None:
	rc, calls = _cleanup(tmp_path)
	assert rc == 0
	assert calls == ["kill kill -KILL -- 4322", "kill kill -KILL -- 4321"]


@pytest.mark.parametrize("kwargs", (
	{"pid": "1"}, {"pid": "0"}, {"pid": "12;id"}, {"comm": "bash"}, {"owner": "1000"},
	{"meta": "1000 regular file"}, {"meta": "0 symbolic link"}, {"meta": ""},
))
def test_cleanup_kills_nothing_it_cannot_verify(tmp_path: Path, kwargs: dict) -> None:
	rc, calls = _cleanup(tmp_path, **kwargs)
	assert rc == 0
	assert calls == []


# Fake sudo/unshare binaries: sudo strips its options (and -o/-g for install)
# and runs the command as the test user; unshare skips to `--`.
FAKE_SUDO = """#!/bin/bash
while [ "$#" -gt 0 ]; do case "$1" in -n) shift ;; -u) shift 2 ;; --) shift; break ;; *) break ;; esac; done
if [ "$1" = install ]; then
	shift; args=()
	while [ "$#" -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
	exec install "${args[@]}"
fi
exec "$@"
"""
FAKE_UNSHARE = """#!/bin/bash
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
shift
case "${FAKE_UNSHARE_MODE}" in
	fail) exit 1 ;;
	nolaunch) [ "$1" = true ] && exit 0; exit 1 ;;
esac
exec "$@"
"""


def _run_harness(tmp: Path, mode: str) -> tuple[subprocess.CompletedProcess, Path]:
	if shutil.which("setsid") is None or shutil.which("git") is None:
		pytest.skip("needs setsid and git")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	for name, body in (("sudo", FAKE_SUDO), ("unshare", FAKE_UNSHARE)):
		(bin_dir / name).write_text(body, encoding="utf-8")
		(bin_dir / name).chmod(0o755)
	ws = tmp / "ws"
	(ws / "validation").mkdir(parents=True)
	subprocess.run(["git", "init", "-q", str(ws)], check=True, capture_output=True)
	marker = tmp / "ran"
	(ws / "validation" / "entry.sh").write_text(
		f"touch {marker}\nmkdir -p validation/logs\necho hi > validation/logs/t.log\nexit 7\n", encoding="utf-8")
	script = f'''
source "{HELPER}"
id() {{ case "$1" in -un) echo someone-else ;; *) echo 4242 ;; esac; }}
sandbox_home() {{ echo "{tmp}/home"; }}
rootless_docker_ready() {{ return 0; }}
cmd_run validation/entry.sh
'''
	result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=ws,
		env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "HOME": str(tmp), "FAKE_UNSHARE_MODE": mode,
			"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(tmp / "status"),
			"VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR": str(tmp / "state")})
	return result, marker


def test_run_launches_the_harness_through_the_namespace_and_returns_its_exit(tmp_path: Path) -> None:
	result, marker = _run_harness(tmp_path, "ok")
	assert result.returncode == 7, result.stderr
	assert marker.exists()
	assert (tmp_path / "ws" / "validation" / "logs" / "t.log").read_text(encoding="utf-8") == "hi\n"
	assert (tmp_path / "status").read_text(encoding="utf-8") == "ok run 7\n"
	# The launch state is removed once the harness has finished.
	assert list((tmp_path / "state").iterdir()) == []


def test_run_fails_closed_when_no_pid_namespace_can_be_created(tmp_path: Path) -> None:
	result, marker = _run_harness(tmp_path, "fail")
	assert result.returncode == 3, result.stderr
	assert "phase=run outcome=fail reason=pid_namespace_unavailable" in result.stderr
	assert (tmp_path / "status").read_text(encoding="utf-8") == "fail run pid_namespace_unavailable\n"
	assert not marker.exists(), "the harness must never run without the namespace"


def test_run_fails_closed_when_the_namespace_launch_never_starts(tmp_path: Path) -> None:
	result, marker = _run_harness(tmp_path, "nolaunch")
	assert result.returncode == 3, result.stderr
	assert "phase=run outcome=fail reason=pid_namespace_launch_failed" in result.stderr
	assert not marker.exists()


# The sandbox user's systemd user manager (auto cgroup mode) runs in the host
# PID namespace; `systemd-run --user` through its sockets would start commands
# outside the harness namespace. Root masks both sockets before the drop.
def _mksock(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	os.mknod(path, stat.S_IFSOCK | 0o600)


# Fake `mount --bind /dev/null <path>` ($3 is the path): `mask` replaces the
# socket with a regular file, as a real bind mount hides it; `fail` refuses;
# `noop` succeeds without masking; `vanish` loses the race (path gone, exit 1).
FAKE_MOUNT_ACTIONS = {
	"mask": 'rm -f "$3" && : > "$3"; exit 0',
	"fail": "exit 1",
	"noop": "exit 0",
	"vanish": 'rm -f "$3"; exit 1',
}


def _pidns_launch(tmp: Path, bus: str, private: str = "systemd/private", mount_mode: str = "mask") -> tuple[str, list[str], Path]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	for name, body in (("sudo", FAKE_SUDO), ("unshare", FAKE_UNSHARE),
			("mount", f'#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOUNT_LOG"\n{FAKE_MOUNT_ACTIONS[mount_mode]}\n')):
		(bin_dir / name).write_text(body, encoding="utf-8")
		(bin_dir / name).chmod(0o755)
	runtime = tmp / "rt"
	runtime.mkdir()
	_mksock(runtime / private)
	_mksock(runtime / "docker.sock")
	(runtime / "notes.txt").write_text("", encoding="utf-8")
	if bus == "socket":
		_mksock(runtime / "bus")
	elif bus == "symlink":
		(runtime / "bus").symlink_to(tmp / "elsewhere")
	started = tmp / "started"
	result = _sourced(tmp, f'''
sandbox_runtime_dir() {{ echo "{runtime}"; }}
SANDBOX_ENV_ARGS=("PATH=$PATH")
rc=0
as_sandbox_in_pidns - "{started}" true || rc=$?
echo "rc=${{rc}}"
''', env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "MOUNT_LOG": str(tmp / "mounts")})
	mounts = tmp / "mounts"
	return result.stdout.strip().splitlines()[-1], sorted(mounts.read_text(encoding="utf-8").splitlines()) if mounts.exists() else [], runtime


def test_the_namespace_masks_the_user_manager_sockets_before_the_drop(tmp_path: Path) -> None:
	rc, mounts, runtime = _pidns_launch(tmp_path, "socket")
	assert rc == "rc=0"
	# Every socket except docker.sock; regular files are left alone.
	assert mounts == [f"--bind /dev/null {runtime}/bus", f"--bind /dev/null {runtime}/systemd/private"]
	assert (tmp_path / "started").exists()
	launcher = _function_body("as_sandbox_in_pidns")
	assert launcher.index('find "$2" -xdev -ignore_readdir_race -type s ! -path "$2/docker.sock"') < launcher.index(': > "$1"')


def test_both_socket_rechecks_stat_each_entry() -> None:
	# Runs even where the real bind-mount tests below skip: plain `-type s`
	# trusts the directory entry and lists masked sockets (#7355).
	launcher = _function_body("as_sandbox_in_pidns")
	assert '-ignore_readdir_race ! -type l -xtype s ! -path "$2/docker.sock" -print -quit' in launcher
	assert '! -type l -xtype s ! -path "${rt}/docker.sock" -print -quit' in _pidns_probe()


def test_a_renamed_user_manager_socket_is_still_masked(tmp_path: Path) -> None:
	# The sandbox user owns the runtime dir and can rename systemd/; the mask
	# finds sockets wherever they are, not at fixed paths.
	rc, mounts, runtime = _pidns_launch(tmp_path, "none", private="systemd2/private")
	assert rc == "rc=0"
	assert mounts == [f"--bind /dev/null {runtime}/systemd2/private"]


def test_a_failed_socket_mask_stops_the_launch(tmp_path: Path) -> None:
	rc, mounts, _ = _pidns_launch(tmp_path, "socket", mount_mode="fail")
	assert rc == "rc=125"
	assert mounts, "the mask was attempted"
	assert not (tmp_path / "started").exists(), "no launch marker means run fails with pid_namespace_launch_failed"


def test_a_socket_that_vanishes_before_the_mask_is_skipped(tmp_path: Path) -> None:
	# A socket removed between `find` and `mount` (a containerd shim cleaning
	# up) holds nothing to reach; it must not fail the launch closed.
	launcher = _function_body("as_sandbox_in_pidns")
	mask = re.search(r"'(for s do \[ -e \"\$s\" \] \|\| continue; mount [^']*)'", launcher)
	assert mask is not None
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "mount").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOUNT_LOG"\n', encoding="utf-8")
	(bin_dir / "mount").chmod(0o755)
	present = tmp_path / "present"
	present.write_text("", encoding="utf-8")
	result = subprocess.run(["sh", "-c", mask.group(1), "mask", str(tmp_path / "gone"), str(present)],
		capture_output=True, text=True, timeout=30,
		env={"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "MOUNT_LOG": str(tmp_path / "mounts")})
	assert result.returncode == 0, result.stderr
	assert (tmp_path / "mounts").read_text(encoding="utf-8").splitlines() == [f"--bind /dev/null {present}"]


def test_a_socket_left_unmasked_stops_the_launch(tmp_path: Path) -> None:
	# A socket renamed between `find` and `mount` is skipped at its old name;
	# the recheck after the mask must still refuse the launch.
	rc, mounts, _ = _pidns_launch(tmp_path, "socket", mount_mode="noop")
	assert rc == "rc=125"
	assert mounts, "the mask was attempted"
	assert not (tmp_path / "started").exists(), "no launch marker means run fails with pid_namespace_launch_failed"


def test_a_socket_removed_between_check_and_mount_does_not_fail_the_launch(tmp_path: Path) -> None:
	rc, mounts, runtime = _pidns_launch(tmp_path, "socket", mount_mode="vanish")
	assert rc == "rc=0"
	assert mounts == [f"--bind /dev/null {runtime}/bus", f"--bind /dev/null {runtime}/systemd/private"]
	assert (tmp_path / "started").exists()


def test_a_symlinked_user_manager_socket_stops_the_launch(tmp_path: Path) -> None:
	rc, mounts, _ = _pidns_launch(tmp_path, "symlink")
	assert rc == "rc=125"
	assert mounts == []
	assert not (tmp_path / "started").exists(), "no launch marker means run fails with pid_namespace_launch_failed"


# The fake `mount` above replaces the socket with a regular file, which also
# changes its directory entry. A real bind mount leaves the entry's type as
# "socket", and `find -type s` trusts that type, so the recheck refused every
# launch and selfcheck failed with probe_failed (#7355). These run the real
# launcher and probe text under a real bind mount in a private mount
# namespace, and skip when the host cannot create one.
def _real_mount_ns() -> list[str] | None:
	prefixes = [["unshare", "--user", "--map-root-user", "--mount"]]
	if os.geteuid() == 0:
		prefixes.append(["unshare", "--mount"])
	for prefix in prefixes:
		with tempfile.TemporaryDirectory() as scratch:
			probe = subprocess.run([*prefix, "--pid", "--fork", "--mount-proc", "--", "sh", "-c",
				': > "$1" && mount --bind /dev/null "$1"', "probe", str(Path(scratch) / "f")],
				capture_output=True, timeout=30)
		if probe.returncode == 0:
			return [*prefix, "--pid", "--fork", "--mount-proc", "--"]
	return None


def _launcher_scripts() -> tuple[str, str]:
	launcher = _function_body("as_sandbox_in_pidns")
	inner = re.search(r"\tsh -c '(for s in [^']*)' started ", launcher)
	mask = re.search(r"'(for s do \[ -e \"\$s\" \] \|\| continue; mount [^']*)'", launcher)
	assert inner is not None and mask is not None
	return inner.group(1), mask.group(1)


def _real_runtime(tmp: Path) -> Path:
	runtime = tmp / "rt"
	for name in ("docker.sock", "dockerd-rootless/api.sock", "docker/containerd/containerd.sock", "systemd/private", "bus"):
		_mksock(runtime / name)
	return runtime


@pytest.mark.parametrize("mask_works, expected_rc", [(True, 0), (False, 125)])
def test_the_launch_recheck_sees_through_a_real_bind_mount(tmp_path: Path, mask_works: bool, expected_rc: int) -> None:
	ns = _real_mount_ns()
	if ns is None:
		pytest.skip("cannot create a mount namespace on this host")
	inner, mask = _launcher_scripts()
	if not mask_works:
		mask = "true"
	runtime = _real_runtime(tmp_path)
	started = tmp_path / "started"
	result = subprocess.run([*ns, "sh", "-c", inner, "started", str(started), str(runtime), mask, "true"],
		capture_output=True, text=True, timeout=60)
	assert result.returncode == expected_rc, result.stderr
	assert started.exists() is mask_works


@pytest.mark.parametrize("masked, expected", [(True, "ok"), (False, "user_manager_reachable")])
def test_the_namespace_probe_sees_through_a_real_bind_mount(tmp_path: Path, masked: bool, expected: str) -> None:
	ns = _real_mount_ns()
	if ns is None:
		pytest.skip("cannot create a mount namespace on this host")
	runtime = _real_runtime(tmp_path)
	probe = _pidns_probe()
	anchor = 'rt="/run/user/$(id -u)"'
	assert anchor in probe
	probe = probe.replace(anchor, f'rt="{runtime}"')
	targets = [str(runtime / name) for name in ("dockerd-rootless/api.sock", "docker/containerd/containerd.sock", "systemd/private", "bus")]
	setup = 'for s do mount --bind /dev/null "$s" || exit 99; done' if masked else "true"
	result = subprocess.run([*ns, "sh", "-c", f'{setup}; exec bash -c "$PROBE" probe "" "pid:[0]"', "setup", *targets],
		capture_output=True, text=True, timeout=60, env={**os.environ, "PROBE": probe})
	assert result.stdout.strip().splitlines()[-1] == expected, result.stdout + result.stderr


# --- fallback runner isolation (#7350, finding validation-fallback-parent-credential-exposure) ---
# When scripts/validate_driver.sh is absent at startup, validate_process.sh runs
# its own fallback runner (VALIDATION_RUNNER_FILE under RUNTIME_DIR). It must
# reach the tests only through the credential-scrubbed sandbox launch, never on
# the host as the credentialed controller's user.

SANDBOX_LAUNCH = '"${VALIDATION_HARNESS_CREDENTIAL_SCRUB[@]}" bash "${VALIDATION_HARNESS_SANDBOX_SCRIPT}" run "${GENERATED_VALIDATE_SCRIPT_PATH}"'


def _phase3_slice(text: str) -> str:
	phase3 = text.split("# Phase 3: Execute validation harness", 1)[1].split("tail -n 200 \"${VALIDATION_LOG_FILE}\"", 1)[0]
	assert phase3.strip()
	return phase3


def test_validate_process_fallback_runner_launches_only_through_scrubbed_sandbox() -> None:
	text = PROCESS.read_text(encoding="utf-8")
	phase3 = _phase3_slice(text)
	# The fallback arm selects the trusted runtime runner; the canonical arm only the wrapper.
	assert re.search(
		r'if \[ "\$\{VALIDATION_USE_CANONICAL_WRAPPER\}" = "true" \]; then\s*\n'
		r'\s*GENERATED_VALIDATE_SCRIPT_PATH="validation/validate\.sh"\s*\n'
		r'\s*else\s*\n\s*ensure_runtime_validation_driver\s*\n'
		r'\s*GENERATED_VALIDATE_SCRIPT_PATH="\$\{VALIDATION_RUNNER_FILE\}"\s*\n\s*fi',
		phase3,
	), "fallback arm must resolve to VALIDATION_RUNNER_FILE"
	assignments = re.findall(r'^\s*GENERATED_VALIDATE_SCRIPT_PATH=(.*)$', phase3, re.M)
	assert assignments == ['"validation/validate.sh"', '"${VALIDATION_RUNNER_FILE}"']
	# Exactly one launch uses the selected entry, and it is the scrubbed sandbox run.
	uses = [line for line in phase3.splitlines() if '"${GENERATED_VALIDATE_SCRIPT_PATH}"' in line]
	assert len(uses) == 1, uses
	assert uses[0].lstrip().startswith(SANDBOX_LAUNCH), uses[0]
	# The credential scrub array starts with `env`, is defined and extended before the launch.
	launch_index = text.index(SANDBOX_LAUNCH)
	scrub_def = re.search(r'^VALIDATION_HARNESS_CREDENTIAL_SCRUB=\(\n\s*env\n', text, re.M)
	assert scrub_def is not None and scrub_def.start() < launch_index
	scrub_call = re.search(r'^validation_harness_scrub_append$', text, re.M)
	assert scrub_call is not None and scrub_call.start() < launch_index
	# Nothing in Phase 3 runs the runner or the selected entry directly.
	assert not re.search(
		r'^\s*(?:bash\s+|sh\s+|exec\s+|source\s+|\.\s+)?"?\$\{(?:VALIDATION_RUNNER_FILE|GENERATED_VALIDATE_SCRIPT_PATH)\}"?(\s|$)',
		phase3, re.M,
	)
	assert not re.search(r'^\s*(bash|sh)\s+"?\$\{?VALIDATION_RUNNER_FILE', phase3, re.M)


def test_sandbox_run_stages_outside_entry_as_overlay_and_rejects_dotdot() -> None:
	text = HELPER.read_text(encoding="utf-8")
	body = text.split("cmd_run()\n{", 1)[1].split("\n}\n", 1)[0]
	assert "sandbox_log run ok staged" in body, "cmd_run slice is wrong"
	assert 'entry_rel=".validation-harness/$(basename -- "${entry}")"' in body
	assert 'overlays+=("${entry_rel}=${entry}")' in body
	assert "*/../*) sandbox_fail run entry_unsafe ;;" in body
	stage = body.index('python3 -I -c "${STAGE_PY}"')
	assert body.index("require_sudo run") < stage
	assert body.index("rootless_docker_ready || sandbox_fail run rootless_docker_unavailable") < stage
	# The entry runs only from its staged copy inside the sandbox work dir.
	assert 'as_sandbox test -f "${work}/${entry_rel}"' in body
	assert 'as_sandbox_in_pidns "${pidfile}" "${started}" setsid --wait bash' in body
	for line in body.splitlines():
		assert not re.match(r'^\s*(?:bash\s+|sh\s+|exec\s+|source\s+|\.\s+)?"?\$\{entry\}"?(\s|$)', line), line


@pytest.mark.parametrize("sudo_body,extra_env,reasons", (
	("exit 1", {}, (b"sudo_unavailable",)),
	(
		'if [ "$1" = -n ] && [ "$2" = true ]; then exit 0; fi\nexit 1',
		{"VALIDATION_HARNESS_SANDBOX_USER": "nobody"},
		(b"rootless_docker_unavailable", b"sandbox_user_missing"),
	),
))
def test_sandbox_run_never_executes_an_outside_workspace_entry_on_the_host(sudo_body, extra_env, reasons) -> None:
	with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as runtime_dir:
		workspace = Path(workspace_dir)
		runtime = Path(runtime_dir)
		marker = runtime / "ran"
		# Mirrors the fallback runner validate_process.sh writes under RUNTIME_DIR.
		outside_entry = runtime / "runner.sh"
		outside_entry.write_text(f"#!/bin/bash\ntouch {marker}\n", encoding="utf-8")
		outside_entry.chmod(0o755)
		bin_dir = _stub_bin(runtime, sudo_body)
		status = runtime / "status"
		env = {
			"PATH": f"{bin_dir}:{os.environ['PATH']}",
			"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(status),
			**extra_env,
		}
		result = _run(["run", str(outside_entry)], env=env, cwd=workspace)
		assert result.returncode == 3, result.stderr
		assert b"VALIDATION_HARNESS_SANDBOX phase=run outcome=fail" in result.stderr
		assert any(reason in result.stderr for reason in reasons), result.stderr
		assert status.read_text().startswith("fail ")
		assert not marker.exists(), "the fallback runner must never run on the host"


# Runtime companions to the source-text checks above: a stub `sudo` runs the
# sandbox user's commands locally (and answers `docker info`), so cmd_run gets
# past its preflight and the staging, traversal and launch paths execute.
_RUNTIME_SUDO_STUB = """#!/bin/bash
[ "$1" = -n ] && shift
[ "$1" = -u ] && shift 2
[ "$1" = -- ] && shift
if [ "$1" = install ]; then
	shift; args=()
	while [ "$#" -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac; done
	exec install "${args[@]}"
fi
for word in "$@"; do
	case "${word}" in
		env|-i|*=*) continue ;;
		docker) exit 0 ;;
		*) break ;;
	esac
done
exec "$@"
"""


def _runtime_sandbox(root: Path) -> tuple[Path, Path, dict[str, str]]:
	if subprocess.run(["id", "-u", "nobody"], capture_output=True).returncode != 0:
		pytest.skip("needs a `nobody` account to stand in for the sandbox user")
	if subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip() == "nobody":
		pytest.skip("the test user must differ from the sandbox user")
	bin_dir = root / "bin"
	bin_dir.mkdir()
	home = root / "home"
	home.mkdir()
	(bin_dir / "sudo").write_text(_RUNTIME_SUDO_STUB, encoding="utf-8")
	(bin_dir / "unshare").write_text(FAKE_UNSHARE, encoding="utf-8")
	(bin_dir / "getent").write_text(f"#!/bin/sh\nprintf '%s:x:65534:65534::{home}:/bin/sh\\n' \"$2\"\n", encoding="utf-8")
	for stub in ("sudo", "unshare", "getent"):
		(bin_dir / stub).chmod(0o755)
	workspace = root / "workspace"
	workspace.mkdir()
	(workspace / "tracked.txt").write_text("tracked\n", encoding="utf-8")
	git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	subprocess.run(["git", "init", "-q", str(workspace)], check=True, env=git_env)
	subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True, env=git_env)
	env = {
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"VALIDATION_HARNESS_SANDBOX_USER": "nobody",
		"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(root / "status"),
		"VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST": str(root / "copyback"),
		"VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR": str(root / "state"),
		"FAKE_UNSHARE_MODE": "ok",
		"GITHUB_RUN_ID": "7350",
		"GITHUB_RUN_ATTEMPT": "1",
	}
	return home / "work" / "7350-1", workspace, env


def test_sandbox_run_executes_an_outside_entry_only_from_its_staged_overlay(tmp_path: Path) -> None:
	work, workspace, env = _runtime_sandbox(tmp_path)
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	outside_entry = runtime / "runner.sh"
	outside_entry.write_text(
		'#!/bin/bash\nmkdir -p validation/logs\nprintf "%s\\n%s\\n" "$(pwd -P)" "$0" > validation/logs/entry.txt\n',
		encoding="utf-8",
	)
	outside_entry.chmod(0o755)
	result = _run(["run", str(outside_entry)], env=env, cwd=workspace)
	assert result.returncode == 0, result.stderr
	assert b"phase=run outcome=ok reason=staged" in result.stderr
	staged = work / ".validation-harness" / "runner.sh"
	assert staged.read_bytes() == outside_entry.read_bytes(), "outside entry must be staged as an overlay"
	assert (work / "tracked.txt").read_text() == "tracked\n"
	ran_from, script = (tmp_path / "copyback" / "entry.txt").read_text().splitlines()
	assert ran_from == str(work.resolve())
	assert script == ".validation-harness/runner.sh", "the entry must run from its staged copy, not the host path"
	assert not (workspace / "validation").exists(), "nothing may be written into the host workspace"


@pytest.mark.parametrize("relative", (True, False))
def test_sandbox_run_rejects_a_dotdot_entry_before_staging(tmp_path: Path, relative: bool) -> None:
	work, workspace, env = _runtime_sandbox(tmp_path)
	marker = tmp_path / "ran"
	(tmp_path / "escape.sh").write_text(f"#!/bin/bash\ntouch {marker}\n", encoding="utf-8")
	entry = "validation/../../escape.sh"
	if not relative:
		entry = f"{workspace.resolve()}/{entry}"
	result = _run(["run", entry], env=env, cwd=workspace)
	assert result.returncode == 3, result.stderr
	assert b"phase=run outcome=fail reason=entry_unsafe" in result.stderr
	assert (tmp_path / "status").read_text().startswith("fail run entry_unsafe")
	assert not marker.exists()
	assert not work.exists(), "a rejected entry must not be staged"


# #7355 unblock round 1: an empty or malformed selfcheck probe output names the
# failing pass and the launcher's exit code, but only while the placeholder
# VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP is `true`.
_SELFCHECK_STDERR_MARKER = "SELFCHECK_STDERR_MARKER"
_SELFCHECK_SUDO_STUB = f"""#!/bin/sh
[ "$1" = -n ] || exit 1
case "$2" in
	true|unshare) exit 0 ;;
	-u) [ -n "${{FAKE_PLAIN_OUT:-}}" ] && printf '%s\\n' "$FAKE_PLAIN_OUT"; echo {_SELFCHECK_STDERR_MARKER} >&2; exit "${{FAKE_PLAIN_RC:-0}}" ;;
	sh) [ -n "${{FAKE_PIDNS_OUT:-}}" ] && printf '%s\\n' "$FAKE_PIDNS_OUT"; echo {_SELFCHECK_STDERR_MARKER} >&2; exit "${{FAKE_PIDNS_RC:-0}}" ;;
esac
exit 1
"""


def _selfcheck_env(tmp: Path, *, plain_rc: int = 0, plain_out: str = "", pidns_rc: int = 0, pidns_out: str = "", flag: str | None = None) -> dict[str, str]:
	if subprocess.run(["id", "-u", "nobody"], capture_output=True).returncode != 0:
		pytest.skip("needs a `nobody` account to stand in for the sandbox user")
	if subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip() == "nobody":
		pytest.skip("the test user must differ from the sandbox user")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	home = tmp / "home"
	home.mkdir()
	(bin_dir / "sudo").write_text(_SELFCHECK_SUDO_STUB, encoding="utf-8")
	(bin_dir / "getent").write_text(f"#!/bin/sh\nprintf '%s:x:65534:65534::{home}:/bin/sh\\n' \"$2\"\n", encoding="utf-8")
	for stub in ("sudo", "getent"):
		(bin_dir / stub).chmod(0o755)
	env = {
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"VALIDATION_HARNESS_SANDBOX_USER": "nobody",
		"VALIDATION_HARNESS_SANDBOX_STATUS_FILE": str(tmp / "status"),
		"VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR": str(tmp / "state"),
		"FAKE_PLAIN_RC": str(plain_rc),
		"FAKE_PLAIN_OUT": plain_out,
		"FAKE_PIDNS_RC": str(pidns_rc),
		"FAKE_PIDNS_OUT": pidns_out,
	}
	if flag is not None:
		env["VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP"] = flag
	return env


@pytest.mark.parametrize(
	("kwargs", "reason"),
	[
		({"flag": "true", "plain_rc": 125}, "probe_failed_plain_rc_125"),
		({"flag": "true", "plain_out": "ok", "pidns_rc": 126}, "probe_failed_pidns_rc_126"),
		({"flag": "true", "plain_out": "Bad Output!", "plain_rc": 0}, "probe_failed_plain_rc_0"),
		({"flag": "true", "plain_out": "ok", "pidns_out": "Bad Output!", "pidns_rc": 3}, "probe_failed_pidns_rc_3"),
		({"flag": "true", "plain_out": "proc_environ_readable", "plain_rc": 1}, "proc_environ_readable"),
		({"flag": "TRUE", "plain_rc": 125}, "probe_failed_plain_rc_125"),
		({"plain_rc": 125}, "probe_failed"),
		({"plain_out": "ok", "pidns_rc": 0}, "probe_failed"),
		({"flag": "false", "plain_rc": 125}, "probe_failed"),
		({"flag": "yes", "plain_rc": 125}, "probe_failed"),
		({"flag": "", "plain_out": "ok", "pidns_rc": 125}, "probe_failed"),
	],
)
def test_selfcheck_probe_failure_reason_names_pass_and_rc_only_behind_the_placeholder(tmp_path: Path, kwargs: dict, reason: str) -> None:
	env = _selfcheck_env(tmp_path, **kwargs)
	result = _run(["selfcheck", "1"], env=env, cwd=tmp_path)
	assert result.returncode == 3, result.stderr
	stderr = result.stderr.decode()
	assert f"VALIDATION_HARNESS_SANDBOX phase=selfcheck outcome=fail reason={reason}\n" in stderr
	status = (tmp_path / "status").read_text(encoding="utf-8")
	assert status == f"fail selfcheck {reason}\n"
	assert _SELFCHECK_STDERR_MARKER not in stderr
	assert _SELFCHECK_STDERR_MARKER not in status


def test_selfcheck_probe_failed_reason_rejects_unsafe_inputs() -> None:
	script = f'source "{HELPER}"; selfcheck_probe_failed_reason "$1" "$2"'
	env = {"PATH": os.environ["PATH"], "VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP": "true"}
	for args, expected in ((["plain", "7"], "probe_failed_plain_rc_7"), (["evil pass", "1"], "probe_failed_unknown_rc_1"), (["pidns", "12a"], "probe_failed_pidns_rc_unknown"), (["pidns", "1234"], "probe_failed_pidns_rc_unknown")):
		out = subprocess.run(["bash", "-c", script, "x", *args], capture_output=True, text=True, env=env, timeout=30)
		assert out.returncode == 0 and out.stdout.strip() == expected, (args, out)


def test_selfcheck_probe_launches_keep_stderr_discarded() -> None:
	selfcheck = _function_body("cmd_selfcheck")
	assert '"${GITHUB_WORKSPACE:-}" 2>/dev/null)" || probe_rc=$?' in selfcheck
	assert '"${caller_pid}" "${host_ns}" 2>/dev/null)" || probe_rc=$?' in selfcheck


def test_selfcheck_placeholder_flag_is_not_passed_to_the_sandbox() -> None:
	script = f'source "{HELPER}"; sandbox_env_is_allowed "$1" && ! sandbox_env_is_denied "$1"'
	for name, passed in (("VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP", False), ("VALIDATION_TEST_USERNAME", True)):
		out = subprocess.run(["bash", "-c", script, "x", name], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=30)
		assert (out.returncode == 0) is passed, (name, out)


# The screened copy (STAGE_PY) is what compose builds with `context: ..` and
# `COPY . /workspace`. These tests run the embedded program as cmd_run does,
# over a throwaway git repository, and read the tar it writes to stdout.
def _stage_program() -> str:
	out = subprocess.run(
		["bash", "-c", f'source "{HELPER}"; printf "%s" "${{STAGE_PY}}"'],
		capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=30,
	)
	assert out.returncode == 0 and "def add(path, arcname):" in out.stdout, out.stderr
	return out.stdout


def _stage_workspace(tmp_path: Path) -> tuple[Path, Path]:
	# A private copy of the trusted sibling, so `python3 -I` never writes
	# bytecode into the repository's scripts/ directory.
	helper_dir = tmp_path / "helper"
	helper_dir.mkdir()
	shutil.copy2(REPO_ROOT / "scripts" / "codex_isolated_workspace.py", helper_dir / "codex_isolated_workspace.py")
	workspace = tmp_path / "workspace"
	workspace.mkdir()
	git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	subprocess.run(["git", "init", "-q", str(workspace)], check=True, env=git_env)
	return helper_dir, workspace


def _git_add(workspace: Path, *names: str) -> None:
	git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	subprocess.run(["git", "-C", str(workspace), "add", "-f", "--", *names], check=True, env=git_env)


def _write(workspace: Path, name: str, text: str) -> None:
	path = workspace / name
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _stage(helper_dir: Path, workspace: Path) -> tuple[dict[str, bytes], str]:
	result = subprocess.run(
		["python3", "-I", "-c", _stage_program(), str(helper_dir), str(workspace)],
		capture_output=True, timeout=60, env={"PATH": os.environ["PATH"], "HOME": str(helper_dir)},
	)
	assert result.returncode == 0, result.stderr
	members: dict[str, bytes] = {}
	with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r|") as archive:
		for member in archive:
			assert member.isreg(), member.name
			handle = archive.extractfile(member)
			assert handle is not None
			members[member.name] = handle.read()
	return members, result.stderr.decode()


def test_stage_copy_includes_untracked_files_that_are_not_ignored(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, ".gitignore", "ignored.txt\nbuild/\n")
	_write(workspace, "tracked.txt", "tracked\n")
	_git_add(workspace, ".gitignore", "tracked.txt")
	# The bootstrap seeds this stub untracked (validation_template_bootstrap.py).
	_write(workspace, "scripts/run_validation_repo_checks.sh", "#!/bin/bash\nexit 0\n")
	_write(workspace, "ignored.txt", "ignored\n")
	_write(workspace, "build/out.js", "ignored\n")
	members, _ = _stage(helper_dir, workspace)
	assert members["tracked.txt"] == b"tracked\n"
	assert "scripts/run_validation_repo_checks.sh" in members, "untracked, non-ignored files must be staged"
	assert "ignored.txt" not in members and "build/out.js" not in members, "ignored files must stay out"


def test_stage_copy_admits_env_templates_but_not_real_env_files(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	for name in (".env.example", "app/.env.sample", "app/.env.template", ".env", ".env.local", "app/.env.production", ".env.example.local"):
		_write(workspace, name, "KEY=value\n")
	_git_add(workspace, ".env.example", "app/.env.sample", ".env", "app/.env.production")
	members, stderr = _stage(helper_dir, workspace)
	for name in (".env.example", "app/.env.sample", "app/.env.template"):
		assert name in members, name
		assert f"VALIDATION_HARNESS_SANDBOX phase=stage outcome=ok reason=env_template_admitted path={name}\n" in stderr
	for name in (".env", ".env.local", "app/.env.production", ".env.example.local"):
		assert name not in members, name
	assert "KEY=value" not in stderr, "logs must never carry file content"


def test_stage_copy_keeps_other_readonly_exclusions(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	for name in ("certs/server.pem", "deploy.key", ".config/tool.json", "secrets/.env.example", ".npmrc/.env.example", "ok.txt"):
		_write(workspace, name, "data\n")
	_git_add(workspace, "certs/server.pem", "deploy.key", ".config/tool.json", "secrets/.env.example", "ok.txt")
	members, _ = _stage(helper_dir, workspace)
	assert set(members) == {"ok.txt"}, sorted(members)


def test_stage_copy_admits_a_clean_npmrc(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	clean = "legacy-peer-deps=true\n# shared settings\nregistry=https://registry.npmjs.org/\nalways-auth=false\n"
	_write(workspace, ".npmrc", clean)
	_write(workspace, "web/.npmrc", "engine-strict=true\n")
	_git_add(workspace, ".npmrc")
	members, stderr = _stage(helper_dir, workspace)
	assert members[".npmrc"] == clean.encode()
	assert members["web/.npmrc"] == b"engine-strict=true\n"
	assert "VALIDATION_HARNESS_SANDBOX phase=stage outcome=ok reason=npmrc_admitted path=.npmrc\n" in stderr
	assert "legacy-peer-deps" not in stderr


@pytest.mark.parametrize("content", (
	"//registry.npmjs.org/:_authToken=abc123\n",
	"  //npm.pkg.github.com/:_authToken=${NPM_TOKEN}\n",
	"_authToken=abc123\n",
	"_auth=dXNlcjpwYXNz\n",
	"//registry.example.com/:_password=cGFzcw==\n",
	"//registry.example.com/:username=octo-user\n",
	"email=me@example.com\n",
	"certfile=/home/me/cert.pem\n",
	"keyfile=/home/me/key.pem\n",
	"cafile=/etc/ca.pem\n",
	"key=\"-----BEGIN PRIVATE KEY-----\\nabc\"\n",
	"cert = something\n",
	"registry=https://user:pass@registry.example.com/\n",
	"registry=${NPM_REGISTRY}\n",
	"legacy-peer-deps=true\nPASSWORD=hunter2\n",
))
def test_stage_copy_drops_an_npmrc_with_credential_material(tmp_path: Path, content: str) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, ".npmrc", content)
	_write(workspace, "ok.txt", "ok\n")
	_git_add(workspace, ".npmrc", "ok.txt")
	members, stderr = _stage(helper_dir, workspace)
	assert ".npmrc" not in members
	assert "ok.txt" in members
	assert "VALIDATION_HARNESS_SANDBOX phase=stage outcome=skip reason=npmrc_credential path=.npmrc\n" in stderr
	for line in content.splitlines():
		value = line.split("=", 1)[-1].strip()
		if value:
			assert value not in stderr, "logs must never carry file content"


def test_stage_copy_drops_a_symlinked_oversized_or_undecodable_npmrc(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "target.txt", "legacy-peer-deps=true\n")
	(workspace / ".npmrc").symlink_to("target.txt")
	_write(workspace, "big/.npmrc", "legacy-peer-deps=true\n" + "#" * (64 * 1024) + "\n")
	(workspace / "bin").mkdir()
	(workspace / "bin" / ".npmrc").write_bytes(b"legacy-peer-deps=\xff\xfe\n")
	_git_add(workspace, ".npmrc", "target.txt", "big/.npmrc", "bin/.npmrc")
	members, stderr = _stage(helper_dir, workspace)
	assert ".npmrc" not in members and "big/.npmrc" not in members and "bin/.npmrc" not in members
	assert "target.txt" in members
	assert "outcome=skip reason=npmrc_symlink path=.npmrc\n" in stderr
	assert "outcome=skip reason=npmrc_too_large path=big/.npmrc\n" in stderr
	assert "outcome=skip reason=npmrc_undecodable path=bin/.npmrc\n" in stderr


def test_stage_copy_logs_a_skip_for_a_symlinked_env_template(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "target.txt", "KEY=value\n")
	(workspace / ".env.example").symlink_to("target.txt")
	_git_add(workspace, ".env.example", "target.txt")
	members, stderr = _stage(helper_dir, workspace)
	assert ".env.example" not in members
	assert "VALIDATION_HARNESS_SANDBOX phase=stage outcome=skip reason=env_template_not_regular path=.env.example\n" in stderr


def test_stage_copy_keeps_ignored_special_files_under_validation_out(tmp_path: Path) -> None:
	# The validation/ walk adds files git ignores; the staging-only admissions
	# must not apply to them.
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, ".gitignore", "validation/.env.example\nvalidation/.npmrc\n")
	_git_add(workspace, ".gitignore")
	_write(workspace, "validation/.env.example", "KEY=value\n")
	_write(workspace, "validation/.npmrc", "legacy-peer-deps=true\n")
	_write(workspace, "validation/tests/ok.sh", "#!/bin/bash\n")
	members, stderr = _stage(helper_dir, workspace)
	assert "validation/tests/ok.sh" in members
	assert "validation/.env.example" not in members and "validation/.npmrc" not in members, sorted(members)
	assert "path=validation/.env.example" not in stderr and "path=validation/.npmrc" not in stderr


def test_stage_copy_leaves_the_shared_readonly_screen_unchanged() -> None:
	# Agent sandboxes use readonly_allowed too; the staging admissions live
	# only in STAGE_PY.
	import importlib.util
	spec = importlib.util.spec_from_file_location("cw_isolated_for_stage_test", REPO_ROOT / "scripts" / "codex_isolated_workspace.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	for name in (".npmrc", "web/.npmrc", ".env.example", ".env.sample", ".env.template"):
		assert module.readonly_allowed(name) is False, name


# Support helpers staged from their committed HEAD blob (#8065).
def _git_commit(workspace: Path) -> None:
	git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
	subprocess.run(
		["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false",
			"commit", "-q", "-m", "fixture"],
		check=True, env=git_env,
	)


def _stage_with_modes(helper_dir: Path, workspace: Path, *extra_args: str) -> tuple[dict[str, tuple[bytes, int]], str]:
	result = subprocess.run(
		["python3", "-I", "-c", _stage_program(), str(helper_dir), str(workspace), *extra_args],
		capture_output=True, timeout=60, env={"PATH": os.environ["PATH"], "HOME": str(helper_dir)},
	)
	assert result.returncode == 0, result.stderr
	members: dict[str, tuple[bytes, int]] = {}
	with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r|") as archive:
		for member in archive:
			assert member.isreg(), member.name
			handle = archive.extractfile(member)
			assert handle is not None
			members[member.name] = (handle.read(), member.mode)
	return members, result.stderr.decode()


def test_stage_copy_uses_head_bytes_over_a_modified_working_tree(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "scripts/validate_process.sh", "committed\n")
	_write(workspace, "scripts/other.sh", "committed other\n")
	_git_add(workspace, "scripts/validate_process.sh", "scripts/other.sh")
	_git_commit(workspace)
	_write(workspace, "scripts/validate_process.sh", "overwritten by staging\n")
	_write(workspace, "scripts/other.sh", "edited other\n")
	members, stderr = _stage_with_modes(helper_dir, workspace, "head-support:scripts/validate_process.sh")
	assert members["scripts/validate_process.sh"][0] == b"committed\n"
	# Paths not on the list keep the working-tree copy.
	assert members["scripts/other.sh"][0] == b"edited other\n"
	assert "VALIDATION_HARNESS_SANDBOX phase=stage outcome=ok reason=support_from_head path=scripts/validate_process.sh\n" in stderr
	assert "committed" not in stderr, "logs must never carry file content"


def test_stage_copy_without_head_support_args_keeps_the_working_tree(tmp_path: Path) -> None:
	# The switch off means cmd_run passes no head-support arguments.
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "scripts/validate_process.sh", "committed\n")
	_git_add(workspace, "scripts/validate_process.sh")
	_git_commit(workspace)
	_write(workspace, "scripts/validate_process.sh", "working tree\n")
	members, stderr = _stage_with_modes(helper_dir, workspace)
	assert members["scripts/validate_process.sh"][0] == b"working tree\n"
	assert "support_from_head" not in stderr


def test_stage_copy_falls_back_to_the_working_tree_for_untracked_helpers(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "tracked.txt", "x\n")
	_git_add(workspace, "tracked.txt")
	_git_commit(workspace)
	_write(workspace, "scripts/validation_harness_sandbox.sh", "untracked helper\n")
	members, stderr = _stage_with_modes(helper_dir, workspace, "head-support:scripts/validation_harness_sandbox.sh")
	assert members["scripts/validation_harness_sandbox.sh"][0] == b"untracked helper\n"
	assert "outcome=ok reason=support_from_head " not in stderr
	assert "outcome=skip reason=support_from_head_unavailable path=scripts/validation_harness_sandbox.sh\n" in stderr


def test_stage_copy_falls_back_when_the_repository_has_no_commit(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "scripts/validate_process.sh", "index only\n")
	_git_add(workspace, "scripts/validate_process.sh")
	members, stderr = _stage_with_modes(helper_dir, workspace, "head-support:scripts/validate_process.sh")
	assert members["scripts/validate_process.sh"][0] == b"index only\n"
	assert "outcome=ok reason=support_from_head " not in stderr
	assert "outcome=skip reason=support_from_head_unavailable path=scripts/validate_process.sh\n" in stderr


def test_stage_copy_keeps_the_head_blob_mode(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "scripts/validate_process.sh", "#!/bin/bash\n")
	(workspace / "scripts/validate_process.sh").chmod(0o755)
	_write(workspace, "scripts/templates/slot_manifest.schema.json", "{}\n")
	(workspace / "scripts/templates/slot_manifest.schema.json").chmod(0o644)
	_git_add(workspace, "scripts/validate_process.sh", "scripts/templates/slot_manifest.schema.json")
	_git_commit(workspace)
	(workspace / "scripts/validate_process.sh").chmod(0o644)
	(workspace / "scripts/templates/slot_manifest.schema.json").chmod(0o755)
	members, _ = _stage_with_modes(
		helper_dir, workspace,
		"head-support:scripts/validate_process.sh", "head-support:scripts/templates/slot_manifest.schema.json",
	)
	assert members["scripts/validate_process.sh"][1] == 0o755
	assert members["scripts/templates/slot_manifest.schema.json"][1] == 0o644


def test_stage_copy_refuses_a_committed_symlink_helper(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "target.txt", "target\n")
	(workspace / "scripts").mkdir()
	(workspace / "scripts" / "codex_isolated_workspace.py").symlink_to("../target.txt")
	_git_add(workspace, "target.txt", "scripts/codex_isolated_workspace.py")
	_git_commit(workspace)
	members, stderr = _stage_with_modes(helper_dir, workspace, "head-support:scripts/codex_isolated_workspace.py")
	assert "scripts/codex_isolated_workspace.py" not in members
	assert "outcome=ok reason=support_from_head " not in stderr
	assert "outcome=skip reason=support_from_head_unavailable path=scripts/codex_isolated_workspace.py\n" in stderr


def test_stage_copy_overlay_wins_over_head_support(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "scripts/validate_driver.sh", "committed driver\n")
	_git_add(workspace, "scripts/validate_driver.sh")
	_git_commit(workspace)
	trusted = tmp_path / "trusted_driver.sh"
	trusted.write_text("trusted driver\n", encoding="utf-8")
	members, stderr = _stage_with_modes(
		helper_dir, workspace,
		"head-support:scripts/validate_driver.sh", f"scripts/validate_driver.sh={trusted}",
	)
	assert members["scripts/validate_driver.sh"][0] == b"trusted driver\n"
	assert "support_from_head" not in stderr


def test_stage_copy_ignores_unsafe_head_support_paths(tmp_path: Path) -> None:
	helper_dir, workspace = _stage_workspace(tmp_path)
	_write(workspace, "a.sh", "committed\n")
	_git_add(workspace, "a.sh")
	_git_commit(workspace)
	_write(workspace, "a.sh", "working\n")
	members, stderr = _stage_with_modes(helper_dir, workspace, "head-support:../a.sh", "head-support:/a.sh")
	assert members["a.sh"][0] == b"working\n"
	assert "support_from_head" not in stderr


@pytest.mark.parametrize("value,enabled,warns", (
	(None, True, False),
	("", True, False),
	("TRUE", True, False),
	("true", True, False),
	("false", False, False),
	("False", False, False),
	("bogus-value", False, True),
))
def test_support_from_head_switch(tmp_path: Path, value, enabled, warns) -> None:
	env = {"PATH": os.environ["PATH"]}
	if value is not None:
		env["VALIDATION_SANDBOX_SUPPORT_FROM_HEAD_ENABLED"] = value
	out = subprocess.run(
		["bash", "-c", f'source "{HELPER}"; sandbox_support_from_head_enabled'],
		capture_output=True, text=True, env=env, timeout=30, cwd=tmp_path,
	)
	assert (out.returncode == 0) is enabled, out
	assert ("reason=support_from_head_invalid_switch" in out.stderr) is warns, out.stderr
	assert "bogus-value" not in out.stderr


def test_cmd_run_passes_head_support_paths_after_the_driver_overlay() -> None:
	text = HELPER.read_text(encoding="utf-8")
	body = text.split("cmd_run()\n{", 1)[1].split("\n}\n", 1)[0]
	assert "sandbox_log run ok staged" in body, "cmd_run slice is wrong"
	overlay = body.index('overlays+=("scripts/validate_driver.sh=${SANDBOX_HELPER_DIR}/validate_driver.sh")')
	gate = body.index("if sandbox_support_from_head_enabled; then")
	assert overlay < gate
	gated = body[gate:body.index("\n\tfi\n", gate)]
	for path in (
		"scripts/validate_process.sh",
		"scripts/validation_harness_sandbox.sh",
		"scripts/codex_isolated_workspace.py",
		"scripts/templates/slot_manifest.schema.json",
	):
		assert path in gated, path
	assert 'head_support_args+=("head-support:${head_support_path}")' in gated
	assert body.count("head_support_args+=") == 1
	assert 'python3 -I -c "${STAGE_PY}" "${SANDBOX_HELPER_DIR}" "${workspace}" "${head_support_args[@]}" "${overlays[@]}"' in body
