#!/usr/bin/env bash
# Shared OpenCode command, output, bootstrap, and alert helpers.

if [ "${_OPENCODE_HELPERS_LOADED:-}" = "true" ]; then
	# shellcheck disable=SC2317
	return 0 2>/dev/null || true
fi
_OPENCODE_HELPERS_LOADED="true"

_opencode_helpers_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_opencode_error()
{
	printf '::error::opencode_helpers.sh: %s\n' "$1" >&2
}

_opencode_alert_field()
{
	printf '%s' "${1:-unknown}" | LC_ALL=C tr -c 'A-Za-z0-9_.:/+-' '_'
}

opencode_strip_ansi()
{
	python3 -c 'import re, sys; data = sys.stdin.buffer.read(); pattern = rb"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|[P^_].*?\x1b\\|\[[0-?]*[ -/]*[@-~]|[ -/]*[0-~])"; sys.stdout.buffer.write(re.sub(pattern, b"", data, flags=re.DOTALL))'
}

opencode_run_cmd()
{
	if [ "$#" -lt 5 ] || [ "$#" -gt 6 ]; then
		_opencode_error "opencode_run_cmd requires role, model, variant, config path, working directory, and optional output format"
		return 2
	fi

	local role="$1"
	local model_slug="$2"
	local variant="$3"
	local config_path="$4"
	local working_directory="$5"
	local output_format="${6:-default}"
	local -a opencode_argv

	case "${role}" in
		reviewer|writer) ;;
		*)
			_opencode_error "invalid role '${role}'"
			return 2
			;;
	esac
	if [[ ! "${model_slug}" =~ ^[A-Za-z0-9][A-Za-z0-9._:+-]*(/[A-Za-z0-9][A-Za-z0-9._:+-]*)+$ ]]; then
		_opencode_error "invalid model '${model_slug}'"
		return 2
	fi
	case "${variant}" in
		xhigh|high|medium|low|none) ;;
		*)
			_opencode_error "invalid variant '${variant}'"
			return 2
			;;
	esac
	case "${output_format}" in
		default|json) ;;
		*)
			_opencode_error "invalid output format '${output_format}'"
			return 2
			;;
	esac
	[ -r "${config_path}" ] || {
		_opencode_error "config is not readable: ${config_path}"
		return 2
	}
	[ -d "${working_directory}" ] || {
		_opencode_error "working directory does not exist: ${working_directory}"
		return 2
	}

	opencode_argv=(
		opencode run
		--dir "${working_directory}"
		-m "openrouter/${model_slug}"
		--agent "${role}"
		--variant "${variant}"
		--title "coding-workflows-agent-run"
		--print-logs
		--log-level INFO
	)
	# Preserve the existing argv unless a caller explicitly needs machine-readable evidence.
	if [ "${output_format}" = "json" ]; then
		opencode_argv+=(--format "${output_format}")
	fi
	if [ "${role}" = "writer" ]; then
		opencode_argv+=(--auto)
	fi

	printf 'opencode_agent_start role=%s expected_provider=openrouter expected_model=%s variant=%s\n' \
		"${role}" "$(_opencode_alert_field "${model_slug}")" "${variant}" >&2
	OPENCODE_CONFIG="${config_path}" NO_COLOR=1 "${opencode_argv[@]}"
}

opencode_emit_failure_alert()
{
	if [ "$#" -ne 5 ]; then
		_opencode_error "opencode_emit_failure_alert requires phase, role, model, rc, and failure class"
		return 2
	fi

	local phase role model_slug original_rc failure_class alert_rc payload
	phase="$(_opencode_alert_field "$1")"
	role="$(_opencode_alert_field "$2")"
	model_slug="$(_opencode_alert_field "$3")"
	original_rc="$4"
	failure_class="$(_opencode_alert_field "$5")"
	if [[ "${original_rc}" =~ ^[1-9][0-9]*$ ]] && [ "${original_rc}" -le 255 ]; then
		alert_rc="${original_rc}"
	else
		alert_rc=1
	fi

	payload="opencode_agent_failure phase=${phase} role=${role} model=${model_slug} rc=${alert_rc} failure_class=${failure_class}"
	printf '%s\n' "${payload}" >&2

	if ! type tg_send_msg >/dev/null 2>&1 && [ -r "${_opencode_helpers_dir}/tg_helpers.sh" ]; then
		# shellcheck source=tg_helpers.sh
		# shellcheck disable=SC1091
		source "${_opencode_helpers_dir}/tg_helpers.sh"
	fi
	if type tg_send_msg >/dev/null 2>&1; then
		tg_send_msg "${payload}" "ERROR" >/dev/null || true
	fi

	return "${alert_rc}"
}

opencode_require_bootstrap()
{
	if [ "$#" -lt 4 ] || [ "$#" -gt 6 ]; then
		_opencode_error "opencode_require_bootstrap requires phase, role, model, config path, optional version, and optional writer path"
		return 2
	fi

	local phase="$1"
	local role="$2"
	local model_slug="$3"
	local config_path="$4"
	local expected_version="${5:-${OPENCODE_VERSION:-1.18.23}}"
	local writer_path="${6:-${_opencode_helpers_dir}/write_opencode_config.sh}"
	local installed_version=""

	if [[ ! "${expected_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		opencode_emit_failure_alert "${phase}" "${role}" "${model_slug}" 2 invalid_expected_version || return $?
	fi
	if ! command -v opencode >/dev/null 2>&1; then
		opencode_emit_failure_alert "${phase}" "${role}" "${model_slug}" 127 binary_missing || return $?
	fi
	installed_version="$(opencode --version 2>/dev/null || true)"
	if [ "${installed_version}" != "${expected_version}" ]; then
		opencode_emit_failure_alert "${phase}" "${role}" "${model_slug}" 1 version_mismatch || return $?
	fi
	if [ ! -r "${writer_path}" ]; then
		opencode_emit_failure_alert "${phase}" "${role}" "${model_slug}" 1 config_writer_missing || return $?
	fi
	if [ ! -r "${config_path}" ] || [ ! -s "${config_path}" ]; then
		opencode_emit_failure_alert "${phase}" "${role}" "${model_slug}" 1 config_unreadable || return $?
	fi
	if ! python3 -m json.tool "${config_path}" >/dev/null 2>&1; then
		opencode_emit_failure_alert "${phase}" "${role}" "${model_slug}" 1 config_invalid || return $?
	fi

	return 0
}
