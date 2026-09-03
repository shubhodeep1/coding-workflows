#!/usr/bin/env bash
set -euo pipefail

case "${BASH_SOURCE[0]:-$0}" in
	*/*)
		_WORKTREE_REGISTRY_SCRIPT_DIR="$(CDPATH= cd -- "${BASH_SOURCE[0]%/*}" && pwd)"
		;;
	*)
		_WORKTREE_REGISTRY_SCRIPT_DIR="$(pwd)"
		;;
esac

if [ -f "${_WORKTREE_REGISTRY_SCRIPT_DIR}/emit_event.sh" ]; then
	# shellcheck disable=SC1091
	source "${_WORKTREE_REGISTRY_SCRIPT_DIR}/emit_event.sh" 2>/dev/null || true
fi
if ! command -v emit_event >/dev/null 2>&1; then
	emit_event()
	{
		return 0
	}
fi

_worktree_registry_is_truthy()
{
	case "${1:-}" in
		1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) return 0 ;;
		*) return 1 ;;
	esac
}

_worktree_registry_repo_root()
{
	if [ -n "${WORKTREE_REGISTRY_ROOT:-}" ] && [ -d "${WORKTREE_REGISTRY_ROOT}" ]; then
		printf '%s\n' "${WORKTREE_REGISTRY_ROOT}"
		return 0
	fi

	if [ -n "${GITHUB_WORKSPACE:-}" ] && [ -d "${GITHUB_WORKSPACE}" ]; then
		printf '%s\n' "${GITHUB_WORKSPACE}"
		return 0
	fi

	if [ -f "${_WORKTREE_REGISTRY_SCRIPT_DIR}/repo_root.py" ] && command -v python3 >/dev/null 2>&1; then
		PYTHONDONTWRITEBYTECODE=1 python3 "${_WORKTREE_REGISTRY_SCRIPT_DIR}/repo_root.py" 2>/dev/null && return 0
	fi

	git rev-parse --show-toplevel 2>/dev/null && return 0
	printf '%s\n' "$(CDPATH= cd -- "${_WORKTREE_REGISTRY_SCRIPT_DIR}/.." && pwd)"
}

_worktree_registry_dir()
{
	printf '%s/.worktrees\n' "$(_worktree_registry_repo_root)"
}

_worktree_registry_index_path()
{
	printf '%s/index.json\n' "$(_worktree_registry_dir)"
}

_worktree_registry_lock_path()
{
	printf '%s/index.lock\n' "$(_worktree_registry_dir)"
}

_worktree_registry_empty_json()
{
	printf '{"schema_version":"worktree_registry.v1.json","entries":[]}\n'
}

_worktree_registry_owner_run_id()
{
	if [ -n "${GITHUB_RUN_ID:-}" ]; then
		printf '%s\n' "${GITHUB_RUN_ID}"
		return 0
	fi
	printf 'local\n'
}

_worktree_registry_ttl_secs()
{
	local raw="${ORCH_WORKTREE_TTL_SECS:-3600}"
	if ! [[ "${raw}" =~ ^[0-9]+$ ]] || [ "${raw}" -lt 300 ] || [ "${raw}" -gt 86400 ]; then
		printf '3600\n'
		return 0
	fi
	printf '%s\n' "${raw}"
}

_worktree_registry_emit()
{
	local prefix="${1:-}"
	shift || true

	printf '%s' "${prefix}" >&2
	if [ "$#" -gt 0 ]; then
		printf ' %s' "$@" >&2
	fi
	printf '\n' >&2
	emit_event "${prefix}" "$@" >/dev/null 2>&1 || true
}

_worktree_registry_validate_name()
{
	[[ "${1:-}" =~ ^[A-Za-z0-9._-]{1,40}$ ]]
}

_worktree_registry_write_json_atomic()
{
	local index_path="${1:?index_path required}"
	local payload="${2:?payload required}"
	local tmp_path=""

	tmp_path="$(mktemp "${index_path}.tmp.XXXXXX" 2>/dev/null)" || return 1
	if ! printf '%s\n' "${payload}" > "${tmp_path}"; then
		rm -f "${tmp_path}" 2>/dev/null || true
		return 1
	fi
	mv -f "${tmp_path}" "${index_path}"
}

_worktree_registry_rebuild_json()
{
	local repo_root="${1:?repo_root required}"
	local created_at="${2:?created_at required}"
	local parser_file=""
	local rc=0

	if ! command -v python3 >/dev/null 2>&1; then
		_worktree_registry_empty_json
		return 0
	fi

	parser_file="$(mktemp "${TMPDIR:-/tmp}/worktree_registry_rebuild.XXXXXX" 2>/dev/null)" || return 1
	if ! cat > "${parser_file}" <<'PY'
from __future__ import annotations

import json
import os
import sys


repo_root = os.path.realpath(sys.argv[1])
created_at = sys.argv[2]
entries: list[dict[str, str]] = []
current: dict[str, object] = {}


def flush_current() -> None:
	global current
	path = str(current.get("path") or "")
	if not path:
		current = {}
		return
	if os.path.realpath(path) == repo_root:
		current = {}
		return
	name = os.path.basename(path.rstrip(os.sep))
	branch = str(current.get("branch") or "")
	if branch.startswith("refs/heads/"):
		branch = branch[len("refs/heads/"):]
	if not branch:
		branch = str(current.get("HEAD") or "")
	entries.append(
		{
			"name": name,
			"path": path,
			"branch": branch,
			"task_id": "",
			"created_at": created_at,
			"owner_phase": "registry-rebuild",
			"owner_run_id": "",
		}
	)
	current = {}


for raw_line in sys.stdin.read().splitlines():
	if not raw_line:
		flush_current()
		continue
	key, _, value = raw_line.partition(" ")
	if key == "worktree":
		current["path"] = value
	elif key in {"HEAD", "branch"}:
		current[key] = value
	else:
		current[key] = True if value == "" else value

flush_current()
print(json.dumps({"schema_version": "worktree_registry.v1.json", "entries": entries}, ensure_ascii=True))
PY
	then
		rm -f "${parser_file}" 2>/dev/null || true
		return 1
	fi

	if ! git -C "${repo_root}" worktree list --porcelain 2>/dev/null | \
		PYTHONDONTWRITEBYTECODE=1 python3 "${parser_file}" "${repo_root}" "${created_at}"
	then
		rc=1
	fi
	rm -f "${parser_file}" 2>/dev/null || true
	return "${rc}"
}

_worktree_registry_load_json_locked()
{
	local repo_root="${1:?repo_root required}"
	local index_path="${2:?index_path required}"
	local registry_json=""

	if [ ! -s "${index_path}" ]; then
		_worktree_registry_empty_json
		return 0
	fi

	registry_json="$(cat "${index_path}" 2>/dev/null || true)"
	if [ -n "${registry_json}" ] && printf '%s' "${registry_json}" | jq -e 'type == "object" and .schema_version == "worktree_registry.v1.json" and (.entries | type == "array")' >/dev/null 2>&1; then
		printf '%s' "${registry_json}" | jq -c . 2>/dev/null
		return 0
	fi

	local rebuilt_json=""
	local created_at=""
	local rebuilt_count="0"
	created_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
	rebuilt_json="$(_worktree_registry_rebuild_json "${repo_root}" "${created_at}" 2>/dev/null || _worktree_registry_empty_json)"
	if ! printf '%s' "${rebuilt_json}" | jq -e 'type == "object" and .schema_version == "worktree_registry.v1.json" and (.entries | type == "array")' >/dev/null 2>&1; then
		rebuilt_json="$(_worktree_registry_empty_json)"
	fi
	rebuilt_count="$(printf '%s' "${rebuilt_json}" | jq -r '(.entries // []) | length' 2>/dev/null || echo '0')"
	_worktree_registry_emit "WORKTREE_REGISTRY_REBUILD" "reason=invalid_registry" "entries=${rebuilt_count}"
	_worktree_registry_write_json_atomic "${index_path}" "${rebuilt_json}" || return 1
	printf '%s' "${rebuilt_json}"
}

_worktree_registry_created_at_to_epoch()
{
	local created_at="${1:-}"
	printf '%s' "${created_at}" | jq -Rr 'try (sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) catch empty' 2>/dev/null || true
}

_worktree_registry_load_active_run_ids()
{
	local active_runs_file="${WORKTREE_ACTIVE_RUNS_FILE:-}"
	if [ -z "${active_runs_file}" ] || [ ! -s "${active_runs_file}" ]; then
		return 1
	fi

	jq -c '[.workflow_runs[]? | select(.status == "queued" or .status == "in_progress") | (.id | tostring)] | unique' "${active_runs_file}" 2>/dev/null
}

worktree_registry_register()
{
	local name="${1:?name required}"
	local path="${2:?path required}"
	local branch="${3:?branch required}"
	local task_id="${4:?task_id required}"
	local owner_phase="${5:?owner_phase required}"

	if ! _worktree_registry_validate_name "${name}"; then
		_worktree_registry_emit "WORKTREE_REGISTER_INVALID_NAME" "name=${name}"
		return 2
	fi

	local repo_root=""
	local registry_dir=""
	local index_path=""
	local lock_path=""
	local owner_run_id=""
	local created_at=""
	local entry_json=""
	local registry_json=""
	local updated_json=""
	local rc=0

	repo_root="$(_worktree_registry_repo_root)"
	registry_dir="${repo_root}/.worktrees"
	index_path="${registry_dir}/index.json"
	lock_path="${registry_dir}/index.lock"
	owner_run_id="$(_worktree_registry_owner_run_id)"
	created_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

	mkdir -p "${registry_dir}" 2>/dev/null || rc=1
	if [ "${rc}" -eq 0 ]; then
		entry_json="$(jq -cn \
			--arg name "${name}" \
			--arg path "${path}" \
			--arg branch "${branch}" \
			--arg task_id "${task_id}" \
			--arg created_at "${created_at}" \
			--arg owner_phase "${owner_phase}" \
			--arg owner_run_id "${owner_run_id}" \
			'{name: $name, path: $path, branch: $branch, task_id: $task_id, created_at: $created_at, owner_phase: $owner_phase, owner_run_id: $owner_run_id}' 2>/dev/null || true)"
		[ -n "${entry_json}" ] || rc=1
	fi

	if [ "${rc}" -eq 0 ]; then
		exec 9>"${lock_path}" || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		flock 9 || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		registry_json="$(_worktree_registry_load_json_locked "${repo_root}" "${index_path}" 2>/dev/null || true)"
		[ -n "${registry_json}" ] || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		updated_json="$(printf '%s' "${registry_json}" | jq -c --arg name "${name}" --argjson entry "${entry_json}" '.schema_version = "worktree_registry.v1.json" | .entries = (((.entries // []) | map(select(.name != $name))) + [$entry])' 2>/dev/null || true)"
		[ -n "${updated_json}" ] || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		_worktree_registry_write_json_atomic "${index_path}" "${updated_json}" || rc=1
	fi
	exec 9>&- || true

	if [ "${rc}" -ne 0 ]; then
		_worktree_registry_emit "WORKTREE_REGISTER_FAIL" "name=${name}" "owner_phase=${owner_phase}"
		return 1
	fi

	_worktree_registry_emit "WORKTREE_REGISTER" "name=${name}" "path=${path}" "branch=${branch}" "task_id=${task_id}" "owner_phase=${owner_phase}" "owner_run_id=${owner_run_id}"
}

worktree_registry_deregister()
{
	local name="${1:?name required}"
	local repo_root=""
	local registry_dir=""
	local index_path=""
	local lock_path=""
	local registry_json=""
	local updated_json=""
	local removed="false"
	local rc=0

	repo_root="$(_worktree_registry_repo_root)"
	registry_dir="${repo_root}/.worktrees"
	index_path="${registry_dir}/index.json"
	lock_path="${registry_dir}/index.lock"

	mkdir -p "${registry_dir}" 2>/dev/null || rc=1
	if [ "${rc}" -eq 0 ]; then
		exec 9>"${lock_path}" || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		flock 9 || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		registry_json="$(_worktree_registry_load_json_locked "${repo_root}" "${index_path}" 2>/dev/null || true)"
		[ -n "${registry_json}" ] || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		removed="$(printf '%s' "${registry_json}" | jq -r --arg name "${name}" 'if ((.entries // []) | any(.name == $name)) then "true" else "false" end' 2>/dev/null || echo 'false')"
		updated_json="$(printf '%s' "${registry_json}" | jq -c --arg name "${name}" '.schema_version = "worktree_registry.v1.json" | .entries = ((.entries // []) | map(select(.name != $name)))' 2>/dev/null || true)"
		[ -n "${updated_json}" ] || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		_worktree_registry_write_json_atomic "${index_path}" "${updated_json}" || rc=1
	fi
	exec 9>&- || true

	if [ "${rc}" -ne 0 ]; then
		_worktree_registry_emit "WORKTREE_DEREGISTER_FAIL" "name=${name}"
		return 1
	fi

	_worktree_registry_emit "WORKTREE_DEREGISTER" "name=${name}" "removed=${removed}"
}

worktree_registry_list()
{
	local task_filter=""
	local owner_phase_filter=""
	while [ "$#" -gt 0 ]; do
		case "${1}" in
			--task)
				task_filter="${2:?--task requires value}"
				shift 2
				;;
			--owner-phase)
				owner_phase_filter="${2:?--owner-phase requires value}"
				shift 2
				;;
			*)
				echo "Unknown list option: ${1}" >&2
				return 1
				;;
		esac
		done

	local repo_root=""
	local registry_dir=""
	local index_path=""
	local lock_path=""
	local registry_json=""
	local rc=0

	repo_root="$(_worktree_registry_repo_root)"
	registry_dir="${repo_root}/.worktrees"
	index_path="${registry_dir}/index.json"
	lock_path="${registry_dir}/index.lock"
	mkdir -p "${registry_dir}" 2>/dev/null || rc=1
	if [ "${rc}" -eq 0 ]; then
		exec 9>"${lock_path}" || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		flock 9 || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		registry_json="$(_worktree_registry_load_json_locked "${repo_root}" "${index_path}" 2>/dev/null || true)"
		[ -n "${registry_json}" ] || rc=1
	fi
	exec 9>&- || true
	if [ "${rc}" -ne 0 ]; then
		echo '[]'
		return 1
	fi

	printf '%s' "${registry_json}" | jq -c \
		--arg task_filter "${task_filter}" \
		--arg owner_phase_filter "${owner_phase_filter}" \
		'[.entries[]? | select(($task_filter == "" or .task_id == $task_filter) and ($owner_phase_filter == "" or .owner_phase == $owner_phase_filter))]'
}

worktree_registry_gc()
{
	local repo_root=""
	local registry_dir=""
	local index_path=""
	local lock_path=""
	local registry_json=""
	local active_run_ids='[]'
	local active_runs_available='false'
	local ttl_secs=""
	local now_epoch=""
	local entry_count="0"
	local rc=0

	repo_root="$(_worktree_registry_repo_root)"
	registry_dir="${repo_root}/.worktrees"
	index_path="${registry_dir}/index.json"
	lock_path="${registry_dir}/index.lock"
	ttl_secs="$(_worktree_registry_ttl_secs)"
	now_epoch="$(date +%s)"

	mkdir -p "${registry_dir}" 2>/dev/null || rc=1
	if [ "${rc}" -eq 0 ]; then
		exec 9>"${lock_path}" || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		flock 9 || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		registry_json="$(_worktree_registry_load_json_locked "${repo_root}" "${index_path}" 2>/dev/null || true)"
		[ -n "${registry_json}" ] || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		entry_count="$(printf '%s' "${registry_json}" | jq -r '(.entries // []) | length' 2>/dev/null || echo '0')"
		if active_run_ids="$(_worktree_registry_load_active_run_ids 2>/dev/null)"; then
			active_runs_available='true'
		fi
	fi

	if [ "${rc}" -ne 0 ]; then
		exec 9>&- || true
		_worktree_registry_emit "WORKTREE_GC" "removed=0" "preserved=0" "ttl_secs=${ttl_secs}" "active_runs_available=false" "reason=registry_unavailable"
		return 1
	fi

	if [ "${active_runs_available}" != 'true' ]; then
		exec 9>&- || true
		_worktree_registry_emit "WORKTREE_GC" "removed=0" "preserved=${entry_count}" "ttl_secs=${ttl_secs}" "active_runs_available=false" "reason=active_runs_unavailable"
		return 0
	fi

	local keep_entries_file=""
	local keep_entries_json='[]'
	local removed_count=0
	local preserved_count=0
	local active_run_count=0
	local reaped_names=""

	keep_entries_file="$(mktemp "${index_path}.keep.XXXXXX" 2>/dev/null)" || rc=1
	active_run_count="$(printf '%s' "${active_run_ids}" | jq -r 'length' 2>/dev/null || echo '0')"

	if [ "${rc}" -eq 0 ]; then
		while IFS= read -r entry; do
			[ -n "${entry}" ] || continue

			local name=""
			local path=""
			local owner_run_id=""
			local created_at=""
			local created_epoch=""
			local is_active_owner="false"
			local should_reap="false"

			name="$(printf '%s' "${entry}" | jq -r '.name // empty' 2>/dev/null || echo '')"
			path="$(printf '%s' "${entry}" | jq -r '.path // empty' 2>/dev/null || echo '')"
			owner_run_id="$(printf '%s' "${entry}" | jq -r '.owner_run_id // empty' 2>/dev/null || echo '')"
			created_at="$(printf '%s' "${entry}" | jq -r '.created_at // empty' 2>/dev/null || echo '')"
			created_epoch="$(_worktree_registry_created_at_to_epoch "${created_at}")"
			if [ -n "${owner_run_id}" ] && printf '%s' "${active_run_ids}" | jq -e --arg owner_run_id "${owner_run_id}" 'index($owner_run_id) != null' >/dev/null 2>&1; then
				is_active_owner='true'
			fi
			if [ -n "${created_epoch}" ] && [[ "${created_epoch}" =~ ^[0-9]+$ ]] && [ "${created_epoch}" -le "${now_epoch}" ]; then
				if [ $((now_epoch - created_epoch)) -gt "${ttl_secs}" ] && [ "${is_active_owner}" != 'true' ]; then
					should_reap='true'
				fi
			fi

			if [ "${should_reap}" = 'true' ]; then
				local removed_path='true'
				if [ -n "${path}" ] && [ -e "${path}" ]; then
					if git -C "${repo_root}" worktree remove --force "${path}" >/dev/null 2>&1; then
						removed_path='true'
					else
						removed_path='false'
					fi
				fi

				if [ "${removed_path}" = 'true' ]; then
					removed_count=$((removed_count + 1))
					if [ -z "${reaped_names}" ]; then
						reaped_names="${name}"
					else
						reaped_names="${reaped_names},${name}"
					fi
					continue
				fi
			fi

			preserved_count=$((preserved_count + 1))
			printf '%s\n' "${entry}" >> "${keep_entries_file}"
		done < <(printf '%s' "${registry_json}" | jq -c '.entries[]?' 2>/dev/null)
	fi

	if [ "${rc}" -eq 0 ]; then
		keep_entries_json="$(jq -sc '.' "${keep_entries_file}" 2>/dev/null || echo '[]')"
		registry_json="$(jq -cn --argjson entries "${keep_entries_json}" '{schema_version: "worktree_registry.v1.json", entries: $entries}' 2>/dev/null || true)"
		[ -n "${registry_json}" ] || rc=1
	fi
	if [ "${rc}" -eq 0 ]; then
		_worktree_registry_write_json_atomic "${index_path}" "${registry_json}" || rc=1
	fi
	rm -f "${keep_entries_file}" 2>/dev/null || true
	exec 9>&- || true

	if [ "${rc}" -ne 0 ]; then
		_worktree_registry_emit "WORKTREE_GC" "removed=0" "preserved=${entry_count}" "ttl_secs=${ttl_secs}" "active_runs_available=true" "active_run_count=${active_run_count}" "reason=gc_failed"
		return 1
	fi

	[ -n "${reaped_names}" ] || reaped_names='none'
	_worktree_registry_emit "WORKTREE_GC" "removed=${removed_count}" "preserved=${preserved_count}" "ttl_secs=${ttl_secs}" "active_runs_available=true" "active_run_count=${active_run_count}" "reaped=${reaped_names}"
}

worktree_registry_usage()
{
	cat <<'EOF'
Usage:
	bash scripts/worktree_registry.sh register <name> <path> <branch> <task_id> <owner_phase>
	bash scripts/worktree_registry.sh deregister <name>
	bash scripts/worktree_registry.sh list [--task <id>] [--owner-phase <phase>]
	bash scripts/worktree_registry.sh gc
EOF
}

worktree_registry_main()
{
	local subcommand="${1:-}"
	case "${subcommand}" in
		register)
			shift
			worktree_registry_register "$@"
			;;
		deregister)
			shift
			worktree_registry_deregister "$@"
			;;
		list)
			shift
			worktree_registry_list "$@"
			;;
		gc)
			shift
			worktree_registry_gc "$@"
			;;
		""|-h|--help)
			worktree_registry_usage
			;;
		*)
			echo "Unknown subcommand: ${subcommand}" >&2
			worktree_registry_usage >&2
			return 1
			;;
	esac
}

worktree_registry_main "$@"
