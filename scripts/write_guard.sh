#!/usr/bin/env bash
set -euo pipefail

WRITE_GUARD_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

write_guard_is_truthy()
{
	local write_guard_value="${1:-}"
	case "$(printf '%s' "${write_guard_value}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
	esac
	return 1
}

write_guard_repo_root()
{
	if command -v git >/dev/null 2>&1; then
		git rev-parse --show-toplevel 2>/dev/null && return 0
	fi
	pwd
}

write_guard_sanitize_log_value()
{
	printf '%s' "${1:-}" | tr '\n\t' '  ' | tr -s ' ' | sed 's/^ //;s/ $//'
}

write_guard_resolve_config_path()
{
	local write_guard_root_dir="$1"
	local write_guard_rel_path=".github/ai/write_guards.v1.json"
	local write_guard_candidate_paths=()
	local write_guard_candidate

	write_guard_candidate_paths+=("${write_guard_root_dir}/${write_guard_rel_path}")
	write_guard_candidate_paths+=("${WRITE_GUARD_SCRIPT_DIR}/../.github/ai/write_guards.v1.json")
	write_guard_candidate_paths+=("${write_guard_root_dir}/.codex-workflow-src/${write_guard_rel_path}")
	write_guard_candidate_paths+=("${write_guard_root_dir}/.codex-workflow-src-main/${write_guard_rel_path}")
	if [ -n "${SUPPORT_ROOT_DIR:-}" ]; then
		write_guard_candidate_paths+=("${SUPPORT_ROOT_DIR}/.github/ai/write_guards.v1.json")
	fi

	for write_guard_candidate in "${write_guard_candidate_paths[@]}"; do
		if [ -f "${write_guard_candidate}" ]; then
			printf '%s\n' "${write_guard_candidate}"
			return 0
		fi
	done

	return 1
}

write_guard_check()
{
	if [ "$#" -ne 2 ]; then
		echo "::error::write_guard_check requires <phase> <staged-files-list>." >&2
		return 64
	fi

	local write_guard_phase="$1"
	local write_guard_staged_files_list="$2"
	local write_guard_enabled_value="${WRITE_GUARDS_ENABLED:-true}"
	local write_guard_root_dir=""
	local write_guard_config_rel=".github/ai/write_guards.v1.json"
	local write_guard_config_path=""
	local write_guard_config_log_path="${write_guard_config_rel}"
	local write_guard_result_file=""
	local write_guard_stderr_file=""
	local write_guard_rc=0
	local write_guard_detail=""
	local write_guard_block_count=0
	local write_guard_reason=""
	local write_guard_path=""
	local write_guard_pattern=""
	local write_guard_env_var=""
	local write_guard_env_value=""

	if ! write_guard_is_truthy "${write_guard_enabled_value}"; then
		echo "WRITE_GUARD_BYPASS_ENV: phase=${write_guard_phase} env=WRITE_GUARDS_ENABLED value=${write_guard_enabled_value}"
		return 0
	fi

	if [ ! -f "${write_guard_staged_files_list}" ] || [ ! -s "${write_guard_staged_files_list}" ]; then
		return 0
	fi

	if ! command -v python3 >/dev/null 2>&1; then
		echo "WRITE_GUARD_CONFIG_ERROR: phase=${write_guard_phase} config=${write_guard_config_log_path} detail=python3_unavailable"
		return 0
	fi

	write_guard_root_dir="$(write_guard_repo_root)"
	if ! write_guard_config_path="$(write_guard_resolve_config_path "${write_guard_root_dir}")"; then
		echo "WRITE_GUARD_CONFIG_ERROR: phase=${write_guard_phase} config=${write_guard_config_log_path} detail=missing"
		return 0
	fi
	write_guard_config_log_path="${write_guard_config_path}"
	if [ -n "${write_guard_root_dir}" ] && [ "${write_guard_config_log_path#${write_guard_root_dir}/}" != "${write_guard_config_log_path}" ]; then
		write_guard_config_log_path="${write_guard_config_log_path#${write_guard_root_dir}/}"
	fi

	write_guard_result_file="$(mktemp "${TMPDIR:-/tmp}/write-guard-result.XXXXXX")"
	write_guard_stderr_file="$(mktemp "${TMPDIR:-/tmp}/write-guard-stderr.XXXXXX")"

	set +e
	PYTHONDONTWRITEBYTECODE=1 python3 - "${write_guard_phase}" "${write_guard_staged_files_list}" "${write_guard_config_path}" > "${write_guard_result_file}" 2> "${write_guard_stderr_file}" <<'PY'
import fnmatch
import json
import os
import pathlib
import sys

phase, list_path, config_path = sys.argv[1:4]


def fail(message):
	raise ValueError(message)


try:
	config = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
	if config.get("schema_version") != "write_guards.v1":
		fail(f"unsupported schema_version {config.get('schema_version')!r}")
	phases = config.get("phases")
	if not isinstance(phases, dict):
		fail("phases must be an object")
	policy = phases.get(phase)
	if not isinstance(policy, dict):
		fail(f"phase {phase!r} is missing or invalid")
	allowed_globs = policy.get("allowed_globs")
	if not isinstance(allowed_globs, list) or not allowed_globs or not all(isinstance(item, str) and item for item in allowed_globs):
		fail("allowed_globs must be a non-empty string array")
	blocked_globs = policy.get("blocked_globs", [])
	if not isinstance(blocked_globs, list) or not all(isinstance(item, str) and item for item in blocked_globs):
		fail("blocked_globs must be a string array")
	conditional_rules = policy.get("conditional_blocked_globs", [])
	if not isinstance(conditional_rules, list):
		fail("conditional_blocked_globs must be an array")
	parsed_rules = []
	for index, rule in enumerate(conditional_rules):
		if not isinstance(rule, dict):
			fail(f"conditional_blocked_globs[{index}] must be an object")
		env_var = rule.get("env_var")
		globs = rule.get("globs")
		allowed_values = rule.get("allowed_values", [])
		if not isinstance(env_var, str) or not env_var:
			fail(f"conditional_blocked_globs[{index}].env_var must be a non-empty string")
		if not isinstance(globs, list) or not globs or not all(isinstance(item, str) and item for item in globs):
			fail(f"conditional_blocked_globs[{index}].globs must be a non-empty string array")
		if not isinstance(allowed_values, list) or not all(isinstance(item, str) for item in allowed_values):
			fail(f"conditional_blocked_globs[{index}].allowed_values must be a string array")
		parsed_rules.append((env_var, [item.lower() for item in allowed_values], globs))
	paths = []
	for raw_line in pathlib.Path(list_path).read_text(encoding="utf-8").splitlines():
		path = raw_line.strip()
		if path:
			paths.append(path)
except Exception as exc:
	print(str(exc), file=sys.stderr)
	sys.exit(2)


def first_match(path, patterns):
	for pattern in patterns:
		if fnmatch.fnmatchcase(path, pattern):
			return pattern
	return None


violations = 0
for path in paths:
	blocked_pattern = first_match(path, blocked_globs)
	if blocked_pattern is not None:
		print("\t".join(["blocked_glob", path, blocked_pattern, "", ""]))
		violations += 1
		continue
	conditional_hit = False
	for env_var, allowed_values, globs in parsed_rules:
		env_value = os.environ.get(env_var, "")
		if env_value.lower() in allowed_values:
			continue
		blocked_pattern = first_match(path, globs)
		if blocked_pattern is not None:
			print("\t".join(["conditional_blocked_glob", path, blocked_pattern, env_var, env_value]))
			violations += 1
			conditional_hit = True
			break
	if conditional_hit:
		continue
	if first_match(path, allowed_globs) is None:
		print("\t".join(["not_allowed", path, "<no-match>", "", ""]))
		violations += 1

sys.exit(1 if violations else 0)
PY
	write_guard_rc=$?
	set -e

	case "${write_guard_rc}" in
		0)
			;;
		1)
			while IFS=$'\t' read -r write_guard_reason write_guard_path write_guard_pattern write_guard_env_var write_guard_env_value; do
				[ -n "${write_guard_reason}" ] || continue
				write_guard_block_count=$((write_guard_block_count + 1))
				if [ -n "${write_guard_env_var}" ]; then
					echo "WRITE_GUARD_BLOCK: phase=${write_guard_phase} path=${write_guard_path} reason=${write_guard_reason} pattern=${write_guard_pattern} env=${write_guard_env_var} env_value=${write_guard_env_value}"
				else
					echo "WRITE_GUARD_BLOCK: phase=${write_guard_phase} path=${write_guard_path} reason=${write_guard_reason} pattern=${write_guard_pattern}"
				fi
			done < "${write_guard_result_file}"
			rm -f "${write_guard_result_file}" "${write_guard_stderr_file}"
			echo "::error::Write guard blocked ${write_guard_block_count} path(s) for phase '${write_guard_phase}'."
			return 1
			;;
		*)
			write_guard_detail="$(write_guard_sanitize_log_value "$(cat "${write_guard_stderr_file}" 2>/dev/null || true)")"
			if [ -z "${write_guard_detail}" ]; then
				write_guard_detail="unknown"
			fi
			rm -f "${write_guard_result_file}" "${write_guard_stderr_file}"
			echo "WRITE_GUARD_CONFIG_ERROR: phase=${write_guard_phase} config=${write_guard_config_log_path} detail=${write_guard_detail}"
			return 0
			;;
	esac

	rm -f "${write_guard_result_file}" "${write_guard_stderr_file}"
	return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	write_guard_check "$@"
fi
