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
#   checked-run <pid> <entry> [args]  selfcheck against <pid>, then run (one call for callers)
#   cleanup              stop harness processes and containers, remove the work copy
#   print-env-names      print the variable NAMES the sandbox would receive (never values)
#   ingest-logs <dest>   validate a log tar on stdin and write it under <dest> (all or nothing)
#
# Exit codes: the harness exit code for `run`; 3 for any sandbox failure.
# Log prefix (stable): VALIDATION_HARNESS_SANDBOX phase=<p> outcome=ok|retry|fail reason=<token>
# (`phase=stage outcome=ok|skip reason=<token> path=<name>` names each file the
# screened copy admits or drops beyond readonly_allowed: env templates and .npmrc.)
# (`retry` only from provision, when a test container would not start under the
# systemd cgroup driver, or under a daemon reused from an earlier provision in
# the job, and the daemon is restarted with cgroupfs: reasons
# container_start_failed_systemd / container_start_failed_reused; #7063).
# Values are never logged.
#
# PID namespace (finding validation-harness-credential-inheritance, project
# security pass for #6664): the harness and the namespace self-check start
# inside a new PID and mount namespace created by root (`unshare --pid --fork
# --mount-proc --propagation private --kill-child`) before dropping to the sandbox user, so a test
# cannot list runner processes or read their /proc entries. When no namespace
# can be created every phase fails closed (pid_namespace_unavailable,
# pid_namespace_launch_failed); there is no launch without it. Root records
# the host pid of `unshare` in a root-owned 0700 state directory, and cleanup
# kills only that pid after checking it is still a root `unshare` process.

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
# Optional absolute destination for the copied-back validation/logs tree.
# Empty (the default) keeps <workspace>/validation/logs. validation-refresh
# sets it so logs stay outside the consumer clone it diffs for drift.
VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST="${VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST:-}"
# Cgroup setup for the sandbox's rootless dockerd: `cgroupfs` (the default)
# starts no systemd user manager for the sandbox user, and releases one left
# by an earlier opt-in `auto` provision. `auto` (opt-in) tries a systemd user
# session first and falls back to cgroupfs; while that user manager runs, a
# container could ask it to start processes in the host PID namespace, so
# `selfcheck` then fails closed with user_manager_reachable_from_container
# (#7380). Any other value is treated as `cgroupfs`. Either way a test
# container that the daemon refuses to start (or a probe image the daemon
# refuses to import) fails provision; only a probe the host cannot build, or
# whose command cannot run inside the container (exit 126/127 without a
# cgroup error), passes as container_probe_inconclusive.
VALIDATION_HARNESS_SANDBOX_CGROUP_MODE="${VALIDATION_HARNESS_SANDBOX_CGROUP_MODE:-cgroupfs}"
# Root-owned directory for the namespace launch state (the host pid of
# `unshare` and the launch marker). It must be absolute and outside the
# sandbox home, so the sandbox user can neither write nor replace it.
VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR="${VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR:-/run/ai-validation-harness}"
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
	local state_dir="${VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR}" state_home=""
	# At least two components (never `/run` or `/tmp` itself), no `//`, no trailing `/`.
	[[ "${state_dir}" =~ ^/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$ ]] || sandbox_fail "${phase}" invalid_pidns_state_dir
	case "/${state_dir}/" in
		*/../*|*/./*) sandbox_fail "${phase}" invalid_pidns_state_dir ;;
	esac
	case "${state_dir}" in
		*//*|*/) sandbox_fail "${phase}" invalid_pidns_state_dir ;;
	esac
	state_home="$(sandbox_home || true)"
	if [ -n "${state_home}" ]; then
		case "${state_dir%/}/" in
			"${state_home%/}/"*) sandbox_fail "${phase}" invalid_pidns_state_dir ;;
		esac
	fi
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
		*TOKEN*|*SECRET*|*_PAT|*PAT_*|*API_KEY*|*PRIVATE_KEY*|*CREDENTIAL*|GIT_CONFIG_*|GIT_ASKPASS|SSH_*|ACTIONS_*|RUNNER_*|GITHUB_*|OPENROUTER_*|TG_*|CLAUDE_*|ANTHROPIC_*|OPENAI_*|VALIDATION_HARNESS_SANDBOX_*|VALIDATION_HARNESS_SELFCHECK_*)
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

# Prove a PID namespace can be created; never fall back to a launch without one.
pidns_supported()
{
	sudo -n unshare --pid --fork --mount-proc --propagation private --kill-child -- true >/dev/null 2>&1 \
		|| sandbox_fail "$1" pid_namespace_unavailable
}

# Create the root-owned state directory (0700, outside the sandbox home).
ensure_pidns_state_dir()
{
	local dir="${VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR}"
	if sudo -n test -L "${dir}"; then
		sandbox_fail "$1" pidns_state_dir_failed
	fi
	# `install -d` re-applies mode and owner to an existing directory, so an
	# existing path is used only when it already is a root-owned 0700 directory.
	if sudo -n test -e "${dir}"; then
		[ "$(sudo -n stat -c '%u %a %F' -- "${dir}" 2>/dev/null || true)" = "0 700 directory" ] || sandbox_fail "$1" pidns_state_dir_failed
		return 0
	fi
	sudo -n install -d -m 0700 -o root -g root "${dir}" >/dev/null 2>&1 || sandbox_fail "$1" pidns_state_dir_failed
}

pidns_state_base()
{
	local key="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
	printf '%s/%s' "${VALIDATION_HARNESS_SANDBOX_PIDNS_STATE_DIR}" "${key//[^A-Za-z0-9_-]/_}"
}

# The only launch path for the harness and the namespace self-check.
# Usage: as_sandbox_in_pidns <pidfile|-> <startedfile|-> cmd...
# Root writes the host pid of `unshare` (the outer `exec` keeps $$) to
# <pidfile>, then, inside the new namespace and before dropping to the
# sandbox user, creates <startedfile>. Every value is a positional argument;
# nothing is spliced into the `sh -c` text. The `env -i` allowlist is the one
# as_sandbox uses.
#
# In `auto` cgroup mode the sandbox user has a systemd user manager in the
# host PID namespace; `systemd-run --user` through its sockets would start
# commands outside this namespace. Before the drop, root bind-mounts
# /dev/null over every socket under <runtime_dir> except docker.sock inside
# the new mount namespace (`--propagation private`, so the daemon outside
# keeps its bus). Every socket is found with `find`, not a fixed list: the
# sandbox user owns <runtime_dir> and could rename `systemd/` to hide
# `private` from a fixed path. A symlink at <runtime_dir>, bus or
# systemd/private, or a failed mount, exits 125 before <startedfile>
# exists, so `run` fails with pid_namespace_launch_failed. An entry that
# vanishes between `find` and `mount` (a containerd shim socket) holds no
# socket and is skipped, so it cannot fail the launch. A socket renamed in
# that window is skipped at its old name too, so after the mask root lists
# the sockets again and exits 125 when any one other than docker.sock is
# left unmasked (a bind-mounted /dev/null is no longer a socket). That
# recheck tests each entry with `! -type l -xtype s`, which stats it: plain
# `-type s` trusts the directory entry's type, which a bind mount does not
# change, so it still lists every masked socket and every launch exited 125
# (#7355).
as_sandbox_in_pidns()
{
	local pidfile="$1" started="$2" uid runtime_dir
	shift 2
	uid="$(sandbox_uid || true)"
	[[ "${uid}" =~ ^[0-9]+$ ]] || return 125
	runtime_dir="$(sandbox_runtime_dir)"
	sudo -n sh -c 'if [ "$1" != "-" ]; then printf "%s\n" "$$" > "$1" || exit 125; fi; shift; exec unshare --pid --fork --mount-proc --propagation private --kill-child -- "$@"' pidns "${pidfile}" \
		sh -c 'for s in "$2" "$2/bus" "$2/systemd/private"; do if [ -L "$s" ]; then exit 125; fi; done; if [ -d "$2" ]; then find "$2" -xdev -ignore_readdir_race -type s ! -path "$2/docker.sock" -exec sh -c "$3" mask {} + || exit 125; if [ -n "$(find "$2" -xdev -ignore_readdir_race ! -type l -xtype s ! -path "$2/docker.sock" -print -quit)" ]; then exit 125; fi; fi; if [ "$1" != "-" ]; then : > "$1" || exit 125; fi; shift 3; exec "$@"' started "${started}" "${runtime_dir}" \
		'for s do [ -e "$s" ] || continue; mount --bind /dev/null "$s" || { if [ -e "$s" ]; then exit 1; fi; }; done' \
		sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- env -i "${SANDBOX_ENV_ARGS[@]}" "$@"
}

# Kill the namespace recorded in <pidfile>: only when the file is a root-owned
# regular file holding a pid > 1 that is still a root `unshare` process (pid
# reuse guard). Its forked child is the namespace's init; killing it takes
# down every process inside the namespace. Always returns 0.
pidns_kill_recorded()
{
	local pidfile="$1" meta pid comm owner child
	meta="$(sudo -n stat -c '%u %F' -- "${pidfile}" 2>/dev/null || true)"
	[ "${meta}" = "0 regular file" ] || return 0
	pid="$(sudo -n cat -- "${pidfile}" 2>/dev/null | head -n 1 || true)"
	{ [[ "${pid}" =~ ^[0-9]{1,10}$ ]] && [ "${pid}" -gt 1 ]; } || return 0
	comm="$(sudo -n cat -- "/proc/${pid}/comm" 2>/dev/null || true)"
	owner="$(sudo -n stat -c '%u' -- "/proc/${pid}" 2>/dev/null || true)"
	{ [ "${comm}" = "unshare" ] && [ "${owner}" = "0" ]; } || return 0
	while IFS= read -r child; do
		{ [[ "${child}" =~ ^[0-9]{1,10}$ ]] && [ "${child}" -gt 1 ]; } || continue
		sudo -n kill -KILL -- "${child}" >/dev/null 2>&1 || true
	done < <(sudo -n pgrep -P "${pid}" 2>/dev/null || true)
	sudo -n kill -KILL -- "${pid}" >/dev/null 2>&1 || true
	return 0
}

# 0 when the sandbox user runs at least one dockerd and every one of them is
# in a PID namespace other than <host_ns> (rootlesskit --pidns). Read as root
# from host pids; the sandbox-written docker.pid is never trusted.
docker_pidns_isolated()
{
	local host_ns="$1" uid pid ns found=0
	uid="$(sandbox_uid)" || return 1
	[[ "${uid}" =~ ^[0-9]+$ ]] || return 1
	while IFS= read -r pid; do
		[[ "${pid}" =~ ^[0-9]{1,10}$ ]] || continue
		ns="$(sudo -n readlink "/proc/${pid}/ns/pid" 2>/dev/null || true)"
		{ [ -n "${ns}" ] && [ "${ns}" != "${host_ns}" ]; } || return 1
		found=1
	done < <(sudo -n pgrep -u "${uid}" -x dockerd 2>/dev/null || true)
	[ "${found}" -eq 1 ]
}

rootless_docker_ready()
{
	as_sandbox docker info >/dev/null 2>&1
}

# Rootless dockerd defaults to the systemd cgroup driver on a cgroup v2 host
# booted with systemd (moby daemon/daemon_unix.go UsingSystemd). That driver
# needs the sandbox user's user-<uid>.slice and user bus, which only exist
# inside a systemd user session; without one every container start fails with
# "open /sys/fs/cgroup/user.slice/user-<uid>.slice/cgroup.controllers: no
# such file or directory" (#6902, #6664, #7063). With
# native.cgroupdriver=cgroupfs a rootless daemon uses no cgroups at all:
# containers start, and resource limits are dropped with a warning. The user
# namespace isolation and the separate daemon are unchanged either way.
SANDBOX_SYSTEMD_RUN_DIR="${SANDBOX_SYSTEMD_RUN_DIR:-/run/systemd/system}"
SANDBOX_CGROUP_ROOT="${SANDBOX_CGROUP_ROOT:-/sys/fs/cgroup}"
SANDBOX_SESSION_WAIT_SECS="${SANDBOX_SESSION_WAIT_SECS:-15}"
is_positive_int "${SANDBOX_SESSION_WAIT_SECS}" || SANDBOX_SESSION_WAIT_SECS=15
SANDBOX_PROBE_IMAGE="ai-validation-probe:local"
SANDBOX_LINGER_DIR="${SANDBOX_LINGER_DIR:-/var/lib/systemd/linger}"

# Start a systemd user session for the sandbox user (linger keeps
# user@<uid>.service running without a login). Returns 0 once the user bus
# and the user slice both exist.
sandbox_systemd_session()
{
	local user="$1" uid="$2" waited=0
	[ -d "${SANDBOX_SYSTEMD_RUN_DIR}" ] || return 1
	command -v loginctl >/dev/null 2>&1 || return 1
	sudo -n loginctl enable-linger "${user}" >/dev/null 2>&1 || return 1
	sudo -n systemctl start "user@${uid}.service" >/dev/null 2>&1 || true
	until sudo -n test -S "/run/user/${uid}/bus" && [ -d "${SANDBOX_CGROUP_ROOT}/user.slice/user-${uid}.slice" ]; do
		[ "${waited}" -ge "${SANDBOX_SESSION_WAIT_SECS}" ] && return 1
		sleep 1
		waited=$((waited + 1))
	done
}

# Undo sandbox_systemd_session (#7380): disable linger and stop the sandbox
# user's systemd user manager, so no manager is left that a container could
# ask to start a transient unit in the host PID namespace. Best effort, always
# returns 0; `selfcheck` (user_manager_route_open) is the enforcer. Callers
# run it before (re)creating the runtime dir, because stopping the user
# manager can remove the /run/user/<uid> tmpfs.
sandbox_release_systemd_session()
{
	local user="$1" uid="$2" waited=0
	[[ "${uid}" =~ ^[0-9]+$ ]] || return 0
	if [ -z "$(sudo -n pgrep -u "${uid}" -x systemd 2>/dev/null || true)" ] \
		&& ! sudo -n test -e "${SANDBOX_LINGER_DIR}/${user}" >/dev/null 2>&1; then
		return 0
	fi
	if command -v loginctl >/dev/null 2>&1; then
		sudo -n loginctl disable-linger "${user}" >/dev/null 2>&1 || true
	fi
	sudo -n systemctl stop "user@${uid}.service" >/dev/null 2>&1 || true
	sudo -n systemctl stop "user-runtime-dir@${uid}.service" >/dev/null 2>&1 || true
	while [ -n "$(sudo -n pgrep -u "${uid}" -x systemd 2>/dev/null || true)" ]; do
		[ "${waited}" -ge "${SANDBOX_SESSION_WAIT_SECS}" ] && break
		sleep 1
		waited=$((waited + 1))
	done
	sandbox_log provision ok systemd_user_session_released
	return 0
}

# Root-side check (#7380), read from host pids and the host mount namespace,
# never from files the sandbox writes. Returns 0 when a systemd user manager
# or a bus daemon runs as the sandbox uid, or its user bus or a socket under
# <runtime_dir>/systemd exists: a container started through the rootless
# daemon could bind-mount that socket and start a transient unit in the host
# PID namespace. Returns 1 when the uid cannot be resolved, 2 when the route
# is closed. Rootlesskit's and containerd's own sockets are not counted:
# they cannot start host processes, and every run has them.
user_manager_route_open()
{
	local uid rt name out rc
	uid="$(sandbox_uid || true)"
	[[ "${uid}" =~ ^[0-9]+$ ]] || return 1
	rt="/run/user/${uid}"
	# The runtime dir holds the daemon's docker.sock, so it exists here; a
	# failed check means sudo denies `test`, and a denied `test -S`/`test -e`
	# below would read as "no socket". A denied pgrep is already caught by
	# docker_pidns_isolated, which needs a pgrep match.
	sudo -n test -d "${rt}" >/dev/null 2>&1 || return 1
	for name in systemd dbus-daemon dbus-broker; do
		rc=0
		out="$(sudo -n pgrep -u "${uid}" -x "${name}" 2>/dev/null)" || rc=$?
		[ -n "${out}" ] && return 0
		# pgrep exits 1 for no match; 2 or more is an error, so the state is
		# unknown and selfcheck fails closed.
		[ "${rc}" -le 1 ] || return 1
	done
	if sudo -n test -S "${rt}/bus" >/dev/null 2>&1; then
		return 0
	fi
	if sudo -n test -e "${rt}/systemd" >/dev/null 2>&1; then
		rc=0
		out="$(sudo -n find "${rt}/systemd" -xdev -type s -print -quit 2>/dev/null)" || rc=$?
		[ -n "${out}" ] && return 0
		# A failed search of an existing directory may hide a socket.
		[ "${rc}" -eq 0 ] || return 1
	fi
	return 2
}

# Start the sandbox user's rootless dockerd and wait until it answers.
# <mode> is `systemd` (inside the user session) or `cgroupfs`.
start_rootless_dockerd()
{
	local mode="$1" home="$2" runtime_dir="$3" waited=0
	# --pidns: rootlesskit gives the daemon (and every container it starts,
	# `--pid=host` ones included) its own PID namespace, so the harness cannot
	# reach runner processes through Docker (#7349). selfcheck verifies it.
	local -a daemon_env=("PATH=${SANDBOX_PATH}" "HOME=${home}" "XDG_RUNTIME_DIR=${runtime_dir}" "DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--pidns") daemon_args=()
	if [ "${mode}" = systemd ]; then
		daemon_env+=("DBUS_SESSION_BUS_ADDRESS=unix:path=${runtime_dir}/bus")
	else
		daemon_args+=(--exec-opt native.cgroupdriver=cgroupfs)
	fi
	sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- env -i "${daemon_env[@]}" \
		setsid dockerd-rootless.sh "${daemon_args[@]}" </dev/null >/dev/null 2>&1 &
	until rootless_docker_ready; do
		[ "${waited}" -ge "${VALIDATION_HARNESS_SANDBOX_DOCKER_START_TIMEOUT}" ] && return 1
		sleep 2
		waited=$((waited + 2))
	done
}

# Stop the sandbox user's daemon. The signals are sent as the sandbox user, so
# they can only reach processes that user already owns.
stop_rootless_dockerd()
{
	local runtime_dir="$1" waited=0 name
	for name in dockerd rootlesskit; do
		sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- pkill -TERM -x "${name}" >/dev/null 2>&1 || true
	done
	while sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- pgrep -x rootlesskit >/dev/null 2>&1; do
		if [ "${waited}" -ge 30 ]; then
			for name in dockerd containerd rootlesskit; do
				sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- pkill -KILL -x "${name}" >/dev/null 2>&1 || true
			done
			break
		fi
		sleep 1
		waited=$((waited + 1))
	done
	sudo -n -u "${VALIDATION_HARNESS_SANDBOX_USER}" -- rm -rf -- "${runtime_dir}/docker.pid" "${runtime_dir}/docker.sock" "${runtime_dir}/dockerd-rootless" >/dev/null 2>&1 || true
}

# Import a one-binary image (the host's /usr/bin/true and the libraries ldd
# lists for it) into the sandbox daemon, so the probe needs no registry pull.
# Returns 0 when the image exists, 1 when the host cannot assemble it (no ldd,
# no files, tar failed), 3 when the daemon refused the import.
build_probe_image()
{
	as_sandbox docker image inspect "${SANDBOX_PROBE_IMAGE}" >/dev/null 2>&1 && return 0
	command -v ldd >/dev/null 2>&1 || return 1
	local -a files=()
	local path
	while IFS= read -r path; do
		[ -n "${path}" ] && [ -f "${path}" ] && files+=("${path#/}")
	done < <(printf '/usr/bin/true\n'; ldd /usr/bin/true 2>/dev/null | grep -oE '(^|[[:space:]])/[^[:space:]]+' | tr -d '[:blank:]')
	[ "${#files[@]}" -ge 1 ] || return 1
	set +e
	local import_err_file
	import_err_file="$(mktemp 2>/dev/null)" || import_err_file=/dev/null
	tar -C / -chf - -- "${files[@]}" 2>/dev/null | as_sandbox docker import --change 'CMD ["/usr/bin/true"]' - "${SANDBOX_PROBE_IMAGE}" >/dev/null 2>"${import_err_file}"
	local -a import_status=("${PIPESTATUS[@]}")
	set -e
	# The daemon's refusal text and exit code, for the caller's log line.
	SANDBOX_PROBE_IMPORT_RC="${import_status[1]}"
	SANDBOX_PROBE_IMPORT_ERR="$(cat -- "${import_err_file}" 2>/dev/null || true)"
	[ "${import_err_file}" = /dev/null ] || rm -f -- "${import_err_file}"
	[ "${import_status[0]}" -eq 0 ] && [ "${import_status[1]}" -eq 0 ] && return 0
	# tar killed by SIGPIPE (141) means the daemon side closed the stream first.
	if [ "${import_status[1]}" -ne 0 ] && { [ "${import_status[0]}" -eq 0 ] || [ "${import_status[0]}" -eq 141 ]; }; then
		return 3
	fi
	return 1
}

# Start (and remove) one container: 0 when it ran, 1 when the daemon refused
# to start it (`docker run` exit 125, or 126/127 with a cgroup error) or to
# import the probe image, 2 when the probe is inconclusive (the host could not
# assemble the image, or the image's command could not run: 126/127 without a
# cgroup error), so a host-side probe problem is never mistaken for a broken
# daemon.
rootless_docker_container_probe()
{
	local probe_err="" rc=0 build_rc=0
	build_probe_image || build_rc=$?
	if [ "${build_rc}" -eq 3 ]; then
		printf 'VALIDATION_HARNESS_SANDBOX phase=provision probe_exit=%s stage=import detail=%s\n' "${SANDBOX_PROBE_IMPORT_RC:-unknown}" \
			"$(printf '%s' "${SANDBOX_PROBE_IMPORT_ERR:-docker import refused}" | tr -c '[:print:]' ' ' | cut -c1-300)" >&2
		return 1
	fi
	[ "${build_rc}" -eq 0 ] || return 2
	probe_err="$(as_sandbox docker run --rm --network none "${SANDBOX_PROBE_IMAGE}" 2>&1 >/dev/null)" || rc=$?
	[ "${rc}" -eq 0 ] && return 0
	# The daemon's own error line, for the job log (no credentials reach the sandbox).
	printf 'VALIDATION_HARNESS_SANDBOX phase=provision probe_exit=%s detail=%s\n' "${rc}" \
		"$(printf '%s' "${probe_err}" | tr -c '[:print:]' ' ' | cut -c1-300)" >&2
	[ "${rc}" -eq 125 ] && return 1
	# The Docker CLI (runStartContainerErr) maps a container-start error whose
	# text contains "no such file or directory" to 127 and "permission denied"
	# to 126, so the cgroup failure (#7063) arrives as 126/127, not 125.
	case "${probe_err}" in
		*cgroup*) return 1 ;;
	esac
	return 2
}

# Bring up a daemon that can start containers. The default (`cgroupfs`)
# releases any systemd user session left for the sandbox user and uses
# cgroupfs. Opt-in `auto` tries a systemd user session first and falls back to
# cgroupfs (releasing the session) when it cannot be set up or a container
# would not start under it; `selfcheck` then refuses while a user manager runs
# (#7380). Every outcome is logged; a daemon whose test container fails under
# both drivers fails provisioning closed (container_start_failed).
sandbox_start_verified_dockerd()
{
	local user="$1" uid="$2" home="$3" runtime_dir="$4" mode="" probe_rc=0
	case "${VALIDATION_HARNESS_SANDBOX_CGROUP_MODE}" in
		auto|cgroupfs) ;;
		*) sandbox_log provision ok cgroup_mode_unrecognized_using_cgroupfs ;;
	esac
	case "${VALIDATION_HARNESS_SANDBOX_CGROUP_MODE}" in
		auto)
			if sandbox_systemd_session "${user}" "${uid}"; then
				mode=systemd
				sandbox_log provision ok systemd_user_session
			else
				mode=cgroupfs
				sandbox_log provision ok systemd_user_session_unavailable
				# enable-linger may have succeeded before the session timed out.
				sandbox_release_systemd_session "${user}" "${uid}"
			fi
			;;
		*)
			mode=cgroupfs
			# Clear a linger left by an earlier opt-in `auto` provision.
			sandbox_release_systemd_session "${user}" "${uid}"
			;;
	esac
	sudo -n install -d -m 0700 -o "${user}" -g "$(id -gn "${user}")" "${runtime_dir}" || sandbox_fail provision runtime_dir_failed
	build_sandbox_env "${home}" "${home}/work" "unix://${runtime_dir}/docker.sock"
	if rootless_docker_ready; then
		# A daemon from an earlier provision in this job: its driver is unknown,
		# so it gets the same container test.
		mode=reused
	else
		start_rootless_dockerd "${mode}" "${home}" "${runtime_dir}" || sandbox_fail provision rootless_dockerd_start_timeout
	fi
	rootless_docker_container_probe || probe_rc=$?
	if [ "${probe_rc}" -eq 1 ] && [ "${mode}" != cgroupfs ]; then
		sandbox_log provision retry "container_start_failed_${mode}"
		stop_rootless_dockerd "${runtime_dir}"
		mode=cgroupfs
		sandbox_release_systemd_session "${user}" "${uid}"
		sudo -n install -d -m 0700 -o "${user}" -g "$(id -gn "${user}")" "${runtime_dir}" || sandbox_fail provision runtime_dir_failed
		start_rootless_dockerd cgroupfs "${home}" "${runtime_dir}" || sandbox_fail provision rootless_dockerd_start_timeout
		probe_rc=0
		rootless_docker_container_probe || probe_rc=$?
	fi
	case "${probe_rc}" in
		0) sandbox_log provision ok "container_probe_${mode}" ;;
		2) sandbox_log provision ok container_probe_inconclusive ;;
		*) sandbox_fail provision container_start_failed ;;
	esac
}

# Docker's apt repository signing key (https://download.docker.com/linux/<distro>/gpg).
# The key is checked against this fingerprint before apt trusts it.
SANDBOX_DOCKER_APT_KEY_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
SANDBOX_DOCKER_APT_KEYRING_DIR="${SANDBOX_DOCKER_APT_KEYRING_DIR:-/etc/apt/keyrings}"
SANDBOX_DOCKER_APT_SOURCES_DIR="${SANDBOX_DOCKER_APT_SOURCES_DIR:-/etc/apt/sources.list.d}"
SANDBOX_OS_RELEASE_FILE="${SANDBOX_OS_RELEASE_FILE:-/etc/os-release}"

sandbox_apt()
{
	sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "$@" >/dev/null 2>&1
}

# Install uidmap and docker-ce-rootless-extras. GitHub's Ubuntu runner images
# install Docker from download.docker.com and then delete that apt source
# (actions/runner-images install-docker.sh, "Cleanup custom repositories"),
# so docker-ce-rootless-extras is not installable from the image's sources.
# When the plain install fails, add Docker's repository for this one install
# (key fingerprint pinned above), prefer the version of the docker-ce already
# installed, and remove the source and key again afterwards.
install_rootless_packages()
{
	sandbox_apt install -y -q uidmap docker-ce-rootless-extras && return 0
	sandbox_apt update -q || true
	sandbox_apt install -y -q uidmap docker-ce-rootless-extras && return 0
	sandbox_apt install -y -q uidmap || return 1

	local distro codename arch key_tmp fingerprint keyring source_list docker_version rc=1
	distro="$(. "${SANDBOX_OS_RELEASE_FILE}" 2>/dev/null && printf '%s' "${ID:-}")"
	codename="$(. "${SANDBOX_OS_RELEASE_FILE}" 2>/dev/null && printf '%s' "${VERSION_CODENAME:-}")"
	arch="$(dpkg --print-architecture 2>/dev/null || true)"
	case "${distro}" in
		ubuntu|debian) ;;
		*) sandbox_log provision fail docker_repo_distro_unsupported; return 1 ;;
	esac
	[[ "${codename}" =~ ^[a-z]+$ ]] && [[ "${arch}" =~ ^[a-z0-9]+$ ]] || { sandbox_log provision fail docker_repo_platform_unknown; return 1; }
	key_tmp="$(mktemp)" || { sandbox_log provision fail docker_repo_tmpfile_failed; return 1; }
	if ! curl -fsSL --retry 3 --max-time 60 -o "${key_tmp}" "https://download.docker.com/linux/${distro}/gpg"; then
		rm -f -- "${key_tmp}"
		sandbox_log provision fail docker_repo_key_download_failed
		return 1
	fi
	fingerprint="$(gpg --show-keys --with-colons "${key_tmp}" 2>/dev/null | awk -F: '$1 == "fpr" { print $10; exit }')"
	if [ "${fingerprint}" != "${SANDBOX_DOCKER_APT_KEY_FINGERPRINT}" ]; then
		rm -f -- "${key_tmp}"
		sandbox_log provision fail docker_repo_key_fingerprint_mismatch
		return 1
	fi
	keyring="${SANDBOX_DOCKER_APT_KEYRING_DIR}/ai-validation-docker.asc"
	source_list="${SANDBOX_DOCKER_APT_SOURCES_DIR}/ai-validation-docker.list"
	if ! { sudo -n install -d -m 0755 "${SANDBOX_DOCKER_APT_KEYRING_DIR}" \
		&& sudo -n install -m 0644 "${key_tmp}" "${keyring}" \
		&& printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/%s %s stable\n' "${arch}" "${keyring}" "${distro}" "${codename}" \
			| sudo -n tee "${source_list}" >/dev/null; }; then
		sandbox_log provision fail docker_repo_source_write_failed
	elif ! sandbox_apt update -q; then
		sandbox_log provision fail docker_repo_update_failed
	else
		docker_version="$(dpkg-query -W -f='${Version}' docker-ce 2>/dev/null || true)"
		if [ -n "${docker_version}" ] && sandbox_apt install -y -q "docker-ce-rootless-extras=${docker_version}"; then
			rc=0
		elif sandbox_apt install -y -q docker-ce-rootless-extras; then
			rc=0
		else
			sandbox_log provision fail docker_repo_install_failed
		fi
	fi
	rm -f -- "${key_tmp}" || true
	sudo -n rm -f -- "${source_list}" "${keyring}" >/dev/null 2>&1 || true
	if [ "${rc}" -eq 0 ]; then
		sandbox_log provision ok rootless_packages_from_docker_repo
	fi
	return "${rc}"
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
		install_rootless_packages || sandbox_fail provision rootless_packages_unavailable
		command -v dockerd-rootless.sh >/dev/null 2>&1 || sandbox_fail provision rootless_dockerd_missing
	fi

	# Ubuntu 24.04 blocks unprivileged user namespaces for unconfined binaries;
	# rootlesskit needs them.
	if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ]; then
		sudo -n sysctl -q -w kernel.apparmor_restrict_unprivileged_userns=0 >/dev/null 2>&1 \
			|| sandbox_fail provision apparmor_userns_restricted
	fi

	runtime_dir="/run/user/${uid}"
	sandbox_start_verified_dockerd "${user}" "${uid}" "${home}" "${runtime_dir}"

	pidns_supported provision
	ensure_pidns_state_dir provision

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

# Runs as the sandbox user inside the new PID namespace.
read -r -d '' SELFCHECK_PIDNS_PROBE <<'PIDNS_PROBE' || true
caller_pid="$1"; host_ns="$2"
fail() { printf '%s\n' "$1"; exit 1; }
own_ns="$(readlink /proc/self/ns/pid 2>/dev/null || true)"
if [ -z "${own_ns}" ] || [ -z "${host_ns}" ] || [ "${own_ns}" = "${host_ns}" ]; then
	fail pid_namespace_shared
fi
if [ -n "${caller_pid}" ] && [ "${caller_pid}" != "$$" ] && [ "${caller_pid}" != "${PPID}" ] && [ "${caller_pid}" != "1" ] \
	&& [ -e "/proc/${caller_pid}" ]; then
	if cat "/proc/${caller_pid}/environ" >/dev/null 2>&1 || cat "/proc/${caller_pid}/cmdline" >/dev/null 2>&1; then
		fail caller_pid_visible
	fi
fi
case "$(cat /proc/1/comm 2>/dev/null || true)" in
	systemd|init) fail host_init_visible ;;
esac
# The user manager runs in the host PID namespace; every socket under the
# runtime dir except docker.sock (wherever it was moved) must be masked.
# `-xtype` stats the entry; `-type` would trust the directory entry, which
# still says "socket" under the bind mount that masks it (#7355).
rt="/run/user/$(id -u)"
if [ -d "${rt}" ] && [ -n "$(find "${rt}" -xdev ! -type l -xtype s ! -path "${rt}/docker.sock" -print -quit 2>/dev/null)" ]; then
	fail user_manager_reachable
fi
printf 'ok\n'
PIDNS_PROBE

# Reason for a selfcheck probe whose output is empty or malformed (the launch
# itself failed). With VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP
# set to `true` (any letter case) it names the pass and the launcher's exit
# code: probe_failed_<plain|pidns>_rc_<n>. Unset or any other value keeps the
# plain probe_failed. The placeholder gates only this reason text; it comes
# from the unblock judge's round 1 on #7355, and the operator step is to export
# it into the validate job's environment (validate.yml forwards no such
# variable). Probe stderr is never read. Always returns 0.
selfcheck_probe_failed_reason()
{
	local pass="$1" rc="$2" flag="${VALIDATION_HARNESS_SELFCHECK_RUNNER_UNSET_OPERATOR_STEP:-}"
	if [ "${flag,,}" != "true" ]; then
		printf 'probe_failed\n'
		return 0
	fi
	case "${pass}" in
		plain|pidns) ;;
		*) pass="unknown" ;;
	esac
	[[ "${rc}" =~ ^[0-9]{1,3}$ ]] || rc="unknown"
	printf 'probe_failed_%s_rc_%s\n' "${pass}" "${rc}"
	return 0
}

cmd_selfcheck()
{
	validate_config selfcheck
	require_sudo selfcheck
	id -u "${VALIDATION_HARNESS_SANDBOX_USER}" >/dev/null 2>&1 || sandbox_fail selfcheck sandbox_user_missing
	local caller_pid="${1:-${PPID}}" home verdict host_ns probe_out probe_rc
	home="$(sandbox_home)"
	build_sandbox_env "${home}" "${home}/work" "$(sandbox_docker_host)"
	probe_rc=0
	probe_out="$(as_sandbox bash -c "${SELFCHECK_PROBE}" selfcheck "${caller_pid}" \
		"${HOME:-}" "${RUNNER_TEMP:-}" "${RUNTIME_DIR:-}" "${GITHUB_WORKSPACE:-}" 2>/dev/null)" || probe_rc=$?
	verdict="$(printf '%s\n' "${probe_out}" | tail -n 1)"
	if [ "${verdict}" != "ok" ]; then
		[[ "${verdict}" =~ ^[a-z_]+$ ]] || verdict="$(selfcheck_probe_failed_reason plain "${probe_rc}")"
		sandbox_fail selfcheck "${verdict}"
	fi
	# Second pass inside a PID namespace created the same way as the harness's.
	pidns_supported selfcheck
	host_ns="$(readlink "/proc/$$/ns/pid" 2>/dev/null || true)"
	[ -n "${host_ns}" ] || sandbox_fail selfcheck pid_namespace_unverifiable
	probe_rc=0
	probe_out="$(as_sandbox_in_pidns - - bash -c "${SELFCHECK_PIDNS_PROBE}" pidns-selfcheck "${caller_pid}" "${host_ns}" 2>/dev/null)" || probe_rc=$?
	verdict="$(printf '%s\n' "${probe_out}" | tail -n 1)"
	if [ "${verdict}" != "ok" ]; then
		[[ "${verdict}" =~ ^[a-z_]+$ ]] || verdict="$(selfcheck_probe_failed_reason pidns "${probe_rc}")"
		sandbox_fail selfcheck "${verdict}"
	fi
	# The harness reaches Docker; a daemon in the host PID namespace would let
	# a `--pid=host` container list runner processes.
	docker_pidns_isolated "${host_ns}" || sandbox_fail selfcheck docker_pid_namespace_shared
	# A container started through that daemon is outside the harness's mount
	# namespace; a systemd user manager for the sandbox uid would let it start
	# processes in the host PID namespace (#7380). An unknown state (rc 1)
	# fails closed too.
	local route_rc=0
	user_manager_route_open || route_rc=$?
	[ "${route_rc}" -eq 2 ] || sandbox_fail selfcheck user_manager_reachable_from_container
	sandbox_log selfcheck ok
	sandbox_status "ok selfcheck"
}

# Builds the screened copy as an uncompressed tar on stdout. Runs as the
# runner user over the trusted workspace; the sandbox user only extracts it.
# The copy holds tracked files and untracked files that are not ignored
# (`git ls-files --cached --others --exclude-standard`), plus `validation/**`
# and `.ai/validate.yml`, all screened by `readonly_allowed`. Staging alone
# additionally admits env templates (`.env.example`, `.env.sample`,
# `.env.template`) and an `.npmrc` whose content holds no credential material
# (stage_npmrc_verdict); the shared `readonly_allowed` is unchanged because
# agent sandboxes use it too. Logs name the file only, never its content.
read -r -d '' STAGE_PY <<'PY' || true
import io, os, re, stat, subprocess, sys, tarfile
from pathlib import Path

helper_dir, workspace, *overlay_args = sys.argv[1:]
sys.path.insert(0, helper_dir)
from codex_isolated_workspace import readonly_allowed  # trusted sibling copy

workspace = Path(workspace)
env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
listed = subprocess.run(
	["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
	cwd=workspace, env=env, check=True, capture_output=True,
).stdout.split(b"\0")
names = {item.decode("utf-8", "surrogateescape") for item in listed if item}
# Staging-only admissions (env templates, .npmrc) apply only to names git lists
# (tracked, or untracked and not ignored); the validation/ walk below can add
# ignored files, which keep the plain readonly_allowed screen.
git_listed = frozenset(names)
for root in ("validation",):
	base = workspace / root
	if base.is_dir() and not base.is_symlink():
		for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
			dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
			for filename in filenames:
				names.add(str((Path(dirpath) / filename).relative_to(workspace)))
names.add(".ai/validate.yml")
# `head-support:<path>` arguments name support helpers whose committed HEAD
# blob is staged instead of the working-tree file (#8065). The prefix is
# checked first, so a path is never read as a `dest=src` overlay.
HEAD_SUPPORT_PREFIX = "head-support:"
head_support_paths = set()
overlays = {}
# "HEAD:<path>" arguments name support files the host overwrote with trusted
# copies; the screened copy takes their committed bytes instead (stage_from_head).
# An entry ending in "/" covers every listed name under that directory.
support_from_head = set()
support_from_head_dirs = []
for item in overlay_args:
	if item.startswith(HEAD_SUPPORT_PREFIX):
		requested = item[len(HEAD_SUPPORT_PREFIX):]
		parts = Path(requested).parts
		if requested and not requested.startswith("/") and ".." not in parts and "." not in parts \
			and "//" not in requested and not requested.endswith("/") and all(c not in requested for c in "\0\n\r"):
			head_support_paths.add(requested)
		continue
	if item.startswith("HEAD:") and "=" not in item:
		head_name = item[len("HEAD:"):]
		if head_name and not head_name.startswith("/") and ".." not in head_name.split("/"):
			if head_name.endswith("/"):
				support_from_head_dirs.append(head_name)
			else:
				support_from_head.add(head_name)
		continue
	dest, _, src = item.partition("=")
	overlays[dest] = src

out = tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT)

def add(path, arcname):
	info = os.lstat(path)
	if not stat.S_ISREG(info.st_mode):
		return False
	member = tarfile.TarInfo(arcname)
	member.size = info.st_size
	member.mode = 0o755 if info.st_mode & 0o111 else 0o644
	member.mtime = int(info.st_mtime)
	with open(path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as handle:
		out.addfile(member, handle)
	return True

def stage_from_head(path, arcname):
	"""Stage the HEAD blob of a tracked regular file instead of its working-tree bytes.

	Returns False (nothing staged) when the working-tree entry is not a regular
	file, HEAD does not hold the path as a regular blob (untracked, symlink,
	submodule, unborn HEAD), or git fails; the caller then keeps the
	working-tree read. cat-file reads the raw object, so no filter runs.
	"""
	try:
		info = os.lstat(path)
	except OSError:
		return False
	if not stat.S_ISREG(info.st_mode):
		return False
	git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
	tree = subprocess.run(git + ["ls-tree", "-z", "HEAD", "--", arcname], cwd=workspace, env=env, check=False, capture_output=True)
	if tree.returncode != 0:
		# An unborn HEAD is a normal fallback; any other git failure is logged.
		head = subprocess.run(git + ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd=workspace, env=env, check=False, capture_output=True)
		if head.returncode == 0:
			stage_log("skip", "support_from_head_git_failed", arcname)
		return False
	records = [record for record in tree.stdout.split(b"\0") if record]
	if len(records) != 1:
		return False
	meta, _, tree_path = records[0].partition(b"\t")
	fields = meta.split(b" ")
	if len(fields) != 3 or fields[1] != b"blob" or fields[0] not in (b"100644", b"100755") or tree_path.decode("utf-8", "surrogateescape") != arcname:
		return False
	# The blob is buffered whole, so a committed file over 8 MiB keeps the
	# streamed working-tree read instead (same cap as review MAX_FILE).
	size = subprocess.run(git + ["cat-file", "-s", fields[2].decode("ascii", "replace")], cwd=workspace, env=env, check=False, capture_output=True)
	if size.returncode != 0 or not size.stdout.strip().isdigit() or int(size.stdout) > 8 * 1024 * 1024:
		stage_log("skip", "support_from_head_too_large" if size.returncode == 0 else "support_from_head_git_failed", arcname)
		return False
	blob = subprocess.run(git + ["cat-file", "blob", fields[2].decode("ascii", "replace")], cwd=workspace, env=env, check=False, capture_output=True)
	if blob.returncode != 0:
		stage_log("skip", "support_from_head_git_failed", arcname)
		return False
	member = tarfile.TarInfo(arcname)
	member.size = len(blob.stdout)
	member.mode = 0o755 if fields[0] == b"100755" else 0o644
	member.mtime = int(info.st_mtime)
	out.addfile(member, io.BytesIO(blob.stdout))
	stage_log("ok", "support_from_head", arcname)
	return True

# Staging-only admissions. Real env files (.env, .env.local, ...) and every
# other readonly_allowed exclusion (*.pem, *.key, .config, ...) stay dropped.
STAGE_ENV_TEMPLATE_NAMES = frozenset((".env.example", ".env.sample", ".env.template"))
STAGE_NPMRC_MAX_BYTES = 64 * 1024
# Any line holding one of these (lower-cased) is treated as credential material.
STAGE_NPMRC_DENY_MARKERS = (
	"_authtoken", "_auth", "_password", "password", "username", "email", "certfile", "keyfile", "cafile",
	"token", "secret", "${", "-----begin",
)
STAGE_NPMRC_DENY_KEYS = frozenset(("cert", "key", "ca", "otp"))
STAGE_NPMRC_URL_USERINFO = re.compile(r"://[^/\s]*@")

def stage_log(outcome, reason, name):
	print(f"VALIDATION_HARNESS_SANDBOX phase=stage outcome={outcome} reason={reason} path={name}", file=sys.stderr)

def stage_special_kind(name):
	"""Kind of staging-only admission for a name readonly_allowed refused, else None."""
	pure = Path(name)
	parent = str(pure.parent)
	if parent != "." and not readonly_allowed(parent):
		return None
	if pure.name in STAGE_ENV_TEMPLATE_NAMES:
		return "env_template"
	if pure.name == ".npmrc":
		return "npmrc"
	return None

def stage_npmrc_verdict(data):
	"""Return None when .npmrc bytes hold no credential material, else a reject reason."""
	if b"\0" in data:
		return "npmrc_undecodable"
	try:
		text = data.decode("utf-8")
	except UnicodeDecodeError:
		return "npmrc_undecodable"
	for raw_line in text.splitlines():
		line = raw_line.strip().lower()
		if not line:
			continue
		if line.startswith("//") or STAGE_NPMRC_URL_USERINFO.search(line):
			return "npmrc_credential"
		if any(marker in line for marker in STAGE_NPMRC_DENY_MARKERS):
			return "npmrc_credential"
		if line.split("=", 1)[0].strip() in STAGE_NPMRC_DENY_KEYS:
			return "npmrc_credential"
	return None

def add_npmrc(path, arcname):
	"""Read the file once without following symlinks, screen it, and stage those exact bytes."""
	info = os.lstat(path)
	if stat.S_ISLNK(info.st_mode):
		return "npmrc_symlink"
	if not stat.S_ISREG(info.st_mode):
		return "npmrc_not_regular"
	if info.st_size > STAGE_NPMRC_MAX_BYTES:
		return "npmrc_too_large"
	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
	with os.fdopen(fd, "rb") as handle:
		opened = os.fstat(handle.fileno())
		if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
			return "npmrc_changed"
		data = handle.read(STAGE_NPMRC_MAX_BYTES + 1)
	if len(data) > STAGE_NPMRC_MAX_BYTES:
		return "npmrc_too_large"
	verdict = stage_npmrc_verdict(data)
	if verdict is not None:
		return verdict
	member = tarfile.TarInfo(arcname)
	member.size = len(data)
	member.mode = 0o644
	member.mtime = int(opened.st_mtime)
	out.addfile(member, io.BytesIO(data))
	return None

def head_blob(name):
	"""Return (mode, bytes) of the regular HEAD blob at name, else None."""
	git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
	try:
		listing = subprocess.run(
			git + ["ls-tree", "-z", "HEAD", "--", name],
			cwd=workspace, env=env, capture_output=True, timeout=60,
		)
		if listing.returncode != 0:
			return None
		records = [record for record in listing.stdout.split(b"\0") if record]
		if len(records) != 1:
			return None
		meta, _, path = records[0].partition(b"\t")
		fields = meta.split(b" ")
		if len(fields) != 3 or path.decode("utf-8", "surrogateescape") != name:
			return None
		mode, kind, oid = (field.decode("ascii") for field in fields)
		if kind != "blob" or mode not in ("100644", "100755") or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
			return None
		# Check the size first so an oversized blob is never read into memory.
		size = subprocess.run(
			git + ["cat-file", "-s", oid],
			cwd=workspace, env=env, capture_output=True, timeout=60,
		)
		if size.returncode != 0 or not 0 <= int(size.stdout.strip()) <= 8 * 1024 * 1024:
			return None
		blob = subprocess.run(
			git + ["cat-file", "blob", oid],
			cwd=workspace, env=env, capture_output=True, timeout=60,
		)
		# A support helper is small; refuse an oversized blob (fall back).
		if blob.returncode != 0 or len(blob.stdout) > 8 * 1024 * 1024:
			return None
		return mode, blob.stdout
	except (OSError, subprocess.SubprocessError, ValueError):
		return None

def add_head_blob(name, mode, data):
	member = tarfile.TarInfo(name)
	member.size = len(data)
	member.mode = 0o755 if mode == "100755" else 0o644
	try:
		member.mtime = int(os.lstat(workspace / name).st_mtime)
	except OSError:
		member.mtime = 0
	out.addfile(member, io.BytesIO(data))

for name in sorted(names):
	if name in overlays:
		continue
	if name in head_support_paths and name in git_listed and readonly_allowed(name):
		committed = head_blob(name)
		if committed is not None:
			add_head_blob(name, *committed)
			stage_log("ok", "support_from_head", name)
			continue
		# Not a regular HEAD blob, or the git read failed: say so, then stage
		# the working-tree copy as before.
		stage_log("skip", "support_from_head_unavailable", name)
	special = None
	if not readonly_allowed(name):
		special = stage_special_kind(name) if name in git_listed else None
		if special is None:
			continue
	path = workspace / name
	try:
		parent = workspace
		for part in Path(name).parts[:-1]:
			parent = parent / part
			if not stat.S_ISDIR(os.lstat(parent).st_mode):
				raise OSError("non-directory parent")
		if special == "npmrc":
			try:
				rejected = add_npmrc(path, name)
			except OSError:
				rejected = "npmrc_unreadable"
			if rejected is None:
				stage_log("ok", "npmrc_admitted", name)
			else:
				stage_log("skip", rejected, name)
		elif special == "env_template":
			if add(path, name):
				stage_log("ok", "env_template_admitted", name)
			else:
				stage_log("skip", "env_template_not_regular", name)
		elif (name in support_from_head or any(name.startswith(prefix) for prefix in support_from_head_dirs)) and stage_from_head(path, name):
			pass
		else:
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

# Off switch for staging support helpers from HEAD (#8065). Unset, empty or
# `true` (any letter case) is on; `false` is off; any other value is off with
# a warning that never prints the value. Defaulted here: validate.yml does
# not forward it.
sandbox_support_from_head_enabled()
{
	local value="${VALIDATION_SANDBOX_SUPPORT_FROM_HEAD_ENABLED-}"
	value="${value,,}"
	case "${value}" in
		""|true) return 0 ;;
		false) return 1 ;;
	esac
	printf '::warning::VALIDATION_HARNESS_SANDBOX phase=stage outcome=skip reason=support_from_head_invalid_switch\n' >&2
	return 1
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
	local home work compose state_base
	home="$(sandbox_home)"
	work="$(sandbox_work_dir)"
	state_base="$(pidns_state_base)"
	build_sandbox_env "${home}" "${home}/work" "$(sandbox_docker_host)"
	# Root kills only the `unshare` pid root itself recorded in the root-owned
	# state directory. ${work}.pgid (written by older helper copies) lives in
	# a directory the sandbox user owns, so it is removed but never read.
	pidns_kill_recorded "${state_base}.pid"
	compose="${work}/validation/docker-compose.test.yml"
	if sudo -n test -f "${compose}"; then
		as_sandbox bash -c 'cd "$1" && docker compose -f "$2" down -v --remove-orphans' cleanup "${work}" "${compose}" >/dev/null 2>&1 || true
	fi
	as_sandbox rm -rf -- "${work}" "${work}.pgid" >/dev/null 2>&1 || true
	sudo -n rm -f -- "${state_base}.pid" "${state_base}.started" >/dev/null 2>&1 || true
	sandbox_log cleanup ok
}

cmd_run()
{
	validate_config run
	local entry="${1:?run requires an entry script}"
	shift
	require_sudo run
	id -u "${VALIDATION_HARNESS_SANDBOX_USER}" >/dev/null 2>&1 || sandbox_fail run sandbox_user_missing
	local home work workspace entry_rel stage_status extract_status harness_exit copyback_dest state_base pidfile started
	local -a overlays=() head_support_args=()
	home="$(sandbox_home)"
	work="$(sandbox_work_dir)"
	workspace="$(pwd -P)"
	copyback_dest="${VALIDATION_HARNESS_SANDBOX_COPYBACK_DEST:-${workspace}/validation/logs}"
	case "${copyback_dest}" in
		/*) ;;
		*) sandbox_fail run invalid_copyback_dest ;;
	esac
	build_sandbox_env "${home}" "${work}/.tmp" "$(sandbox_docker_host)"
	rootless_docker_ready || sandbox_fail run rootless_docker_unavailable
	[ -f "${SANDBOX_HELPER_DIR}/codex_isolated_workspace.py" ] || sandbox_fail run support_missing
	pidns_supported run

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
	# Self-repo support staging writes trusted copies over these tracked files
	# so the host runs only trusted code (#6865). The sandbox machinery does
	# not run them (STAGE_PY imports codex_isolated_workspace from
	# SANDBOX_HELPER_DIR); only the branch's own tests, inside the
	# credential-free sandbox, read or run them. So the copy under test takes
	# their committed HEAD bytes and those tests see the branch's code (#7913, #8064).
	# The renderer and the validation-harness templates are overwritten the
	# same way; the harness tests are rendered on the host before staging, so
	# their HEAD copies are only read by the branch's own tests. An entry
	# ending in "/" covers every tracked file under that directory.
	# validate_driver.sh is left out on purpose: the sandbox runs it, so it
	# stays the trusted overlay above, and a branch that changes it together
	# with tests that read it still sees the trusted driver.
	local -a support_from_head_args=(
		"HEAD:scripts/validate_process.sh"
		"HEAD:scripts/validation_harness_sandbox.sh"
		"HEAD:scripts/codex_isolated_workspace.py"
		"HEAD:scripts/templates/slot_manifest.schema.json"
		"HEAD:scripts/render_validation_templates.py"
		"HEAD:workflow-templates/validation-harness/"
	)
	# Stage these support helpers from their committed HEAD blob, not from the
	# working tree a staging step may have overwritten (#8065). Untracked or
	# non-regular paths keep the working-tree copy; the overlay above wins.
	if sandbox_support_from_head_enabled; then
		local head_support_path
		for head_support_path in \
			scripts/validate_process.sh \
			scripts/validation_harness_sandbox.sh \
			scripts/codex_isolated_workspace.py \
			scripts/templates/slot_manifest.schema.json; do
			head_support_args+=("head-support:${head_support_path}")
		done
	fi

	cmd_cleanup >/dev/null 2>&1 || true
	ensure_pidns_state_dir run
	state_base="$(pidns_state_base)"
	pidfile="${state_base}.pid"
	started="${state_base}.started"
	sudo -n rm -f -- "${pidfile}" "${started}" >/dev/null 2>&1 || true
	build_sandbox_env "${home}" "${work}/.tmp" "$(sandbox_docker_host)"
	as_sandbox mkdir -p -- "${work}/.tmp" || sandbox_fail run work_dir_failed
	set +e
	python3 -I -c "${STAGE_PY}" "${SANDBOX_HELPER_DIR}" "${workspace}" "${head_support_args[@]}" "${overlays[@]}" "${support_from_head_args[@]}" \
		| as_sandbox tar -x --no-same-owner --no-same-permissions -C "${work}" -f -
	stage_status=("${PIPESTATUS[@]}")
	set -e
	if [ "${stage_status[0]}" -ne 0 ] || [ "${stage_status[1]}" -ne 0 ]; then
		sandbox_fail run stage_failed
	fi
	as_sandbox test -f "${work}/${entry_rel}" || sandbox_fail run entry_missing
	sandbox_log run ok staged

	set +e
	# The harness runs inside its own PID namespace; cleanup stops it through
	# the root-recorded `unshare` pid even after the caller SIGKILLs this
	# helper (signals to sudo do not reach the sandbox user's processes).
	as_sandbox_in_pidns "${pidfile}" "${started}" setsid --wait bash -c 'cd "$1" || exit 1; shift; exec bash "$@"' \
		harness "${work}" "${entry_rel}" "$@"
	harness_exit=$?
	set -e
	# Only a marker root wrote inside the namespace proves the harness ran
	# there; never retry without the namespace.
	sudo -n test -f "${started}" || sandbox_fail run pid_namespace_launch_failed
	sudo -n rm -f -- "${pidfile}" "${started}" >/dev/null 2>&1 || true

	set +e
	as_sandbox bash -c 'cd "$1" && if [ -d validation/logs ]; then tar -c -f - validation/logs; else tar -c -f - --files-from /dev/null; fi' \
		copyback "${work}" 2>/dev/null \
		| cmd_ingest_logs "${copyback_dest}" "validation/logs"
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

# One call for callers that cannot sequence subcommands themselves
# (scripts/validation_refresh_runner.py): prove isolation against the
# credentialed caller's pid, then run. Either failure exits 3.
cmd_checked_run()
{
	local caller_pid="${1:-}"
	[[ "${caller_pid}" =~ ^[0-9]+$ ]] || sandbox_fail selfcheck invalid_caller_pid
	shift
	cmd_selfcheck "${caller_pid}"
	cmd_run "$@"
}

main()
{
	local command="${1:-}"
	[ "$#" -gt 0 ] && shift
	case "${command}" in
		provision) cmd_provision "$@" ;;
		selfcheck) cmd_selfcheck "$@" ;;
		run) cmd_run "$@" ;;
		checked-run) cmd_checked_run "$@" ;;
		cleanup) cmd_cleanup "$@" ;;
		print-env-names) cmd_print_env_names "$@" ;;
		ingest-logs) cmd_ingest_logs "$@" ;;
		*)
			echo "usage: $(basename -- "$0") {provision|selfcheck [pid]|run <entry> [args]|checked-run <pid> <entry> [args]|cleanup|print-env-names|ingest-logs <dest> [prefix]}" >&2
			exit 2
			;;
	esac
}

# Sourcing (tests) defines the functions without running a subcommand.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	main "$@"
fi
