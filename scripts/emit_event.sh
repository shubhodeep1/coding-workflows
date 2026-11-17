#!/usr/bin/env bash
# emit_event.sh — fail-open append-only JSONL mirror helper.

if [ "${_EMIT_EVENT_SH_LOADED:-}" = "1" ]; then
	return 0 2>/dev/null || exit 0
fi
_EMIT_EVENT_SH_LOADED=1

case "${BASH_SOURCE[0]:-$0}" in
	*/*)
		_EMIT_EVENT_SCRIPT_DIR="$(CDPATH= cd -- "${BASH_SOURCE[0]%/*}" && pwd)"
		;;
	*)
		_EMIT_EVENT_SCRIPT_DIR="$(pwd)"
		;;
esac

_events_jsonl_enabled()
{
	case "${EVENTS_JSONL_ENABLED:-false}" in
		1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn]|[Yy]) return 0 ;;
		*) return 1 ;;
	esac
}

_emit_event_shell_fail()
{
	local emit_event_reason="${1:-unknown}"
	emit_event_reason="${emit_event_reason//[[:space:]]/_}"
	emit_event_reason="${emit_event_reason//=/_}"
	printf 'EVENTS_EMIT_FAIL reason=%s\n' "${emit_event_reason}" >&2
}

emit_event()
{
	local prefix="${1:-}"
	shift || true

	if ! _events_jsonl_enabled; then
		return 0
	fi
	if [ -z "${prefix}" ]; then
		_emit_event_shell_fail "missing prefix"
		return 0
	fi
	if [ "${prefix}" = "EVENTS_EMIT" ] || [ "${prefix}" = "EVENTS_EMIT_FAIL" ]; then
		return 0
	fi
	if ! command -v python3 >/dev/null 2>&1; then
		_emit_event_shell_fail "python3 unavailable for prefix=${prefix}"
		return 0
	fi
	if [ ! -f "${_EMIT_EVENT_SCRIPT_DIR}/emit_event.py" ]; then
		_emit_event_shell_fail "helper missing ${_EMIT_EVENT_SCRIPT_DIR}/emit_event.py for prefix=${prefix}"
		return 0
	fi

	PYTHONDONTWRITEBYTECODE=1 python3 "${_EMIT_EVENT_SCRIPT_DIR}/emit_event.py" "${prefix}" "$@" || true
	return 0
}

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
	emit_event "$@"
fi
