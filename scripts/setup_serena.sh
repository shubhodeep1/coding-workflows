#!/usr/bin/env bash
# setup_serena.sh — fail-soft Serena bootstrapper for Codex MCP usage.

set -euo pipefail

SERENA_VERSION="1.2.0"
SERENA_SPEC="serena-agent==${SERENA_VERSION}"
SERENA_BIN_NAME="serena"
SERENA_UV_PYTHON_BIN="${SERENA_UV_PYTHON_BIN:-python3}"
SERENA_STARTUP_TIMEOUT_SEC="30"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
SERENA_TEMPLATE_PATH="${SCRIPT_DIR}/templates/serena_project.yml.j2"
SERENA_PROJECT_PATH="${REPO_ROOT}/.serena/project.yml"

log()
{
	printf 'setup_serena: %s\n' "$*" >&2
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

env_is_truthy()
{
	local value="${1:-}"
	case "$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')" in
		1|true|yes|on)
			return 0
			;;
		*)
			return 1
			;;
	esac
}

export_serena_available()
{
	local value="${1:?export_serena_available: value required}"
	export SERENA_AVAILABLE="${value}"
	write_github_env "SERENA_AVAILABLE" "${value}"
}

uv_tool_bin_dir()
{
	if command -v uv >/dev/null 2>&1; then
		if uv tool dir --bin >/dev/null 2>&1; then
			uv tool dir --bin 2>/dev/null
			return 0
		fi
	fi
	if [ -n "${HOME:-}" ]; then
		printf '%s/.local/bin\n' "${HOME}"
		return 0
	fi
	return 1
}

current_serena_version()
{
	local serena_bin=""

	serena_bin="$(command -v "${SERENA_BIN_NAME}" 2>/dev/null || true)"
	if [ -z "${serena_bin}" ]; then
		return 1
	fi
	"${serena_bin}" --version 2>/dev/null || return 1
}

binary_matches_pin()
{
	local version_text=""
	local version_pattern=""

	version_text="$(current_serena_version)" || return 1
	version_text="${version_text%%$'\n'*}"
	version_pattern="${SERENA_VERSION//./\\.}"
	if printf '%s\n' "${version_text}" | grep -Eq "(^|[^0-9.])${version_pattern}([^0-9.]|$)"; then
		return 0
	fi
	return 1
}

clear_serena_codex_config()
{
	if [ -z "${HOME:-}" ]; then
		log 'HOME is unavailable; cannot clear ~/.codex/config.toml.'
		return 1
	fi

	SERENA_CONFIG_PATH="${HOME}/.codex/config.toml" \
	SERENA_BLOCK_COMMAND="" \
	PYTHONDONTWRITEBYTECODE=1 \
	"${SERENA_UV_PYTHON_BIN}" - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

try:
	import tomllib
except ModuleNotFoundError:  # pragma: no cover
	tomllib = None  # type: ignore[assignment]


def is_serena_header(line: str) -> bool:
	stripped = line.strip()
	return stripped == "[mcp_servers.serena]" or stripped.startswith("[mcp_servers.serena.")


def is_table_header(line: str) -> bool:
	stripped = line.strip()
	return stripped.startswith("[") and stripped.endswith("]")


def strip_serena_block(text: str) -> str:
	lines = text.splitlines(keepends=True)
	out: list[str] = []
	i = 0
	while i < len(lines):
		line = lines[i]
		if is_serena_header(line):
			i += 1
			while i < len(lines):
				if is_table_header(lines[i]) and not is_serena_header(lines[i]):
					break
				i += 1
			continue
		out.append(line)
		i += 1
	return "".join(out).rstrip("\n")


config_path = Path(os.environ["SERENA_CONFIG_PATH"])
command_path = os.environ.get("SERENA_BLOCK_COMMAND", "")
existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
rendered = strip_serena_block(existing)

if command_path:
	block_lines = [
		"[mcp_servers.serena]",
		f"command = {json.dumps(command_path)}",
		'args = ["start-mcp-server", "--context=codex", "--project-from-cwd", "--transport", "stdio"]',
		"startup_timeout_sec = 30",
	]
	block = "\n".join(block_lines)
	if rendered:
		rendered = rendered.rstrip() + "\n\n" + block + "\n"
	else:
		rendered = block + "\n"
elif rendered:
	rendered = rendered.rstrip() + "\n"

if tomllib is not None and rendered.strip():
	tomllib.loads(rendered)

if rendered:
	config_path.parent.mkdir(parents=True, exist_ok=True)
	config_path.write_text(rendered, encoding="utf-8")
elif config_path.exists():
	config_path.unlink()
PY
}

write_serena_codex_config()
{
	local serena_bin="${1:?write_serena_codex_config: serena binary path required}"

	if [ -z "${HOME:-}" ]; then
		log 'HOME is unavailable; cannot write ~/.codex/config.toml.'
		return 1
	fi

	SERENA_CONFIG_PATH="${HOME}/.codex/config.toml" \
	SERENA_BLOCK_COMMAND="${serena_bin}" \
	PYTHONDONTWRITEBYTECODE=1 \
	"${SERENA_UV_PYTHON_BIN}" - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

try:
	import tomllib
except ModuleNotFoundError:  # pragma: no cover
	tomllib = None  # type: ignore[assignment]


def is_serena_header(line: str) -> bool:
	stripped = line.strip()
	return stripped == "[mcp_servers.serena]" or stripped.startswith("[mcp_servers.serena.")


def is_table_header(line: str) -> bool:
	stripped = line.strip()
	return stripped.startswith("[") and stripped.endswith("]")


def strip_serena_block(text: str) -> str:
	lines = text.splitlines(keepends=True)
	out: list[str] = []
	i = 0
	while i < len(lines):
		line = lines[i]
		if is_serena_header(line):
			i += 1
			while i < len(lines):
				if is_table_header(lines[i]) and not is_serena_header(lines[i]):
					break
				i += 1
			continue
		out.append(line)
		i += 1
	return "".join(out).rstrip("\n")


config_path = Path(os.environ["SERENA_CONFIG_PATH"])
command_path = os.environ["SERENA_BLOCK_COMMAND"]
existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
rendered = strip_serena_block(existing)
block_lines = [
	"[mcp_servers.serena]",
	f"command = {json.dumps(command_path)}",
	'args = ["start-mcp-server", "--context=codex", "--project-from-cwd", "--transport", "stdio"]',
	"startup_timeout_sec = 30",
]
block = "\n".join(block_lines)

if rendered:
	rendered = rendered.rstrip() + "\n\n" + block + "\n"
else:
	rendered = block + "\n"

if tomllib is not None and rendered.strip():
	tomllib.loads(rendered)

config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(rendered, encoding="utf-8")
PY
}

render_serena_project()
{
	if [ ! -f "${SERENA_TEMPLATE_PATH}" ]; then
		log "template missing: ${SERENA_TEMPLATE_PATH}"
		return 1
	fi
	mkdir -p "$(dirname -- "${SERENA_PROJECT_PATH}")"
	cat "${SERENA_TEMPLATE_PATH}" > "${SERENA_PROJECT_PATH}"
}

attempt_uv_install()
{
	local install_log=""
	local uv_bin=""

	if ! command -v uv >/dev/null 2>&1; then
		log "uv is unavailable; cannot install ${SERENA_SPEC}."
		return 1
	fi

	uv_bin="$(uv_tool_bin_dir || true)"
	append_github_path "${uv_bin}"

	install_log="$(mktemp 2>/dev/null || true)"
	if [ -z "${install_log}" ]; then
		log "mktemp unavailable; skipping install attempt for ${SERENA_SPEC}."
		return 1
	fi

	if uv tool install --quiet --force -p "${SERENA_UV_PYTHON_BIN}" "${SERENA_SPEC}" >"${install_log}" 2>&1; then
		rm -f "${install_log}"
		return 0
	fi

	if uv tool install --quiet --force "${SERENA_SPEC}" >"${install_log}" 2>&1; then
		rm -f "${install_log}"
		return 0
	fi

	log "uv tool install failed for ${SERENA_SPEC}: $(tail -n 1 "${install_log}" 2>/dev/null || printf 'unknown-error')"
	rm -f "${install_log}"
	return 1
}

probe_mcp_handshake()
{
	local serena_bin="${1:?probe_mcp_handshake: serena binary path required}"

	PYTHONDONTWRITEBYTECODE=1 \
	"${SERENA_UV_PYTHON_BIN}" "${SCRIPT_DIR}/mcp_handshake_probe.py" \
		--name "serena" \
		-- "${serena_bin}" start-mcp-server --context=codex --project-from-cwd --transport stdio
}

main()
{
	local uv_bin=""
	local existing_version=""
	local serena_bin=""

	uv_bin="$(uv_tool_bin_dir || true)"
	append_github_path "${uv_bin}"

	if ! env_is_truthy "${SERENA_ENABLED:-false}"; then
		log 'SERENA_ENABLED is not true; skipping Serena bootstrap.'
		clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration; continuing.'
		export_serena_available "false"
		return 0
	fi

	if binary_matches_pin; then
		serena_bin="$(command -v "${SERENA_BIN_NAME}" 2>/dev/null || true)"
	else
		existing_version="$(current_serena_version || true)"
		if [ -n "${existing_version}" ]; then
			log "found non-pinned Serena (${existing_version%%$'\n'*}); attempting install of ${SERENA_SPEC}."
		else
			log "Serena not found; attempting install of ${SERENA_SPEC}."
		fi
		if ! attempt_uv_install; then
			clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration after install failure; continuing.'
			export_serena_available "false"
			return 0
		fi
		uv_bin="$(uv_tool_bin_dir || true)"
		append_github_path "${uv_bin}"
		if ! binary_matches_pin; then
			log "Serena install completed but the pinned ${SERENA_BIN_NAME} binary is still unavailable."
			clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration after install validation failure; continuing.'
			export_serena_available "false"
			return 0
		fi
		serena_bin="$(command -v "${SERENA_BIN_NAME}" 2>/dev/null || true)"
	fi

	if [ -z "${serena_bin}" ]; then
		log "${SERENA_BIN_NAME} is unavailable after install checks."
		clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration after binary lookup failure; continuing.'
		export_serena_available "false"
		return 0
	fi

	if ! render_serena_project; then
		log "unable to render ${SERENA_PROJECT_PATH}; leaving Serena unavailable."
		clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration after project render failure; continuing.'
		export_serena_available "false"
		return 0
	fi

	if ! probe_mcp_handshake "${serena_bin}"; then
		log 'Serena handshake probe failed; omitting MCP registration.'
		clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration after probe failure; continuing.'
		export_serena_available "false"
		return 0
	fi

	if ! write_serena_codex_config "${serena_bin}"; then
		log 'unable to update ~/.codex/config.toml for Serena; leaving Serena unavailable.'
		clear_serena_codex_config || log 'unable to clear stale Serena MCP configuration after config write failure; continuing.'
		export_serena_available "false"
		return 0
	fi

	export_serena_available "true"
	log "Serena ${SERENA_VERSION} is available."
	return 0
}

main "$@"
