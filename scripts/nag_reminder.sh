#!/usr/bin/env bash
# nag_reminder.sh — fail-open reminder injection helper for long-running unattended wrapper loops.

NAG_REMINDER_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || printf '.')"
NAG_REMINDER_HELPER_ROOT_DIR="$(cd "${NAG_REMINDER_HELPER_DIR}/.." 2>/dev/null && pwd || printf '.')"

nag_reminder_enabled()
{
	case "$(printf '%s' "${UNATTENDED_NAG_REMINDER_ENABLED:-false}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
		1|true|yes|on) return 0 ;;
	esac
	return 1
}

nag_silent_round_threshold()
{
	local raw="${UNATTENDED_NAG_SILENT_ROUNDS:-3}"
	local normalized=""

	case "${raw}" in
		''|*[!0-9]*)
			printf '3\n'
			return 0
			;;
	esac

	normalized="$(printf '%s' "${raw}" | sed -E 's/^0+//')"
	if [ -z "${normalized}" ]; then
		normalized="0"
	fi

	case "${normalized}" in
		1|2|3|4|5|6|7|8|9|10)
			printf '%s\n' "${normalized}"
			return 0
			;;
	esac

	printf '3\n'
}

_nag_reminder_prompt_file()
{
	local candidate=""

	for candidate in \
		"${SUPPORT_PROMPTS_DIR:+${SUPPORT_PROMPTS_DIR}/_nag_reminders.txt}" \
		"${SUPPORT_ROOT_DIR:+${SUPPORT_ROOT_DIR}/prompts/_nag_reminders.txt}" \
		"${NAG_REMINDER_HELPER_ROOT_DIR}/prompts/_nag_reminders.txt" \
		"prompts/_nag_reminders.txt" \
		".codex-workflow-src/prompts/_nag_reminders.txt" \
		".codex-workflow-src-main/prompts/_nag_reminders.txt"; do
		if [ -n "${candidate}" ] && [ -r "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	printf '\n'
}

load_nag_reminder_text()
{
	local phase="${1:-}"
	local prompt_file=""
	local reminder_text=""

	prompt_file="$(_nag_reminder_prompt_file)"
	if [ -z "${prompt_file}" ]; then
		echo "NAG_REMINDER_LOAD_FAIL: prompts/_nag_reminders.txt unavailable for phase ${phase}" >&2
		printf '\n'
		return 0
	fi

	reminder_text="$(awk -F= -v key="${phase}" '
		/^[[:space:]]*#/ { next }
		NF && $1 == key {
			sub(/^[^=]*=/, "")
			print
			exit
		}
	' "${prompt_file}" 2>/dev/null || true)"

	if [ -z "${reminder_text}" ]; then
		echo "NAG_REMINDER_LOAD_FAIL: phase ${phase} missing in ${prompt_file}" >&2
		printf '\n'
		return 0
	fi

	printf '%s' "${reminder_text}"
}

maybe_inject_nag()
{
	local phase="${1:-}"
	local silent_rounds_raw="${2:-0}"
	local reminder_text="${3:-}"
	local threshold=""
	local silent_rounds="0"

	nag_reminder_enabled || return 0

	threshold="$(nag_silent_round_threshold)"
	case "${silent_rounds_raw}" in
		''|*[!0-9]*) silent_rounds="0" ;;
		*)
			silent_rounds="$(printf '%s' "${silent_rounds_raw}" | sed -E 's/^0+//')"
			if [ -z "${silent_rounds}" ]; then
				silent_rounds="0"
			fi
			case "${silent_rounds}" in
				0|1|2|3|4|5|6|7|8|9|10) ;;
				*) silent_rounds="11" ;;
			esac
			;;
	esac

	if [ "${silent_rounds}" -lt "${threshold}" ]; then
		return 0
	fi

	if [ -z "${reminder_text}" ]; then
		reminder_text="$(load_nag_reminder_text "${phase}")"
	fi
	if [ -z "${reminder_text}" ]; then
		return 0
	fi

	printf '<reminder>\n%s\n</reminder>\n' "${reminder_text}"
}
