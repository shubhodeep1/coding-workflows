#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
	echo "Usage: $0 <prompt-file>" >&2
	exit 1
fi

PROMPT_FILE_ARG="$1"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

resolve_prompt_file()
{
	local prompt_path="$1"
	local candidate=""
	local script_root=""
	local -a candidates=()

	if [ -f "${prompt_path}" ]; then
		printf '%s\n' "${prompt_path}"
		return 0
	fi

	if [[ "${prompt_path}" = /* ]]; then
		return 1
	fi

	script_root="$(dirname -- "${SCRIPT_DIR}")"
	candidates=(
		"${script_root}/${prompt_path}"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

if ! PROMPT_FILE="$(resolve_prompt_file "${PROMPT_FILE_ARG}")"; then
	echo "Prompt file not found: ${PROMPT_FILE_ARG}" >&2
	exit 1
fi

PROMPT_BASENAME="$(basename -- "${PROMPT_FILE}")"
MODE_NAME="${PROMPT_BASENAME%.*}"

resolve_render_prompt_py()
{
	local candidate=""
	local -a candidates=()

	candidates+=(
		"${SCRIPT_DIR}/render_prompt.py"
		"$(pwd)/.codex-workflow-src/scripts/render_prompt.py"
		"$(pwd)/.codex-workflow-src-main/scripts/render_prompt.py"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

resolve_assembly_source_path()
{
	local prompt_path="$1"
	local prompt_dir=""
	local prompt_dir_name=""
	local template_candidate=""

	prompt_dir="$(dirname -- "${prompt_path}")"
	prompt_dir_name="$(basename -- "${prompt_dir}")"
	if [ "${prompt_dir_name}" = "_templates" ]; then
		printf '%s\n' "${prompt_path}"
		return 0
	fi
	if [ "${prompt_dir_name}" = "prompts" ]; then
		template_candidate="${prompt_dir}/_templates/$(basename -- "${prompt_path}")"
		if [ -f "${template_candidate}" ]; then
			printf '%s\n' "${template_candidate}"
			return 0
		fi
	fi
	printf '%s\n' "${prompt_path}"
}

assemble_prompt_run_isolated_python()
{
	local environment_name=""
	local -a isolated_environment=(env -i
		HOME="${HOME:-}"
		PATH="${PATH:-/usr/bin:/bin}"
		TMPDIR="${TMPDIR:-/tmp}"
		LANG="C.UTF-8"
		LC_ALL="C.UTF-8"
		PYTHONDONTWRITEBYTECODE="1")
	local -a forwarded_environment_names=(
		ALLOW_WORKFLOW_EDITS
		MODEL_FAMILY_OVERLAY
		PROMPT_PERSONA_PREFIX_ENABLED
		PROMPT_PRELUDE_REFACTOR_ENABLED
		SEMBLE_PREFETCH
		SERENA_TOOL_HINTS
		UNATTENDED_IDENTITY_REINJECT_ENABLED
		WORKFLOW_OVERLAY_PROMPT_OVERRIDES_JSON
		WORKFLOW_OVERLAY_REPO_ROOT
	)

	for environment_name in "${forwarded_environment_names[@]}"; do
		if [ "${!environment_name+x}" = "x" ]; then
			isolated_environment+=("${environment_name}=${!environment_name}")
		fi
	done
	"${isolated_environment[@]}" python3 "${ASSEMBLE_PROMPT_PYTHON_ARGS[@]}" "$@"
}

if ! command -v python3 >/dev/null 2>&1; then
	echo "Python interpreter not found for render_prompt.py" >&2
	exit 1
fi

# Isolation is mandatory. PYTHON_ISOLATED_MODE remains accepted for caller
# compatibility, but no value can disable the clean environment or -I/-B.
declare -a ASSEMBLE_PROMPT_PYTHON_ARGS=(-I -B)

if ! RENDER_PROMPT_PY="$(resolve_render_prompt_py)"; then
	echo "render_prompt.py not found for ${PROMPT_FILE}" >&2
	exit 1
fi

ASSEMBLY_SOURCE_FILE="$(resolve_assembly_source_path "${PROMPT_FILE}")"

assemble_prompt_run_isolated_python "${RENDER_PROMPT_PY}" \
	"${ASSEMBLY_SOURCE_FILE}" \
	--legacy-mode-name "${MODE_NAME}" \
	--assemble-only
