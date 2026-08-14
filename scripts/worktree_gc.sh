#!/usr/bin/env bash
set -euo pipefail

case "${BASH_SOURCE[0]:-$0}" in
	*/*)
		_WORKTREE_GC_SCRIPT_DIR="$(CDPATH= cd -- "${BASH_SOURCE[0]%/*}" && pwd)"
		;;
	*)
		_WORKTREE_GC_SCRIPT_DIR="$(pwd)"
		;;
esac

_worktree_gc_is_truthy()
{
	case "${1:-}" in
		1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]) return 0 ;;
		*) return 1 ;;
	esac
}

_worktree_gc_valid_runs_file()
{
	local runs_file="${1:-}"
	[ -n "${runs_file}" ] && [ -s "${runs_file}" ] || return 1
	jq -e 'type == "object" and (.workflow_runs | type == "array")' "${runs_file}" >/dev/null 2>&1
}

_worktree_gc_write_cache_snapshot()
{
	local repo="${1:-}"
	local out_file="${2:-}"
	local cache_json="{}"
	local cached_runs='[]'

	[ -n "${repo}" ] || return 1
	[ -n "${out_file}" ] || return 1
	[ -f "${_WORKTREE_GC_SCRIPT_DIR}/ai_memory.py" ] || return 1
	command -v python3 >/dev/null 2>&1 || return 1

	cache_json="$(PYTHONDONTWRITEBYTECODE=1 python3 "${_WORKTREE_GC_SCRIPT_DIR}/ai_memory.py" actions-runs-cache get --repo "${repo}" 2>/dev/null || echo '{}')"
	if ! printf '%s' "${cache_json}" | jq -e '(.ok == true) and (.hit == true) and (.cache | type == "object") and (.cache.runs | type == "array")' >/dev/null 2>&1; then
		return 1
	fi

	cached_runs="$(printf '%s' "${cache_json}" | jq -c '.cache.runs // []' 2>/dev/null || echo '[]')"
	jq -cn --argjson runs "${cached_runs}" '{workflow_runs: $runs}' > "${out_file}" 2>/dev/null
	_worktree_gc_valid_runs_file "${out_file}"
}

main()
{
	if ! _worktree_gc_is_truthy "${ORCH_WORKTREE_REGISTRY_ENABLED:-false}"; then
		exit 0
	fi

	if ! command -v jq >/dev/null 2>&1; then
		echo "WORKTREE_GC removed=0 preserved=0 active_runs_available=false reason=jq_missing" >&2
		exit 0
	fi

	local active_runs_file=""
	local cleanup_file=""
	local runtime_snapshot="${RUNTIME_DIR:-}/state_snapshot_actions_runs.json"

	if _worktree_gc_valid_runs_file "${runtime_snapshot}"; then
		active_runs_file="${runtime_snapshot}"
	elif [ -n "${GITHUB_REPOSITORY:-}" ]; then
		cleanup_file="$(mktemp "${TMPDIR:-/tmp}/worktree-active-runs.XXXXXX" 2>/dev/null || true)"
		if [ -n "${cleanup_file}" ] && _worktree_gc_write_cache_snapshot "${GITHUB_REPOSITORY}" "${cleanup_file}"; then
			active_runs_file="${cleanup_file}"
		else
			rm -f "${cleanup_file}" 2>/dev/null || true
			cleanup_file=""
		fi
	fi

	if [ -n "${active_runs_file}" ]; then
		WORKTREE_ACTIVE_RUNS_FILE="${active_runs_file}" \
			bash "${_WORKTREE_GC_SCRIPT_DIR}/worktree_registry.sh" gc
	else
		bash "${_WORKTREE_GC_SCRIPT_DIR}/worktree_registry.sh" gc
	fi

	rm -f "${cleanup_file}" 2>/dev/null || true
}

main "$@"
