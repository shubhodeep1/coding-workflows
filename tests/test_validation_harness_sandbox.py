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


def test_cleanup_never_kills_the_sandbox_written_pgid_as_root() -> None:
	# ${work}.pgid is written by the untrusted sandbox user; a forged value
	# must not reach a root-privileged kill (it could target runner processes).
	text = HELPER.read_text(encoding="utf-8")
	cleanup = re.search(r"^cmd_cleanup\(\)\n\{\n.*?^\}\n", text, re.M | re.S)
	assert cleanup is not None
	body = cleanup.group(0)
	assert 'as_sandbox kill -KILL -- "-${pgid}"' in body
	assert "sudo -n kill" not in body


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
