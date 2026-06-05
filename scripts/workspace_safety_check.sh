#!/usr/bin/env bash
set -euo pipefail

truthy()
{
	case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
	esac
	return 1
}

fail()
{
	printf 'workspace_safety_violation: %s\n' "$*" >&2
	exit 78
}

if ! truthy "${WORKSPACE_REUSE_ENABLED:-false}"; then
	exit 0
fi

runner_temp="${RUNNER_TEMP:-}"
workspace_path="${WORKSPACE_PATH:-}"
workspace_key="${WORKSPACE_KEY:-}"

[ -n "${runner_temp}" ] || fail "RUNNER_TEMP is required when workspace reuse is enabled"
[ -n "${workspace_path}" ] || fail "WORKSPACE_PATH is required when workspace reuse is enabled"
[ -n "${workspace_key}" ] || fail "WORKSPACE_KEY is required when workspace reuse is enabled"

if ! [[ "${workspace_key}" =~ ^[A-Za-z0-9._-]+$ ]]; then
	fail "WORKSPACE_KEY is invalid: ${workspace_key}"
fi

command -v python3 >/dev/null 2>&1 || fail "python3 is required"

resolved_paths="$({
	PYTHONDONTWRITEBYTECODE=1 python3 - "${runner_temp}" "${workspace_path}" <<'PY'
from pathlib import Path
import sys

root = (Path(sys.argv[1]) / "workspaces").resolve(strict=False)
path = Path(sys.argv[2]).resolve(strict=False)

print(root)
print(path)

if root not in path.parents:
	raise SystemExit(1)
PY
})" || fail "WORKSPACE_PATH escapes ${runner_temp}/workspaces: ${workspace_path}"

workspace_root_resolved="$(printf '%s\n' "${resolved_paths}" | sed -n '1p')"
workspace_path_resolved="$(printf '%s\n' "${resolved_paths}" | sed -n '2p')"

current_dir="$(pwd -P 2>/dev/null)" || fail "failed to resolve current working directory"
launch_dir="${current_dir}"

# BASH_ENV already `cd`s into WORKSPACE_PATH before reused-workspace helpers
# run. When that masks the real launch directory, OLDPWD still points to the
# pre-hook cwd. Only trust that signal when it resolves inside the shared
# workspaces root; outer step shells often start from GITHUB_WORKSPACE.
if [ "${current_dir}" = "${workspace_path_resolved}" ] && [ -n "${OLDPWD:-}" ]; then
	old_pwd_resolved="$(cd "${OLDPWD}" 2>/dev/null && pwd -P)" || old_pwd_resolved=""
	case "${old_pwd_resolved}/" in
		"${workspace_root_resolved}/"*)
			launch_dir="${old_pwd_resolved}"
			;;
	esac
fi

if [ "${launch_dir}" != "${workspace_path_resolved}" ]; then
	fail "launch directory (${launch_dir}) does not match WORKSPACE_PATH (${workspace_path_resolved})"
fi

if [ -z "${workspace_root_resolved}" ] || [ -z "${workspace_path_resolved}" ]; then
	fail "failed to resolve workspace paths"
fi
