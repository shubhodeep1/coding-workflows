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

workspace_safety_violation()
{
	printf 'workspace_safety_violation: %s\n' "$*" >&2
	exit 78
}

resolve_workspace_path()
{
	local runner_temp="$1"
	local workspace_path="$2"

	PYTHONDONTWRITEBYTECODE=1 python3 - "${runner_temp}" "${workspace_path}" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=False) / 'workspaces'
path = Path(sys.argv[2]).resolve(strict=False)
try:
	path.relative_to(root)
except ValueError:
	raise SystemExit(1)

print(path)
PY
}

if ! truthy "${WORKSPACE_REUSE_ENABLED:-false}"; then
	exit 0
fi

workspace_path="${WORKSPACE_PATH:-}"
workspace_key="${WORKSPACE_KEY:-}"
runner_temp="${RUNNER_TEMP:-}"

if [ -z "${workspace_path}" ] || [ -z "${workspace_key}" ] || [ -z "${runner_temp}" ]; then
	workspace_safety_violation "missing workspace reuse metadata"
fi

if ! printf '%s' "${workspace_key}" | grep -Eq '^[A-Za-z0-9._-]+$'; then
	workspace_safety_violation "invalid workspace key: ${workspace_key}"
fi

if ! resolved_workspace_path="$(resolve_workspace_path "${runner_temp}" "${workspace_path}")"; then
	workspace_safety_violation "workspace path escapes ${runner_temp}/workspaces: ${workspace_path}"
fi

current_pwd="$(pwd -P 2>/dev/null || true)"
if [ -z "${current_pwd}" ]; then
	workspace_safety_violation "unable to resolve current working directory"
fi

if [ "${current_pwd}" != "${resolved_workspace_path}" ]; then
	workspace_safety_violation "pwd -P (${current_pwd}) does not match workspace path (${resolved_workspace_path})"
fi
