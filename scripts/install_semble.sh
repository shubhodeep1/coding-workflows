#!/usr/bin/env bash
# install_semble.sh — fail-soft Semble installer for GitHub Actions jobs.

set -euo pipefail

SEMBLE_VERSION="0.1.3"
SEMBLE_SPEC="semble==${SEMBLE_VERSION}"
SEMBLE_PYTHON_BIN="${SEMBLE_PYTHON_BIN:-python3}"

log()
{
	printf 'install_semble: %s\n' "$*" >&2
}

write_github_env()
{
	local key="${1:?write_github_env: key required}"
	local value="${2:?write_github_env: value required}"

	if [ -z "${GITHUB_ENV:-}" ]; then
		return 0
	fi
	if ! printf '%s=%s\n' "${key}" "${value}" >> "${GITHUB_ENV}" 2>/dev/null; then
		log "unable to append ${key} to GITHUB_ENV=${GITHUB_ENV}; continuing."
	fi
}

append_github_path()
{
	local dir="${1:-}"

	if [ -z "${dir}" ]; then
		return 0
	fi
	case ":${PATH}:" in
		*":${dir}:"*) ;;
		*) PATH="${dir}:${PATH}" ;;
	esac
	if [ -n "${GITHUB_PATH:-}" ]; then
		if ! printf '%s\n' "${dir}" >> "${GITHUB_PATH}" 2>/dev/null; then
			log "unable to append ${dir} to GITHUB_PATH=${GITHUB_PATH}; continuing."
		fi
	fi
}

python_user_bin()
{
	if command -v "${SEMBLE_PYTHON_BIN}" >/dev/null 2>&1; then
		"${SEMBLE_PYTHON_BIN}" - <<'PY'
import site
print(site.USER_BASE + "/bin")
PY
		return 0
	fi
	if [ -n "${HOME:-}" ]; then
		printf '%s/.local/bin\n' "${HOME}"
		return 0
	fi
	return 1
}

current_semble_version()
{
	local semble_bin=""

	semble_bin="$(command -v semble 2>/dev/null || true)"
	if [ -z "${semble_bin}" ]; then
		return 1
	fi
	"${semble_bin}" --version 2>/dev/null || return 1
}

binary_matches_pin()
{
	local version_text=""

	version_text="$(current_semble_version)" || return 1
	case "${version_text}" in
		*"${SEMBLE_VERSION}"*)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

mark_available()
{
	write_github_env "SEMBLE_AVAILABLE" "true"
	log "Semble ${SEMBLE_VERSION} is available."
}

mark_unavailable()
{
	write_github_env "SEMBLE_AVAILABLE" "false"
	log "Semble ${SEMBLE_VERSION} is unavailable; callers should use fallback paths."
}

attempt_pip_install()
{
	local pip_log=""
	local user_bin=""

	if ! command -v "${SEMBLE_PYTHON_BIN}" >/dev/null 2>&1; then
		log "${SEMBLE_PYTHON_BIN} is unavailable; cannot install ${SEMBLE_SPEC}."
		return 1
	fi

	user_bin="$(python_user_bin || true)"
	append_github_path "${user_bin}"

	pip_log="$(mktemp 2>/dev/null || true)"
	if [ -z "${pip_log}" ]; then
		log "mktemp unavailable; skipping install attempt for ${SEMBLE_SPEC}."
		return 1
	fi

	if "${SEMBLE_PYTHON_BIN}" -m pip install \
		--disable-pip-version-check \
		--quiet \
		--user \
		"${SEMBLE_SPEC}" >"${pip_log}" 2>&1; then
		rm -f "${pip_log}"
		return 0
	fi

	if "${SEMBLE_PYTHON_BIN}" -m pip install \
		--disable-pip-version-check \
		--quiet \
		--user \
		--break-system-packages \
		"${SEMBLE_SPEC}" >"${pip_log}" 2>&1; then
		rm -f "${pip_log}"
		return 0
	fi

	log "pip install failed for ${SEMBLE_SPEC}: $(tail -n 1 "${pip_log}" 2>/dev/null || printf 'unknown-error')"
	rm -f "${pip_log}"
	return 1
}

main()
{
	local user_bin=""
	local existing_version=""

	user_bin="$(python_user_bin || true)"
	append_github_path "${user_bin}"

	if binary_matches_pin; then
		mark_available
		return 0
	fi

	existing_version="$(current_semble_version || true)"
	if [ -n "${existing_version}" ]; then
		log "found non-pinned Semble (${existing_version}); attempting install of ${SEMBLE_SPEC}."
	else
		log "Semble not found; attempting install of ${SEMBLE_SPEC}."
	fi

	if attempt_pip_install && binary_matches_pin; then
		mark_available
		return 0
	fi

	mark_unavailable
	return 0
}

main "$@"
