#!/usr/bin/env bash
# Editor isolation preflight — sourced (never executed) by
# scripts/review_run_reviewers.sh and scripts/review_apply_fixes.sh.
#
# Why this exists
# ---------------
# The review editor launches as EDITOR_ISOLATION_USER (default `nobody`)
# under `sudo -n -u <user> env -i ...`. Review runs 34304993091 and
# 34320556598 on PR #4057 each burned three editor attempts on
#
#   opencode-editor: line 1: .../scripts/opencode_helpers.sh: Permission denied
#
# after 1.5-2.5 hours of reviewer fan-out, and nothing in the log named the
# path component the unprivileged identity could not traverse. This helper
# probes every path the isolated editor must reach *as that identity* and,
# on denial, walks the path from `/` downward and reports the first
# component that identity cannot traverse or read, with its mode and owner.
#
# Two entry points are shared by both callers:
#
#   editor_isolation_prepare_shared_paths
#     Grants traverse/read on the runner-owned, secret-free paths the
#     isolated editor needs: `o+rX` on the staged support bundle
#     (SUPPORT_ROOT_DIR: scripts, prompts, ai-memory schemas — all committed
#     repository content) and 0711 on RUNTIME_DIR (traverse only, no
#     listing; matches the `chmod 0711 "${RUNTIME_DIR}"` the workflow
#     definition applies once it runs from this commit). Idempotent. Never
#     touches GITHUB_WORKSPACE, the source checkout, `.git`, or the runner
#     command-file directory — those stay closed to the editor identity.
#
#   editor_isolation_preflight_probe
#     Returns 0 when every required path is reachable as the isolation
#     identity and prints one `EDITOR_ISOLATION_PREFLIGHT_OK` line.
#     Returns 1 otherwise and prints one `::error::` line per denied path:
#
#       EDITOR_ISOLATION_PREFLIGHT_DENIED user=<u> kind=<k> path=<p>
#         first_denied=<component> mode=<perms> owner=<user:group>
#
#     `first_denied` is the actual blocker; `path` is only what the editor
#     asked for. A preflight run before reviewer fan-out turns a multi-hour
#     cycle into a seconds-long diagnosis that names the exact directory
#     to fix.
#
#   editor_isolation_open_ancestor_traverse /
#   editor_isolation_restore_ancestor_traverse
#     GitHub-hosted runners keep the runner home at 0750 (`/home/runner`,
#     `drwxr-x--- runner:runner`), which closes everything under RUNNER_TEMP
#     (the staged support bundle and the detached workspace) to the
#     isolation identity: runs 34335652907 / 34337926193 on PR #4057 failed
#     the probe with `first_denied=/home/runner` on every RUNNER_TEMP path.
#     The paths cannot move (the workspace root is a documented contract),
#     so `open` walks every probe path from `/` and grants traverse-only
#     (`o+x`, never read or listing) on each ancestor directory the identity
#     cannot traverse, recording the prior mode; `restore` puts every
#     recorded mode back, newest first. Callers hold the grant only for the
#     probe (review_run_reviewers.sh) or for the editor attempt
#     (review_apply_fixes.sh, which closes `.git` and the runner command
#     files *before* opening any ancestor and restores in its cleanup path).
#     Leaves are never touched here: prepare owns the support bundle and
#     RUNTIME_DIR, and the editor stage chowns the workspace itself.
#
# Inputs (environment): EDITOR_ISOLATION_USER, SUPPORT_SCRIPTS_DIR,
#   SUPPORT_ROOT_DIR, RUNTIME_DIR, WORKSPACE_PATH, OPENCODE_HELPERS_PATH,
#   EDITOR_CODEX_PATH (PATH the editor is launched with; defaults to PATH).
# Kill switch: EDITOR_ISOLATION_PREFLIGHT_ENABLED=false (default true).
#
# Every probe is a `sudo -n -u <user> -- test ...`; no GitHub API calls, no
# network, no writes other than the chmod calls in the prepare and
# open/restore functions.

editor_isolation_preflight_enabled()
{
	case "$(printf '%s' "${EDITOR_ISOLATION_PREFLIGHT_ENABLED:-true}" | tr '[:upper:]' '[:lower:]')" in
		0|false|no|off) return 1 ;;
	esac
	return 0
}

# Load runner-owned metadata published by codex_stall_guard.sh. The guard
# starts its child in a new session, so child_pid and process_group_id must
# match. Callers may also bind the metadata to the guard PID they launched.
_editor_isolation_load_process_group_metadata()
{
	local metadata_path="$1" expected_user="$2" expected_guard_pid="${3:-}"
	local require_live_guard="${4:-false}"
	local metadata_key metadata_value metadata_owner metadata_mode metadata_size current_pgid actual_pgid expected_isolated_uid
	local child_stat_text child_stat_tail actual_child_uid actual_child_start_time_ticks
	local guard_stat_text guard_stat_tail actual_guard_uid actual_guard_start_time_ticks
	local -a child_stat_fields=()
	local -a guard_stat_fields=()
	local loaded_guard_pid="" loaded_guard_uid="" loaded_guard_start_time_ticks=""
	local loaded_child_pid="" loaded_process_group_id="" loaded_isolated_user="" loaded_isolated_uid=""
	local loaded_child_uid="" loaded_child_start_time_ticks="" loaded_signal_mode=""
	if [ ! -f "${metadata_path}" ] || [ -L "${metadata_path}" ]; then
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=missing_or_not_regular" >&2
		return 1
	fi
	metadata_owner="$(stat -c '%u' -- "${metadata_path}" 2>/dev/null || true)"
	metadata_mode="$(stat -c '%a' -- "${metadata_path}" 2>/dev/null || true)"
	metadata_size="$(stat -c '%s' -- "${metadata_path}" 2>/dev/null || true)"
	if [ "${metadata_owner}" != "$(id -u)" ] || [ "${metadata_mode}" != "600" ] \
		|| ! [[ "${metadata_size}" =~ ^[0-9]+$ ]] || [ "${metadata_size}" -gt 1024 ]; then
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=ownership_mode_or_size" >&2
		return 1
	fi
	while IFS='=' read -r metadata_key metadata_value; do
		case "${metadata_key}" in
			guard_pid)
				[ -z "${loaded_guard_pid}" ] || return 1
				loaded_guard_pid="${metadata_value}"
				;;
			guard_uid)
				[ -z "${loaded_guard_uid}" ] || return 1
				loaded_guard_uid="${metadata_value}"
				;;
			guard_start_time_ticks)
				[ -z "${loaded_guard_start_time_ticks}" ] || return 1
				loaded_guard_start_time_ticks="${metadata_value}"
				;;
			child_pid)
				[ -z "${loaded_child_pid}" ] || return 1
				loaded_child_pid="${metadata_value}"
				;;
			process_group_id)
				[ -z "${loaded_process_group_id}" ] || return 1
				loaded_process_group_id="${metadata_value}"
				;;
			isolated_user)
				[ -z "${loaded_isolated_user}" ] || return 1
				loaded_isolated_user="${metadata_value}"
				;;
			isolated_uid)
				[ -z "${loaded_isolated_uid}" ] || return 1
				loaded_isolated_uid="${metadata_value}"
				;;
			child_uid)
				[ -z "${loaded_child_uid}" ] || return 1
				loaded_child_uid="${metadata_value}"
				;;
			child_start_time_ticks)
				[ -z "${loaded_child_start_time_ticks}" ] || return 1
				loaded_child_start_time_ticks="${metadata_value}"
				;;
			signal_mode)
				[ -z "${loaded_signal_mode}" ] || return 1
				loaded_signal_mode="${metadata_value}"
				;;
			*)
				echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=unknown_field" >&2
				return 1
				;;
		esac
	done < "${metadata_path}"
	expected_isolated_uid="$(id -u -- "${expected_user}" 2>/dev/null || true)"
	if ! [[ "${loaded_guard_pid}" =~ ^[1-9][0-9]*$ ]] \
		|| ! [[ "${loaded_guard_uid}" =~ ^[0-9]+$ ]] \
		|| ! [[ "${loaded_guard_start_time_ticks}" =~ ^[1-9][0-9]*$ ]] \
		|| ! [[ "${loaded_child_pid}" =~ ^[1-9][0-9]*$ ]] \
		|| ! [[ "${loaded_process_group_id}" =~ ^[1-9][0-9]*$ ]] \
		|| ! [[ "${loaded_isolated_uid}" =~ ^[0-9]+$ ]] \
		|| ! [[ "${loaded_child_uid}" =~ ^[0-9]+$ ]] \
		|| ! [[ "${loaded_child_start_time_ticks}" =~ ^[1-9][0-9]*$ ]] \
		|| [ "${loaded_process_group_id}" -le 1 ] \
		|| [ "${loaded_child_pid}" != "${loaded_process_group_id}" ] \
		|| [ "${loaded_isolated_user}" != "${expected_user}" ] \
		|| [ "${loaded_isolated_uid}" != "${expected_isolated_uid}" ] \
		|| [ "${loaded_signal_mode}" != "privileged" ] \
		|| { [ -n "${expected_guard_pid}" ] && [ "${loaded_guard_pid}" != "${expected_guard_pid}" ]; }; then
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=field_mismatch" >&2
		return 1
	fi
	if [ "${require_live_guard}" = "true" ]; then
		if [ -z "${expected_guard_pid}" ]; then
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=guard_identity_unavailable" >&2
			return 1
		fi
		[ -e "/proc/${loaded_guard_pid}/stat" ] || return 3
		guard_stat_text="$(<"/proc/${loaded_guard_pid}/stat")" || {
			return 3
		}
		guard_stat_tail="${guard_stat_text##*) }"
		read -r -a guard_stat_fields <<< "${guard_stat_tail}"
		actual_guard_start_time_ticks="${guard_stat_fields[19]:-}"
		actual_guard_uid="$(stat -c '%u' -- "/proc/${loaded_guard_pid}" 2>/dev/null || true)"
		if [ "${actual_guard_start_time_ticks}" != "${loaded_guard_start_time_ticks}" ] \
			|| [ "${actual_guard_uid}" != "${loaded_guard_uid}" ]; then
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=guard_identity_mismatch" >&2
			return 1
		fi
	fi
	current_pgid="$(ps -o pgid= -p "${BASHPID:-$$}" 2>/dev/null | tr -d '[:space:]')"
	if [ -n "${current_pgid}" ] && [ "${loaded_process_group_id}" = "${current_pgid}" ]; then
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=caller_group" >&2
		return 1
	fi
	actual_pgid="$(ps -o pgid= -p "${loaded_child_pid}" 2>/dev/null | tr -d '[:space:]')"
	if [ -n "${actual_pgid}" ] && [ "${actual_pgid}" != "${loaded_process_group_id}" ]; then
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=live_group_mismatch" >&2
		return 1
	fi
	if [ -e "/proc/${loaded_child_pid}/stat" ]; then
		child_stat_text="$(<"/proc/${loaded_child_pid}/stat")" || {
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=process_identity_unreadable" >&2
			return 1
		}
		child_stat_tail="${child_stat_text##*) }"
		read -r -a child_stat_fields <<< "${child_stat_tail}"
		actual_child_start_time_ticks="${child_stat_fields[19]:-}"
		actual_child_uid="$(stat -c '%u' -- "/proc/${loaded_child_pid}" 2>/dev/null || true)"
		if [ "${actual_child_start_time_ticks}" != "${loaded_child_start_time_ticks}" ] \
			|| [ "${actual_child_uid}" != "${loaded_child_uid}" ]; then
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=process_identity_mismatch" >&2
			return 1
		fi
	fi
	EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID="${loaded_process_group_id}"
	EDITOR_ISOLATION_TARGET_USER="${loaded_isolated_user}"
	return 0
}

# editor_isolation_process_group_has_members <metadata> <user> [guard_pid]
#   0 when a live (non-zombie) process owned by <user> is still in the
#   recorded group, 1 when none is, 2 when the metadata is unusable. A killed
#   member its parent has not reaped yet is a zombie that pgrep still lists;
#   it holds no resources and cannot touch the workspace, so it is not a
#   survivor.
editor_isolation_process_group_has_members()
{
	local metadata_path="$1" expected_user="$2" expected_guard_pid="${3:-}"
	local member_pid member_state member_stat_path member_stat_text member_stat_tail
	local process_group_member_pids process_group_probe_status
	_editor_isolation_load_process_group_metadata "${metadata_path}" "${expected_user}" "${expected_guard_pid}" || return 2
	if process_group_member_pids="$(pgrep -u "${EDITOR_ISOLATION_TARGET_USER}" -g "${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID}" 2>/dev/null)"; then
		process_group_probe_status=0
	else
		process_group_probe_status=$?
		[ "${process_group_probe_status}" -eq 1 ] && return 1
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_PROBE_FAILED user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} rc=${process_group_probe_status}" >&2
		return 2
	fi
	while read -r member_pid; do
		if ! [[ "${member_pid}" =~ ^[0-9]+$ ]]; then
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_PROBE_FAILED user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} reason=invalid_member_pid" >&2
			return 2
		fi
		member_stat_path="/proc/${member_pid}/stat"
		if ! member_stat_text="$(<"${member_stat_path}")"; then
			[ ! -e "${member_stat_path}" ] && continue
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_PROBE_FAILED user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} pid=${member_pid} reason=stat_unreadable" >&2
			return 2
		fi
		member_stat_tail="${member_stat_text##*) }"
		member_state="${member_stat_tail%% *}"
		if ! [[ "${member_state}" =~ ^[A-Za-z]$ ]]; then
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_PROBE_FAILED user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} pid=${member_pid} reason=stat_malformed" >&2
			return 2
		fi
		[ "${member_state}" = "Z" ] && continue
		return 0
	done <<< "${process_group_member_pids}"
	return 1
}

editor_isolation_signal_process_group()
{
	local metadata_path="$1" expected_user="$2" requested_signal="$3" expected_guard_pid="${4:-}"
	local metadata_load_status=0 process_group_probe_status
	case "${requested_signal}" in
		TERM|KILL) ;;
		*)
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_SIGNAL_INVALID signal=${requested_signal}" >&2
			return 1
			;;
	esac
	_editor_isolation_load_process_group_metadata "${metadata_path}" "${expected_user}" "${expected_guard_pid}" true \
		|| metadata_load_status=$?
	if [ "${metadata_load_status}" -eq 3 ]; then
		if editor_isolation_process_group_has_members "${metadata_path}" "${expected_user}" "${expected_guard_pid}"; then
			echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_METADATA_INVALID path=${metadata_path} reason=guard_identity_unavailable" >&2
			return 1
		else
			process_group_probe_status=$?
			[ "${process_group_probe_status}" -eq 1 ] && return 0
			return 1
		fi
	elif [ "${metadata_load_status}" -ne 0 ]; then
		return 1
	fi
	if sudo -n kill "-${requested_signal}" -- "-${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID}" 2>/dev/null; then
		return 0
	fi
	pgrep -g "${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID}" >/dev/null 2>&1 || process_group_probe_status=$?
	[ "${process_group_probe_status:-0}" -eq 1 ] && return 0
	if [ "${process_group_probe_status:-0}" -ne 0 ]; then
		echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_PROBE_FAILED user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} rc=${process_group_probe_status}" >&2
		return 1
	fi
	echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_SIGNAL_FAILED user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} signal=${requested_signal}" >&2
	return 1
}

editor_isolation_verify_process_group_stopped()
{
	local metadata_path="$1" expected_user="$2" expected_guard_pid="${3:-}"
	local verify_attempts="${EDITOR_ISOLATION_KILL_VERIFY_ATTEMPTS:-20}" verify_index member_status
	if ! [[ "${verify_attempts}" =~ ^[1-9][0-9]*$ ]] || [ "${verify_attempts}" -gt 100 ]; then
		verify_attempts=20
	fi
	for (( verify_index = 0; verify_index < verify_attempts; verify_index++ )); do
		if editor_isolation_process_group_has_members "${metadata_path}" "${expected_user}" "${expected_guard_pid}"; then
			member_status=0
		else
			member_status=$?
			[ "${member_status}" -eq 1 ] && return 0
			return 1
		fi
		sleep 0.1
	done
	echo "::error::EDITOR_ISOLATION_PROCESS_GROUP_SURVIVOR user=${EDITOR_ISOLATION_TARGET_USER} pgid=${EDITOR_ISOLATION_TARGET_PROCESS_GROUP_ID} attempts=${verify_attempts} interval_seconds=0.1" >&2
	return 1
}

# Resolve the support bundle root: SUPPORT_ROOT_DIR is exported by
# stage_workflow_support.sh; fall back to the parent of SUPPORT_SCRIPTS_DIR.
_editor_isolation_support_root()
{
	if [ -n "${SUPPORT_ROOT_DIR:-}" ] && [ -d "${SUPPORT_ROOT_DIR}" ]; then
		printf '%s' "${SUPPORT_ROOT_DIR}"
		return 0
	fi
	if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ] && [ -d "${SUPPORT_SCRIPTS_DIR}" ]; then
		dirname -- "${SUPPORT_SCRIPTS_DIR}"
		return 0
	fi
	return 1
}

editor_isolation_prepare_shared_paths()
{
	local support_root
	if support_root="$(_editor_isolation_support_root)"; then
		# Committed repository content only (scripts/, prompts/, ai-memory
		# schemas, unattended_system_instructions.md). No secrets are staged
		# here; the editor identity must read these to run at all.
		chmod -R o+rX -- "${support_root}" 2>/dev/null || {
			echo "::warning::editor_isolation_prepare_shared_paths: could not chmod o+rX ${support_root}; the isolation preflight will report the consequence." >&2
		}
	fi
	if [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ]; then
		# Traverse-only: the editor reads its 0444 config and its sandbox
		# home beneath RUNTIME_DIR but must not be able to list the
		# runner-owned prompt/diff/ledger files that live beside them.
		chmod 0711 -- "${RUNTIME_DIR}" 2>/dev/null || {
			echo "::warning::editor_isolation_prepare_shared_paths: could not chmod 0711 ${RUNTIME_DIR}; the isolation preflight will report the consequence." >&2
		}
	fi
	return 0
}

# _editor_isolation_test_as <user> <kind> <path>
#   kind: traverse (dir, needs x) | dir (needs r+x) | file (needs r) |
#         exec (needs x)
_editor_isolation_test_as()
{
	local user="$1" kind="$2" path="$3"
	case "${kind}" in
		traverse) sudo -n -u "${user}" -- test -d "${path}" -a -x "${path}" ;;
		dir) sudo -n -u "${user}" -- test -d "${path}" -a -r "${path}" -a -x "${path}" ;;
		file) sudo -n -u "${user}" -- test -f "${path}" -a -r "${path}" ;;
		exec) sudo -n -u "${user}" -- test -f "${path}" -a -x "${path}" ;;
		*) return 2 ;;
	esac
}

# _editor_isolation_first_denied <user> <kind> <path>
#   Prints the first path component (walking from `/`) that <user> cannot
#   traverse, or the final path itself when only the leaf check fails.
_editor_isolation_first_denied()
{
	local user="$1" kind="$2" path="$3"
	local resolved rest comp acc=""
	resolved="$(realpath -m -- "${path}")"
	rest="${resolved#/}"
	while [ -n "${rest}" ]; do
		comp="${rest%%/*}"
		if [ "${rest}" = "${comp}" ]; then
			rest=""
		else
			rest="${rest#*/}"
		fi
		acc="${acc}/${comp}"
		if [ "${acc}" = "${resolved}" ]; then
			break
		fi
		if ! sudo -n -u "${user}" -- test -x "${acc}"; then
			printf '%s' "${acc}"
			return 0
		fi
	done
	printf '%s' "${resolved}"
}

_editor_isolation_describe_path()
{
	local path="$1"
	if [ -e "${path}" ]; then
		stat -c 'mode=%A owner=%U:%G' -- "${path}" 2>/dev/null || printf 'mode=? owner=?'
	else
		printf 'mode=missing owner=missing'
	fi
}

# _editor_isolation_probe_specs
#   Prints one `<kind>|<path>` line per path the isolated editor must reach
#   (kinds as in _editor_isolation_test_as), or `missing|opencode` when the
#   editor PATH carries no opencode binary. Shared by the probe and by the
#   ancestor open/restore pair so both always agree on the path set.
_editor_isolation_probe_specs()
{
	local editor_path="${EDITOR_CODEX_PATH:-${PATH}}"
	local opencode_bin="" node_bin=""

	if [ -n "${OPENCODE_HELPERS_PATH:-}" ]; then
		printf 'file|%s\n' "${OPENCODE_HELPERS_PATH}"
	elif [ -n "${SUPPORT_SCRIPTS_DIR:-}" ]; then
		printf 'file|%s\n' "${SUPPORT_SCRIPTS_DIR}/opencode_helpers.sh"
	fi
	if [ -n "${SUPPORT_SCRIPTS_DIR:-}" ]; then
		printf 'dir|%s\n' "${SUPPORT_SCRIPTS_DIR}"
	fi
	if [ -n "${RUNTIME_DIR:-}" ]; then
		printf 'traverse|%s\n' "${RUNTIME_DIR}"
	fi
	if [ -n "${WORKSPACE_PATH:-}" ] && [ -d "${WORKSPACE_PATH}" ]; then
		printf 'dir|%s\n' "${WORKSPACE_PATH}"
	fi
	opencode_bin="$(PATH="${editor_path}" command -v opencode 2>/dev/null || true)"
	if [ -n "${opencode_bin}" ]; then
		printf 'exec|%s\n' "${opencode_bin}"
	else
		printf 'missing|opencode\n'
	fi
	node_bin="$(PATH="${editor_path}" command -v node 2>/dev/null || true)"
	if [ -n "${node_bin}" ]; then
		printf 'exec|%s\n' "${node_bin}"
	fi
}

# _editor_isolation_chmod <mode> <path>
#   chmod as the runner user first (it owns its own home and RUNNER_TEMP);
#   fall back to passwordless sudo for a root-owned ancestor on a
#   self-hosted runner. sudo is already a prerequisite of the isolation.
_editor_isolation_chmod()
{
	local mode="$1" path="$2"
	chmod "${mode}" -- "${path}" 2>/dev/null && return 0
	sudo -n chmod "${mode}" -- "${path}" 2>/dev/null
}

# _editor_isolation_ancestor_recorded <path>
#   True when <path> already has an entry in EDITOR_ISOLATION_ANCESTOR_RESTORE
#   (so a second open call, or a path shared by two probe specs, never
#   overwrites the original mode with an already-opened one).
_editor_isolation_ancestor_recorded()
{
	local path="$1" entry
	local idx
	for (( idx = 0; idx < ${#EDITOR_ISOLATION_ANCESTOR_RESTORE[@]}; idx++ )); do
		entry="${EDITOR_ISOLATION_ANCESTOR_RESTORE[idx]}"
		if [ "${entry#*|}" = "${path}" ]; then
			return 0
		fi
	done
	return 1
}

# editor_isolation_open_ancestor_traverse [user]
#   For every probe path, walk its ancestors from `/` and grant traverse-only
#   access (`o+x`; read and listing are never added) on each directory <user>
#   cannot traverse, recording `<octal mode>|<path>` in
#   EDITOR_ISOLATION_ANCESTOR_RESTORE so editor_isolation_restore_ancestor_traverse
#   can undo it. On GitHub-hosted runners this is exactly `/home/runner`
#   (0750 -> 0751). Prints one EDITOR_ISOLATION_ANCESTOR_TRAVERSE_GRANTED line
#   per directory opened. Returns 1 when a required ancestor could not be
#   opened; the probe then reports it as `first_denied`.
editor_isolation_open_ancestor_traverse()
{
	local user="${1:-${EDITOR_ISOLATION_USER:-nobody}}"
	local spec probe_path resolved rest comp acc mode_before mode_after
	local failures=0
	if ! declare -p EDITOR_ISOLATION_ANCESTOR_RESTORE >/dev/null 2>&1; then
		declare -g -a EDITOR_ISOLATION_ANCESTOR_RESTORE=()
	fi
	while IFS= read -r spec; do
		case "${spec}" in
			missing\|*) continue ;;
		esac
		probe_path="${spec#*|}"
		resolved="$(realpath -m -- "${probe_path}")"
		rest="${resolved#/}"
		acc=""
		while [ -n "${rest}" ]; do
			comp="${rest%%/*}"
			if [ "${rest}" = "${comp}" ]; then
				rest=""
			else
				rest="${rest#*/}"
			fi
			acc="${acc}/${comp}"
			# The leaf is owned by prepare (support bundle, RUNTIME_DIR) or
			# by the editor stage's chown (workspace); only ancestors here.
			if [ "${acc}" = "${resolved}" ] || [ ! -d "${acc}" ]; then
				break
			fi
			if sudo -n -u "${user}" -- test -x "${acc}"; then
				continue
			fi
			if _editor_isolation_ancestor_recorded "${acc}"; then
				continue
			fi
			if ! mode_before="$(stat -c '%a' -- "${acc}" 2>/dev/null)"; then
				echo "::error::EDITOR_ISOLATION_ANCESTOR_TRAVERSE_DENIED user=${user} path=${acc} reason=stat_failed" >&2
				failures=$((failures + 1))
				break
			fi
			if ! _editor_isolation_chmod o+x "${acc}"; then
				echo "::error::EDITOR_ISOLATION_ANCESTOR_TRAVERSE_DENIED user=${user} path=${acc} mode=${mode_before} reason=chmod_failed" >&2
				failures=$((failures + 1))
				break
			fi
			EDITOR_ISOLATION_ANCESTOR_RESTORE+=("${mode_before}|${acc}")
			mode_after="$(stat -c '%a' -- "${acc}" 2>/dev/null || printf '?')"
			echo "EDITOR_ISOLATION_ANCESTOR_TRAVERSE_GRANTED user=${user} path=${acc} mode_before=${mode_before} mode_after=${mode_after}"
		done
	done < <(_editor_isolation_probe_specs)
	[ "${failures}" -eq 0 ]
}

# editor_isolation_restore_ancestor_traverse
#   Restores every mode recorded by editor_isolation_open_ancestor_traverse,
#   newest first, and clears the record. Returns 1 when any restore fails;
#   callers treat that as a failed permission restoration (fail closed).
editor_isolation_restore_ancestor_traverse()
{
	local idx entry mode path
	local restore_rc=0
	if ! declare -p EDITOR_ISOLATION_ANCESTOR_RESTORE >/dev/null 2>&1; then
		return 0
	fi
	for (( idx = ${#EDITOR_ISOLATION_ANCESTOR_RESTORE[@]} - 1; idx >= 0; idx-- )); do
		entry="${EDITOR_ISOLATION_ANCESTOR_RESTORE[idx]}"
		mode="${entry%%|*}"
		path="${entry#*|}"
		if _editor_isolation_chmod "${mode}" "${path}"; then
			echo "EDITOR_ISOLATION_ANCESTOR_TRAVERSE_RESTORED path=${path} mode=${mode}"
		else
			echo "::error::EDITOR_ISOLATION_ANCESTOR_TRAVERSE_RESTORE_FAILED path=${path} mode=${mode}" >&2
			restore_rc=1
		fi
	done
	EDITOR_ISOLATION_ANCESTOR_RESTORE=()
	return "${restore_rc}"
}

# editor_isolation_preflight_probe [user]
editor_isolation_preflight_probe()
{
	local user="${1:-${EDITOR_ISOLATION_USER:-nobody}}"
	local spec probe_path probe_kind first_denied description
	local failures=0 probes=0

	command -v sudo >/dev/null 2>&1 || {
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=sudo_missing" >&2
		return 1
	}
	sudo -n true >/dev/null 2>&1 || {
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=sudo_requires_password" >&2
		return 1
	}
	id "${user}" >/dev/null 2>&1 || {
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=identity_missing" >&2
		return 1
	}

	while IFS= read -r spec; do
		probe_kind="${spec%%|*}"
		probe_path="${spec#*|}"
		if [ "${probe_kind}" = "missing" ]; then
			echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} reason=${probe_path}_not_on_editor_path" >&2
			failures=$((failures + 1))
			continue
		fi
		probes=$((probes + 1))
		if _editor_isolation_test_as "${user}" "${probe_kind}" "${probe_path}"; then
			continue
		fi
		failures=$((failures + 1))
		first_denied="$(_editor_isolation_first_denied "${user}" "${probe_kind}" "${probe_path}")"
		description="$(_editor_isolation_describe_path "${first_denied}")"
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_DENIED user=${user} kind=${probe_kind} path=${probe_path} first_denied=${first_denied} ${description}" >&2
	done < <(_editor_isolation_probe_specs)

	if [ "${failures}" -gt 0 ]; then
		echo "::error::EDITOR_ISOLATION_PREFLIGHT_FAILED user=${user} denied=${failures}: the review editor cannot run as the unprivileged identity in this job; fix the first_denied component(s) above or set EDITOR_ISOLATION_PREFLIGHT_ENABLED=false to skip this probe." >&2
		return 1
	fi
	echo "EDITOR_ISOLATION_PREFLIGHT_OK user=${user} probes=${probes}"
	return 0
}
