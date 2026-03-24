#!/usr/bin/env bash
# setup_serena.sh — Install and configure Serena MCP server for CI workflows.
#
# Usage:
#   bash scripts/setup_serena.sh [--mode MODE] [--context CONTEXT]
#
# Options:
#   --mode MODE       Serena mode: "editing" (default) or "planning" (read-only)
#   --context CONTEXT Serena context: "codex" (default), "agent", "claude-code"
#
# Environment variables (optional overrides):
#   SERENA_VERSION         Git ref to install (default: "main")
#   SERENA_LANGUAGES       Space-separated list of languages to force (e.g. "typescript python")
#   SERENA_DISABLED        Set to "true" to skip Serena setup entirely
#
# This script:
#   1. Installs uv if not present
#   2. Warms the Serena uvx cache
#   3. Auto-detects project languages and installs language servers
#   4. Creates .serena/project.yml if missing
#   5. Creates ~/.serena/serena_config.yml (global config)
#   6. Appends Serena MCP server config to ~/.codex/config.toml
#   7. Runs a health check
#
# On any failure, the script emits a warning and exits 0 so that the
# calling workflow can fall back to normal file-based editing.

set -euo pipefail

# ── Parse arguments ──────────────────────────────────────────────────────────

SERENA_MODE="editing"
SERENA_CONTEXT="codex"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--mode)
			SERENA_MODE="$2"
			shift 2
			;;
		--context)
			SERENA_CONTEXT="$2"
			shift 2
			;;
		*)
			echo "Unknown argument: $1"
			exit 1
			;;
	esac
done

# ── Guard: skip if disabled ──────────────────────────────────────────────────

if [ "${SERENA_DISABLED:-false}" = "true" ]; then
	echo "Serena setup skipped (SERENA_DISABLED=true)."
	exit 0
fi

SERENA_VERSION="${SERENA_VERSION:-main}"
SERENA_DEBUG_LOG="${TMPDIR:-/tmp}/serena_debug.log"
echo "Setting up Serena MCP server (version=${SERENA_VERSION}, mode=${SERENA_MODE}, context=${SERENA_CONTEXT})"

# ── Helper: warn and exit cleanly ────────────────────────────────────────────

warn_and_exit() {
	echo "::warning::Serena setup failed: $1 — workflow will fall back to file-based editing."
	# Dump debug log if available for CI troubleshooting
	if [ -f "${SERENA_DEBUG_LOG}" ] && [ -s "${SERENA_DEBUG_LOG}" ]; then
		echo "--- Serena debug log (last 50 lines) ---"
		tail -50 "${SERENA_DEBUG_LOG}" >&2 || true
		echo "--- End Serena debug log ---"
	fi
	# Remove the Serena MCP server block from Codex config so that
	# required=true doesn't prevent Codex from starting at all.
	CODEX_CFG="${HOME}/.codex/config.toml"
	if [ -f "${CODEX_CFG}" ] && grep -q '\[mcp_servers\.serena\]' "${CODEX_CFG}"; then
		# Delete from [mcp_servers.serena] to the next section header or EOF
		sed -i '/^\[mcp_servers\.serena\]/,/^\[/{/^\[mcp_servers\.serena\]/d;/^\[/!d;}' "${CODEX_CFG}"
		echo "Removed Serena MCP server from ${CODEX_CFG} to allow Codex to start without it."
	fi
	rm -f "${SERENA_DEBUG_LOG}"
	exit 0
}

if printf '%s' "${SERENA_VERSION}" | grep -q '"'; then
	warn_and_exit "SERENA_VERSION contains unsupported quote character"
fi

# ── 1. Install uv if not present ─────────────────────────────────────────────

if ! command -v uv >/dev/null 2>&1; then
	if ! command -v uvx >/dev/null 2>&1; then
		echo "Installing uv..."
		curl -LsSf https://astral.sh/uv/install.sh | sh || warn_and_exit "uv installation failed"
		export PATH="${HOME}/.local/bin:${PATH}"
		# Persist PATH for subsequent workflow steps
		if [ -n "${GITHUB_ENV:-}" ]; then
			echo "PATH=${HOME}/.local/bin:${PATH}" >> "${GITHUB_ENV}"
		fi
	fi
fi

# ── 2. Warm Serena cache (with retry) ────────────────────────────────────────

SERENA_CACHE_MAX_RETRIES=3
for _cache_attempt in $(seq 1 "${SERENA_CACHE_MAX_RETRIES}"); do
	echo "Warming Serena uvx cache (attempt ${_cache_attempt}/${SERENA_CACHE_MAX_RETRIES})..."
	if uvx --from "git+https://github.com/oraios/serena@${SERENA_VERSION}" serena --help >"${SERENA_DEBUG_LOG}" 2>&1; then
		echo "Serena cache warm succeeded."
		break
	fi
	echo "::warning::Serena cache warm failed on attempt ${_cache_attempt}."
	if [ "${_cache_attempt}" -lt "${SERENA_CACHE_MAX_RETRIES}" ]; then
		_sleep_secs=$(( 5 * _cache_attempt + RANDOM % 5 ))
		echo "Retrying in ${_sleep_secs}s..."
		sleep "${_sleep_secs}"
	else
		warn_and_exit "Serena cache warm failed after ${SERENA_CACHE_MAX_RETRIES} attempts"
	fi
done

# ── 3. Auto-detect languages and install language servers ─────────────────────

# Single find pass for all language extensions (avoids 5 separate traversals)
DETECTED_LANGS=""
_found_exts="$(find . -maxdepth 3 \( -name '*.ts' -o -name '*.tsx' -o -name '*.py' -o -name '*.go' -o -name '*.rs' -o -name '*.java' \) -printf '%f\n' 2>/dev/null || true)"
echo "${_found_exts}" | grep -qE '\.(ts|tsx)$' && DETECTED_LANGS="${DETECTED_LANGS} typescript"
echo "${_found_exts}" | grep -qE '\.py$' && DETECTED_LANGS="${DETECTED_LANGS} python"
echo "${_found_exts}" | grep -qE '\.go$' && DETECTED_LANGS="${DETECTED_LANGS} go"
echo "${_found_exts}" | grep -qE '\.rs$' && DETECTED_LANGS="${DETECTED_LANGS} rust"
echo "${_found_exts}" | grep -qE '\.java$' && DETECTED_LANGS="${DETECTED_LANGS} java"

# Merge with explicit override
ALL_LANGS="${SERENA_LANGUAGES:-} ${DETECTED_LANGS}"
ALL_LANGS="$(echo "${ALL_LANGS}" | tr ' ' '\n' | sort -u | tr '\n' ' ' | xargs)"

echo "Languages detected/configured: ${ALL_LANGS:-none}"

for lang in ${ALL_LANGS}; do
	case "${lang}" in
		typescript)
			echo "Installing TypeScript language server..."
			npm install -g typescript-language-server typescript --no-audit --no-fund 2>/dev/null || echo "::warning::TypeScript LSP install failed"
			;;
		python)
			echo "Installing Python language server..."
			# Use uv tool install to avoid PEP 668 "externally managed" errors
			# on Ubuntu 24.04+ system Python. This installs pylsp into an
			# isolated venv while making the binary available on PATH.
			if command -v uv >/dev/null 2>&1; then
				uv tool install python-lsp-server 2>/dev/null || echo "::warning::Python LSP install via uv failed"
			else
				pip install --break-system-packages python-lsp-server 2>/dev/null || echo "::warning::Python LSP install failed"
			fi
			;;
		go)
			echo "Installing Go language server..."
			go install golang.org/x/tools/gopls@latest 2>/dev/null || echo "::warning::Go LSP install failed"
			;;
		rust)
			echo "Installing Rust analyzer..."
			rustup component add rust-analyzer 2>/dev/null || echo "::warning::rust-analyzer install failed"
			;;
		java)
			echo "::warning::Java LSP requires manual setup — skipping"
			;;
	esac
done

# ── 4. Create .serena/project.yml if missing ─────────────────────────────────

# Per-repo overrides via repository variables
SERENA_IGNORED_DIRS="${SERENA_IGNORED_DIRS:-}"

if [ ! -f .serena/project.yml ]; then
	echo "Creating .serena/project.yml..."
	mkdir -p .serena

	# Build languages list for YAML
	LANG_YAML=""
	for lang in ${ALL_LANGS}; do
		LANG_YAML="${LANG_YAML}  - ${lang}\n"
	done
	if [ -z "${LANG_YAML}" ]; then
		LANG_YAML="  - typescript\n  - python\n"
	fi

	READ_ONLY="false"
	if [ "${SERENA_MODE}" = "planning" ]; then
		READ_ONLY="true"
	fi

	# Build ignored paths list (defaults + custom overrides)
	IGNORED_YAML="  - node_modules\n  - dist\n  - build\n  - __pycache__\n  - .next\n  - vendor\n  - .git\n"
	if [ -n "${SERENA_IGNORED_DIRS}" ]; then
		# Keep space-delimited semantics but prevent pathname expansion.
		had_noglob=0
		if [ -o noglob ]; then
			had_noglob=1
		else
			set -f
		fi
		for dir in ${SERENA_IGNORED_DIRS}; do
			# SERENA_IGNORED_DIRS is a space-delimited list by design.
			[ -n "${dir}" ] || continue
			safe_dir="$(printf '%s' "${dir}" | tr -d '[:cntrl:]')"
			safe_dir="${safe_dir//\\/\\\\}"
			safe_dir="${safe_dir//\'/\'\'}"
			IGNORED_YAML="${IGNORED_YAML}  - '${safe_dir}'\n"
		done
		if [ "${had_noglob}" -eq 0 ]; then
			set +f
		fi
	fi

	cat > .serena/project.yml <<SERENA_PROJECT_EOF
project_name: auto
read_only: ${READ_ONLY}

languages:
$(printf '%b' "${LANG_YAML}")

encoding: utf-8
ignore_all_files_in_gitignore: true
ignored_paths:
$(printf '%b' "${IGNORED_YAML}")

excluded_tools: []
included_optional_tools: []

base_modes: []
default_modes:
  - ${SERENA_MODE}

symbol_info_budget: 10
initial_prompt: "Skip onboarding. Do not run serena.onboarding(). Proceed directly to the task."
SERENA_PROJECT_EOF
else
	echo ".serena/project.yml already exists — using existing config."
fi

# ── 5. Create global Serena config ───────────────────────────────────────────

mkdir -p ~/.serena
PROJECT_ROOT="$(pwd)"
cat > ~/.serena/serena_config.yml <<SERENA_GLOBAL_EOF
language_backend: LSP
gui_log_window: false
web_dashboard: false
web_dashboard_open_on_launch: false
log_level: 20
tool_timeout: 240
default_modes:
  - editing
projects:
  - "${PROJECT_ROOT}"
SERENA_GLOBAL_EOF

# ── 6. Append Serena MCP server to Codex config.toml ─────────────────────────
#
# Codex CLI reads MCP servers from [mcp_servers.<name>] tables in config.toml,
# NOT from a separate mcp.json file. Append to the existing config.toml that
# the workflow creates earlier.
#
# IMPORTANT: Codex's process spawning does NOT work reliably with `uvx` as the
# MCP server command. The `uvx` launcher adds a process indirection layer that
# causes Codex MCP handshake timeouts. Instead, we resolve the actual `serena`
# binary from the uvx cache (which was warmed in step 2) and use it directly.

CODEX_CONFIG="${HOME}/.codex/config.toml"
mkdir -p ~/.codex

if [ ! -f "${CODEX_CONFIG}" ]; then
	touch "${CODEX_CONFIG}"
fi

# Resolve the actual serena binary from the uvx cache.
# The cache was warmed in step 2, so the binary should be available.
# Using the direct binary avoids the uvx process indirection that causes
# Codex MCP handshake timeouts ("mcp startup: no servers" / timeout).
SERENA_BIN="$(uvx --from "git+https://github.com/oraios/serena@${SERENA_VERSION}" which serena 2>/dev/null || true)"
if [ -z "${SERENA_BIN}" ] || [ ! -x "${SERENA_BIN}" ]; then
	# Fallback: try to find the binary in the uvx cache
	SERENA_BIN="$(find "${HOME}/.cache/uv" -name "serena" -type f -executable 2>/dev/null | head -1 || true)"
fi
if [ -z "${SERENA_BIN}" ] || [ ! -x "${SERENA_BIN}" ]; then
	warn_and_exit "Could not resolve serena binary path from uvx cache"
fi
echo "Resolved serena binary: ${SERENA_BIN}"

# Build the [mcp_servers.serena.env] sub-table with explicit key=value pairs.
# This is the canonical format that `codex mcp add --env` uses.
# Forward critical environment variables so the Codex sandbox subprocess
# can locate dependencies and caches.
ENV_BLOCK='[mcp_servers.serena.env]'
ENV_BLOCK="${ENV_BLOCK}\nHOME = \"${HOME}\""
ENV_BLOCK="${ENV_BLOCK}\nPATH = \"${PATH}\""
if [ -n "${TMPDIR:-}" ]; then
	ENV_BLOCK="${ENV_BLOCK}\nTMPDIR = \"${TMPDIR}\""
fi
if [ -n "${UV_CACHE_DIR:-}" ]; then
	ENV_BLOCK="${ENV_BLOCK}\nUV_CACHE_DIR = \"${UV_CACHE_DIR}\""
fi
if [ -n "${PYTHONDONTWRITEBYTECODE:-}" ]; then
	ENV_BLOCK="${ENV_BLOCK}\nPYTHONDONTWRITEBYTECODE = \"${PYTHONDONTWRITEBYTECODE}\""
fi

cat >> "${CODEX_CONFIG}" <<MCP_EOF

[mcp_servers.serena]
command = "${SERENA_BIN}"
args = ["start-mcp-server", "--context", "${SERENA_CONTEXT}", "--mode", "one-shot", "--mode", "${SERENA_MODE}", "--project-from-cwd", "--enable-web-dashboard", "false", "--open-web-dashboard", "false"]
startup_timeout_sec = 30
tool_timeout_sec = 240
required = true

$(printf '%b' "${ENV_BLOCK}")
MCP_EOF

echo "Serena MCP server appended to ${CODEX_CONFIG}"

# Verify the MCP config was actually written correctly
if grep -q '\[mcp_servers\.serena\]' "${CODEX_CONFIG}"; then
	echo "MCP server config verified in ${CODEX_CONFIG}"
else
	echo "::warning::MCP server config NOT found in ${CODEX_CONFIG} after write — Serena tools will be unavailable."
fi

# Print the final config for CI debugging
echo "--- Final config.toml MCP section ---"
sed -n '/\[mcp_servers/,$ p' "${CODEX_CONFIG}"
echo "--- End MCP section ---"

# ── 7. Verify Codex supports MCP ────────────────────────────────────────────

echo "Checking Codex MCP support..."
CODEX_MCP_SUPPORTED="false"
if command -v codex >/dev/null 2>&1; then
	CODEX_HELP="$(codex --help 2>&1 || true)"
	if echo "${CODEX_HELP}" | grep -qi "mcp"; then
		CODEX_MCP_SUPPORTED="true"
		echo "Codex MCP support confirmed."
	else
		CODEX_VER="$(codex --version 2>&1 || echo "unknown")"
		echo "::warning::Codex ${CODEX_VER} may not support MCP — Serena tools may be silently unavailable."
	fi
else
	echo "::warning::Codex CLI not found yet (may be installed later in workflow). MCP support cannot be verified at setup time."
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
	echo "serena_mcp_supported=${CODEX_MCP_SUPPORTED}" >> "${GITHUB_OUTPUT}"
fi

# ── 8. Health check (with retry) ─────────────────────────────────────────────
# Validate the actual MCP server startup, not just --help. This catches mode
# combination errors, LSP failures, and config issues that only surface when
# serena tries to initialize as an MCP server.

HEALTH_LOG="${TMPDIR:-/tmp}/serena_health_check.log"
SERENA_HEALTH_MAX_RETRIES=2
SERENA_HEALTH_OK="false"
for _health_attempt in $(seq 1 "${SERENA_HEALTH_MAX_RETRIES}"); do
	echo "Validating Serena MCP server startup (attempt ${_health_attempt}/${SERENA_HEALTH_MAX_RETRIES})..."
	HEALTH_EXIT=0
	timeout 30s "${SERENA_BIN}" \
		start-mcp-server --context "${SERENA_CONTEXT}" --mode one-shot --mode "${SERENA_MODE}" \
		--project-from-cwd --enable-web-dashboard false --open-web-dashboard false </dev/null >"${HEALTH_LOG}" 2>&1 || HEALTH_EXIT=$?
	if [ "${HEALTH_EXIT}" -eq 0 ] || [ "${HEALTH_EXIT}" -eq 124 ]; then
		# Exit 0 = clean exit; Exit 124 = timeout killed a still-running server (expected).
		echo "Serena MCP server validated successfully."
		SERENA_HEALTH_OK="true"
		break
	fi
	echo "::warning::Serena health check failed on attempt ${_health_attempt} (exit=${HEALTH_EXIT})."
	echo "--- Serena health check output (attempt ${_health_attempt}) ---"
	cat "${HEALTH_LOG}" >&2 || true
	echo "--- End health check output ---"
	if [ "${_health_attempt}" -lt "${SERENA_HEALTH_MAX_RETRIES}" ]; then
		_sleep_secs=$(( 5 * _health_attempt + RANDOM % 5 ))
		echo "Retrying health check in ${_sleep_secs}s..."
		sleep "${_sleep_secs}"
	fi
done
rm -f "${HEALTH_LOG}"

if [ "${SERENA_HEALTH_OK}" != "true" ]; then
	warn_and_exit "Serena MCP server failed to start after ${SERENA_HEALTH_MAX_RETRIES} attempts — check output above for details"
fi

# ── 9. Debug diagnostics ────────────────────────────────────────────────────
# Print key diagnostic info to CI logs for troubleshooting "no servers" issues.
echo "--- Serena setup diagnostics ---"
echo "uvx path: $(command -v uvx 2>/dev/null || echo 'NOT FOUND')"
echo "uv path: $(command -v uv 2>/dev/null || echo 'NOT FOUND')"
echo "UV_CACHE_DIR: ${UV_CACHE_DIR:-'(not set)'}"
echo "Codex config location: ${CODEX_CONFIG}"
echo "MCP section present: $(grep -c '\[mcp_servers\.serena\]' "${CODEX_CONFIG}" 2>/dev/null || echo '0')"
echo "required=true set: $(grep -c 'required = true' "${CODEX_CONFIG}" 2>/dev/null || echo '0')"
echo "Project root: ${PROJECT_ROOT}"
echo ".serena/project.yml exists: $([ -f .serena/project.yml ] && echo 'yes' || echo 'no')"
echo "Languages configured: ${ALL_LANGS:-none}"
echo "--- End diagnostics ---"
rm -f "${SERENA_DEBUG_LOG}"

echo "Serena setup complete."
