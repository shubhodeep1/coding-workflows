#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_TIMEOUT_SECS=600

parse_override_command()
{
	local check_cmd="$1"
	python3 - "$check_cmd" <<'PY'
import re
import shlex
import sys

if len(sys.argv) != 2:
	raise SystemExit(1)

command = sys.argv[1]
if not command.strip():
	raise SystemExit(1)

try:
	parts = shlex.split(command, posix=True)
except ValueError as exc:
	print(str(exc), file=sys.stderr)
	raise SystemExit(1)

assignment_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
env_parts = []
while parts and assignment_re.fullmatch(parts[0]):
	env_parts.append(parts.pop(0))

if not parts:
	raise SystemExit(1)

sys.stdout.write(str(len(env_parts)))
sys.stdout.write("\0")
for part in env_parts + parts:
	sys.stdout.write(part)
	sys.stdout.write("\0")
PY
}

run_override_command()
{
	local check_cmd="$1"
	local env_count=0
	local parsed_cmd_start=0
	local parsed_file=""
	local -a parsed_env=()
	local -a parsed_cmd=()
	parsed_file="$(mktemp)" || return 1
	if ! parse_override_command "$check_cmd" >"${parsed_file}"; then
		rm -f "${parsed_file}"
		return 1
	fi
	if ! mapfile -d '' -t parsed_cmd <"${parsed_file}"; then
		rm -f "${parsed_file}"
		return 1
	fi
	rm -f "${parsed_file}"
	env_count="${parsed_cmd[0]:-}"
	[[ "${env_count}" =~ ^[0-9]+$ ]] || return 1
	if [ "${env_count}" -gt 0 ]; then
		parsed_env=("${parsed_cmd[@]:1:${env_count}}")
	fi
	parsed_cmd_start=$(( 1 + env_count ))
	parsed_cmd=("${parsed_cmd[@]:${parsed_cmd_start}}")
	if [ "${#parsed_cmd[@]}" -eq 0 ]; then
		return 1
	fi
	if [ "${#parsed_env[@]}" -gt 0 ]; then
		env "${parsed_env[@]}" timeout "${CHECK_TIMEOUT_SECS}" "${parsed_cmd[@]}"
	else
		timeout "${CHECK_TIMEOUT_SECS}" "${parsed_cmd[@]}"
	fi
}

CHECK_COMMANDS=(
	"python3 tests/test_render_validation_templates.py"
	"python3 tests/test_family_python_repo_checks.py"
	"python3 tests/test_codex_stall_guard_poller.py"
	"python3 tests/test_codex_stall_guard_scripts.py"
	"python3 tests/test_run_substate_ledger.py"
	"python3 tests/test_memory_record_schema.py"
	"python3 tests/test_render_prompt_foundation.py"
	"python3 tests/test_render_prompt_core_modes.py"
	"python3 tests/test_render_prompt_review_judge_modes.py"
	"python3 tests/test_render_prompt_validate_analytics_modes.py"
	"python3 tests/test_validate_process_template_mode.py"
	"python3 tests/test_validate_workflow_validate_bootstrap.py"
)

if [ "$#" -gt 0 ]; then
	CHECK_COMMANDS=("$@")
fi

for check_cmd in "${CHECK_COMMANDS[@]}"; do
	echo "# repo-check start: ${check_cmd}"
	output_file="$(mktemp)" || { echo "# repo-check error: mktemp failed" >&2; exit 1; }
	set +e
	(cd "${ROOT_DIR}" && run_override_command "${check_cmd}") >"${output_file}" 2>&1
	check_rc=$?
	set -e
	if [ "${check_rc}" -ne 0 ]; then
		echo "# repo-check failed: ${check_cmd} rc=${check_rc} timeout_secs=${CHECK_TIMEOUT_SECS}" >&2
		cat "${output_file}" >&2
		rm -f "${output_file}"
		exit 1
	fi
	cat "${output_file}"
	rm -f "${output_file}"
	echo "# repo-check ok: ${check_cmd}"
done
