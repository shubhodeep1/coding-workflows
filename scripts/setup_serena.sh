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
#   CONTEXT7_DISABLED      Set to "true" to skip Context7 MCP setup (default: "false")
#
# This script:
#   1. Installs uv if not present
#   2. Warms the Serena uvx cache
#   3. Auto-detects project languages and installs language servers
#   4. Creates .serena/project.yml if missing
#   5. Creates ~/.serena/serena_config.yml (global config)
#   6. Writes Serena and optional Context7 MCP config to ~/.codex/config.toml
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
CONTEXT7_CONFIG_TOUCHED="false"
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
	# Remove Serena MCP config so Codex can start without Serena when setup fails.
	CODEX_CFG="${HOME}/.codex/config.toml"
	if [ -f "${CODEX_CFG}" ] && grep -Eq '^\[mcp_servers\.serena(\]|[.])' "${CODEX_CFG}"; then
		remove_mcp_server_blocks "serena" "${CODEX_CFG}" || true
		echo "Removed Serena MCP server from ${CODEX_CFG} to allow Codex to start without it."
	fi
	if [ "${CONTEXT7_CONFIG_TOUCHED:-false}" = "true" ] && [ -f "${CODEX_CFG}" ] && grep -Eq '^\[mcp_servers\.context7(\]|[.])' "${CODEX_CFG}"; then
		remove_mcp_server_blocks "context7" "${CODEX_CFG}" || true
		echo "Removed Context7 MCP server from ${CODEX_CFG}."
	fi
	rm -f "${SERENA_DEBUG_LOG}"
	exit 0
}

# Remove one MCP server table and all its sub-tables (e.g. [mcp_servers.<name>.env]).
remove_mcp_server_blocks() {
	local server_name="$1"
	local codex_cfg="$2"

	if [ ! -f "${codex_cfg}" ]; then
		return 0
	fi

	awk -v server_name="${server_name}" '
		$0 ~ "^\\[mcp_servers\\." server_name "\\]$" { skip=1; next }
		$0 ~ "^\\[mcp_servers\\." server_name "\\." { skip=1; next }
		$0 ~ "^\\[" && $0 !~ "^\\[mcp_servers\\." server_name "(\\]|\\.)" { skip=0 }
		!skip { print }
	' "${codex_cfg}" > "${codex_cfg}.tmp" && mv "${codex_cfg}.tmp" "${codex_cfg}" || { rm -f "${codex_cfg}.tmp"; return 1; }
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

# Single find pass for all language extensions (avoids separate traversals).
# Maps file extensions to Serena language identifiers. Covers every language
# Serena supports (see https://oraios.github.io/serena/01-about/020_programming-languages.html).
# The TypeScript language server handles both TS and JS files; .vue maps to
# the dedicated vue LSP. HTML/CSS have no standalone Serena language — they
# are partially covered by the vue and typescript LSPs when present.
DETECTED_LANGS=""
_found_exts="$(find . -maxdepth 3 \( \
	-name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.jsx' \
	-o -name '*.vue' \
	-o -name '*.py' \
	-o -name '*.go' \
	-o -name '*.rs' \
	-o -name '*.java' -o -name '*.kt' \
	-o -name '*.rb' \
	-o -name '*.php' \
	-o -name '*.cs' \
	-o -name '*.cpp' -o -name '*.c' -o -name '*.h' -o -name '*.hpp' \
	-o -name '*.sh' -o -name '*.bash' \
	-o -name '*.lua' -o -name '*.luau' \
	-o -name '*.ex' -o -name '*.exs' \
	-o -name '*.dart' \
	-o -name '*.swift' \
	-o -name '*.scala' \
	-o -name '*.pl' -o -name '*.pm' \
	-o -name '*.zig' \
	-o -name '*.fs' -o -name '*.fsx' \
	-o -name '*.hs' -o -name '*.lhs' \
	-o -name '*.clj' -o -name '*.cljs' -o -name '*.cljc' \
	-o -name '*.elm' \
	-o -name '*.erl' -o -name '*.hrl' \
	-o -name '*.jl' \
	-o -name '*.r' -o -name '*.R' \
	-o -name '*.ml' -o -name '*.mli' \
	-o -name '*.pas' -o -name '*.pp' \
	-o -name '*.f90' -o -name '*.f95' -o -name '*.f03' \
	-o -name '*.groovy' -o -name '*.gvy' \
	-o -name '*.sol' \
	-o -name '*.nix' \
	-o -name '*.lean' \
	-o -name '*.al' \
	-o -name '*.hlsl' -o -name '*.glsl' -o -name '*.wgsl' \
	-o -name '*.yml' -o -name '*.yaml' \
	\) -printf '%f\n' 2>/dev/null | head -100 || true)"
# Map detected extensions to Serena language identifiers
echo "${_found_exts}" | grep -qE '\.(ts|tsx|js|jsx)$'       && DETECTED_LANGS="${DETECTED_LANGS} typescript"
echo "${_found_exts}" | grep -qE '\.vue$'                   && DETECTED_LANGS="${DETECTED_LANGS} vue"
echo "${_found_exts}" | grep -qE '\.py$'                    && DETECTED_LANGS="${DETECTED_LANGS} python"
echo "${_found_exts}" | grep -qE '\.go$'                    && DETECTED_LANGS="${DETECTED_LANGS} go"
echo "${_found_exts}" | grep -qE '\.rs$'                    && DETECTED_LANGS="${DETECTED_LANGS} rust"
echo "${_found_exts}" | grep -qE '\.java$'                  && DETECTED_LANGS="${DETECTED_LANGS} java"
echo "${_found_exts}" | grep -qE '\.kt$'                    && DETECTED_LANGS="${DETECTED_LANGS} kotlin"
echo "${_found_exts}" | grep -qE '\.rb$'                    && DETECTED_LANGS="${DETECTED_LANGS} ruby"
echo "${_found_exts}" | grep -qE '\.php$'                   && DETECTED_LANGS="${DETECTED_LANGS} php"
echo "${_found_exts}" | grep -qE '\.cs$'                    && DETECTED_LANGS="${DETECTED_LANGS} csharp"
echo "${_found_exts}" | grep -qE '\.(cpp|c|h|hpp)$'         && DETECTED_LANGS="${DETECTED_LANGS} cpp"
echo "${_found_exts}" | grep -qE '\.(sh|bash)$'             && DETECTED_LANGS="${DETECTED_LANGS} bash"
echo "${_found_exts}" | grep -qE '\.lua$'                   && DETECTED_LANGS="${DETECTED_LANGS} lua"
echo "${_found_exts}" | grep -qE '\.luau$'                  && DETECTED_LANGS="${DETECTED_LANGS} luau"
echo "${_found_exts}" | grep -qE '\.(ex|exs)$'              && DETECTED_LANGS="${DETECTED_LANGS} elixir"
echo "${_found_exts}" | grep -qE '\.dart$'                  && DETECTED_LANGS="${DETECTED_LANGS} dart"
echo "${_found_exts}" | grep -qE '\.swift$'                 && DETECTED_LANGS="${DETECTED_LANGS} swift"
echo "${_found_exts}" | grep -qE '\.scala$'                 && DETECTED_LANGS="${DETECTED_LANGS} scala"
echo "${_found_exts}" | grep -qE '\.(pl|pm)$'               && DETECTED_LANGS="${DETECTED_LANGS} perl"
echo "${_found_exts}" | grep -qE '\.zig$'                   && DETECTED_LANGS="${DETECTED_LANGS} zig"
echo "${_found_exts}" | grep -qE '\.(fs|fsx)$'              && DETECTED_LANGS="${DETECTED_LANGS} fsharp"
echo "${_found_exts}" | grep -qE '\.(hs|lhs)$'              && DETECTED_LANGS="${DETECTED_LANGS} haskell"
echo "${_found_exts}" | grep -qE '\.(clj|cljs|cljc)$'       && DETECTED_LANGS="${DETECTED_LANGS} clojure"
echo "${_found_exts}" | grep -qE '\.elm$'                   && DETECTED_LANGS="${DETECTED_LANGS} elm"
echo "${_found_exts}" | grep -qE '\.(erl|hrl)$'             && DETECTED_LANGS="${DETECTED_LANGS} erlang"
echo "${_found_exts}" | grep -qE '\.jl$'                    && DETECTED_LANGS="${DETECTED_LANGS} julia"
echo "${_found_exts}" | grep -qiE '\.[rR]$'                 && DETECTED_LANGS="${DETECTED_LANGS} r"
echo "${_found_exts}" | grep -qE '\.(ml|mli)$'              && DETECTED_LANGS="${DETECTED_LANGS} ocaml"
echo "${_found_exts}" | grep -qE '\.(pas|pp)$'              && DETECTED_LANGS="${DETECTED_LANGS} pascal"
echo "${_found_exts}" | grep -qE '\.(f90|f95|f03)$'         && DETECTED_LANGS="${DETECTED_LANGS} fortran"
echo "${_found_exts}" | grep -qE '\.(groovy|gvy)$'          && DETECTED_LANGS="${DETECTED_LANGS} groovy"
echo "${_found_exts}" | grep -qE '\.sol$'                   && DETECTED_LANGS="${DETECTED_LANGS} solidity"
echo "${_found_exts}" | grep -qE '\.nix$'                   && DETECTED_LANGS="${DETECTED_LANGS} nix"
echo "${_found_exts}" | grep -qE '\.lean$'                  && DETECTED_LANGS="${DETECTED_LANGS} lean4"
echo "${_found_exts}" | grep -qE '\.al$'                    && DETECTED_LANGS="${DETECTED_LANGS} al"
echo "${_found_exts}" | grep -qE '\.(hlsl|glsl)$'           && DETECTED_LANGS="${DETECTED_LANGS} hlsl"
echo "${_found_exts}" | grep -qE '\.wgsl$'                  && DETECTED_LANGS="${DETECTED_LANGS} wgsl"
echo "${_found_exts}" | grep -qE '\.(yml|yaml)$'            && DETECTED_LANGS="${DETECTED_LANGS} yaml"

# Merge with explicit override
ALL_LANGS="${SERENA_LANGUAGES:-} ${DETECTED_LANGS}"
ALL_LANGS="$(echo "${ALL_LANGS}" | tr ' ' '\n' | sort -u | tr '\n' ' ' | xargs)"

echo "Languages detected/configured: ${ALL_LANGS:-none}"

# Install language servers for detected languages.
# Only languages that need an explicit install step are listed here.
# Some languages (bash, perl, lua, etc.) have LSPs that Serena manages
# internally or that require manual setup — we register them in
# project.yml so Serena knows to index those files, but skip LSP install
# if the server is bundled with Serena or requires complex setup.
for lang in ${ALL_LANGS}; do
	case "${lang}" in
		typescript)
			echo "Installing TypeScript language server..."
			npm install -g typescript-language-server typescript --no-audit --no-fund 2>/dev/null || echo "::warning::TypeScript LSP install failed"
			;;
		vue)
			echo "Installing Vue language server (Volar)..."
			npm install -g @vue/language-server --no-audit --no-fund 2>/dev/null || echo "::warning::Vue LSP install failed"
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
		bash)
			echo "Installing Bash language server..."
			npm install -g bash-language-server --no-audit --no-fund 2>/dev/null || echo "::warning::Bash LSP install failed"
			;;
		php)
			echo "Installing PHP language server (Intelephense)..."
			npm install -g intelephense --no-audit --no-fund 2>/dev/null || echo "::warning::PHP LSP install failed"
			;;
		ruby)
			echo "Installing Ruby language server..."
			if command -v gem >/dev/null 2>&1; then
				gem install ruby-lsp 2>/dev/null || echo "::warning::Ruby LSP install failed"
			else
				echo "::warning::Ruby LSP skipped — gem not found"
			fi
			;;
		dart)
			echo "Installing Dart language server..."
			if command -v dart >/dev/null 2>&1; then
				echo "Dart SDK found — LSP is bundled."
			else
				echo "::warning::Dart LSP skipped — dart SDK not found"
			fi
			;;
		yaml)
			echo "Installing YAML language server..."
			npm install -g yaml-language-server --no-audit --no-fund 2>/dev/null || echo "::warning::YAML LSP install failed"
			;;
		elm)
			echo "Installing Elm language server..."
			npm install -g @elm-tooling/elm-language-server --no-audit --no-fund 2>/dev/null || echo "::warning::Elm LSP install failed"
			;;
		solidity)
			echo "Installing Solidity language server..."
			npm install -g @nomicfoundation/solidity-language-server --no-audit --no-fund 2>/dev/null || echo "::warning::Solidity LSP install failed"
			;;
		clojure)
			echo "Installing Clojure language server..."
			if command -v brew >/dev/null 2>&1; then
				brew install clojure-lsp/brew/clojure-lsp-native 2>/dev/null || echo "::warning::Clojure LSP install failed"
			else
				echo "Language 'clojure' registered for Serena indexing (LSP requires manual install)."
			fi
			;;
		haskell)
			echo "Installing Haskell language server..."
			if command -v ghcup >/dev/null 2>&1; then
				ghcup install hls 2>/dev/null || echo "::warning::Haskell LSP install failed"
			else
				echo "Language 'haskell' registered for Serena indexing (LSP requires ghcup)."
			fi
			;;
		ocaml)
			echo "Installing OCaml language server..."
			if command -v opam >/dev/null 2>&1; then
				opam install ocaml-lsp-server -y 2>/dev/null || echo "::warning::OCaml LSP install failed"
			else
				echo "Language 'ocaml' registered for Serena indexing (LSP requires opam)."
			fi
			;;
		r)
			echo "Installing R language server..."
			if command -v Rscript >/dev/null 2>&1; then
				Rscript -e 'install.packages("languageserver", repos="https://cloud.r-project.org")' 2>/dev/null || echo "::warning::R LSP install failed"
			else
				echo "Language 'r' registered for Serena indexing (LSP requires R)."
			fi
			;;
		nix)
			echo "Installing Nix language server..."
			if command -v nix >/dev/null 2>&1; then
				nix profile install nixpkgs#nixd 2>/dev/null || echo "::warning::Nix LSP install failed"
			else
				echo "Language 'nix' registered for Serena indexing (LSP requires nix)."
			fi
			;;
		kotlin)
			echo "Language 'kotlin' registered for Serena indexing (LSP requires manual setup)."
			;;
		csharp)
			echo "Language 'csharp' registered for Serena indexing (LSP requires manual setup)."
			;;
		java)
			echo "Language 'java' registered for Serena indexing (LSP requires manual setup)."
			;;
		scala)
			echo "Language 'scala' registered for Serena indexing (LSP requires Metals — manual setup)."
			;;
		swift)
			echo "Language 'swift' registered for Serena indexing (LSP requires Xcode/SourceKit)."
			;;
		*)
			# Catch-all for languages that are registered in project.yml so
			# Serena indexes their files. LSP install is either handled by
			# Serena internally, requires complex setup, or uses system tools.
			# Covers: cpp, lua, luau, elixir, perl, zig, fsharp, erlang,
			# julia, pascal, fortran, groovy, lean4, al, hlsl, wgsl
			echo "Language '${lang}' registered for Serena indexing (LSP may require manual setup)."
			;;
	esac
done

# ── 4. Create .serena/project.yml if missing ─────────────────────────────────

# Per-repo overrides via repository variables
SERENA_IGNORED_DIRS="${SERENA_IGNORED_DIRS:-}"

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

if [ ! -f .serena/project.yml ]; then
	echo "Creating .serena/project.yml..."
	mkdir -p .serena

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
	echo ".serena/project.yml already exists — syncing languages from auto-detection."
	# The caller repo may ship a project.yml with a stale or incomplete
	# languages list (e.g. only "python" when the repo also has .js/.ts/.yml
	# files). Replace the languages block with the freshly detected set so
	# Serena indexes all file types and provides accurate symbol resolution.
	#
	# The awk script replaces everything between `languages:` and the next
	# top-level key (non-indented, non-blank, non-comment line) with the
	# new language entries. It preserves the blank separator line between
	# sections, and skips all indented content (list items, comments) that
	# belonged to the old languages block.
	_NEW_LANGS="$(printf '%b' "${LANG_YAML}")"
	awk -v new_langs="${_NEW_LANGS}" '
		/^languages:/ { print; printf "%s\n", new_langs; skip=1; next }
		skip && /^[^ #]/ && !/^$/ { skip=0 }
		skip { next }
		!skip { print }
	' .serena/project.yml > .serena/project.yml.tmp && mv .serena/project.yml.tmp .serena/project.yml
	# Validate the result is parseable YAML; restore original on failure.
	if command -v python3 >/dev/null 2>&1; then
		if ! python3 -c "
import yaml, sys
try:
    yaml.safe_load(open('.serena/project.yml'))
except Exception as e:
    print(f'YAML validation failed: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; then
			echo "::warning::Language sync produced invalid YAML; restoring original .serena/project.yml."
			git checkout -- .serena/project.yml 2>/dev/null || true
		fi
	fi
	echo "Languages updated in .serena/project.yml: ${ALL_LANGS}"
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

# Keep config idempotent across re-runs and toggle changes.
remove_mcp_server_blocks "serena" "${CODEX_CONFIG}" || warn_and_exit "Failed to clean existing Serena MCP config"

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

CONTEXT7_CONFIG_TOUCHED="true"
remove_mcp_server_blocks "context7" "${CODEX_CONFIG}" || warn_and_exit "Failed to clean existing Context7 MCP config"

if [ "${CONTEXT7_DISABLED:-false}" != "true" ]; then
	cat >> "${CODEX_CONFIG}" <<CONTEXT7_MCP_EOF || warn_and_exit "Failed to append Context7 MCP config"

[mcp_servers.context7]
command = "npx"
args = ["-y", "@upstash/context7-mcp@latest"]
required = false
CONTEXT7_MCP_EOF

	echo "Context7 MCP server appended to ${CODEX_CONFIG}"
else
	echo "Context7 MCP setup skipped (CONTEXT7_DISABLED=true)."
fi

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

# ── 8b. Post-activation keepalive advisory ────────────────────────────────
# Serena MCP connections can silently drop after initial activation (observed
# as 0ms failures on list_dir/get_symbols_overview while activate_project
# succeeds). Increase the tool_timeout and add a longer startup_timeout to
# give the language servers time to fully index before the first real call.
# Also set required=false so that transient Serena failures cause graceful
# fallback to shell commands instead of hard Codex errors.
CODEX_CONFIG="${HOME}/.codex/config.toml"
if [ -f "${CODEX_CONFIG}" ] && grep -q '\[mcp_servers\.serena\]' "${CODEX_CONFIG}"; then
	# Bump startup_timeout to give LSPs time to index after activation
	# Scope both replacements to the [mcp_servers.serena] section to avoid
	# accidentally modifying settings for other MCP servers.
	sed -i '/^\[mcp_servers\.serena\]/,/^\[/ s/startup_timeout_sec = 30/startup_timeout_sec = 60/' "${CODEX_CONFIG}"
	# Switch to required=false so Serena failures degrade gracefully
	sed -i '/^\[mcp_servers\.serena\]/,/^\[/ s/required = true/required = false/' "${CODEX_CONFIG}"
	echo "Serena MCP config hardened: startup_timeout=60s, required=false (graceful fallback on failure)."
fi

# ── 9. Debug diagnostics ────────────────────────────────────────────────────
# Print key diagnostic info to CI logs for troubleshooting "no servers" issues.
echo "--- Serena setup diagnostics ---"
echo "uvx path: $(command -v uvx 2>/dev/null || echo 'NOT FOUND')"
echo "uv path: $(command -v uv 2>/dev/null || echo 'NOT FOUND')"
echo "UV_CACHE_DIR: ${UV_CACHE_DIR:-'(not set)'}"
echo "Codex config location: ${CODEX_CONFIG}"
echo "MCP section present: $(grep -c '\[mcp_servers\.serena\]' "${CODEX_CONFIG}" 2>/dev/null || echo '0')"
echo "Context7 MCP section present: $(grep -c '\[mcp_servers\.context7\]' "${CODEX_CONFIG}" 2>/dev/null || echo '0')"
echo "required=true set: $(grep -c 'required = true' "${CODEX_CONFIG}" 2>/dev/null || echo '0')"
echo "Project root: ${PROJECT_ROOT}"
echo ".serena/project.yml exists: $([ -f .serena/project.yml ] && echo 'yes' || echo 'no')"
echo "Languages configured: ${ALL_LANGS:-none}"
echo "--- End diagnostics ---"
rm -f "${SERENA_DEBUG_LOG}"

echo "Serena setup complete."
