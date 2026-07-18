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

# {{WORKFLOW_EDIT_RESTRICTION}} resolves to one of two lines based on
# ${ALLOW_WORKFLOW_EDITS}. The implement-mode prompt previously hard-coded
# "Do not change CI workflows." which contradicted plans whose `files_touched`
# set legitimately includes `.github/workflows/**` when the workflow runs with
# ALLOW_WORKFLOW_EDITS=true (downstream commit/validate gates already honour
# the same env var). Default is "false" so the prohibitive line stays the
# safe fallback for any caller that forgets to export the env var.
if [ "${ALLOW_WORKFLOW_EDITS:-false}" = "true" ]; then
	WORKFLOW_EDIT_RESTRICTION_LINE="- CI workflow edits under .github/workflows/ are permitted when required by the approved plan; keep changes inside the plan's stated file scope."
else
	WORKFLOW_EDIT_RESTRICTION_LINE="- Do not change CI workflows."
fi

# {{SEMBLE_PREFETCH}} resolves from the optional ${SEMBLE_PREFETCH}
# environment variable. Prompt consumers set it per render invocation so the
# bounded Semble block stays in the dynamic prompt section and does not leak
# across different prompt builds in the same shell process.
SEMBLE_PREFETCH_BLOCK="${SEMBLE_PREFETCH:-}"

# {{SERENA_TOOL_HINTS}} resolves from the optional ${SERENA_TOOL_HINTS}
# environment variable. Prompt consumers set it per render invocation so the
# Serena tool-usage guidance stays prompt-local and renders to an empty block
# when Serena is unavailable.
# Editor-only Serena guidance can be injected through the shared renderer
# without touching reviewer or judge prompt assembly.
SERENA_TOOL_HINTS_BLOCK="${SERENA_TOOL_HINTS:-}"

# {{MODEL_FAMILY_OVERLAY}} resolves from the optional
# ${MODEL_FAMILY_OVERLAY} environment variable. Prompt consumers set it per
# render invocation so reviewer-family-specific guidance stays prompt-local
# and renders to an empty block when no overlay applies.
MODEL_FAMILY_OVERLAY_BLOCK="${MODEL_FAMILY_OVERLAY:-}"

# Legacy source-contract sentinels intentionally preserved while the shim now
# delegates rendering to scripts/render_prompt.py.
# "{{WORKFLOW_EDIT_RESTRICTION}}")
# "{{SEMBLE_PREFETCH}}")
# "{{SERENA_TOOL_HINTS}}")
# "{{MODEL_FAMILY_OVERLAY}}")
# Unresolved WORKFLOW_EDIT_RESTRICTION placeholder in rendered output for ${PROMPT_FILE}
# Unresolved SEMBLE_PREFETCH placeholder in rendered output for ${PROMPT_FILE}
# Unresolved SERENA_TOOL_HINTS placeholder in rendered output for ${PROMPT_FILE}
# Unresolved MODEL_FAMILY_OVERLAY placeholder in rendered output for ${PROMPT_FILE}

PROMPT_BASENAME="$(basename -- "${PROMPT_FILE}")"
MODE_NAME="${PROMPT_BASENAME%.*}"

render_prompt_env_is_truthy()
{
	local value="${1:-}"
	value="${value#"${value%%[![:space:]]*}"}"
	value="${value%"${value##*[![:space:]]}"}"
	case "${value}" in
		1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy]) return 0 ;;
		*) return 1 ;;
	esac
}

resolve_identity_recall_template()
{
	local prompt_path="${1:-}"
	local prompt_root_dir=""
	local candidate=""
	local -a candidates=()

	if [ -n "${prompt_path}" ]; then
		prompt_root_dir="$(resolve_prompt_root_dir "${prompt_path}")"
		candidates+=("${prompt_root_dir}/_identity_recall.txt")
	fi

	candidates+=(
		"$(pwd)/prompts/_identity_recall.txt"
		"${SCRIPT_DIR}/../prompts/_identity_recall.txt"
		"$(pwd)/.codex-workflow-src/prompts/_identity_recall.txt"
		"$(pwd)/.codex-workflow-src-main/prompts/_identity_recall.txt"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

resolve_identity_source_prompt()
{
	local prompt_path="$1"
	local prompt_basename=""
	local prompt_dir=""
	local prompt_dir_name=""
	local candidate=""
	local template_candidate=""
	local ancestor=""
	local parent=""
	local -a candidates=()

	prompt_basename="$(basename -- "${prompt_path}")"
	prompt_dir="$(dirname -- "${prompt_path}")"
	prompt_dir_name="$(basename -- "${prompt_dir}")"

	if [[ "${prompt_basename}" = mode-*-inline.txt ]]; then
		prompt_basename="${prompt_basename%-inline.txt}.txt"
	fi
	if [ "${prompt_dir_name}" = "_templates" ]; then
		candidate="$(dirname -- "${prompt_dir}")/${prompt_basename}"
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	fi
	if [ "${prompt_dir_name}" = "prompts" ] && [ "${PROMPT_PRELUDE_REFACTOR_ENABLED:-false}" = "true" ]; then
		template_candidate="${prompt_dir}/_templates/${prompt_basename}"
		if [ -f "${template_candidate}" ]; then
			printf '%s\n' "${template_candidate}"
			return 0
		fi
	fi

	if [ -f "${prompt_path}" ] && [[ "${prompt_basename}" = mode-*.txt ]]; then
		if [ "${prompt_basename}" = "$(basename -- "${prompt_path}")" ]; then
			printf '%s\n' "${prompt_path}"
			return 0
		fi
	fi

	candidates+=("${prompt_dir}/${prompt_basename}")
	ancestor="${prompt_dir}"
	while :; do
		if [ "${PROMPT_PRELUDE_REFACTOR_ENABLED:-false}" = "true" ]; then
			candidates+=("${ancestor}/prompts/_templates/${prompt_basename}")
		fi
		candidates+=("${ancestor}/prompts/${prompt_basename}")
		parent="$(dirname -- "${ancestor}")"
		if [ "${parent}" = "${ancestor}" ]; then
			break
		fi
		ancestor="${parent}"
	done
	candidates+=(
		"$(pwd)/prompts/${prompt_basename}"
		"$(pwd)/.codex-workflow-src/prompts/${prompt_basename}"
		"$(pwd)/.codex-workflow-src-main/prompts/${prompt_basename}"
		"${SCRIPT_DIR}/../prompts/${prompt_basename}"
	)

	for candidate in "${candidates[@]}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

resolve_identity_phase_name()
{
	local canonical_prompt_file="$1"
	basename -- "${canonical_prompt_file}" .txt
}

extract_identity_recall_metadata()
{
	local canonical_prompt_file="$1"
	"${PYTHON_BIN}" - <<'PY' "${canonical_prompt_file}"
import pathlib
import re
import sys

prompt_path = pathlib.Path(sys.argv[1])
text = prompt_path.read_text(encoding="utf-8")
lines = text.splitlines()
paragraph_lines = []
started = False
in_compaction_rules = False

for raw_line in lines:
	line = raw_line.strip()
	if not started:
		if in_compaction_rules:
			if line == "</compaction-rules>":
				in_compaction_rules = False
			continue
		if not line or line.startswith("#") or (line.startswith("{%") and line.endswith("%}")):
			continue
		if line.startswith("<compaction-rules>"):
			if not line.endswith("</compaction-rules>"):
				in_compaction_rules = True
			continue
		started = True
	if not line:
		break
	paragraph_lines.append(line)

paragraph = " ".join(paragraph_lines)
match = re.match(r"^Role:\s*(?P<role>.+?)\s+Goal:\s*(?P<goal>.+?)\s*$", paragraph)
if not match:
	sys.exit(1)

def normalize(value: str) -> str:
	return value.strip().rstrip(". ")

role = normalize(match.group("role"))
goal = normalize(match.group("goal"))
if not role or not goal:
	sys.exit(1)

print(role)
print(goal)
PY
}

render_identity_recall_block()
{
	local phase_name="$1"
	local phase_role="$2"
	local phase_mission="$3"
	local prompt_path="${4:-}"
	local identity_template_file=""
	local -a identity_render_args=()

	if ! identity_template_file="$(resolve_identity_recall_template "${prompt_path}")"; then
		return 1
	fi

	identity_render_args=(
		"${PYTHON_BIN}" "${RENDER_PROMPT_PY}" "${identity_template_file}"
		--var "PHASE_NAME=${phase_name}"
		--var "PHASE_ROLE=${phase_role}"
		--var "PHASE_MISSION=${phase_mission}"
	)

	"${identity_render_args[@]}"
}

inject_identity_recall_block()
{
	local prompt_input_file="$1"
	local rendered_identity_block="$2"
	local injected_file=""

	injected_file="$(mktemp "${TMPDIR:-/tmp}/.${PROMPT_BASENAME}.identity.XXXXXX")"
	IDENTITY_RECALL_INJECTED_FILE="${injected_file}"
	trap cleanup_temp_files EXIT

	"${PYTHON_BIN}" - <<'PY' "${prompt_input_file}" "${injected_file}" "${rendered_identity_block}"
import pathlib
import re
import sys

source_path = pathlib.Path(sys.argv[1])
target_path = pathlib.Path(sys.argv[2])
identity_block = sys.argv[3]
text = source_path.read_text(encoding="utf-8")

ROLE_GOAL_RE = re.compile(r"^Role:\s*(?P<role>.+?)\s+Goal:\s*(?P<goal>.+?)\s*$")


def inject_after_offset(rendered_text: str, paragraph_end: int) -> str:
	before = rendered_text[:paragraph_end].rstrip("\n")
	remainder = rendered_text[paragraph_end:].lstrip("\r\n")
	if remainder:
		return before + "\n\n" + identity_block + "\n\n" + remainder
	return before + "\n\n" + identity_block + "\n"


def opening_role_goal_end_offset(rendered_text: str) -> int | None:
	paragraph_lines: list[str] = []
	paragraph_end: int | None = None
	started = False
	in_compaction_rules = False
	offset = 0

	for raw_line in rendered_text.splitlines(keepends=True):
		line = raw_line.strip()
		if not started:
			if in_compaction_rules:
				if line == "</compaction-rules>":
					in_compaction_rules = False
				offset += len(raw_line)
				continue
			if not line or line.startswith("#") or (line.startswith("{%") and line.endswith("%}")):
				offset += len(raw_line)
				continue
			if line.startswith("<compaction-rules>"):
				if not line.endswith("</compaction-rules>"):
					in_compaction_rules = True
				offset += len(raw_line)
				continue
			started = True
		if not line:
			paragraph = " ".join(paragraph_lines)
			if paragraph_end is None or ROLE_GOAL_RE.match(paragraph) is None:
				return None
			return paragraph_end
		paragraph_lines.append(line)
		paragraph_end = offset + len(raw_line.rstrip("\r\n"))
		offset += len(raw_line)

	if not started or not paragraph_lines or paragraph_end is None:
		return None
	paragraph = " ".join(paragraph_lines)
	if ROLE_GOAL_RE.match(paragraph) is None:
		return None
	return paragraph_end


paragraph_end = opening_role_goal_end_offset(text)
if paragraph_end is not None:
	updated = inject_after_offset(text, paragraph_end)
else:
	parts = text.split("\n\n", 1)
	if len(parts) == 2:
		updated = parts[0] + "\n\n" + identity_block + "\n\n" + parts[1]
	else:
		updated = text + "\n\n" + identity_block + "\n"
target_path.write_text(updated, encoding="utf-8")
PY
}

emit_identity_reinject_parse_fail()
{
	local failure_reason="$1"
	echo "IDENTITY_REINJECT_PARSE_FAIL: ${MODE_NAME} reason=${failure_reason}" >&2
}

resolve_render_prompt_py()
{
	local candidate=""
	local -a candidates=()

	# Only trust renderer backends shipped with the workflow source itself.
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

resolve_assemble_prompt_sh()
{
	local candidate=""
	local -a candidates=()

	candidates+=(
		"${SCRIPT_DIR}/assemble_prompt.sh"
		"$(pwd)/.codex-workflow-src/scripts/assemble_prompt.sh"
		"$(pwd)/.codex-workflow-src-main/scripts/assemble_prompt.sh"
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

resolve_prompt_root_dir()
{
	local prompt_path="$1"
	local prompt_dir=""
	local prompt_dir_name=""

	prompt_dir="$(dirname -- "${prompt_path}")"
	prompt_dir_name="$(basename -- "${prompt_dir}")"
	if [ "${prompt_dir_name}" = "prompts" ]; then
		printf '%s\n' "${prompt_dir}"
		return 0
	fi
	if [ "${prompt_dir_name}" = "_templates" ] && [ "$(basename -- "$(dirname -- "${prompt_dir}")")" = "prompts" ]; then
		printf '%s\n' "$(dirname -- "${prompt_dir}")"
		return 0
	fi
	printf '%s\n' "${prompt_dir}"
}

cleanup_temp_files()
{
	if [ -n "${IDENTITY_RECALL_INJECTED_FILE:-}" ] && [ -f "${IDENTITY_RECALL_INJECTED_FILE}" ]; then
		rm -f "${IDENTITY_RECALL_INJECTED_FILE}"
	fi
	if [ -n "${ASSEMBLED_PROMPT_FILE:-}" ] && [ -f "${ASSEMBLED_PROMPT_FILE}" ]; then
		rm -f "${ASSEMBLED_PROMPT_FILE}"
	fi
}

collect_prompt_placeholders()
{
	local placeholder_source_file="$1"
	"${PYTHON_BIN}" - <<'PY' "${placeholder_source_file}"
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
for name in sorted(set(re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", text))):
	print(name)
PY
}

append_render_var()
{
	local name="$1"
	local value="$2"
	if [ -n "${RENDER_VARS_SEEN[${name}]+x}" ]; then
		return 0
	fi
	RENDER_VARS_SEEN["${name}"]=1
	RENDER_ARGS+=(--var "${name}=${value}")
}

if command -v python3 >/dev/null 2>&1; then
	PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
	PYTHON_BIN="python"
else
	echo "Python interpreter not found for render_prompt.py" >&2
	exit 1
fi

if ! RENDER_PROMPT_PY="$(resolve_render_prompt_py)"; then
	echo "render_prompt.py not found for ${PROMPT_FILE}" >&2
	exit 1
fi

RENDER_INPUT_FILE="${PROMPT_FILE}"
PLACEHOLDER_SOURCE_FILE="${PROMPT_FILE}"
ASSEMBLED_PROMPT_FILE=""

if [ "${PROMPT_PRELUDE_REFACTOR_ENABLED:-false}" = "true" ]; then
	ASSEMBLY_SOURCE_FILE="$(resolve_assembly_source_path "${PROMPT_FILE}")"
	if [ "${ASSEMBLY_SOURCE_FILE}" != "${PROMPT_FILE}" ] || [ "$(basename -- "$(dirname -- "${PROMPT_FILE}")")" = "_templates" ]; then
		if ! ASSEMBLE_PROMPT_SH="$(resolve_assemble_prompt_sh)"; then
			echo "assemble_prompt.sh not found for ${PROMPT_FILE}" >&2
			exit 1
		fi
		PROMPT_ROOT_DIR="$(resolve_prompt_root_dir "${ASSEMBLY_SOURCE_FILE}")"
		ASSEMBLED_PROMPT_FILE="$(mktemp "${PROMPT_ROOT_DIR}/.${PROMPT_BASENAME}.assembled.XXXXXX")"
		trap cleanup_temp_files EXIT
		"${ASSEMBLE_PROMPT_SH}" "${PROMPT_FILE}" > "${ASSEMBLED_PROMPT_FILE}"
		RENDER_INPUT_FILE="${ASSEMBLED_PROMPT_FILE}"
		PLACEHOLDER_SOURCE_FILE="${ASSEMBLED_PROMPT_FILE}"
	fi
fi

if render_prompt_env_is_truthy "${UNATTENDED_IDENTITY_REINJECT_ENABLED:-false}" && [[ "${MODE_NAME}" = mode-* ]]; then
	IDENTITY_RECALL_CANONICAL_PROMPT=""
	IDENTITY_RECALL_PHASE_NAME=""
	IDENTITY_RECALL_METADATA=""
	IDENTITY_RECALL_ROLE=""
	IDENTITY_RECALL_MISSION=""
	IDENTITY_RECALL_BLOCK=""

	if ! IDENTITY_RECALL_CANONICAL_PROMPT="$(resolve_identity_source_prompt "${PROMPT_FILE}")"; then
		emit_identity_reinject_parse_fail "canonical_prompt_missing"
	elif ! IDENTITY_RECALL_PHASE_NAME="$(resolve_identity_phase_name "${IDENTITY_RECALL_CANONICAL_PROMPT}")"; then
		emit_identity_reinject_parse_fail "phase_name_missing"
	elif ! IDENTITY_RECALL_METADATA="$(extract_identity_recall_metadata "${IDENTITY_RECALL_CANONICAL_PROMPT}")"; then
		emit_identity_reinject_parse_fail "metadata_extract_failed"
	else
		IDENTITY_RECALL_ROLE="$(printf '%s\n' "${IDENTITY_RECALL_METADATA}" | sed -n '1p')"
		IDENTITY_RECALL_MISSION="$(printf '%s\n' "${IDENTITY_RECALL_METADATA}" | sed -n '2p')"
		if [ -z "${IDENTITY_RECALL_PHASE_NAME}" ] || [ -z "${IDENTITY_RECALL_ROLE}" ] || [ -z "${IDENTITY_RECALL_MISSION}" ]; then
			emit_identity_reinject_parse_fail "identity_metadata_incomplete"
		elif ! IDENTITY_RECALL_BLOCK="$(render_identity_recall_block "${IDENTITY_RECALL_PHASE_NAME}" "${IDENTITY_RECALL_ROLE}" "${IDENTITY_RECALL_MISSION}" "${IDENTITY_RECALL_CANONICAL_PROMPT}")"; then
			emit_identity_reinject_parse_fail "render_failed"
		elif ! inject_identity_recall_block "${RENDER_INPUT_FILE}" "${IDENTITY_RECALL_BLOCK}"; then
			emit_identity_reinject_parse_fail "injection_failed"
		else
			RENDER_INPUT_FILE="${IDENTITY_RECALL_INJECTED_FILE}"
			PLACEHOLDER_SOURCE_FILE="${RENDER_INPUT_FILE}"
		fi
	fi
fi

declare -A RENDER_VARS_SEEN=()
declare -a RENDER_ARGS=()

RENDER_ARGS=(
	"${PYTHON_BIN}" "${RENDER_PROMPT_PY}" "${RENDER_INPUT_FILE}"
	--legacy-mode-name "${MODE_NAME}"
)

INPUT_ALREADY_ASSEMBLED_FLAG_ADDED=false
if [ -n "${ASSEMBLED_PROMPT_FILE}" ]; then
	RENDER_ARGS+=(--input-already-assembled)
	INPUT_ALREADY_ASSEMBLED_FLAG_ADDED=true
fi

# Opt-in: callers rendering an already-COMPOSED prompt body that embeds
# untrusted content (reviewer/editor bodies concatenate raw PR-diff + comment
# text, which can legitimately contain literal `{% include "..." %}` lines from
# a template-driven consumer repo — Jinja/Django/Nunjucks/Twig/Liquid all use
# that syntax) set RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED=1 so render_prompt.py
# does NOT re-run include-assembly over that body. Without it, a diff/context
# line like `{% include "_partials/site_footer.html" %}` is parsed as a real
# prompt-fragment include, fails to resolve under the prompt search path, and
# hard-fails the whole render with PromptAssemblyError (observed on
# tele-funtoken-msg-scoring run 29182737982). This is distinct from
# RENDER_PROMPT_SKIP_SYNTAX_VALIDATION below: the skip-syntax gate only silences
# validate_supported_template_syntax and does NOT stop the earlier
# assemble_prompt_fragments include expansion. Callers that embed untrusted
# content may need this flag, RENDER_PROMPT_SKIP_SYNTAX_VALIDATION, or both,
# depending on which failure mode they need to suppress. Placeholder
# substitution for the static scaffolding still runs. Default (unset) keeps
# include assembly for every trusted template render.
case "${RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED:-}" in
	1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy])
		if [ "${INPUT_ALREADY_ASSEMBLED_FLAG_ADDED}" != "true" ]; then
			RENDER_ARGS+=(--input-already-assembled)
			INPUT_ALREADY_ASSEMBLED_FLAG_ADDED=true
		fi
		;;
esac

# Opt-in: callers rendering an already-assembled prompt body that embeds
# untrusted content (reviewer/editor bodies carry raw PR-diff + comment text,
# which can legitimately contain literal {{...}} / {%...%} tokens) set
# RENDER_PROMPT_SKIP_SYNTAX_VALIDATION=1 so those embedded tokens are not
# mistaken for prompt-authoring errors and the whole render is not hard-failed.
# Default (unset) keeps the strict syntax gate for every static template render.
case "${RENDER_PROMPT_SKIP_SYNTAX_VALIDATION:-}" in
	1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy])
		RENDER_ARGS+=(--skip-syntax-validation)
		;;
esac

append_render_var "WORKFLOW_EDIT_RESTRICTION" "${WORKFLOW_EDIT_RESTRICTION_LINE}"
append_render_var "SEMBLE_PREFETCH" "${SEMBLE_PREFETCH_BLOCK}"
append_render_var "SERENA_TOOL_HINTS" "${SERENA_TOOL_HINTS_BLOCK}"
append_render_var "MODEL_FAMILY_OVERLAY" "${MODEL_FAMILY_OVERLAY_BLOCK}"

while IFS= read -r placeholder_name; do
	[ -n "${placeholder_name}" ] || continue
	if [ "${!placeholder_name+x}" = "x" ]; then
		append_render_var "${placeholder_name}" "${!placeholder_name}"
	fi
done < <(collect_prompt_placeholders "${PLACEHOLDER_SOURCE_FILE}")

if [ -n "${ASSEMBLED_PROMPT_FILE}" ] || [ -n "${IDENTITY_RECALL_INJECTED_FILE:-}" ]; then
	UNATTENDED_IDENTITY_REINJECT_ENABLED=false "${RENDER_ARGS[@]}"
	exit 0
fi

exec env UNATTENDED_IDENTITY_REINJECT_ENABLED=false "${RENDER_ARGS[@]}"
