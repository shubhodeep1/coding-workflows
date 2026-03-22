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
#   6. Creates ~/.codex/mcp.json with Serena MCP server
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
echo "Setting up Serena MCP server (version=${SERENA_VERSION}, mode=${SERENA_MODE}, context=${SERENA_CONTEXT})"

# ── Helper: warn and exit cleanly ────────────────────────────────────────────

warn_and_exit() {
	echo "::warning::Serena setup failed: $1 — workflow will fall back to file-based editing."
	exit 0
}

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

# ── 2. Warm Serena cache ─────────────────────────────────────────────────────

echo "Warming Serena uvx cache..."
if ! uvx --from "git+https://github.com/oraios/serena@${SERENA_VERSION}" serena --help >/dev/null 2>&1; then
	warn_and_exit "Serena cache warm failed"
fi

# ── 3. Auto-detect languages and install language servers ─────────────────────

DETECTED_LANGS=""
[ -n "$(find . -maxdepth 3 -name '*.ts' -o -name '*.tsx' | head -1 2>/dev/null)" ] && DETECTED_LANGS="${DETECTED_LANGS} typescript"
[ -n "$(find . -maxdepth 3 -name '*.py' | head -1 2>/dev/null)" ] && DETECTED_LANGS="${DETECTED_LANGS} python"
[ -n "$(find . -maxdepth 3 -name '*.go' | head -1 2>/dev/null)" ] && DETECTED_LANGS="${DETECTED_LANGS} go"
[ -n "$(find . -maxdepth 3 -name '*.rs' | head -1 2>/dev/null)" ] && DETECTED_LANGS="${DETECTED_LANGS} rust"
[ -n "$(find . -maxdepth 3 -name '*.java' | head -1 2>/dev/null)" ] && DETECTED_LANGS="${DETECTED_LANGS} java"

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
			pip install python-lsp-server 2>/dev/null || echo "::warning::Python LSP install failed"
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

	cat > .serena/project.yml <<SERENA_PROJECT_EOF
project_name: auto
read_only: ${READ_ONLY}

languages:
$(printf "${LANG_YAML}")

encoding: utf-8
ignore_all_files_in_gitignore: true
ignored_paths:
  - node_modules
  - dist
  - build
  - __pycache__
  - .next
  - vendor
  - .git

excluded_tools: []
included_optional_tools: []

base_modes: []
default_modes:
  - ${SERENA_MODE}

symbol_info_budget: null
initial_prompt: ""
SERENA_PROJECT_EOF
else
	echo ".serena/project.yml already exists — using existing config."
fi

# ── 5. Create global Serena config ───────────────────────────────────────────

mkdir -p ~/.serena
cat > ~/.serena/serena_config.yml <<'SERENA_GLOBAL_EOF'
language_backend: LSP
gui_log_window: false
web_dashboard: false
web_dashboard_open_on_launch: false
record_tool_usage_stats: true
log_level: 20
tool_timeout: 240
default_modes:
  - editing
SERENA_GLOBAL_EOF

# ── 6. Append Serena MCP server to Codex config.toml ─────────────────────────
#
# Codex CLI reads MCP servers from [mcp_servers.<name>] tables in config.toml,
# NOT from a separate mcp.json file. Append to the existing config.toml that
# the workflow creates earlier.

CODEX_CONFIG="${HOME}/.codex/config.toml"
mkdir -p ~/.codex

if [ ! -f "${CODEX_CONFIG}" ]; then
	touch "${CODEX_CONFIG}"
fi

cat >> "${CODEX_CONFIG}" <<MCP_EOF

[mcp_servers.serena]
command = "uvx"
args = ["--from", "git+https://github.com/oraios/serena@${SERENA_VERSION}", "serena", "start-mcp-server", "--context", "${SERENA_CONTEXT}", "--mode", "one-shot", "--mode", "${SERENA_MODE}", "--project-from-cwd", "--open-web-dashboard", "false"]
startup_timeout_sec = 30
tool_timeout_sec = 240
MCP_EOF

echo "Serena MCP server appended to ${CODEX_CONFIG}"

# ── 7. Health check ──────────────────────────────────────────────────────────

echo "Validating Serena MCP server..."
if timeout 30s uvx --from "git+https://github.com/oraios/serena@${SERENA_VERSION}" \
	serena --help >/dev/null 2>&1; then
	echo "Serena MCP server validated successfully."
else
	warn_and_exit "Serena health check timed out or failed"
fi

echo "Serena setup complete."
