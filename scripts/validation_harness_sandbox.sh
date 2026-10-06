#!/usr/bin/env bash
# Validation harness sandbox: run generated validation tests away from the job's credentials.
#
# Generated validation tests are untrusted: they are rendered from
# repository-controlled .ai/validate.yml or written by a model, and the
# harness drives `docker compose`. On a runner whose user is in the `docker`
# group, a test could otherwise read every secret the job references
# (environment, /proc/<pid>/environ, Runner.Worker memory via the host Docker
# socket, the checkout's git extraheader). This helper runs the whole harness
# as a separate unprivileged OS user with its own rootless Docker daemon, a
# screened copy of the workspace and an allowlisted environment, and copies
# back only bounded regular log files. Every failure is fail-closed (exit 3);
# there is no fallback to host execution.
#
# Subcommands:
#   provision            create the sandbox user and start its rootless dockerd (idempotent)
#   selfcheck [pid]      prove, as the sandbox user, that credentials are out of reach
#   run <entry> [args]   stage a screened copy, run <entry> as the sandbox user, copy logs back
#   cleanup              stop harness processes and containers, remove the work copy
#   print-env-names      print the variable NAMES the sandbox would receive (never values)
#   ingest-logs <dest>   validate a log tar on stdin and write it under <dest> (all or nothing)
#
# Exit codes: the harness exit code for `run`; 3 for any sandbox failure.
# Log prefix (stable): VALIDATION_HARNESS_SANDBOX phase=<p> outcome=ok|fail reason=<token>
# Values are never logged.

set -euo pipefail

SANDBOX_HELPER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SANDBOX_FAIL_EXIT=3

# Defaults live here (unattended_system_instructions.md §8): the workflow
# does not need to export any of these.
VALIDATION_HARNESS_SANDBOX_USER="${VALIDATION_HARNESS_SANDBOX_USER:-ai-validation}"
VALIDATION_HARNESS_SANDBOX_MAX_COPYBACK_BYTES="${VALIDATION_HARNESS_SANDBOX_MAX_COPYBACK_BYTES:-67108864}"
VALIDATION_HARNESS_SANDBOX_MAX_FILES="${VALIDATION_HARNESS_SANDBOX_MAX_FILES:-50000}"
VALIDATION_HARNESS_SANDBOX_DOCKER_START_TIMEOUT="${VALIDATION_HARNESS_SANDBOX_DOCKER_START_TIMEOUT:-90}"
VALIDATION_HARNESS_SANDBOX_STATUS_FILE="${VALIDATION_HARNESS_SANDBOX_STATUS_FILE:-${RUNTIME_DIR:-${TMPDIR:-/tmp}}/validation_harness_sandbox.status}"
SANDBOX_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

sandbox_log()
{
	printf 'VALIDATION_HARNESS_SANDBOX phase=%s outcome=%s reason=%s\n' "$1" "$2" "${3:-none}" >&2
}

sandbox_status()
{
	local status_dir
	status_dir="$(dirname -- "${VALIDATION_HARNESS_SANDBOX_STATUS_FILE}")"
	mkdir -p "${status_dir}" 2>/dev/null || true
	printf '%s\n' "$*" > "${VALIDATION_HARNESS_SANDBOX_STATUS_FILE}" 2>/dev/null || true
}

sandbox_fail()
{
	local phase="$1" reason="$2"
	sandbox_log "${phase}" fail "${reason}"
	sandbox_status "fail ${phase} ${reason}"
	exit "${SANDBOX_FAIL_EXIT}"
}

is_positive_int()
{
	[[ "${1:-}" =~ ^[0-9]+$ ]] && [ "$1" -gt 0 ]
}

validate_config()
{
	local phase="$1"
	if [[ ! "${VALIDATION_HARNESS_SANDBOX_USER}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] \
		|| [ "${VALIDATION_HARNESS_SANDBOX_USER}" = "root" ] \
		|| [ "${VALIDATION_HARNESS_SANDBOX_USER}" = "$(id -un 2>/dev/null || true)" ]; then
		sandbox_fail "${phase}" invalid_sandbox_user
	fi
	is_positive_int "${VALIDATION_HARNESS_SANDBOX_MAX_COPYBACK_BYTES}" || sandbox_fail "${phase}" invalid_copyback_cap
	is_positive_int "${VALIDATION_HARNESS_SANDBOX_MAX_FILES}" || sandbox_fail "${phase}" invalid_file_cap
	is_positive_int "${VALIDATION_HARNESS_SANDBOX_DOCKER_START_TIMEOUT}" || VALIDATION_HARNESS_SANDBOX_DOCKER_START_TIMEOUT=90
}

require_sudo()
{
	command -v sudo >/dev/null 2>&1 || sandbox_fail "$1" sudo_missing
	sudo -n true >/dev/null 2>&1 || sandbox_fail "$1" sudo_unavailable
}

sandbox_uid()
{
	id -u "${VALIDATION_HARNESS_SANDBOX_USER}" 2>/dev/null
}

sandbox_home()
{
	getent passwd "${VALIDATION_HARNESS_SANDBOX_USER}" 2>/dev/null | cut -d: -f6
}

sandbox_runtime_dir()
{
	printf '/run/user/%s' "$(sandbox_uid)"
}

sandbox_docker_host()
{
	printf 'unix://%s/docker.sock' "$(sandbox_runtime_dir)"
}

# Allowlisted environment for the sandbox user. Names only flow through this
# filter; the denylist is applied after the allowlist so a credential can
# never pass via an allowed prefix (for example VALIDATION_*TOKEN*).
sandbox_env_is_denied()
{
	case "$1" in
		VALIDATION_TEST_USERNAME|VALIDATION_TEST_PASSWORD|VALIDATION_TEST_API_KEY|TEST_USERNAME|TEST_PASSWORD|TEST_API_KEY)
			return 1
			;;
		*TOKEN*|*SECRET*|*_PAT|*PAT_*|*API_KEY*|*PRIVATE_KEY*|*CREDENTIAL*|GIT_CONFIG_*|GIT_ASKPASS|SSH_*|ACTIONS_*|RUNNER_*|GITHUB_*|OPENROUTER_*|TG_*|CLAUDE_*|ANTHROPIC_*|OPENAI_*|VALIDATION_HARNESS_SANDBOX_*)
			return 0
			;;
	esac
	return 1
}

sandbox_env_is_allowed()
{
	case "$1" in
		LANG|LC_ALL|TZ|TERM \
		|APP_SERVICE|APP_URL|COMPOSE_FILE|COMPOSE_LOG|VALIDATE_ENV_FILE \
		|HEALTH_TIMEOUT|HEALTH_TIMEOUT_SECONDS|HEALTH_POLL_INTERVAL|HEALTH_POLL_INTERVAL_SECONDS \
		|PHASE|TEST_DIR|LOG_DIR|TAIL_LINES|CANARY_PATTERN|CANARY_REQUIRED|HELPER_PATTERN \
		|TEST_USERNAME|TEST_PASSWORD|TEST_API_KEY|VALIDATION_*)
			return 0
			;;
	esac
	return 1
}

# Fill the global array SANDBOX_ENV_ARGS with NAME=VALUE pairs for `env -i`.
build_sandbox_env()
{
	local home="$1" work_tmp="$2" docker_host="$3"
	local name
	SANDBOX_ENV_ARGS=(
		"PATH=${SANDBOX_PATH}"
		"HOME=${home}"
		"USER=${VALIDATION_HARNESS_SANDBOX_USER}"
		"LOGNAME=${VALIDATION_HARNESS_SANDBOX_USER}"
		"TMPDIR=${work_tmp}"
		"DOCKER_HOST=${docker_host}"
		"DOCKER_CONFIG=${home}/.docker"
		"GH_CONFIG_DIR=${home}/.gh-empty"
		"GIT_CONFIG_NOSYSTEM=1"
		"GIT_CONFIG_GLOBAL=/dev/null"
		"GIT_TERMINAL_PROMPT=0"
	)
	while IFS= read -r name; do
		sandbox_env_is_allowed "${name}" || continue
		sandbox_env_is_denied "${name}" && continue
		SANDBOX_ENV_ARGS+=("${name}=${!name}")
	done < <(compgen -e | sort)
}

cmd_print_env_names()
{
	build_sandbox_env "/sandbox-home" "/sandbox-home/tmp" "unix:///sandbox/docker.sock"
	local pair
	for pair in "${SANDBOX_ENV_ARGS[@]}"; do
		printf '%s\n' "${pair%%=*}"
	done
}

as_sandbox()
{
	# sudo resets the environment; `env -i` guarantees nothing else survives.
	sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- env -i "${SANDBOX_ENV_ARGS[@]}" "$@"
}

rootless_docker_ready()
{
	as_sandbox docker info >/dev/null 2>&1
}

next_subid_start()
{
	awk -F: 'BEGIN { max = 524288 } { end = $2 + $3; if (end > max) max = end } END { print max }' /etc/subuid /etc/subgid 2>/dev/null || echo 524288
}

cmd_provision()
{
	validate_config provision
	require_sudo provision
	local user="${VALIDATION_HARNESS_SANDBOX_USER}" home uid runtime_dir groups_line start dir

	if ! id -u "${user}" >/dev/null 2>&1; then
		sudo -n useradd --create-home --shell /usr/sbin/nologin --user-group "${user}" >/dev/null 2>&1 \
			|| sandbox_fail provision useradd_failed
	fi
	groups_line=" $(id -nG "${user}" 2>/dev/null || true) "
	case "${groups_line}" in
		*" docker "*|*" sudo "*|*" admin "*|*" wheel "*|*" adm "*|*" root "*)
			sandbox_fail provision sandbox_user_privileged_group
			;;
	esac
	home="$(sandbox_home)"
	uid="$(sandbox_uid)"
	{ [ -n "${home}" ] && [ -n "${uid}" ] && [ "${uid}" != "0" ]; } || sandbox_fail provision sandbox_user_lookup_failed
	sudo -n install -d -m 0700 -o "${user}" -g "$(id -gn "${user}")" "${home}" "${home}/.docker" "${home}/.gh-empty" "${home}/work" \
		|| sandbox_fail provision sandbox_home_failed

	if ! grep -q "^${user}:" /etc/subuid 2>/dev/null || ! grep -q "^${user}:" /etc/subgid 2>/dev/null; then
		start="$(next_subid_start)"
		sudo -n usermod --add-subuids "${start}-$((start + 65535))" --add-subgids "${start}-$((start + 65535))" "${user}" >/dev/null 2>&1 \
			|| sandbox_fail provision subid_allocation_failed
	fi

	if ! command -v dockerd-rootless.sh >/dev/null 2>&1 || ! command -v newuidmap >/dev/null 2>&1 || ! command -v rootlesskit >/dev/null 2>&1; then
		if ! sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q uidmap docker-ce-rootless-extras >/dev/null 2>&1; then
			sudo -n env DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null 2>&1 || true
			sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q uidmap docker-ce-rootless-extras >/dev/null 2>&1 \
				|| sandbox_fail provision rootless_packages_unavailable
		fi
		command -v dockerd-rootless.sh >/dev/null 2>&1 || sandbox_fail provision rootless_dockerd_missing
	fi

	# Ubuntu 24.04 blocks unprivileged user namespaces for unconfined binaries;
	# rootlesskit needs them.
	if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ]; then
		sudo -n sysctl -q -w kernel.apparmor_restrict_unprivileged_userns=0 >/dev/null 2>&1 \
			|| sandbox_fail provision apparmor_userns_restricted
	fi

	runtime_dir="/run/user/${uid}"
	sudo -n install -d -m 0700 -o "${user}" -g "$(id -gn "${user}")" "${runtime_dir}" || sandbox_fail provision runtime_dir_failed

	build_sandbox_env "${home}" "${home}/work" "unix://${runtime_dir}/docker.sock"
	if ! rootless_docker_ready; then
		sudo -n -u "${user}" -- env -i "PATH=${SANDBOX_PATH}" "HOME=${home}" "XDG_RUNTIME_DIR=${runtime_dir}" \
			setsid dockerd-rootless.sh </dev/null >/dev/null 2>&1 &
		local waited=0
		until rootless_docker_ready; do
			if [ "${waited}" -ge "${VALIDATION_HARNESS_SANDBOX_DOCKER_START_TIMEOUT}" ]; then
				sandbox_fail provision rootless_dockerd_start_timeout
			fi
			sleep 2
			waited=$((waited + 2))
		done
	fi

	# Keep the credentialed job's own state unreadable to the sandbox user.
	for dir in "${HOME:-}" "${RUNNER_TEMP:-}" "${RUNTIME_DIR:-}" "${GITHUB_WORKSPACE:-}"; do
		[ -n "${dir}" ] && [ -d "${dir}" ] && [ -O "${dir}" ] || continue
		chmod 0700 "${dir}" 2>/dev/null || sandbox_fail provision runner_dir_chmod_failed
	done

	sandbox_log provision ok
	sandbox_status "ok provision"
}

# The probe runs as the sandbox user; its arguments are paths and a pid only.
read -r -d '' SELFCHECK_PROBE <<'PROBE' || true
caller_pid="$1"; shift
fail() { printf '%s\n' "$1"; exit 1; }
if [ -n "${caller_pid}" ] && [ -e "/proc/${caller_pid}" ] && cat "/proc/${caller_pid}/environ" >/dev/null 2>&1; then
	fail proc_environ_readable
fi
if [ -S /var/run/docker.sock ] && docker -H unix:///var/run/docker.sock info >/dev/null 2>&1; then
	fail host_docker_socket_usable
fi
if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
	fail sudo_available
fi
if env | cut -d= -f1 | grep -Eq '^(GH_TOKEN|GH_PAT|GITHUB_TOKEN|OPENROUTER_API_KEY|TG_BOT_SECRET|ACTIONS_.*|GIT_CONFIG_(COUNT|KEY_.*|VALUE_.*|PARAMETERS))$'; then
	fail credential_env_present
fi
if (cd "${HOME}" && git config --show-origin --list 2>/dev/null) | grep -Eiq 'extraheader|credential\.'; then
	fail git_credential_config_present
fi
for dir in "$@"; do
	[ -n "${dir}" ] || continue
	if ls -- "${dir}" >/dev/null 2>&1; then
		fail runner_dir_listable
	fi
done
docker info >/dev/null 2>&1 || fail rootless_docker_unavailable
printf 'ok\n'
PROBE

cmd_selfcheck()
{
	validate_config selfcheck
	require_sudo selfcheck
	id -u "${VALIDATION_HARNESS_SANDBOX_USER}" >/dev/null 2>&1 || sandbox_fail selfcheck sandbox_user_missing
	local caller_pid="${1:-${PPID}}" home verdict
	home="$(sandbox_home)"
	build_sandbox_env "${home}" "${home}/work" "$(sandbox_docker_host)"
	verdict="$(as_sandbox bash -c "${SELFCHECK_PROBE}" selfcheck "${caller_pid}" \
		"${HOME:-}" "${RUNNER_TEMP:-}" "${RUNTIME_DIR:-}" "${GITHUB_WORKSPACE:-}" 2>/dev/null | tail -n 1 || true)"
	if [ "${verdict}" != "ok" ]; then
		[[ "${verdict}" =~ ^[a-z_]+$ ]] || verdict="probe_failed"
		sandbox_fail selfcheck "${verdict}"
	fi
	sandbox_log selfcheck ok
	sandbox_status "ok selfcheck"
}

# Builds the screened copy as an uncompressed tar on stdout. Runs as the
# runner user over the trusted workspace; the sandbox user only extracts it.
read -r -d '' STAGE_PY <<'PY' || true
import os, stat, subprocess, sys, tarfile
from pathlib import Path

helper_dir, workspace, *overlay_args = sys.argv[1:]
sys.path.insert(0, helper_dir)
from codex_isolated_workspace import readonly_allowed  # trusted sibling copy

workspace = Path(workspace)
env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
listed = subprocess.run(
	["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "ls-files", "-z", "--cached"],
	cwd=workspace, env=env, check=True, capture_output=True,
).stdout.split(b"\0")
names = {item.decode("utf-8", "surrogateescape") for item in listed if item}
for root in ("validation",):
	base = workspace / root
	if base.is_dir() and not base.is_symlink():
		for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
			dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
			for filename in filenames:
				names.add(str((Path(dirpath) / filename).relative_to(workspace)))
names.add(".ai/validate.yml")
overlays = {}
for item in overlay_args:
	dest, _, src = item.partition("=")
	overlays[dest] = src

out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT)

def add(path, arcname):
	info = os.lstat(path)
	if not stat.S_ISREG(info.st_mode):
		return
	member = tarfile.TarInfo(arcname)
	member.size = info.st_size
	member.mode = 0o755 if info.st_mode & 0o111 else 0o644
	member.mtime = int(info.st_mtime)
	with open(path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as handle:
		out.addfile(member, handle)

for name in sorted(names):
	if name in overlays or not readonly_allowed(name):
		continue
	path = workspace / name
	try:
		parent = workspace
		for part in Path(name).parts[:-1]:
			parent = parent / part
			if not stat.S_ISDIR(os.lstat(parent).st_mode):
				raise OSError("non-directory parent")
		add(path, name)
	except OSError:
		continue
for dest, src in sorted(overlays.items()):
	add(src, dest)
out.close()
PY

# Validates a tar stream produced by the sandbox user and replays regular
# files under <dest>. Any unsafe member rejects the whole copy-back before
# the first host write.
read -r -d '' INGEST_PY <<'PY' || true
import os, stat, sys, tarfile, tempfile
from pathlib import Path, PurePosixPath

dest, prefix, max_bytes, max_files = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])

def reject(reason):
	print(reason)
	sys.exit(2)

staged = []
total = 0
with tempfile.TemporaryDirectory(prefix="harness-copyback-") as scratch:
	try:
		archive = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")
		for member in archive:
			name = member.name.rstrip("/")
			parts = PurePosixPath(name).parts
			if not parts or name.startswith("/") or ".." in parts or "\\" in name or any(c in name for c in "\0\n\r"):
				reject("copyback_unsafe")
			if parts[:len(PurePosixPath(prefix).parts)] != PurePosixPath(prefix).parts:
				reject("copyback_unsafe")
			if member.isdir():
				continue
			if not member.isreg():
				reject("copyback_unsafe")
			total += member.size
			if total > max_bytes:
				reject("copyback_oversize")
			if len(staged) + 1 > max_files:
				reject("copyback_too_many_files")
			rel = PurePosixPath(*parts[len(PurePosixPath(prefix).parts):])
			if not rel.parts:
				reject("copyback_unsafe")
			source = archive.extractfile(member)
			tmp = Path(scratch) / str(len(staged))
			with open(tmp, "wb") as handle:
				remaining = member.size
				while remaining > 0:
					chunk = source.read(min(remaining, 1 << 20))
					if not chunk:
						reject("copyback_truncated")
					handle.write(chunk)
					remaining -= len(chunk)
			staged.append((rel, tmp))
	except tarfile.TarError:
		reject("copyback_malformed")
	root = Path(dest)
	if root.is_symlink() or (root.exists() and not root.is_dir()):
		reject("copyback_dest_unsafe")
	root.mkdir(parents=True, exist_ok=True)
	for rel, tmp in staged:
		parent = root
		for part in rel.parts[:-1]:
			parent = parent / part
			if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
				reject("copyback_dest_unsafe")
			parent.mkdir(exist_ok=True)
		target = parent / rel.parts[-1]
		if target.is_symlink() or (target.exists() and not stat.S_ISREG(os.lstat(target).st_mode)):
			reject("copyback_dest_unsafe")
		fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
		with os.fdopen(fd, "wb") as out, open(tmp, "rb") as src:
			out.write(src.read())
print("ok")
PY

cmd_ingest_logs()
{
	validate_config copyback
	local dest="${1:?ingest-logs requires a destination}" prefix="${2:-validation/logs}" verdict
	verdict="$(python3 -I -c "${INGEST_PY}" "${dest}" "${prefix}" \
		"${VALIDATION_HARNESS_SANDBOX_MAX_COPYBACK_BYTES}" "${VALIDATION_HARNESS_SANDBOX_MAX_FILES}" 2>/dev/null | tail -n 1 || true)"
	if [ "${verdict}" != "ok" ]; then
		[[ "${verdict}" =~ ^[a-z_]+$ ]] || verdict="copyback_failed"
		sandbox_fail copyback "${verdict}"
	fi
	sandbox_log copyback ok
}

sandbox_work_dir()
{
	printf '%s/work/%s' "$(sandbox_home)" "${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
}

cmd_cleanup()
{
	validate_config cleanup
	command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1 || { sandbox_log cleanup fail sudo_unavailable; return 0; }
	id -u "${VALIDATION_HARNESS_SANDBOX_USER}" >/dev/null 2>&1 || { sandbox_log cleanup ok no_sandbox_user; return 0; }
	local home work pgid compose
	home="$(sandbox_home)"
	work="$(sandbox_work_dir)"
	build_sandbox_env "${home}" "${home}/work" "$(sandbox_docker_host)"
	pgid="$(sudo -n cat "${work}.pgid" 2>/dev/null || true)"
	if [[ "${pgid}" =~ ^[0-9]+$ ]] && [ "${pgid}" -gt 1 ]; then
		sudo -n kill -KILL -- "-${pgid}" >/dev/null 2>&1 || true
	fi
	compose="${work}/validation/docker-compose.test.yml"
	if sudo -n test -f "${compose}"; then
		as_sandbox bash -c 'cd "$1" && docker compose -f "$2" down -v --remove-orphans' cleanup "${work}" "${compose}" >/dev/null 2>&1 || true
	fi
	as_sandbox rm -rf -- "${work}" "${work}.pgid" >/dev/null 2>&1 || true
	sandbox_log cleanup ok
}

cmd_run()
{
	validate_config run
	local entry="${1:?run requires an entry script}"
	shift
	require_sudo run
	id -u "${VALIDATION_HARNESS_SANDBOX_USER}" >/dev/null 2>&1 || sandbox_fail run sandbox_user_missing
	local home work workspace entry_rel stage_status extract_status harness_exit
	local -a overlays=()
	home="$(sandbox_home)"
	work="$(sandbox_work_dir)"
	workspace="$(pwd -P)"
	build_sandbox_env "${home}" "${work}/.tmp" "$(sandbox_docker_host)"
	rootless_docker_ready || sandbox_fail run rootless_docker_unavailable
	[ -f "${SANDBOX_HELPER_DIR}/codex_isolated_workspace.py" ] || sandbox_fail run support_missing

	case "${entry}" in
		/*)
			case "${entry}" in
				"${workspace}"/*) entry_rel="${entry#"${workspace}"/}" ;;
				*)
					# A trusted runtime driver outside the workspace (VALIDATION_RUNNER_FILE).
					[ -f "${entry}" ] || sandbox_fail run entry_missing
					entry_rel=".validation-harness/$(basename -- "${entry}")"
					overlays+=("${entry_rel}=${entry}")
					;;
			esac
			;;
		*) entry_rel="${entry}" ;;
	esac
	case "/${entry_rel}/" in
		*/../*) sandbox_fail run entry_unsafe ;;
	esac
	# Overlay the trusted driver so the copy runs the staged support version.
	if [ -f "${SANDBOX_HELPER_DIR}/validate_driver.sh" ]; then
		overlays+=("scripts/validate_driver.sh=${SANDBOX_HELPER_DIR}/validate_driver.sh")
	fi

	cmd_cleanup >/dev/null 2>&1 || true
	build_sandbox_env "${home}" "${work}/.tmp" "$(sandbox_docker_host)"
	as_sandbox mkdir -p -- "${work}/.tmp" || sandbox_fail run work_dir_failed
	set +e
	python3 -I -c "${STAGE_PY}" "${SANDBOX_HELPER_DIR}" "${workspace}" "${overlays[@]}" \
		| as_sandbox tar -x --no-same-owner --no-same-permissions -C "${work}" -f -
	stage_status=("${PIPESTATUS[@]}")
	set -e
	if [ "${stage_status[0]}" -ne 0 ] || [ "${stage_status[1]}" -ne 0 ]; then
		sandbox_fail run stage_failed
	fi
	as_sandbox test -f "${work}/${entry_rel}" || sandbox_fail run entry_missing
	sandbox_log run ok staged

	set +e
	# setsid gives the harness its own process group so cleanup can stop it
	# even after the caller SIGKILLs this helper (signals to sudo do not
	# reach the sandbox user's processes).
	as_sandbox setsid --wait bash -c 'printf "%s\n" "$$" > "$1.pgid"; cd "$1" || exit 1; shift; exec bash "$@"' \
		harness "${work}" "${entry_rel}" "$@"
	harness_exit=$?
	set -e

	set +e
	as_sandbox bash -c 'cd "$1" && if [ -d validation/logs ]; then tar -c -f - validation/logs; else tar -c -f - --files-from /dev/null; fi' \
		copyback "${work}" 2>/dev/null \
		| cmd_ingest_logs "${workspace}/validation/logs" "validation/logs"
	extract_status=("${PIPESTATUS[@]}")
	set -e
	# cmd_ingest_logs exits 3 itself on refusal (status already written).
	if [ "${extract_status[1]}" -ne 0 ]; then
		exit "${SANDBOX_FAIL_EXIT}"
	fi
	sandbox_log run ok "harness_exit_${harness_exit}"
	sandbox_status "ok run ${harness_exit}"
	exit "${harness_exit}"
}

main()
{
	local command="${1:-}"
	[ "$#" -gt 0 ] && shift
	case "${command}" in
		provision) cmd_provision "$@" ;;
		selfcheck) cmd_selfcheck "$@" ;;
		run) cmd_run "$@" ;;
		cleanup) cmd_cleanup "$@" ;;
		print-env-names) cmd_print_env_names "$@" ;;
		ingest-logs) cmd_ingest_logs "$@" ;;
		*)
			echo "usage: $(basename -- "$0") {provision|selfcheck [pid]|run <entry> [args]|cleanup|print-env-names|ingest-logs <dest> [prefix]}" >&2
			exit 2
			;;
	esac
}

main "$@"
