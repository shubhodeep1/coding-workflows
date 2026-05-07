#!/usr/bin/env bash
# write_codex_config.sh — central writer for ~/.codex/config.toml.
#
# Every workflow / script in this repo that runs `codex exec` previously
# duplicated the same TOML-emit block (~25 lines × 9 sites). The block is
# load-bearing: it wires `model_catalog_json` so codex can resolve
# `apply_patch_tool_type = "freeform"` for our `openai/gpt-5.*` slugs
# (PR openai/codex#11238 removed the offline fallback) and pre-trusts the
# working directory to bypass the v0.113+ trust prompt
# (codex#14345 / #14547 / #14599). It also writes approval/sandbox to
# the repo-policy elevated defaults (codex#19020 / #16643 —
# danger-full-access is required to avoid workspace-write apply_patch
# hangs / sandbox-refresh failures, and is safe under the repo policy
# that codex only runs on ephemeral GH-hosted runners), but this is
# defence-in-depth only: every `codex exec` site in this repo now also
# passes `--ask-for-approval never --sandbox danger-full-access`
# explicitly, because CLI flags override config.toml (PR #2196 wrote the
# config but kept `--full-auto`, which silently re-forced
# sandbox=workspace-write at runtime — see run 25470900024 banner).
# Drift across the 9 inline copies is the kind of regression Copilot's
# review comment on PR #2196 warned about, so every caller now invokes
# this helper instead.
#
# Usage:
#   bash scripts/write_codex_config.sh \
#     --model openai/gpt-5.4 \
#     --reasoning medium \
#     [--web-search live|disabled] \
#     [--catalog-path /abs/path/to/codex_model_catalog.json] \
#     [--project-path /abs/path/to/workdir] \
#     [--config-path  /abs/path/to/config.toml] \
#     [--allow-elevation auto|force|forbid]
#
# Defaults:
#   --web-search        live
#   --catalog-path      $(pwd)/scripts/codex_model_catalog.json
#                       (skipped from the TOML if the file does not exist;
#                        a ::warning:: is emitted in that case)
#   --project-path      $(pwd)
#   --config-path       ${HOME}/.codex/config.toml
#   --allow-elevation   auto
#                       auto    → approval=never + sandbox=danger-full-access
#                                 iff GITHUB_ACTIONS=true OR
#                                 VALIDATE_FORCE_FULL_ACCESS=1; otherwise
#                                 approval=on-request + sandbox=workspace-write
#                       force   → always elevated regardless of GH context
#                       forbid  → always workspace-write/on-request
#
# Required:
#   --model      Model slug to write into `model = "..."`. No default (the
#                empty string is rejected).
#   --reasoning  Reasoning effort. Accepted values: xhigh|high|medium|low|none.
#                No default (matches the explicit choices each caller already
#                makes — silently defaulting would make a typo undebuggable).
#
# Behavior contract:
#   - The output file is overwritten atomically (write to temp + mv).
#   - Trailing settings ARE always present:
#       approval_policy = "<auto|forced>"
#       sandbox_mode    = "<auto|forced>"
#       model_provider  = "openrouter"
#       [model_providers.openrouter] block
#       [sandbox_workspace_write] block (consumed only when sandbox is
#         workspace-write, harmless otherwise)
#       [projects."<project-path>"] trust_level = "trusted"
#   - model_catalog_json is included only when the catalog file exists.
#   - All non-success paths exit non-zero with a ::error:: annotation so
#     a missing required arg surfaces in the GHA log.
#
# Stability contract for callers:
#   - The CLI flag set is the supported surface. Add new flags before
#     adding new behavior in callers; do not parse free-form positional
#     args.
#   - tests/test_write_codex_config.py asserts the output for several
#     argument combinations. Update that test alongside any TOML shape
#     change here so consumer-repo bootstrap stays predictable.

set -euo pipefail

usage() {
	cat >&2 <<'USAGE'
Usage: write_codex_config.sh --model <slug> --reasoning <effort>
                             [--web-search live|disabled]
                             [--catalog-path PATH]
                             [--project-path PATH]
                             [--config-path  PATH]
                             [--allow-elevation auto|force|forbid]
USAGE
}

_model=""
_reasoning=""
_web_search="live"
_catalog_path=""
_project_path=""
_config_path=""
_allow_elevation="auto"

while [ $# -gt 0 ]; do
	case "$1" in
		--model)            _model="${2:-}"; shift 2 ;;
		--reasoning)        _reasoning="${2:-}"; shift 2 ;;
		--web-search)       _web_search="${2:-}"; shift 2 ;;
		--catalog-path)     _catalog_path="${2:-}"; shift 2 ;;
		--project-path)     _project_path="${2:-}"; shift 2 ;;
		--config-path)      _config_path="${2:-}"; shift 2 ;;
		--allow-elevation)  _allow_elevation="${2:-}"; shift 2 ;;
		-h|--help)          usage; exit 0 ;;
		*)
			echo "::error::write_codex_config.sh: unknown argument: $1" >&2
			usage
			exit 2
			;;
	esac
done

if [ -z "${_model}" ]; then
	echo "::error::write_codex_config.sh: --model is required" >&2
	exit 2
fi
if [ -z "${_reasoning}" ]; then
	echo "::error::write_codex_config.sh: --reasoning is required" >&2
	exit 2
fi
case "${_reasoning}" in
	xhigh|high|medium|low|none) ;;
	*)
		echo "::error::write_codex_config.sh: invalid --reasoning '${_reasoning}' (expected xhigh|high|medium|low|none)" >&2
		exit 2
		;;
esac
case "${_web_search}" in
	live|disabled) ;;
	*)
		echo "::error::write_codex_config.sh: invalid --web-search '${_web_search}' (expected live|disabled)" >&2
		exit 2
		;;
esac
case "${_allow_elevation}" in
	auto|force|forbid) ;;
	*)
		echo "::error::write_codex_config.sh: invalid --allow-elevation '${_allow_elevation}' (expected auto|force|forbid)" >&2
		exit 2
		;;
esac

if [ -z "${_catalog_path}" ]; then
	_catalog_path="$(pwd)/scripts/codex_model_catalog.json"
fi
if [ -z "${_project_path}" ]; then
	_project_path="$(pwd)"
fi
if [ -z "${_config_path}" ]; then
	_config_path="${HOME:-/root}/.codex/config.toml"
fi

# Reject project paths containing characters that would need TOML
# basic-string escaping (double-quote, backslash, control bytes incl.
# newline/CR/tab/etc.). The emitted `[projects."<path>"]` header uses
# raw substitution — a stray `"` would close the key string early and
# corrupt the config; a newline would break the header onto two lines
# and codex would re-prompt for trust on the next run (defeating the
# whole purpose of the pre-seed). Codex matches the trust path strictly
# (no globs — see openai/codex#14599), so silently quoting/escaping
# would also break the bypass even if the TOML itself stayed valid.
# Fail loudly instead — every legitimate caller passes either `$(pwd)`
# from a hosted runner (no such characters) or a script-relative
# constant, so this guard only fires when something has already gone
# very wrong.
case "${_project_path}" in
	*$'\n'*|*$'\r'*|*$'\t'*|*\"*|*\\*)
		echo "::error::write_codex_config.sh: --project-path contains characters that would require TOML escaping (quote/backslash/newline/CR/tab); refusing to emit a malformed [projects.\"...\"] header. Path: ${_project_path}" >&2
		exit 2
		;;
esac
# Also reject any other ASCII control byte (0x00–0x1F minus the three
# above, plus 0x7F DEL). A bash `case` glob can't express ranges
# portably, so use a Python-free shell check via `printf | LC_ALL=C tr`.
if printf '%s' "${_project_path}" | LC_ALL=C tr -d '\000-\010\013\014\016-\037\177' \
	| { read -r _printable_only || true; [ "${_printable_only}" = "${_project_path}" ]; } \
	; then
	:
else
	echo "::error::write_codex_config.sh: --project-path contains ASCII control bytes; refusing to emit a malformed [projects.\"...\"] header. Path: ${_project_path}" >&2
	exit 2
fi

# Decide approval/sandbox based on elevation policy. The "auto" branch
# matches the contract documented above the script: GH-hosted runners
# (or an explicit force override) get the elevated combo; everything else
# falls back to codex's safer workspace-write defaults.
case "${_allow_elevation}" in
	force)
		_approval_policy="never"
		_sandbox_mode="danger-full-access"
		;;
	forbid)
		_approval_policy="on-request"
		_sandbox_mode="workspace-write"
		;;
	auto)
		if [ "${GITHUB_ACTIONS:-}" = "true" ] || [ "${VALIDATE_FORCE_FULL_ACCESS:-}" = "1" ]; then
			_approval_policy="never"
			_sandbox_mode="danger-full-access"
		else
			_approval_policy="on-request"
			_sandbox_mode="workspace-write"
		fi
		;;
esac

mkdir -p "$(dirname "${_config_path}")"

_tmp_path="$(mktemp "${_config_path}.XXXXXX")"
trap 'rm -f "${_tmp_path}"' EXIT

{
	printf 'web_search = "%s"\n' "${_web_search}"
	printf 'approval_policy = "%s"\n' "${_approval_policy}"
	printf 'sandbox_mode = "%s"\n' "${_sandbox_mode}"
	printf 'model_provider = "openrouter"\n'
	printf 'model = "%s"\n' "${_model}"
	printf 'model_reasoning_effort = "%s"\n' "${_reasoning}"
	if [ -f "${_catalog_path}" ]; then
		printf 'model_catalog_json = "%s"\n' "${_catalog_path}"
	else
		# Surface as a warning rather than failing — some callers
		# (e.g. integration tests) may run without the catalog and
		# accept the codex-shipped defaults.
		echo "::warning::write_codex_config.sh: catalog not found at ${_catalog_path}; omitting model_catalog_json" >&2
	fi
	printf '\n'
	printf '[model_providers.openrouter]\n'
	printf 'name = "OpenRouter"\n'
	printf 'base_url = "https://openrouter.ai/api/v1"\n'
	printf 'env_key = "OPENROUTER_API_KEY"\n'
	printf 'wire_api = "responses"\n'
	printf 'stream_idle_timeout_ms = 600000\n'
	printf 'stream_max_retries = 5\n'
	printf 'request_max_retries = 3\n'
	printf '\n'
	printf '[sandbox_workspace_write]\n'
	printf 'network_access = true\n'
	printf '\n'
	printf '[projects."%s"]\n' "${_project_path}"
	printf 'trust_level = "trusted"\n'
} > "${_tmp_path}"

mv "${_tmp_path}" "${_config_path}"
trap - EXIT
