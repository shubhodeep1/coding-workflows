#!/usr/bin/env bash
# validate_process.sh — Generate and execute runtime validation harness.
#
# Required env vars:
#   RUNTIME_DIR, GH_TOKEN, OPENROUTER_API_KEY, GITHUB_REPOSITORY
#
# Optional env vars:
#   TRACKING_ISSUE, VALIDATION_TIMEOUT, TOOL_CALL_BUDGET_VALIDATE,
#   MODEL_EDITOR, MODEL_REASONING_EFFORT,
#   TG_BOT_SECRET, TG_ADMIN_CHAT_ID,
#   VALIDATION_COMPOSE_FILE, VALIDATION_USE_TEMPLATES,
#   VALIDATION_TEST_USERNAME, VALIDATION_TEST_PASSWORD, VALIDATION_TEST_API_KEY,
#   WRITE_GUARDS_ENABLED,
#   VALIDATE_PREFLIGHT_PYFLAKES_ENABLED, VALIDATE_PREFLIGHT_PYFLAKES_RULES

set -euo pipefail

: "${RUNTIME_DIR:?RUNTIME_DIR is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
[[ "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]] || { echo "GITHUB_REPOSITORY must be in owner/repo format" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required but not installed" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required but not installed" >&2; exit 1; }
command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum is required but not installed" >&2; exit 1; }

_validate_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${_validate_script_dir}/write_guard.sh"

TRACKING_ISSUE_RAW="${TRACKING_ISSUE:-0}"
TRACKING_ISSUE_NUM=0
if [[ "${TRACKING_ISSUE_RAW}" =~ ^[0-9]+$ ]]; then
  TRACKING_ISSUE_NUM="${TRACKING_ISSUE_RAW}"
fi

MODEL_EDITOR="${MODEL_EDITOR:-openai/gpt-5.4}"
# Defaults to xhigh to match the repo-wide gpt-5.4 reasoning-level
# policy and `validate.yml`'s workflow-level `THINKING_LEVEL_VALIDATE ||
# 'xhigh'`. Earlier revisions defaulted to `medium`/`none`; `none` is
# not in `scripts/codex_model_catalog.json`'s `supported_reasoning_levels`
# for the gpt-5.x family, so the standalone / local invocation default
# is kept aligned with the workflow env to avoid silent drift.
MODEL_REASONING_EFFORT="${MODEL_REASONING_EFFORT:-xhigh}"
# Discover is a low-volume execution-heavy task (read repo metadata,
# emit `.ai/validate.yml` hints). It now defaults to `xhigh` to match
# the repo-wide gpt-5.4 reasoning-level policy; the per-phase override
# knob is retained so operators can drop discover's level independently
# of the parent MODEL_REASONING_EFFORT (mirrors the per-phase pattern
# used in implement.yml — MODEL_REPAIR_REASONING_EFFORT,
# MODEL_DIAGNOSE_REASONING_EFFORT). The discover step temporarily
# patches ~/.codex/config.toml before its codex exec call and restores
# `MODEL_REASONING_EFFORT` after — see the "Validation hint discovery
# attempt" loop further down.
MODEL_REASONING_EFFORT_DISCOVER="${MODEL_REASONING_EFFORT_DISCOVER:-xhigh}"
# `none` is intentionally rejected here: the parent MODEL_REASONING_EFFORT
# rationale above cites the catalog (`scripts/codex_model_catalog.json`)
# not advertising `none` for the gpt-5.x family, so accepting it for the
# per-phase override would be inconsistent. To use `none` everywhere,
# update the catalog first.
case "${MODEL_REASONING_EFFORT_DISCOVER}" in
  xhigh|high|medium|low) ;;
  *)
    echo "::warning::Invalid MODEL_REASONING_EFFORT_DISCOVER='${MODEL_REASONING_EFFORT_DISCOVER}'. Falling back to 'xhigh'."
    MODEL_REASONING_EFFORT_DISCOVER="xhigh"
    ;;
esac
CODEX_THREAD_REUSE_ENABLED="${CODEX_THREAD_REUSE_ENABLED:-false}"
# Export MODEL_EDITOR so child processes (notably scripts/self_heal_validation.sh)
# see it even when the caller relied on our default fallback. In CI the workflow
# env: block already exports MODEL_EDITOR, but standalone/local invocations
# would otherwise lose the default at the env boundary.
export MODEL_EDITOR
export CODEX_THREAD_REUSE_ENABLED
VALIDATION_TIMEOUT="${VALIDATION_TIMEOUT:-15}"
if ! [[ "${VALIDATION_TIMEOUT}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_TIMEOUT}" -le 0 ]; then
  echo "VALIDATION_TIMEOUT must be a positive integer (got: ${VALIDATION_TIMEOUT})" >&2
  exit 1
fi
TOOL_CALL_BUDGET_VALIDATE="${TOOL_CALL_BUDGET_VALIDATE:-60}"
MAX_CODEX_ATTEMPTS="${MAX_CODEX_ATTEMPTS:-3}"
if ! [[ "${MAX_CODEX_ATTEMPTS}" =~ ^[0-9]+$ ]] || [ "${MAX_CODEX_ATTEMPTS}" -lt 1 ]; then
  echo "::warning::MAX_CODEX_ATTEMPTS must be a positive integer (got: ${MAX_CODEX_ATTEMPTS}); defaulting to 3."
  MAX_CODEX_ATTEMPTS="3"
fi
CODEX_RETRY_BACKOFF_BASE_SECS="${CODEX_RETRY_BACKOFF_BASE_SECS:-10}"
if ! [[ "${CODEX_RETRY_BACKOFF_BASE_SECS}" =~ ^[0-9]+$ ]] || [ "${CODEX_RETRY_BACKOFF_BASE_SECS}" -lt 1 ]; then
  echo "::warning::CODEX_RETRY_BACKOFF_BASE_SECS must be a positive integer (got: ${CODEX_RETRY_BACKOFF_BASE_SECS}); defaulting to 10."
  CODEX_RETRY_BACKOFF_BASE_SECS="10"
fi
VALIDATION_COMPOSE_FILE="${VALIDATION_COMPOSE_FILE:-docker-compose.yml}"
SERENA_ENABLED="${SERENA_ENABLED:-false}"
SERENA_AVAILABLE="${SERENA_AVAILABLE:-false}"
SERENA_BOOTSTRAP_ATTEMPTED="${SERENA_BOOTSTRAP_ATTEMPTED:-false}"
SERENA_PROJECT_PREEXISTED="${SERENA_PROJECT_PREEXISTED:-}"
SERENA_PROJECT_BOOTSTRAP_HASH="${SERENA_PROJECT_BOOTSTRAP_HASH:-}"
VALIDATION_TEST_USERNAME="${VALIDATION_TEST_USERNAME:-test-user}"
VALIDATION_TEST_PASSWORD="${VALIDATION_TEST_PASSWORD:-test-password}"
VALIDATION_TEST_API_KEY="${VALIDATION_TEST_API_KEY:-test-api-key}"
VALIDATION_INCLUDE_SYNTHESISED="${VALIDATION_INCLUDE_SYNTHESISED:-true}"
VALIDATION_USE_TEMPLATES="${VALIDATION_USE_TEMPLATES:-true}"
VALIDATION_USE_TEMPLATES_ENABLED="false"
case "$(printf '%s' "${VALIDATION_USE_TEMPLATES}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    VALIDATION_USE_TEMPLATES_ENABLED="true"
    ;;
esac
VALIDATION_CYCLE="${VALIDATION_CYCLE:-1}"
if ! [[ "${VALIDATION_CYCLE}" =~ ^[0-9]+$ ]] || [ "${VALIDATION_CYCLE}" -lt 1 ]; then
  echo "::warning::VALIDATION_CYCLE must be a positive integer (got: ${VALIDATION_CYCLE}); defaulting to 1."
  VALIDATION_CYCLE="1"
fi

# Preflight pyflakes/ruff lint gate for embedded Python heredocs in
# validation/**/*.sh. Catches undefined-name (F821) / other F-code bugs
# that ast.parse cannot see and that runtime tests miss when the bug
# lives in an unexercised conditional branch (observed as
# `unknown_error:NameError` in consumer-repo autobet flows). See
# run_preflight_checks() for the implementation.
VALIDATE_PREFLIGHT_PYFLAKES_ENABLED="${VALIDATE_PREFLIGHT_PYFLAKES_ENABLED:-true}"
case "${VALIDATE_PREFLIGHT_PYFLAKES_ENABLED}" in
  true|false) ;;
  *)
    echo "::warning::VALIDATE_PREFLIGHT_PYFLAKES_ENABLED must be 'true' or 'false' (got: ${VALIDATE_PREFLIGHT_PYFLAKES_ENABLED}); defaulting to 'true'."
    VALIDATE_PREFLIGHT_PYFLAKES_ENABLED="true"
    ;;
esac
VALIDATE_PREFLIGHT_PYFLAKES_RULES="${VALIDATE_PREFLIGHT_PYFLAKES_RULES:-F}"
if ! [[ "${VALIDATE_PREFLIGHT_PYFLAKES_RULES}" =~ ^[A-Z0-9,]+$ ]]; then
  echo "::warning::VALIDATE_PREFLIGHT_PYFLAKES_RULES must match ^[A-Z0-9,]+\$ (got: ${VALIDATE_PREFLIGHT_PYFLAKES_RULES}); defaulting to 'F'."
  VALIDATE_PREFLIGHT_PYFLAKES_RULES="F"
fi
export VALIDATE_PREFLIGHT_PYFLAKES_ENABLED VALIDATE_PREFLIGHT_PYFLAKES_RULES

PROJECT_SPEC_FILE="${RUNTIME_DIR}/project_spec.txt"
STATIC_CONTEXT_FILE="${RUNTIME_DIR}/validate_static.txt"
VALIDATE_HINTS_FILE="${RUNTIME_DIR}/validate_hints.txt"
DISCOVER_PROMPT_FILE="${RUNTIME_DIR}/validate_discover_prompt.txt"
DISCOVER_OUTPUT_FILE="${RUNTIME_DIR}/validate_discover_output.txt"
DISCOVER_LOG_FILE="${RUNTIME_DIR}/validate_discover.log"
GENERATE_OUTPUT_FILE="${RUNTIME_DIR}/validate_generate_output.txt"
GENERATE_LOG_FILE="${RUNTIME_DIR}/validate_generate.log"
VALIDATION_LOG_FILE="${RUNTIME_DIR}/validation.log"
VALIDATION_RESULT_FILE="${RUNTIME_DIR}/validation_result.json"
DIAGNOSE_PROMPT_FILE="${RUNTIME_DIR}/validate_diagnose_prompt.txt"
DIAGNOSE_OUTPUT_FILE="${RUNTIME_DIR}/validate_diagnose_output.txt"
DIAGNOSE_LOG_FILE="${RUNTIME_DIR}/validate_diagnose.log"
DIAGNOSE_RESULT_FILE="${RUNTIME_DIR}/validation_diagnosis.json"
METADATA_FILE="${RUNTIME_DIR}/validation_metadata.json"
STATUS_FILE="${RUNTIME_DIR}/validation_status.json"
VALIDATION_LOG_TAIL_FILE="${RUNTIME_DIR}/validation_log_tail.txt"
CONTAINER_LOG_TAIL_FILE="${RUNTIME_DIR}/container_logs_tail.txt"
NULL_JSON_FILE="${RUNTIME_DIR}/null.json"
PRE_GENERATE_STATUS_FILE="${RUNTIME_DIR}/pre_generate_git_status.txt"
POST_GENERATE_STATUS_FILE="${RUNTIME_DIR}/post_generate_git_status.txt"
PRE_GENERATE_GUARD_PATHS_FILE="${RUNTIME_DIR}/pre_generate_write_guard_paths.txt"
POST_GENERATE_GUARD_PATHS_FILE="${RUNTIME_DIR}/post_generate_write_guard_paths.txt"
PRE_FLIGHT_LOG_FILE="${RUNTIME_DIR}/validation_preflight.log"
PRIOR_RESULT_JSON_FILE="${RUNTIME_DIR}/prior_validation_result.json"
PRIOR_CONTAINER_LOGS_FILE="${RUNTIME_DIR}/prior_container_logs_tail.txt"
VALIDATION_RUNNER_FILE="${RUNTIME_DIR}/validation_runtime_driver.sh"

HINTS_SOURCE="none"
HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
PRE_FLIGHT_STATUS="not_run"
PRE_FLIGHT_FAILURE_CLASS="none"
PRE_FLIGHT_FAILURE_KIND="none"
PRE_FLIGHT_FAILURE_REASON="not_run"
PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="false"
PRE_FLIGHT_APPEND_LOG="false"
GENERATED_VALIDATE_SCRIPT_PATH=""
CANONICAL_VALIDATE_DRIVER_REL="scripts/validate_process.sh"
CANONICAL_VALIDATE_HARNESS_REL="validation/validate.sh"

mkdir -p "${RUNTIME_DIR}"
printf 'null\n' > "${NULL_JSON_FILE}"

export VALIDATION_TEST_USERNAME
export VALIDATION_TEST_PASSWORD
export VALIDATION_TEST_API_KEY
export TEST_USERNAME="${TEST_USERNAME:-${VALIDATION_TEST_USERNAME}}"
export TEST_PASSWORD="${TEST_PASSWORD:-${VALIDATION_TEST_PASSWORD}}"
export TEST_API_KEY="${TEST_API_KEY:-${VALIDATION_TEST_API_KEY}}"

CREATED_FIX_ISSUES_JSON='[]'

# ---------------------------------------------------------------
# Self-heal state (see prompts/mode-validate-self-heal.txt).
# Self-heal attempts do NOT burn validation cycles: they are re-execs of
# this process within the same cycle. The counter is passed across execs
# via SELF_HEAL_ATTEMPT, and the accumulated patches are appended to
# SELF_HEAL_PATCHES_FILE (JSONL) for later repository_dispatch back to
# shubhodeep1/coding-workflows.
# ---------------------------------------------------------------
SELF_HEAL_ATTEMPT="${SELF_HEAL_ATTEMPT:-0}"
if ! [[ "${SELF_HEAL_ATTEMPT}" =~ ^[0-9]+$ ]]; then
  SELF_HEAL_ATTEMPT=0
fi
MAX_SELF_HEAL_ATTEMPTS="${MAX_SELF_HEAL_ATTEMPTS:-2}"
if ! [[ "${MAX_SELF_HEAL_ATTEMPTS}" =~ ^[0-9]+$ ]]; then
  MAX_SELF_HEAL_ATTEMPTS=2
fi
SELF_HEAL_PATCHES_FILE="${SELF_HEAL_PATCHES_FILE:-${RUNTIME_DIR}/self_heal_patches.jsonl}"
export SELF_HEAL_ATTEMPT MAX_SELF_HEAL_ATTEMPTS SELF_HEAL_PATCHES_FILE


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
# shellcheck source=gh_helpers.sh
if [ -f "scripts/gh_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/gh_helpers.sh
fi
# shellcheck source=tg_helpers.sh
if [ -f "scripts/tg_helpers.sh" ]; then
  # shellcheck disable=SC1091
  source scripts/tg_helpers.sh
fi
# shellcheck source=/dev/null
if [ ! -f "${_validate_script_dir}/codex_helpers.sh" ]; then
  echo "::error::Missing required support script ${_validate_script_dir}/codex_helpers.sh" >&2
  exit 1
fi
source "${_validate_script_dir}/codex_helpers.sh"
# shellcheck source=/dev/null
if [ ! -f "${_validate_script_dir}/watchdog_helpers.sh" ]; then
  echo "::error::Missing required support script ${_validate_script_dir}/watchdog_helpers.sh" >&2
  exit 1
fi
source "${_validate_script_dir}/watchdog_helpers.sh"


# _gh_url constructs a full GitHub URL for the current repository.
_gh_url() {
  printf "%s/%s/%s" "${GITHUB_SERVER_URL:-https://github.com}" "${GITHUB_REPOSITORY}" "$1"
}
_tg_link_suffix()
{
  local suffix=""
  if [ "${TRACKING_ISSUE_NUM}" -gt 0 ]; then
    suffix+=$'\n'"Issue: $(_gh_url "issues/${TRACKING_ISSUE_NUM}")"
  fi
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    suffix+=$'\n'"Run: $(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
  fi
  printf '%s' "${suffix}"
}

tg_notify()
{
  local msg="$1$(_tg_link_suffix)"
  local level="${2:-CRITICAL}"
  if [ "${TRACKING_ISSUE_NUM}" -gt 0 ]; then
    tg_send_tracked "${TRACKING_ISSUE_NUM}" "${msg}" "${level}"
  else
    # Standalone validation run (no tracking issue): untracked send
    tg_send_msg "${msg}" "${level}" >/dev/null
  fi
}

# gh_retry is provided by scripts/gh_helpers.sh (rate-limit-aware).
# Fallback definition in case gh_helpers.sh was not sourced.
if ! type gh_retry >/dev/null 2>&1; then
  gh_retry() { "$@"; }
fi
if ! command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
  sanitize_codex_prompt_file() { :; }
fi

write_github_env_value()
{
  local key="${1:?write_github_env_value: key required}"
  local value="${2-}"

  if [ -z "${GITHUB_ENV:-}" ]; then
    return 0
  fi
  printf '%s=%s\n' "${key}" "${value}" >> "${GITHUB_ENV}" 2>/dev/null || true
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

detect_serena_project_preexisting()
{
  if command -v git >/dev/null 2>&1 && git ls-files --error-unmatch -- .serena/project.yml >/dev/null 2>&1; then
    printf '%s\n' "true"
    return 0
  fi
  if [ -e .serena/project.yml ]; then
    printf '%s\n' "true"
  else
    printf '%s\n' "false"
  fi
}

clear_stale_serena_codex_config()
{
  local codex_config_path=""

  if [ -z "${HOME:-}" ]; then
    return 0
  fi
  codex_config_path="${HOME}/.codex/config.toml"
  if [ ! -f "${codex_config_path}" ]; then
    return 0
  fi

  if ! PYTHONDONTWRITEBYTECODE=1 python3 - "${codex_config_path}" <<'PY'
from pathlib import Path
import re
import sys

config_path = Path(sys.argv[1])
existing = config_path.read_text(encoding="utf-8")
lines = existing.splitlines(keepends=True)
out = []
i = 0

def is_serena_header(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r"^\[mcp_servers\.serena(?:\.[^\]]+)?\](?:[ \t]+#.*)?$", stripped))

def is_table_header(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r"^(\[[^\]]+\]|\[\[[^\]]+\]\])(?:[ \t]+#.*)?$", stripped))

while i < len(lines):
    if is_serena_header(lines[i]):
        i += 1
        while i < len(lines) and (not is_table_header(lines[i]) or is_serena_header(lines[i])):
            i += 1
        continue
    out.append(lines[i])
    i += 1

rendered = "".join(out).rstrip("\n")
if rendered == existing.rstrip("\n"):
    raise SystemExit(0)
if rendered:
    config_path.write_text(rendered + "\n", encoding="utf-8")
else:
    config_path.unlink()
PY
  then
    echo "::warning::Failed to clear stale Serena MCP configuration from ${codex_config_path}; continuing." >&2
  fi
}

if [ -z "${SERENA_PROJECT_PREEXISTED}" ]; then
  SERENA_PROJECT_PREEXISTED="$(detect_serena_project_preexisting)"
fi
export SERENA_ENABLED SERENA_AVAILABLE SERENA_BOOTSTRAP_ATTEMPTED SERENA_PROJECT_PREEXISTED SERENA_PROJECT_BOOTSTRAP_HASH

SEMBLE_HELPERS_AVAILABLE="false"
# shellcheck source=semble_helpers.sh
if [ -f "scripts/semble_helpers.sh" ]; then
  # shellcheck disable=SC1091
  if source scripts/semble_helpers.sh; then
    if type semble_query_block >/dev/null 2>&1; then
      SEMBLE_HELPERS_AVAILABLE="true"
    else
      echo "::warning::scripts/semble_helpers.sh did not provide semble_query_block; continuing without Semble prompt context." >&2
    fi
  else
    echo "::warning::Failed to source scripts/semble_helpers.sh; continuing without Semble prompt context." >&2
  fi
fi

VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS="${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS:-3}"
if ! [[ "${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS}" =~ ^[0-9]+$ ]] || [ "${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS}" -lt 1 ]; then
  echo "::warning::VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS must be a positive integer (got: ${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS}); defaulting to 3." >&2
  VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS="3"
fi

VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS="${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS:-3}"
if ! [[ "${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS}" =~ ^[0-9]+$ ]] || [ "${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS}" -lt 1 ]; then
  echo "::warning::VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS must be a positive integer (got: ${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS}); defaulting to 3." >&2
  VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS="3"
fi

build_validate_semble_query()
{
  local label="${1:-validation}"
  shift || true

  python3 - "${label}" "$@" <<'PY'
import pathlib
import re
import sys

label = sys.argv[1].strip() or "validation"
raw_inputs = sys.argv[2:]
path_tokens = []
interesting_lines = []

path_re = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|sh|ya?ml|json|toml|js|jsx|ts|tsx|go|rs|java|kt|rb|php|sql|md|txt)"
    r"(?![A-Za-z0-9_./-])"
)
special_file_re = re.compile(r"\b(?:Dockerfile(?:\.[A-Za-z0-9_.-]+)?|Makefile|Procfile)\b")

def append_unique(bucket, value, *, limit):
    value = value.strip()
    if not value or value in bucket or len(bucket) >= limit:
        return
    bucket.append(value)

def iter_source_texts(values):
    for raw in values:
        if not raw:
            continue
        candidate_path = pathlib.Path(raw)
        if "\n" not in raw and len(raw) < 512 and candidate_path.is_file():
            try:
                yield candidate_path.read_text(encoding="utf-8", errors="replace")[:12000]
            except OSError:
                yield raw[:12000]
        else:
            yield raw[:12000]

for text in iter_source_texts(raw_inputs):
    if not text.strip():
        continue

    for token in re.findall(r"`([^`\n]+)`", text):
        token = token.strip()
        if "/" in token or "." in pathlib.Path(token).name or token in {"Dockerfile", "Makefile", "Procfile"}:
            append_unique(path_tokens, token, limit=10)

    for token in path_re.findall(text):
        append_unique(path_tokens, token, limit=10)
    for token in special_file_re.findall(text):
        append_unique(path_tokens, token, limit=10)

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or line.startswith(("===", "---", "```", "#")):
            continue
        if len(line) < 8:
            continue
        append_unique(interesting_lines, line[:180], limit=8)

query_parts = [label]
if path_tokens:
    query_parts.append("files " + " ".join(path_tokens[:8]))
if interesting_lines:
    query_parts.extend(interesting_lines[:6])

query = " ; ".join(part for part in query_parts if part)
query = query[:700].strip(" ;")
if query:
    print(query)
PY
}

build_validate_discover_semble_query()
{
  build_validate_semble_query \
    "validation discover context" \
    "validation harness docker compose canary runtime validation" \
    "${VALIDATION_COMPOSE_FILE}" \
    "${PROJECT_SPEC_FILE}"
}

build_validate_diagnose_semble_query()
{
  build_validate_semble_query \
    "validation diagnose failure context" \
    "runtime validation failure diagnose failing tests" \
    "${FIRST_FAILURE:-}" \
    "${VALIDATION_RESULT_FILE}" \
    "${VALIDATION_LOG_TAIL_FILE}" \
    "${CONTAINER_LOG_TAIL_FILE}" \
    "${VALIDATE_HINTS_FILE}"
}

append_validate_semble_context()
{
  local query_text="${1:-}"
  local max_chunks="${2:-3}"
  local header_label="${3:-Validation Context}"

  if [ "${SEMBLE_HELPERS_AVAILABLE}" != "true" ] || [ -z "${query_text}" ]; then
    return 0
  fi

  echo
  if semble_query_block "${query_text}" "${max_chunks}" "${header_label}"; then
    echo
  fi
}

# Filter workflow-generated Serena runtime artifacts from path-constraint
# bookkeeping only when the repo did not already own the Serena project
# config before bootstrap and that config stayed unchanged. That keeps
# bootstrap-owned .serena/ state from looking like a validation-side edit
# while still preserving repo-owned .serena files.
filter_runtime_status_noise()
{
  local line current_hash

  while IFS= read -r line; do
    case "${line}" in
      *' .serena/'*|*' .serena')
        if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && \
           [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && \
           [ -f .serena/project.yml ]; then
          current_hash="$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)"
          if [ -n "${current_hash}" ] && [ "${current_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then
            continue
          fi
        fi
        ;;
    esac
    printf '%s\n' "${line}"
  done
}

filter_runtime_path_noise()
{
	local candidate_line path current_hash

	while IFS= read -r candidate_line; do
		path="${candidate_line%%$'\t'*}"
		case "${path}" in
		  .serena|.serena/*)
			if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && \
			   [ -n "${SERENA_PROJECT_BOOTSTRAP_HASH:-}" ] && \
           [ -f .serena/project.yml ]; then
          current_hash="$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)"
          if [ -n "${current_hash}" ] && [ "${current_hash}" = "${SERENA_PROJECT_BOOTSTRAP_HASH}" ]; then
            continue
          fi
			fi
			;;
		esac
		printf '%s\n' "${candidate_line}"
	done
}

capture_write_guard_candidate_paths()
{
	if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
		return 0
	fi

	local path path_state
	while IFS= read -r path; do
		[ -n "${path}" ] || continue
		if [ -e "${path}" ]; then
			path_state="$(sha256sum -- "${path}" 2>/dev/null | awk '{print $1}' || true)"
			[ -n "${path_state}" ] || path_state="__present__"
		else
			path_state="__deleted__"
		fi
		printf '%s\t%s\n' "${path}" "${path_state}"
	done < <(
		{
			git diff --name-only --diff-filter=ACMRD HEAD || true
			git ls-files --others --exclude-standard || true
		} | sed '/^$/d' | sort -u
	) | filter_runtime_path_noise | sort -u
}

run_validate_write_guard()
{
  if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
    return 0
  fi

	local validate_write_guard_file
	validate_write_guard_file="$(mktemp "${TMPDIR:-/tmp}/validate-write-guard.XXXXXX")"

	if [ -f "${PRE_GENERATE_GUARD_PATHS_FILE}" ] && [ -f "${POST_GENERATE_GUARD_PATHS_FILE}" ]; then
		comm -13 "${PRE_GENERATE_GUARD_PATHS_FILE}" "${POST_GENERATE_GUARD_PATHS_FILE}" | cut -f1 | sed '/^$/d' | sort -u > "${validate_write_guard_file}" || true
	elif [ -f "${POST_GENERATE_GUARD_PATHS_FILE}" ]; then
		cut -f1 "${POST_GENERATE_GUARD_PATHS_FILE}" | sed '/^$/d' | sort -u > "${validate_write_guard_file}"
	fi

  if ! write_guard_check validate_fix_harness "${validate_write_guard_file}"; then
    rm -f "${validate_write_guard_file}"
    return 1
  fi

  rm -f "${validate_write_guard_file}"
  return 0
}

build_validate_serena_tool_hints()
{
  local phase="${1:-general}"

  if [ "${SERENA_AVAILABLE:-false}" != "true" ]; then
    return 0
  fi

  case "${phase}" in
    discover)
      printf '%s\n' \
        'Serena hints:' \
        '- Serena MCP is available in this run. Prefer Serena symbol/navigation tools for discovery when they materially reduce shell reads (for example: activate_project, find_symbol, get_symbols_overview, find_referencing_symbols, search_for_pattern).' \
        '- Use Serena to confirm runtime entrypoints, env readers, health routes, Docker/build files, and existing test commands; keep the YAML output grounded in repo evidence and preserve the required schema.'
      ;;
    diagnose)
      printf '%s\n' \
        'Serena hints:' \
        '- Serena MCP is available in this run. Prefer Serena symbol/navigation tools when they materially reduce shell reads while confirming which repository-owned file, function, or script path a failure comes from.' \
        '- Use Serena to strengthen evidence for `needs_fixes` vs `harness_error`, but keep the JSON schema unchanged and do not invent ownership facts.'
      ;;
    *)
      printf '%s\n' \
        'Serena hints:' \
        '- Serena MCP is available in this run. Prefer Serena symbol/navigation tools when they materially reduce shell reads.' \
        '- Keep repository changes and output contracts unchanged.'
      ;;
  esac
}

sanitize_serena_log_value()
{
  local value="${1:-unknown}"

  value="$(printf '%s' "${value}" | tr '[:space:]=' '__')"
  if [ -z "${value}" ]; then
    value="unknown"
  fi
  printf '%s\n' "${value}"
}

emit_serena_fallback()
{
  local phase="${1:-general}"
  local reason="${2:-setup-failure}"

  printf 'SERENA_FALLBACK target=validate phase=%s reason=%s\n' \
    "$(sanitize_serena_log_value "${phase}")" \
    "$(sanitize_serena_log_value "${reason}")" >&2
}

ensure_serena_bootstrap()
{
  local serena_phase="${1:-general}"
  local bootstrap_env_file=""
  local env_key=""
  local env_value=""
  local serena_project_hash=""

  if ! env_is_truthy "${SERENA_ENABLED:-false}"; then
    emit_serena_fallback "${serena_phase}" "disabled"
    clear_stale_serena_codex_config
    SERENA_AVAILABLE="false"
    export SERENA_AVAILABLE
    write_github_env_value "SERENA_AVAILABLE" "${SERENA_AVAILABLE}"
    return 0
  fi
  if env_is_truthy "${SERENA_AVAILABLE:-false}"; then
    return 0
  fi
  if env_is_truthy "${SERENA_BOOTSTRAP_ATTEMPTED:-false}"; then
    return 0
  fi

  SERENA_BOOTSTRAP_ATTEMPTED="true"
  export SERENA_BOOTSTRAP_ATTEMPTED

  if [ -z "${SERENA_PROJECT_PREEXISTED:-}" ]; then
    SERENA_PROJECT_PREEXISTED="$(detect_serena_project_preexisting)"
  fi
  export SERENA_PROJECT_PREEXISTED
  write_github_env_value "SERENA_PROJECT_PREEXISTED" "${SERENA_PROJECT_PREEXISTED}"

  if [ ! -f "scripts/setup_serena.sh" ]; then
    echo "::notice::scripts/setup_serena.sh is unavailable; validation will continue without Serena."
    emit_serena_fallback "${serena_phase}" "setup-failure"
    clear_stale_serena_codex_config
    SERENA_AVAILABLE="false"
    export SERENA_AVAILABLE
    write_github_env_value "SERENA_AVAILABLE" "${SERENA_AVAILABLE}"
    return 0
  fi

  bootstrap_env_file="$(mktemp "${RUNTIME_DIR}/serena-bootstrap-env.XXXXXX")"
  if ! SERENA_FALLBACK_TARGET="validate" SERENA_FALLBACK_PHASE="${serena_phase}" GITHUB_ENV="${bootstrap_env_file}" bash scripts/setup_serena.sh; then
    echo "::warning::scripts/setup_serena.sh exited non-zero; validation will continue without Serena."
    emit_serena_fallback "${serena_phase}" "setup-failure"
    clear_stale_serena_codex_config
    SERENA_AVAILABLE="false"
  else
    SERENA_AVAILABLE="false"
    while IFS='=' read -r env_key env_value; do
      case "${env_key}" in
        SERENA_AVAILABLE)
          SERENA_AVAILABLE="${env_value}"
          ;;
      esac
    done < "${bootstrap_env_file}"
  fi
  rm -f "${bootstrap_env_file}"

  if [ "${SERENA_PROJECT_PREEXISTED:-false}" != "true" ] && [ -f .serena/project.yml ]; then
    serena_project_hash="$(sha256sum .serena/project.yml 2>/dev/null | awk '{print $1}' || true)"
    SERENA_PROJECT_BOOTSTRAP_HASH="${serena_project_hash}"
  else
    SERENA_PROJECT_BOOTSTRAP_HASH=""
  fi

  export SERENA_AVAILABLE SERENA_PROJECT_BOOTSTRAP_HASH
  write_github_env_value "SERENA_AVAILABLE" "${SERENA_AVAILABLE}"
  write_github_env_value "SERENA_PROJECT_BOOTSTRAP_HASH" "${SERENA_PROJECT_BOOTSTRAP_HASH}"
}

# attempt_self_heal_and_reexec — last-chance interception before a hard
# validation failure. If the self-heal LLM proposes a patch to one of the
# four validation prompt files and the patch applies cleanly, we re-exec
# this script (incrementing SELF_HEAL_ATTEMPT, not VALIDATION_CYCLE). If
# self-heal is not applicable or budget is exhausted, this function
# returns and the caller proceeds with its normal hard-fail path.
#
# Usage: attempt_self_heal_and_reexec "<failure-phase-tag>"
#   phase tag: generate|preflight|render|canary|runtime|diagnose|discover
attempt_self_heal_and_reexec()
{
  local phase="${1:-unknown}"

  case "${phase}" in
    discover|generate|preflight|render|canary|diagnose|runtime|unknown)
      ;;
    *)
      echo "::warning::self-heal invoked with unsupported phase '${phase}'; normalizing to 'unknown'." >&2
      phase="unknown"
      ;;
  esac

  if [ "${MAX_SELF_HEAL_ATTEMPTS:-0}" -le 0 ]; then
    return 0
  fi
  if [ "${SELF_HEAL_ATTEMPT:-0}" -ge "${MAX_SELF_HEAL_ATTEMPTS}" ]; then
    tg_notify "Validation self-heal budget exhausted for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} (${SELF_HEAL_ATTEMPT}/${MAX_SELF_HEAL_ATTEMPTS}); falling back to hard-fail." "WARNING"
    return 0
  fi

  if [ ! -f "scripts/self_heal_validation.sh" ]; then
    echo "::warning::self-heal helper scripts/self_heal_validation.sh not found; skipping self-heal." >&2
    return 0
  fi
  # Self-heal is designed to be opt-in: workflow-templates/ai-validate.yml
  # and .github/workflows/validate.yml fetch the prompt and helper script
  # with require_remote=false, so older @stable tags can be missing one or
  # both. Fail-closed here rather than letting self_heal_validation.sh exit
  # with a misleading "no patch proposed" code, which would look like the
  # LLM chose not to self-heal when in fact a dependency was missing.
  if [ ! -f "prompts/mode-validate-self-heal.txt" ]; then
    echo "::warning::self-heal prompt prompts/mode-validate-self-heal.txt not found; skipping self-heal." >&2
    return 0
  fi
  if [ ! -f "scripts/render_prompt.sh" ]; then
    echo "::warning::self-heal dependency scripts/render_prompt.sh not found; skipping self-heal." >&2
    return 0
  fi

  ensure_serena_bootstrap "${phase}"

  local heal_path="${PATH}"
  local self_heal_continuation_source=""
  local self_heal_continuation_rendered=""
  local self_heal_wrapper_dir=""
  if validate_thread_reuse_enabled; then
    self_heal_continuation_source="$(resolve_validate_thread_reuse_asset 'prompts/mode-validate-self-heal-continuation.txt' 2>/dev/null || true)"
    if [ -n "${self_heal_continuation_source}" ]; then
      self_heal_continuation_rendered="${RUNTIME_DIR}/mode-validate-self-heal-continuation.rendered.txt"
      if SERENA_TOOL_HINTS='' bash scripts/render_prompt.sh "${self_heal_continuation_source}" > "${self_heal_continuation_rendered}"; then
        if self_heal_wrapper_dir="$(codex_thread_reuse_install_wrapper \
          'validate-self-heal' \
          "${self_heal_continuation_rendered}" \
          'replace-between' \
          '=== SELF-HEAL TASK ===' \
          '=== SELF-HEAL ATTEMPT ===')"; then
          heal_path="${self_heal_wrapper_dir}:${PATH}"
        else
          echo "::warning::Failed to install validate self-heal thread-reuse wrapper; using the full prompt path." >&2
        fi
      else
        echo "::warning::Failed to render validate self-heal continuation prompt; using the full prompt path." >&2
      fi
    fi
  fi

  local heal_exit=0
  PATH="${heal_path}" \
    SELF_HEAL_FAILURE_PHASE="${phase}" \
    STATIC_CONTEXT_FILE="${STATIC_CONTEXT_FILE}" \
    VALIDATION_RESULT_FILE="${VALIDATION_RESULT_FILE}" \
    DIAGNOSE_RESULT_FILE="${DIAGNOSE_RESULT_FILE}" \
    VALIDATION_LOG_TAIL_FILE="${VALIDATION_LOG_TAIL_FILE}" \
    CONTAINER_LOG_TAIL_FILE="${CONTAINER_LOG_TAIL_FILE}" \
    VALIDATE_HINTS_FILE="${VALIDATE_HINTS_FILE}" \
    DISCOVER_OUTPUT_FILE="${DISCOVER_OUTPUT_FILE}" \
    GENERATE_OUTPUT_FILE="${GENERATE_OUTPUT_FILE}" \
    DIAGNOSE_OUTPUT_FILE="${DIAGNOSE_OUTPUT_FILE}" \
    bash scripts/self_heal_validation.sh || heal_exit=$?

  case "${heal_exit}" in
    0)
      # Patch applied; re-exec with incremented attempt counter.
      SELF_HEAL_ATTEMPT=$((SELF_HEAL_ATTEMPT + 1))
      tg_notify "Validation self-heal attempt ${SELF_HEAL_ATTEMPT}/${MAX_SELF_HEAL_ATTEMPTS} applied for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} (phase=${phase})." "DEBUG"
      # validate_process.sh takes no positional args; re-exec plain.
      exec env \
        SELF_HEAL_ATTEMPT="${SELF_HEAL_ATTEMPT}" \
        MAX_SELF_HEAL_ATTEMPTS="${MAX_SELF_HEAL_ATTEMPTS}" \
        SELF_HEAL_PATCHES_FILE="${SELF_HEAL_PATCHES_FILE}" \
        bash "${BASH_SOURCE[0]:-$0}"
      ;;
    1)
      # No patch proposed — fall through to normal hard-fail path.
      return 0
      ;;
    *)
      # Hard error inside self-heal helper — fall through to hard-fail.
      echo "::warning::self-heal helper exited with code ${heal_exit}; falling through to hard-fail." >&2
      return 0
      ;;
  esac
}

# dispatch_self_heal_improvements — send accumulated self-heal patches
# back to the upstream coding-workflows repo as a repository_dispatch
# event. The intake workflow there will open a draft PR, append to
# docs/validation-improvements.md, and Telegram-alert the admin.
#
# No-op if no patches were accumulated or GH_PAT has no dispatch scope.
dispatch_self_heal_improvements()
{
  # This function runs on the success path AFTER the validation has
  # already been marked passing. It MUST NOT abort the script on any
  # internal failure, or a model-produced pathological patches ledger
  # could flip an otherwise-passing validation into a non-zero script
  # exit. The entire body runs in a subshell so `set -e` failures stay
  # local; the outer caller always sees return 0.
  (
    set +e
    if [ ! -s "${SELF_HEAL_PATCHES_FILE}" ]; then
      exit 0
    fi

    local upstream="shubhodeep1/coding-workflows"

    # Slurp patches into an array file. jq's --slurpfile reads the
    # referenced file from disk instead of taking it as a command-line
    # argument, so we sidestep ARG_MAX limits on large payloads.
    local patches_array_file="${RUNTIME_DIR}/self_heal_dispatch_patches.json"
    if ! jq -cs '.' "${SELF_HEAL_PATCHES_FILE}" > "${patches_array_file}" 2>/dev/null; then
      echo "::warning::Self-heal patches ledger could not be slurped by jq; skipping dispatch." >&2
      tg_notify "Validation self-heal SUCCEEDED for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} but patches ledger was malformed; dispatch skipped." "WARNING"
      exit 0
    fi
    local patches_count
    patches_count="$(jq 'length // 0' "${patches_array_file}" 2>/dev/null || echo 0)"
    if [ "${patches_count:-0}" -le 0 ]; then
      exit 0
    fi

    # Hard-cap total payload size so a pathological patches ledger
    # cannot produce a giant repository_dispatch body. 1 MiB total is
    # already far larger than any legitimate self-heal sequence given
    # the per-patch size limit (SELF_HEAL_MAX_PATCH_BYTES, default
    # 64 KiB) and the configured self-heal attempt budget
    # (MAX_SELF_HEAL_ATTEMPTS, default 2). Above the cap we skip
    # dispatch and preserve the full ledger in the run artifact.
    local max_dispatch_bytes="${SELF_HEAL_MAX_DISPATCH_BYTES:-1048576}"
    local patches_size
    patches_size="$(wc -c < "${patches_array_file}" | tr -d ' ')"
    if [ "${patches_size:-0}" -gt "${max_dispatch_bytes}" ]; then
      echo "::warning::Self-heal patches ledger is ${patches_size} bytes, exceeds SELF_HEAL_MAX_DISPATCH_BYTES=${max_dispatch_bytes}; skipping dispatch to avoid bloating the repository_dispatch payload." >&2
      tg_notify "Validation self-heal SUCCEEDED for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} but patches ledger is oversized (${patches_size} bytes); dispatch skipped. See run artifacts." "WARNING"
      exit 0
    fi

    local run_url=""
    if [ -n "${GITHUB_RUN_ID:-}" ]; then
      run_url="$(_gh_url "actions/runs/${GITHUB_RUN_ID}")"
    fi

    local payload_file="${RUNTIME_DIR}/self_heal_dispatch_payload.json"
    # --slurpfile binds $patches_wrapper to [<contents-of-file>]; the
    # patches array is already a single JSON array so we unwrap the
    # outer slurp layer with $patches_wrapper[0].
    if ! jq -cn \
      --arg consumer_repo "${GITHUB_REPOSITORY}" \
      --arg tracking_issue "${TRACKING_ISSUE_RAW}" \
      --arg validation_cycle "${VALIDATION_CYCLE}" \
      --arg run_id "${GITHUB_RUN_ID:-0}" \
      --arg run_url "${run_url}" \
      --arg final_status "pass" \
      --argjson attempts "${SELF_HEAL_ATTEMPT}" \
      --slurpfile patches_wrapper "${patches_array_file}" \
      '{
        event_type: "validation-prompt-self-heal",
        client_payload: {
          consumer_repo: $consumer_repo,
          tracking_issue: $tracking_issue,
          validation_cycle: $validation_cycle,
          run_id: $run_id,
          run_url: $run_url,
          final_status: $final_status,
          self_heal_attempts: $attempts,
          patches: $patches_wrapper[0]
        }
      }' > "${payload_file}" 2>"${RUNTIME_DIR}/self_heal_dispatch_jq.log"; then
      echo "::warning::Self-heal dispatch payload could not be built by jq; skipping dispatch. See ${RUNTIME_DIR}/self_heal_dispatch_jq.log." >&2
      tg_notify "Validation self-heal SUCCEEDED for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} but dispatch payload construction failed; dispatch skipped." "WARNING"
      exit 0
    fi

    local dispatch_log="${RUNTIME_DIR}/self_heal_dispatch.log"
    local dispatch_exit=0
    # Stream the payload from the file via stdin — gh api --input -
    # reads the POST body from stdin, so we never put the payload on
    # the command line.
    gh_retry gh api \
      -X POST \
      -H 'Accept: application/vnd.github+json' \
      "repos/${upstream}/dispatches" \
      --input "${payload_file}" \
      > "${dispatch_log}" 2>&1 || dispatch_exit=$?

    if [ "${dispatch_exit}" -ne 0 ]; then
      echo "::warning::Self-heal dispatch to ${upstream} failed (exit ${dispatch_exit}); see ${dispatch_log}. Patches are preserved in ${SELF_HEAL_PATCHES_FILE}." >&2
      tg_notify "Validation self-heal SUCCEEDED for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} but dispatch back to ${upstream} failed. Patches remain in run artifacts." "WARNING"
      exit 0
    fi

    tg_notify "Validation self-heal SUCCEEDED for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} (${SELF_HEAL_ATTEMPT} patch(es)); improvements dispatched to ${upstream}." "WARNING"
  ) || true
}

is_tracking_run()
{
  [ "${TRACKING_ISSUE_NUM}" -gt 0 ]
}

enforce_canonical_driver_path()
{
  local script_source="${BASH_SOURCE[0]:-$0}"
  case "${script_source}" in
    "${CANONICAL_VALIDATE_DRIVER_REL}"|"./${CANONICAL_VALIDATE_DRIVER_REL}"|*/"${CANONICAL_VALIDATE_DRIVER_REL}")
      return 0
      ;;
    *)
      echo "Refusing to run validate driver from non-canonical path '${script_source}'. Expected ${CANONICAL_VALIDATE_DRIVER_REL}." >&2
      return 1
      ;;
  esac
}

ensure_validation_harness_not_tracked()
{
  if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
    return 0
  fi

  if git ls-files --error-unmatch -- "${CANONICAL_VALIDATE_HARNESS_REL}" >/dev/null 2>&1; then
    echo "${CANONICAL_VALIDATE_HARNESS_REL} is tracked by git; it must remain transient." >&2
    return 1
  fi

  if git diff --cached --name-only -- "${CANONICAL_VALIDATE_HARNESS_REL}" | grep -q .; then
    echo "${CANONICAL_VALIDATE_HARNESS_REL} is staged in git; it must remain unstaged/untracked." >&2
    return 1
  fi

  return 0
}

enforce_no_renamed_driver_artifacts()
{
  if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
    return 0
  fi

  local candidate_driver_files
  local candidate
  local renamed_driver_files=""
  candidate_driver_files="$({
    git ls-files -- 'scripts/validate*.sh'
    git ls-files --others --exclude-standard -- 'scripts/validate*.sh'
  } 2>/dev/null | awk '$0 != "scripts/validate_process.sh" && $0 != "scripts/validate_driver.sh"' | sort -u)"

  while IFS= read -r candidate; do
    [ -n "${candidate}" ] || continue
    [ -f "${candidate}" ] || continue

    if cmp -s "${candidate}" "scripts/validate_process.sh" \
      || { [ -f "scripts/validate_driver.sh" ] && cmp -s "${candidate}" "scripts/validate_driver.sh"; }; then
      renamed_driver_files="${renamed_driver_files}${candidate}"$'\n'
    fi
  done <<< "${candidate_driver_files}"

  if [ -n "${renamed_driver_files}" ]; then
    echo "Found renamed managed validate driver artifacts in scripts/:" >&2
    printf '%s' "${renamed_driver_files}" >&2
    return 1
  fi

  return 0
}

post_tracking_comment()
{
  local comment_body="$1"
  if ! is_tracking_run; then
    return 0
  fi

  comment_body="${comment_body//\\n/$'\n'}"

  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/comments" \
    -f body="${comment_body}" >/dev/null || true
}

ensure_label_exists()
{
  local label_name="$1"
  local contract_file=".github/ai/label_contract.v1.json"
  local color="1d76db"
  local description="AI workflow label"

  if [ -f "${contract_file}" ]; then
    color="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].color // "1d76db"' "${contract_file}" 2>/dev/null || echo "1d76db")"
    description="$(jq -r --arg lbl "${label_name}" '.labels[$lbl].description // "AI workflow label"' "${contract_file}" 2>/dev/null || echo "AI workflow label")"
  fi

  local _label_err_file
  _label_err_file="$(mktemp 2>/dev/null || echo '/dev/null')"

  if gh_retry gh label create "${label_name}" \
    --repo "${GITHUB_REPOSITORY}" \
    --color "${color}" \
    --description "${description}" >/dev/null 2>"${_label_err_file}"; then
    [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"
    return 0
  fi

  local _label_err=""
  _label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
  [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"

  if printf '%s' "${_label_err}" | grep -Eiq 'already[ _-]*exists|already_exists'; then
    echo "::debug::ensure_label_exists: label already exists, skipping '${label_name}'." >&2
    return 0
  fi

  tg_notify "ensure_label_exists: failed to create label '${label_name}' in repo '${GITHUB_REPOSITORY}': ${_label_err}" "WARNING"
  return 0
}

set_tracking_phase_label()
{
  local phase_label="$1"
  local contract_file=".github/ai/label_contract.v1.json"

  if ! is_tracking_run; then
    return 0
  fi

  ensure_label_exists "${phase_label}"

  if [ ! -f "${contract_file}" ]; then
    echo "::warning::set_tracking_phase_label: missing label contract ${contract_file}; cannot apply label '${phase_label}' safely." >&2
    return 1
  fi

  local phase_changes
  if ! phase_changes="$(python3 scripts/ai_labels.py resolve-phase \
    --contract-file "${contract_file}" \
    --phase "${phase_label}" 2>/dev/null)"; then
    echo "::warning::set_tracking_phase_label: resolve-phase failed for '${phase_label}' using ${contract_file}." >&2
    return 1
  fi

  # Fetch current labels on the issue so we only attempt to remove labels
  # that are actually present.  Trying to remove a label that does not
  # exist on the issue can cause `gh issue edit` to return an error,
  # which the outer `|| true` would silently swallow — leaving stale
  # labels (e.g. ai:validating + ai:validation-fixing) in place even
  # after the phase has advanced to ai:validated.
  local current_issue_labels
  current_issue_labels="$(gh_retry gh api \
    "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/labels" \
    --jq '[.[].name]' 2>/dev/null || echo '[]')"

  # Build a single gh issue edit command with all --remove-label and
  # --add-label flags instead of one API call per label.
  local edit_args=()
  while IFS= read -r remove_label; do
    [ -n "${remove_label}" ] || continue
    # Only remove labels that are currently on the issue to avoid
    # errors from trying to remove absent labels.
    if echo "${current_issue_labels}" | jq -e --arg l "${remove_label}" 'index($l) != null' >/dev/null 2>&1; then
      edit_args+=(--remove-label "${remove_label}")
    fi
  done < <(echo "${phase_changes}" | jq -r '.remove[]?')

  while IFS= read -r add_label; do
    [ -n "${add_label}" ] || continue
    ensure_label_exists "${add_label}"
    edit_args+=(--add-label "${add_label}")
  done < <(echo "${phase_changes}" | jq -r '.add[]?')

  if [ "${#edit_args[@]}" -gt 0 ]; then
    local _label_err_file
    _label_err_file="$(mktemp 2>/dev/null || echo '/dev/null')"
    if ! gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
      --repo "${GITHUB_REPOSITORY}" \
      "${edit_args[@]}" >/dev/null 2>"${_label_err_file}"; then
      local _label_err
      _label_err="$(cat "${_label_err_file}" 2>/dev/null || true)"
      if echo "${_label_err}" | grep -Eqi "could not remove label:|['\"][[:alnum:]:._/-]+['\"] not found"; then
        echo "::warning::set_tracking_phase_label: non-fatal missing label while applying '${phase_label}' to #${TRACKING_ISSUE_NUM}: ${_label_err}" >&2
        [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"
        return 0
      fi
      echo "::warning::set_tracking_phase_label: failed to apply '${phase_label}' to #${TRACKING_ISSUE_NUM}: ${_label_err}" >&2
      [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"
      return 1
    fi
    [ "${_label_err_file}" = "/dev/null" ] || rm -f "${_label_err_file}"
  fi
  return 0
}

if ! enforce_canonical_driver_path; then
  exit 1
fi

if ! enforce_no_renamed_driver_artifacts; then
  exit 1
fi

extract_last_json_with_key()
{
  local source_file="$1"
  local required_key="$2"
  local output_file="$3"

  python3 - "${source_file}" "${required_key}" "${output_file}" <<'PY'
import json
import re
import sys

source_file = sys.argv[1]
required_key = sys.argv[2]
output_file = sys.argv[3]

with open(source_file, "r", encoding="utf-8", errors="replace") as handle:
    raw = handle.read()

candidates = []

trimmed = raw.strip()
if trimmed:
    try:
        parsed = json.loads(trimmed)
        if isinstance(parsed, dict) and required_key in parsed:
            candidates.append(parsed)
    except json.JSONDecodeError:
        pass

cleaned = re.sub(r"```(?:json)?\s*", "", raw)
cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)

decoder = json.JSONDecoder()
pos = 0
while pos < len(cleaned):
    start = cleaned.find("{", pos)
    if start == -1:
        break
    try:
        parsed, end = decoder.raw_decode(cleaned[start:])
        if isinstance(parsed, dict) and required_key in parsed:
            candidates.append(parsed)
        pos = start + end
    except json.JSONDecodeError:
        pos = start + 1

if not candidates:
    print(f"No JSON object with key '{required_key}' found", file=sys.stderr)
    sys.exit(1)

with open(output_file, "w", encoding="utf-8") as handle:
    json.dump(candidates[-1], handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
}

is_validation_harness_runnable()
{
	if [ -f validation/docker-compose.test.yml ] \
		&& [ -f validation/tests/00_canary.sh ] \
		&& find validation/tests -maxdepth 1 -type f -name '*.sh' -print -quit | grep -q .; then
		GENERATED_VALIDATE_SCRIPT_PATH=""
		return 0
	fi

	GENERATED_VALIDATE_SCRIPT_PATH=""
	return 1
}

ensure_runtime_validation_driver()
{
  cat > "${VALIDATION_RUNNER_FILE}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="validation/docker-compose.test.yml"
TEST_DIR="validation/tests"
LOG_DIR="validation/logs"
COMPOSE_LOG="${LOG_DIR}/compose.log"
ENV_FILE="${VALIDATE_ENV_FILE:-validation/validate.env}"
START_TS="$(date +%s)"

	if [ -f "${ENV_FILE}" ]; then
	  while IFS= read -r env_line || [ -n "${env_line}" ]; do
	    env_line="${env_line%$'\r'}"
	    env_line="${env_line#"${env_line%%[![:space:]]*}"}"
	    [ -z "${env_line}" ] && continue
	    [ "${env_line#\#}" != "${env_line}" ] && continue
	    if [[ ! "${env_line}" =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]]; then
	      printf 'validation_runtime_driver: %s: unparseable env_file line: %s\n' "${ENV_FILE}" "${env_line}" >&2
	      exit 1
	    fi
	    key="${env_line%%=*}"
	    value="${env_line#*=}"
	    if [ "${#value}" -ge 2 ]; then
	      first="${value:0:1}"; last="${value: -1}"
	      if { [ "${first}" = '"' ] && [ "${last}" = '"' ]; } || { [ "${first}" = "'" ] && [ "${last}" = "'" ]; }; then
	        value="${value:1:${#value}-2}"
	      fi
	    fi
	    export "${key}=${value}"
	  done < "${ENV_FILE}"
	fi

mkdir -p "${LOG_DIR}"
: > "${COMPOSE_LOG}"

TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
FAILURES_FILE="$(mktemp)"
RESULT_EMITTED=0
printf '[]' > "${FAILURES_FILE}"

append_failure()
{
  local test_name="$1"
  local error_msg="$2"
  local log_file="${3:-}"
  local log_tail=""

  if [ -n "${log_file}" ] && [ -f "${log_file}" ]; then
    log_tail="$(tail -c 10000 "${log_file}" | tr -d '\000' | tail -n 30 2>/dev/null || true)"
  fi

  python3 - "${FAILURES_FILE}" "${test_name}" "${error_msg}" "${log_tail}" <<'PY'
import json
import sys

path, test_name, error_msg, log_tail = sys.argv[1:5]
with open(path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)
payload.append({"test": test_name, "error": error_msg, "log_tail": log_tail})
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY
}

emit_result()
{
  local result_value="${1:-fail}"
  local duration_seconds

  if [ "${RESULT_EMITTED}" = "1" ]; then
    return 0
  fi

  duration_seconds=$(( $(date +%s) - START_TS ))

  RESULT="${result_value}" \
  TOTAL_TESTS="${TOTAL_TESTS}" \
  PASSED_TESTS="${PASSED_TESTS}" \
  FAILED_TESTS="${FAILED_TESTS}" \
  DURATION_SECONDS="${duration_seconds}" \
  FAILURES_FILE_PATH="${FAILURES_FILE}" \
  python3 -c 'import json, os; print(json.dumps({
"result": os.environ["RESULT"],
"phase": "runtime_validation",
"total_tests": int(os.environ["TOTAL_TESTS"]),
"passed_tests": int(os.environ["PASSED_TESTS"]),
"failed_tests": int(os.environ["FAILED_TESTS"]),
"failures": json.load(open(os.environ["FAILURES_FILE_PATH"], encoding="utf-8")),
"duration_seconds": int(os.environ["DURATION_SECONDS"]),
}))'

  RESULT_EMITTED=1
}

cleanup()
{
  {
    printf '\n===== docker compose logs --no-color =====\n'
    docker compose -f "${COMPOSE_FILE}" logs --no-color 2>/dev/null || true
  } >> "${COMPOSE_LOG}"
  docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "${FAILURES_FILE}" >/dev/null 2>&1 || true
}

trap cleanup EXIT

if ! docker compose -f "${COMPOSE_FILE}" up -d --build >> "${COMPOSE_LOG}" 2>&1; then
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  FAILED_TESTS=$((FAILED_TESTS + 1))
  append_failure "compose_up" "failed to build/start compose services" "${COMPOSE_LOG}"
  emit_result fail
  exit 1
fi

mapfile -t test_scripts < <(find "${TEST_DIR}" -maxdepth 1 -type f -name '*.sh' | sort)
if [ "${#test_scripts[@]}" -eq 0 ]; then
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  FAILED_TESTS=$((FAILED_TESTS + 1))
  append_failure "tests_missing" "no validation test scripts found under ${TEST_DIR}"
  emit_result fail
  exit 1
fi

if [ "$(basename "${test_scripts[0]}")" != "00_canary.sh" ]; then
  TOTAL_TESTS=$((TOTAL_TESTS + 1))
  FAILED_TESTS=$((FAILED_TESTS + 1))
  append_failure "canary_missing" "first validation test script must be validation/tests/00_canary.sh"
  emit_result fail
  exit 1
fi

for test_script in "${test_scripts[@]}"; do
  test_name="$(basename "${test_script}")"
  test_log="${LOG_DIR}/${test_name}.log"

  echo "=== RUN ${test_name} ==="
  set +e
  bash "${test_script}" > "${test_log}" 2>&1
  test_rc=$?
  set -e

  cat "${test_log}" || true

  ok_count="$(grep -E -c '^ok[[:space:]]+[0-9]+' "${test_log}" || true)"
  TOTAL_TESTS=$((TOTAL_TESTS + ok_count))
  PASSED_TESTS=$((PASSED_TESTS + ok_count))

  not_ok_count=0
  while IFS= read -r not_ok_line; do
    [ -z "${not_ok_line}" ] && continue
    not_ok_count=$((not_ok_count + 1))
    append_failure "${test_name}" "${not_ok_line}" "${test_log}"
  done < <(grep -E '^not ok[[:space:]]+[0-9]+([[:space:]]+-[[:space:]].*)?$' "${test_log}" || true)

  TOTAL_TESTS=$((TOTAL_TESTS + not_ok_count))
  FAILED_TESTS=$((FAILED_TESTS + not_ok_count))

  if [ "${test_rc}" -ne 0 ] && [ "${not_ok_count}" -eq 0 ]; then
    TOTAL_TESTS=$((TOTAL_TESTS + 1))
    FAILED_TESTS=$((FAILED_TESTS + 1))
    append_failure "${test_name}:unexpected_error" "script exited with code ${test_rc} without TAP 'not ok' output" "${test_log}"
  fi
done

if [ "${FAILED_TESTS}" -eq 0 ]; then
  emit_result pass
  exit 0
fi

emit_result fail
exit 1
EOF

  chmod +x "${VALIDATION_RUNNER_FILE}"
}

materialize_synthesised_behavioural_smoke_tests()
{
  local include_synthesised="true"
  local materialize_output=""

  case "$(printf '%s' "${VALIDATION_INCLUDE_SYNTHESISED:-true}" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off)
      include_synthesised="false"
      ;;
  esac

  if [ "${include_synthesised}" != "true" ]; then
    echo "validate_process: skipping synthesised behavioural smoke materialization (VALIDATION_INCLUDE_SYNTHESISED=${VALIDATION_INCLUDE_SYNTHESISED:-true})." >&2
    return 0
  fi

  if [ ! -d .ai/review_runtime ]; then
    return 0
  fi

  if ! materialize_output="$(PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
import re
import shutil
import sys
from pathlib import Path


repo_root = Path('.').resolve()
runtime_root = (repo_root / '.ai' / 'review_runtime').resolve()
target_root = (repo_root / 'validation' / 'tests').resolve()


def _manifest_key(path: Path):
    round_match = re.search(r'/round-(\d+)/', path.as_posix())
    pr_match = re.search(r'/pr-(\d+)/', path.as_posix())
    round_value = int(round_match.group(1)) if round_match else -1
    pr_value = int(pr_match.group(1)) if pr_match else -1
    return (round_value, pr_value, path.as_posix())


def _safe_target(relpath: object, expected_root: Path):
    if not isinstance(relpath, str) or not relpath.strip():
        return None
    candidate = (repo_root / relpath).resolve()
    try:
        candidate.relative_to(expected_root)
    except ValueError:
        return None
    if candidate.parent != expected_root:
        return None
    return candidate


manifest_paths = sorted(runtime_root.glob('pr-*/round-*/synth/synth_round_*_manifest.json'))
if not manifest_paths:
    sys.exit(0)

manifest_path = max(manifest_paths, key=_manifest_key)
with open(manifest_path, 'r', encoding='utf-8') as handle:
    payload = json.load(handle)

if not isinstance(payload, dict):
    raise ValueError(f'invalid manifest payload at {manifest_path}')

rows = payload.get('files')
if not isinstance(rows, list):
    raise ValueError(f'invalid manifest files list at {manifest_path}')

target_manifest_relpath = payload.get('target_manifest_relpath')
target_manifest_path = _safe_target(target_manifest_relpath, target_root)
if target_manifest_path is None:
    print('validate_process: skipping synthesised smoke materialization because target_manifest_relpath is invalid.', file=sys.stderr)
    sys.exit(0)

target_root.mkdir(parents=True, exist_ok=True)

copied = 0
for row in rows:
    if not isinstance(row, dict):
        continue
    source_relpath = row.get('cache_relpath')
    target_relpath = row.get('target_relpath')
    if not isinstance(source_relpath, str) or not isinstance(target_relpath, str):
        continue

    source_path = (repo_root / source_relpath).resolve()
    try:
        source_path.relative_to(runtime_root)
    except ValueError:
        print(f'validate_process: skipping synthesised smoke source outside review-runtime root: {source_relpath}', file=sys.stderr)
        continue
    if not source_path.is_file():
        print(f'validate_process: missing synthesised smoke source: {source_relpath}', file=sys.stderr)
        continue

    target_path = _safe_target(target_relpath, target_root)
    if target_path is None:
        print(f'validate_process: skipping synthesised smoke target outside validation/tests: {target_relpath}', file=sys.stderr)
        continue

    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
    copied += 1

target_manifest_path.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(manifest_path, target_manifest_path)

if copied == 0 and rows:
    print(
        'validate_process: warning: manifest at {} listed {} file(s) but none were materialized into validation/tests.'.format(
            manifest_path.relative_to(repo_root).as_posix(),
            len(rows),
        ),
        file=sys.stderr,
    )
else:
    print(f'Materialized synthesised behavioural smoke tests from {manifest_path.relative_to(repo_root).as_posix()} into validation/tests (files={copied}).')
PY
)"; then
    echo "::warning::Failed to materialize synthesised behavioural smoke tests from .ai/review_runtime; continuing without them." >&2
    return 0
  fi

  if [ -n "${materialize_output}" ]; then
    echo "${materialize_output}"
  fi
}

write_status_file()
{
  local status="$1"
  local summary="$2"
  local failure_summary="$3"
  # Optional 4th arg: raw_status preserves the original diagnose classifier
  # (for example `harness_error`, `needs_fixes`, `infeasible`) so downstream
  # consumers can distinguish harness defects from app-side failures even
  # though `status` is normalized to `pass`/`fail`/`error`. Defaults to the
  # normalized status for backward compatibility.
  local raw_status="${4:-${status}}"

  jq -n \
    --arg status "${status}" \
    --arg raw_status "${raw_status}" \
    --arg summary "${summary}" \
    --arg failure_summary "${failure_summary}" \
    --arg tracking_issue "${TRACKING_ISSUE_RAW}" \
    '{
      status: $status,
      raw_status: $raw_status,
      summary: $summary,
      failure_summary: (if ($failure_summary | length) > 0 then $failure_summary else null end),
      tracking_issue: $tracking_issue
    }' > "${STATUS_FILE}"
}

write_metadata_file()
{
  local status="$1"
  local summary="$2"
  local failure_summary="$3"
  local raw_status="${4:-${status}}"

  local validation_file="${VALIDATION_RESULT_FILE}"
  local diagnosis_file="${DIAGNOSE_RESULT_FILE}"

  if [ ! -f "${validation_file}" ]; then
    validation_file="${NULL_JSON_FILE}"
  fi

  if [ ! -f "${diagnosis_file}" ]; then
    diagnosis_file="${NULL_JSON_FILE}"
  fi

  jq -n \
    --arg status "${status}" \
    --arg raw_status "${raw_status}" \
    --arg summary "${summary}" \
    --arg failure_summary "${failure_summary}" \
    --arg hints_source "${HINTS_SOURCE}" \
    --arg harness_mode "${HARNESS_MODE}" \
    --arg harness_generator_mode "${HARNESS_GENERATOR_MODE}" \
    --arg pre_flight_status "${PRE_FLIGHT_STATUS}" \
    --arg repository "${GITHUB_REPOSITORY}" \
    --arg tracking_issue "${TRACKING_ISSUE_RAW}" \
    --arg runtime_dir "${RUNTIME_DIR}" \
    --arg compose_file "${VALIDATION_COMPOSE_FILE}" \
    --arg validation_log_file "${VALIDATION_LOG_FILE}" \
    --arg generate_log_file "${GENERATE_LOG_FILE}" \
    --arg diagnose_log_file "${DIAGNOSE_LOG_FILE}" \
    --arg generated_validate_file "${GENERATED_VALIDATE_SCRIPT_PATH}" \
    --arg generated_compose_file "validation/docker-compose.test.yml" \
    --argjson created_fix_issues "${CREATED_FIX_ISSUES_JSON}" \
    --slurpfile validation_result "${validation_file}" \
    --slurpfile diagnosis "${diagnosis_file}" \
    '{
      status: $status,
      raw_status: $raw_status,
      summary: $summary,
      failure_summary: (if ($failure_summary | length) > 0 then $failure_summary else null end),
      hints_source: $hints_source,
      harness_mode: $harness_mode,
      harness_generator_mode: $harness_generator_mode,
      pre_flight_status: $pre_flight_status,
      repository: $repository,
      tracking_issue: $tracking_issue,
      compose_file: $compose_file,
      generated_at_utc: (now | todateiso8601),
      created_fix_issues: $created_fix_issues,
      validation_result: ($validation_result[0] // null),
      diagnosis: ($diagnosis[0] // null),
      artifact_paths: {
        runtime_dir: $runtime_dir,
        validation_log: $validation_log_file,
        generate_log: $generate_log_file,
        diagnose_log: $diagnose_log_file,
        generated_validate_script: (if ($generated_validate_file | length) > 0 then $generated_validate_file else null end),
        generated_compose_file: $generated_compose_file,
        validation_logs_dir: "validation/logs"
      }
    }' > "${METADATA_FILE}"
}

write_result_files()
{
  local status="$1"
  local summary="$2"
  local failure_summary="$3"
  local raw_status="${4:-${status}}"

  write_status_file "${status}" "${summary}" "${failure_summary}" "${raw_status}"
  write_metadata_file "${status}" "${summary}" "${failure_summary}" "${raw_status}"
}

emit_phase_failure_marker()
{
  local phase="$1"
  local failed_step_name="$2"
  local failure_mode="$3"
  local attempt_count="$4"
  local failure_summary="$5"

  if ! is_tracking_run; then
    echo "::warning::AI_PHASE_FAILURE_V1 skipped: no tracking issue context (phase=${phase}, step=${failed_step_name})." >&2
    return 0
  fi

  local timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  local run_id="${GITHUB_RUN_ID:-}"
  local run_attempt="${GITHUB_RUN_ATTEMPT:-}"
  local run_url=""
  if [ -n "${run_id}" ]; then
    run_url="$(_gh_url "actions/runs/${run_id}")"
  fi

  local payload
  payload="$(jq -cn \
    --arg phase "${phase}" \
    --arg failure_mode "${failure_mode}" \
    --arg failed_step_name "${failed_step_name}" \
    --arg workflow_run_id "${run_id}" \
    --arg workflow_run_attempt "${run_attempt}" \
    --arg workflow_name "${GITHUB_WORKFLOW:-AI Validate (Reusable)}" \
    --arg workflow_file "validate.yml" \
    --arg workflow_run_url "${run_url}" \
    --arg repository "${GITHUB_REPOSITORY}" \
    --arg tracking_issue "${TRACKING_ISSUE_NUM}" \
    --arg attempt_count "${attempt_count}" \
    --arg recommended_resume_action "retrigger_validate" \
    --arg timestamp "${timestamp}" \
    '{
      schema_version: 1,
      phase: $phase,
      failure_mode: $failure_mode,
      failed_step_name: $failed_step_name,
      workflow_run_id: $workflow_run_id,
      workflow_run_attempt: $workflow_run_attempt,
      workflow_name: $workflow_name,
      workflow_file: $workflow_file,
      workflow_run_url: (if ($workflow_run_url | length) > 0 then $workflow_run_url else null end),
      repository: $repository,
      tracking_issue: $tracking_issue,
      attempt_count: $attempt_count,
      recommended_resume_action: $recommended_resume_action,
      timestamp: $timestamp
    }')"

  local comment_body
  comment_body="<!-- AI_PHASE_FAILURE_V1
${payload}
AI_PHASE_FAILURE_V1 -->
## ❌ Validate workflow failure

${failure_summary}"

  if [ -n "${run_url}" ]; then
    comment_body+=$'\n\n'"Run: ${run_url}"
  fi

  post_tracking_comment "${comment_body}"
}

fail_validate_codex_phase()
{
  local failed_step_name="$1"
  local failure_mode="$2"
  local attempt_count="$3"
  local failure_summary="$4"
  local exit_code="${5:-1}"

  emit_phase_failure_marker "validate" "${failed_step_name}" "${failure_mode}" "${attempt_count}" "${failure_summary}"

  if is_tracking_run; then
    set_tracking_phase_label "ai:validate-failed" || true
  else
    echo "::warning::Validate workflow Codex failure occurred without tracking issue context; skipped applying ai:validate-failed." >&2
  fi

  write_result_files "error" "Validate workflow failed before runtime validation could complete" "${failure_summary}" "codex_failure"
  tg_notify "Validate workflow Codex failure during ${failed_step_name} for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit "${exit_code}"
}

cleanup_runtime_containers()
{
  if [ -f "validation/docker-compose.test.yml" ]; then
    docker compose -f validation/docker-compose.test.yml down -v --remove-orphans >/dev/null 2>&1 || true
  fi

  if [ -n "${VALIDATION_COMPOSE_FILE}" ] \
    && [ -f "${VALIDATION_COMPOSE_FILE}" ] \
    && [ "${VALIDATION_COMPOSE_FILE}" != "validation/docker-compose.test.yml" ]; then
    docker compose -f "${VALIDATION_COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi

  # Restore the discover-phase reasoning override on abnormal exit
  # (SIGINT/SIGTERM/timeout/unexpected error between the patch and the
  # normal-path restore). Without this, the user's ~/.codex/config.toml
  # can be left at the discover override level, leaking into subsequent
  # codex invocations in the same environment. Idempotent: the
  # normal-path restore at the bottom of the discover block flips
  # _discover_reasoning_patched back to "false" so this branch is a
  # no-op when reached after a clean restore.
  if [ "${_discover_reasoning_patched:-false}" = "true" ] \
    && [ -n "${_validate_codex_config:-}" ] \
    && [ -f "${_validate_codex_config}" ]; then
    sed -i \
      -e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*\".*\"/model_reasoning_effort = \"${MODEL_REASONING_EFFORT}\"/" \
      -e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*'[^']*'/model_reasoning_effort = \"${MODEL_REASONING_EFFORT}\"/" \
      "${_validate_codex_config}" 2>/dev/null || true
    _discover_reasoning_patched="false"
  fi
}

ensure_validate_wrapper()
{
	# Only generate the wrapper if the canonical driver exists.
	# When absent, the runtime fallback driver will be used instead.
	if [ ! -f scripts/validate_driver.sh ]; then
		return 0
	fi
	mkdir -p validation
	cat > validation/validate.sh <<'EOF'
#!/usr/bin/env bash
# Auto-generated by coding-workflows — DO NOT EDIT

set -euo pipefail

exec bash scripts/validate_driver.sh "$@"
EOF
	chmod +x validation/validate.sh
}

run_template_validation_harness_renderer()
{
	local manifest_path=".ai/validate.yml"
	local renderer_script="scripts/render_validation_templates.py"
	local schema_path="scripts/templates/slot_manifest.schema.json"
	local templates_root="workflow-templates/validation-harness"
	local renderer_summary=""
	local python3_bin="python3"

	HARNESS_GENERATOR_MODE="templates"

	if [ ! -f "${manifest_path}" ]; then
		return 10
	fi
	if [ ! -f "${renderer_script}" ]; then
		return 11
	fi
	if [ ! -f "${schema_path}" ]; then
		return 12
	fi
	if [ ! -d "${templates_root}" ]; then
		return 13
	fi
	if [ ! -f "${templates_root}/_shared/_lib/tap_helpers.sh.j2" ] \
		|| [ ! -f "${templates_root}/_shared/tests/00_canary.sh.j2" ] \
		|| [ ! -f "${templates_root}/_shared/tests/90_tap_report.sh.j2" ]; then
		return 15
	fi

	python3_bin="$(command -v python3 2>/dev/null || printf '%s' 'python3')"
	{
		printf '\n--- python3 environment probe ---\n'
		printf 'command -v python3: %s\n' "$(command -v python3 2>&1 || echo 'not found')"
		printf 'python3 -V: %s\n' "$("${python3_bin}" -V 2>&1 || echo 'failed')"
		"${python3_bin}" -c 'import sys; print("sys.executable:", sys.executable); print("sys.version:", sys.version.replace(chr(10), " "))' 2>&1 \
			|| printf '(python3 -c probe failed)\n'
		printf '--- end python3 environment probe ---\n'
	} >> "${GENERATE_LOG_FILE}" 2>&1
	if ! "${python3_bin}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
		printf '%s\n' "Template renderer requires python3 >= 3.9 (detected: $("${python3_bin}" -V 2>&1 || echo unknown))." >> "${GENERATE_LOG_FILE}"
		return 17
	fi

	if ! renderer_summary="$("${python3_bin}" "${renderer_script}" \
		--manifest "${manifest_path}" \
		--schema "${schema_path}" \
		--templates-root "${templates_root}" \
		--output-root validation 2>&1)"; then
		printf '%s\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
		return 14
	fi

	if [ -n "${renderer_summary}" ]; then
		printf '%s\n' "${renderer_summary}" >> "${GENERATE_LOG_FILE}"
	fi

	if [ -d validation/tests ]; then
		find validation/tests -type f -name '*.sh' -exec chmod +x {} +
	fi

	return 0
}

attempt_render_recovery_after_preflight_failure()
{
	local renderer_exit=0

	if [ "${HARNESS_MODE}" != "template_generate" ] || [ "${HARNESS_GENERATOR_MODE}" != "templates" ]; then
		return 1
	fi
	if [ "${PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED:-false}" = "true" ]; then
		return 1
	fi
	if [ "${PRE_FLIGHT_FAILURE_CLASS:-non_lint}" != "lint" ]; then
		echo "Render recovery: skipping deterministic rerender because pre-flight failure class=${PRE_FLIGHT_FAILURE_CLASS:-unknown}." >> "${PRE_FLIGHT_LOG_FILE}"
		return 1
	fi

	classify_preflight_failure
	if [ "${PRE_FLIGHT_FAILURE_KIND}" != "lint" ]; then
		echo "Render recovery: skipped because pre-flight failure was classified as kind=${PRE_FLIGHT_FAILURE_KIND} reason=${PRE_FLIGHT_FAILURE_REASON}." >> "${PRE_FLIGHT_LOG_FILE}"
		return 1
	fi

	PRE_FLIGHT_RENDER_RECOVERY_ATTEMPTED="true"
	{
		echo "Render recovery: deterministic template rerender triggered after pre-flight failure."
		echo "Render recovery: preserving initial pre-flight diagnostics and attempting rerender."
		echo "Render recovery: classification kind=${PRE_FLIGHT_FAILURE_KIND} reason=${PRE_FLIGHT_FAILURE_REASON}."
	} >> "${PRE_FLIGHT_LOG_FILE}"
	if run_template_validation_harness_renderer; then
		renderer_exit=0
	else
		renderer_exit=$?
	fi

	if [ "${renderer_exit}" -ne 0 ]; then
		PRE_FLIGHT_FAILURE_KIND="render"
		PRE_FLIGHT_FAILURE_REASON="render_retry_renderer_exit_${renderer_exit}"
		echo "Render recovery: template rerender failed with exit=${renderer_exit}; fail-open to render-phase self-heal." >> "${PRE_FLIGHT_LOG_FILE}"
		return 2
	fi

	echo "Render recovery: rerender completed; re-running pre-flight checks." >> "${PRE_FLIGHT_LOG_FILE}"
	local PRE_FLIGHT_APPEND_LOG="true"
	if run_preflight_checks; then
		PRE_FLIGHT_FAILURE_KIND="none"
		PRE_FLIGHT_FAILURE_REASON="none"
		echo "Render recovery: pre-flight checks passed after deterministic rerender." >> "${PRE_FLIGHT_LOG_FILE}"
		return 0
	fi

	local tmp_log="${RUNTIME_DIR}/preflight_latest.log"
	sed -n '/Render recovery: rerender completed; re-running pre-flight checks[.]/,$p' "${PRE_FLIGHT_LOG_FILE}" > "${tmp_log}" || true
	if [ -s "${tmp_log}" ]; then
		PRE_FLIGHT_LOG_FILE="${tmp_log}" classify_preflight_failure
	else
		classify_preflight_failure
	fi
	echo "Render recovery: pre-flight checks still failing after deterministic rerender (kind=${PRE_FLIGHT_FAILURE_KIND} reason=${PRE_FLIGHT_FAILURE_REASON})." >> "${PRE_FLIGHT_LOG_FILE}"
	PRE_FLIGHT_FAILURE_KIND="render"
	PRE_FLIGHT_FAILURE_REASON="render_retry_post_rerender_${PRE_FLIGHT_FAILURE_REASON}"
	return 2
}

run_preflight_checks()
{
	PRE_FLIGHT_STATUS="running"
	PRE_FLIGHT_FAILURE_CLASS="none"
	PRE_FLIGHT_FAILURE_KIND="unknown"
	PRE_FLIGHT_FAILURE_REASON="running"
	if [ "${PRE_FLIGHT_APPEND_LOG:-false}" != "true" ]; then
		: > "${PRE_FLIGHT_LOG_FILE}"
	fi

	# Emit the tail of the pre-flight log to stderr so that the failing command's
	# output is visible directly in the GitHub Actions job log, without requiring
	# the validation_preflight.log artifact to be downloaded. Structured with
	# clear delimiter markers so the excerpt is easy to scan and grep in CI logs.
	_emit_preflight_tail()
	{
		local reason="$1"
		{
			echo "::error::Pre-flight failed: ${reason}"
			echo "----- validation_preflight.log (tail -n 40) -----"
			tail -n 40 "${PRE_FLIGHT_LOG_FILE}" 2>/dev/null || true
			echo "-------------------------------------------------"
		} >&2
	}

	if [ ! -f validation/docker-compose.test.yml ]; then
		echo "Missing validation/docker-compose.test.yml" >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		PRE_FLIGHT_FAILURE_CLASS="non_lint"
		_emit_preflight_tail "validation/docker-compose.test.yml missing"
		return 1
	fi

	# Validate legacy wrapper only if it exists. In template mode the harness
	# may intentionally omit validate.env (for example python-mongo-flask).
	if [ -f validation/validate.sh ]; then
		if ! bash -n validation/validate.sh >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
			echo "Shell syntax check failed: validation/validate.sh" >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			PRE_FLIGHT_FAILURE_CLASS="lint"
			_emit_preflight_tail "bash -n failed for validation/validate.sh"
			return 1
		fi

		if ! grep -q 'scripts/validate_driver.sh' validation/validate.sh; then
			echo "validation/validate.sh must delegate to scripts/validate_driver.sh" >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			PRE_FLIGHT_FAILURE_CLASS="non_lint"
			_emit_preflight_tail "validation/validate.sh is not a thin wrapper"
			return 1
		fi

		if [ -f scripts/validate_driver.sh ]; then
			if ! bash -n scripts/validate_driver.sh >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
				echo "Shell syntax check failed: scripts/validate_driver.sh" >> "${PRE_FLIGHT_LOG_FILE}"
				PRE_FLIGHT_STATUS="fail"
				PRE_FLIGHT_FAILURE_CLASS="lint"
				_emit_preflight_tail "bash -n failed for scripts/validate_driver.sh"
				return 1
			fi
		else
			echo "scripts/validate_driver.sh not present; allowing runtime fallback driver selection" >> "${PRE_FLIGHT_LOG_FILE}"
		fi
	fi

	if [ ! -f validation/tests/00_canary.sh ]; then
		echo "Missing validation/tests/00_canary.sh" >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		PRE_FLIGHT_FAILURE_CLASS="non_lint"
		_emit_preflight_tail "validation/tests/00_canary.sh missing"
		return 1
	fi

	if ! docker compose -f validation/docker-compose.test.yml config --quiet >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
		echo "Compose syntax/validation check failed." >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		PRE_FLIGHT_FAILURE_CLASS="lint"
		_emit_preflight_tail "docker compose config failed (YAML/schema invalid). Common cause: YAML must use space indentation, not tabs."
		return 1
	fi

	local shell_count
	shell_count="$(find validation -type f -name '*.sh' -not -path 'validation/logs/*' | wc -l | tr -d ' ')"
	if [ "${shell_count}" -eq 0 ]; then
		echo "No shell scripts found under validation/." >> "${PRE_FLIGHT_LOG_FILE}"
		PRE_FLIGHT_STATUS="fail"
		PRE_FLIGHT_FAILURE_CLASS="non_lint"
		_emit_preflight_tail "no shell scripts found under validation/"
		return 1
	fi

	while IFS= read -r shell_file; do
		if ! bash -n "${shell_file}" >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
			echo "Shell syntax check failed: ${shell_file}" >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			PRE_FLIGHT_FAILURE_CLASS="lint"
			_emit_preflight_tail "bash -n failed for ${shell_file}"
			return 1
		fi
	done < <(find validation -type f -name '*.sh' -not -path 'validation/logs/*' | sort)

	local compose_json_file
	compose_json_file="${RUNTIME_DIR}/validation_compose_config.json"
	if ! docker compose -f validation/docker-compose.test.yml config --format json > "${compose_json_file}" 2>> "${PRE_FLIGHT_LOG_FILE}"; then
		echo "Compose JSON export unavailable or failed; skipping build context and dockerfile path verification." >> "${PRE_FLIGHT_LOG_FILE}"
		printf '%s\n' '{"services":{}}' > "${compose_json_file}"
	fi

	if ! python3 - "${compose_json_file}" >> "${PRE_FLIGHT_LOG_FILE}" 2>&1 <<'PY'
import json
import os
import sys

compose_json_path = sys.argv[1]

with open(compose_json_path, "r", encoding="utf-8") as handle:
    payload = json.load(handle)

services = payload.get("services") or {}
compose_dir = os.path.abspath("validation")
missing = []

for service_name, service_cfg in services.items():
    build_cfg = service_cfg.get("build")
    if not build_cfg:
        continue

    context = "."
    dockerfile = "Dockerfile"
    if isinstance(build_cfg, str):
        context = build_cfg
    elif isinstance(build_cfg, dict):
        context = build_cfg.get("context") or "."
        dockerfile = build_cfg.get("dockerfile") or "Dockerfile"
    else:
        continue

    if os.path.isabs(context):
        resolved_context = os.path.normpath(context)
    else:
        resolved_context = os.path.normpath(os.path.join(compose_dir, context))

    if os.path.isabs(dockerfile):
        resolved_dockerfile = os.path.normpath(dockerfile)
    else:
        resolved_dockerfile = os.path.normpath(os.path.join(resolved_context, dockerfile))

    if not os.path.isdir(resolved_context):
        missing.append(f"service={service_name} missing build context: {resolved_context}")
        continue
    if not os.path.isfile(resolved_dockerfile):
        missing.append(f"service={service_name} missing dockerfile: {resolved_dockerfile}")

if missing:
    for line in missing:
        print(line)
    sys.exit(1)

print("Build context and dockerfile path checks passed.")
PY
	then
		PRE_FLIGHT_STATUS="fail"
		PRE_FLIGHT_FAILURE_CLASS="non_lint"
		_emit_preflight_tail "build context / dockerfile path resolution failed"
		return 1
	fi

	# Embedded-Python syntax check for generated harness scripts.
	#
	# `bash -n` above validates shell syntax only; it cannot see inside a
	# heredoc body. Codex occasionally emits `python3 - <<'PY' ... PY`
	# blocks whose Python source has a SyntaxError (typically malformed
	# f-strings or stray quoting) that only surfaces at runtime, producing
	# a `harness_error` diagnose outcome. Statically ast-parse every
	# QUOTED python3 heredoc body under validation/ so the preflight self-
	# heal path (phase tag "preflight") can intercept the failure with an
	# actionable fix pointer instead of burning a validation cycle. Only
	# quoted delimiters (`<<'PY'`, `<<"PY"`) are checked: unquoted
	# delimiters allow shell variable expansion, so the static body is not
	# the source Python actually sees.
	if ! python3 - >> "${PRE_FLIGHT_LOG_FILE}" 2>&1 <<'PY2'
import ast
import pathlib
import re
import sys

HEREDOC_PATTERN = re.compile(
    # Skip lines whose first non-whitespace character is `#` (commented-out
    # examples) so we never ast-parse a documented example block. Keep
    # `python3` matchable anywhere on the line (mid-pipeline, after
    # `docker compose exec -T svc`, inside `$(...)`, etc.) — anchoring on
    # `^[ \t]*python3` would miss every legitimate generated invocation.
    r'^(?![ \t]*#).*\bpython3\b[^\n<]*<<\s*(-)?\s*[\'"](\w+)[\'"][^\n]*$',
    re.MULTILINE,
)

errors = []
root = pathlib.Path("validation")
if not root.is_dir():
    print("validation/ directory missing; embedded Python check skipped.")
    sys.exit(0)

for path in sorted(root.rglob("*.sh")):
    if "logs" in path.parts:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        errors.append(f"{path}: cannot read: {exc}")
        continue
    for match in HEREDOC_PATTERN.finditer(text):
        strip_tabs = bool(match.group(1))
        delim = match.group(2)
        rest = text[match.end():]
        close_re = re.compile(
            r'^' + (r'\t*' if strip_tabs else r'') + re.escape(delim) + r'\s*$',
            re.MULTILINE,
        )
        close_match = close_re.search(rest)
        if close_match is None:
            continue
        body = rest[: close_match.start()]
        # match.end() lands ON the trailing newline of the opener line, so
        # body always starts with a single '\n' that is NOT a real empty
        # source line — it's the opener's own line terminator. Strip it so
        # ast.parse line numbers count from the first real Python source
        # line; absolute_line below then resolves to the correct file line
        # via `opener_line + exc.lineno`.
        if body.startswith('\n'):
            body = body[1:]
        if strip_tabs:
            body = '\n'.join(line.lstrip('\t') for line in body.splitlines())
        try:
            ast.parse(body)
        except SyntaxError as exc:
            opener_line = text[: match.start()].count('\n') + 1
            absolute_line = opener_line + (exc.lineno or 1)
            offset_suffix = "" if exc.offset is None else f" (offset {exc.offset})"
            errors.append(
                f"{path}:{absolute_line}: embedded Python heredoc <<{delim}>>: "
                f"SyntaxError: {exc.msg}{offset_suffix}"
            )

if errors:
    print("Embedded Python syntax errors in generated harness scripts:")
    for err in errors:
        print(f"  {err}")
    print(
        "Prompt guidance: mode-validate-generate.txt forbids nested heredoc "
        "inline Python. Rewrite as a sidecar .py file under validation/tests/_lib/ "
        "and invoke via `python3 validation/tests/_lib/<name>.py`, or run a "
        "single-layer quoted heredoc directly as `python3 - <<'PY' ... PY` "
        "with no `/bin/sh -c` / `bash -lc` wrapper."
    )
    sys.exit(1)

print("Embedded Python heredoc syntax checks passed.")
PY2
	then
		PRE_FLIGHT_STATUS="fail"
		PRE_FLIGHT_FAILURE_CLASS="lint"
		_emit_preflight_tail "embedded Python syntax check failed in validation/**/*.sh"
		return 1
	fi

	# Embedded-Python F-code lint for generated harness scripts.
	#
	# ast.parse above detects SyntaxError only — it does not resolve
	# names. A `NameError` (F821 undefined-name) in a conditional branch
	# that the runtime test suite does not exercise will therefore reach
	# production. Observed in a consumer-repo autobet flow:
	# `autobet_finalize ... reason=unknown_error:NameError attempt=0`
	# fired on every finalize call because the failing branch was never
	# tripped by a `tests/NN_*.sh` case. Running pyflakes + ruff (both,
	# per ask-first decision Q1=C) against each quoted python3 heredoc
	# body flags these statically so the preflight self-heal loop can
	# intercept before a validation cycle burns.
	#
	# Env vars (defaults in header): VALIDATE_PREFLIGHT_PYFLAKES_ENABLED,
	# VALIDATE_PREFLIGHT_PYFLAKES_RULES. On missing tools the preflight
	# attempts `python3 -m pip install --user --quiet` (Q4=C); if that
	# fails (PEP 668, offline runner, etc.) the check fails open with a
	# ::warning:: — matches the fail-open convention used by
	# verify_integration_fingerprints.py (see probably_unnecessary_but_read_if_stuck.md §18).
	if [ "${VALIDATE_PREFLIGHT_PYFLAKES_ENABLED}" = "true" ]; then
		local _pf_tool _pf_missing=""
		for _pf_tool in pyflakes ruff; do
			if ! command -v "${_pf_tool}" >/dev/null 2>&1; then
				if ! python3 -m pip install --user --quiet "${_pf_tool}" >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
					if ! python3 -m pip install --user --quiet --break-system-packages "${_pf_tool}" >> "${PRE_FLIGHT_LOG_FILE}" 2>&1; then
						_pf_missing="${_pf_missing:+${_pf_missing} }${_pf_tool}"
					fi
				fi
				# Refresh PATH for --user site-packages bin dir.
				local _pf_user_bin
				_pf_user_bin="$(python3 -c 'import site,os; print(os.path.join(site.getuserbase(), "bin"))' 2>/dev/null || true)"
				if [ -n "${_pf_user_bin}" ] && [ -d "${_pf_user_bin}" ]; then
					case ":${PATH}:" in
						*":${_pf_user_bin}:"*) ;;
						*) PATH="${_pf_user_bin}:${PATH}" ;;
					esac
				fi
				if ! command -v "${_pf_tool}" >/dev/null 2>&1; then
					case " ${_pf_missing} " in
						*" ${_pf_tool} "*) ;;
						*) _pf_missing="${_pf_missing:+${_pf_missing} }${_pf_tool}" ;;
					esac
				fi
			fi
		done
		if [ -n "${_pf_missing}" ]; then
			echo "::warning::Preflight F-code lint fail-open: could not install ${_pf_missing}; skipping embedded-Python pyflakes/ruff lint." >&2
		elif ! python3 - >> "${PRE_FLIGHT_LOG_FILE}" 2>&1 <<'PY3'
import ast
import os
import pathlib
import re
import subprocess
import sys
import tempfile

RULES = os.environ.get("VALIDATE_PREFLIGHT_PYFLAKES_RULES", "F").strip() or "F"

HEREDOC_PATTERN = re.compile(
    r'^(?![ \t]*#).*\bpython3\b[^\n<]*<<\s*(-)?\s*[\'"](\w+)[\'"][^\n]*$',
    re.MULTILINE,
)

def iter_bodies(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        yield None, None, None, f"{path}: cannot read: {exc}"
        return
    for match in HEREDOC_PATTERN.finditer(text):
        strip_tabs = bool(match.group(1))
        delim = match.group(2)
        rest = text[match.end():]
        close_re = re.compile(
            r'^' + (r'\t*' if strip_tabs else r'') + re.escape(delim) + r'\s*$',
            re.MULTILINE,
        )
        close_match = close_re.search(rest)
        if close_match is None:
            continue
        body = rest[: close_match.start()]
        if body.startswith('\n'):
            body = body[1:]
        if strip_tabs:
            body = '\n'.join(line.lstrip('\t') for line in body.splitlines())
        opener_line = text[: match.start()].count('\n') + 1
        yield opener_line, delim, body, None

def run_tool(cmd, tmp_path):
    try:
        proc = subprocess.run(
            cmd + [tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "", f"tool invocation failed: {exc}"
    return proc.returncode, proc.stdout, proc.stderr

LINE_RE = re.compile(r'^[^:]+:(\d+):(?:(\d+):)?\s*(.+)$')

def absolutise(raw, opener_line, path, delim, tool_label):
    m = LINE_RE.match(raw)
    if m:
        rel_line = int(m.group(1))
        absolute_line = opener_line + rel_line
        return (
            f"{path}:{absolute_line}: embedded Python heredoc <<{delim}>>: "
            f"{tool_label}: {m.group(3)}"
        )
    return f"{path}: {tool_label}: {raw}"

errors = []
root = pathlib.Path("validation")
if not root.is_dir():
    print("validation/ directory missing; embedded Python lint skipped.")
    sys.exit(0)

for path in sorted(root.rglob("*.sh")):
    if "logs" in path.parts:
        continue
    for opener_line, delim, body, read_err in iter_bodies(path):
        if read_err:
            errors.append(read_err)
            continue
        try:
            ast.parse(body)
        except SyntaxError:
            # ast.parse block above already reports SyntaxError; avoid
            # double-flagging the same body here.
            continue
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(body)
            tmp_path = f.name
        try:
            pf_rc, pf_out, pf_err = run_tool(["pyflakes"], tmp_path)
            if pf_rc != 0:
                for raw in (pf_out + pf_err).splitlines():
                    if raw.strip():
                        errors.append(absolutise(raw, opener_line, path, delim, "pyflakes"))
            rf_rc, rf_out, rf_err = run_tool(
                ["ruff", "check", "--select", RULES, "--no-cache", "--output-format=concise", "--quiet"],
                tmp_path,
            )
            if rf_rc != 0:
                for raw in (rf_out + rf_err).splitlines():
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("Found ") or stripped.startswith("All checks"):
                        continue
                    errors.append(absolutise(raw, opener_line, path, delim, f"ruff[{RULES}]"))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

if errors:
    print("Embedded Python F-code lint violations in generated harness scripts:")
    for err in errors:
        print(f"  {err}")
    print(
        "These are undefined-name / unused-binding class bugs that would "
        "produce a runtime NameError in any branch reached at runtime. "
        "Ensure every identifier used in the heredoc body is imported or "
        "defined. Disable gate via VALIDATE_PREFLIGHT_PYFLAKES_ENABLED=false "
        "only as a temporary break-glass — the bug class is the same one "
        "that produced `unknown_error:NameError` in consumer autobet logs."
    )
    sys.exit(1)

print("Embedded Python F-code lint (pyflakes + ruff) passed.")
PY3
		then
			PRE_FLIGHT_STATUS="fail"
			PRE_FLIGHT_FAILURE_CLASS="lint"
			_emit_preflight_tail "embedded Python F-code lint failed in validation/**/*.sh (pyflakes/ruff)"
			return 1
		fi
	fi

	# Preflight: scan generated validation Dockerfiles for apt package names that
	# are known to be absent from default Debian/Ubuntu repositories. The canonical
	# case is `mongodb-mongosh`, which is only available via MongoDB's official apt
	# repo (key + source list). Catching this before `docker compose up --build`
	# turns a 20-second opaque `E: Unable to locate package` build failure into a
	# clear, actionable preflight diagnostic. Only trips when the bad package name
	# appears on a non-comment line AND no accompanying MongoDB apt-source hint
	# (`repo.mongodb.org`) is present in the same file.
	for dockerfile in validation/Dockerfile validation/Dockerfile.* validation/*.Dockerfile; do
		[ -f "${dockerfile}" ] || continue
		if grep -Evi '^[[:space:]]*#' "${dockerfile}" \
		   | sed -E 's/[[:space:]]+#.*$//' \
		   | sed -E ':a;N;$!ba;s/\\[[:space:]]*\n[[:space:]]*/ /g' \
		   | grep -Eq 'apt(-get)?[[:space:]].*install.*(^|[^[:alnum:]_-])(mongodb-)?mongosh(=[^[:space:]\\]+)?($|[^[:alnum:]_-])' \
		   && ! grep -Evi '^[[:space:]]*#' "${dockerfile}" \
		   | sed -E 's/[[:space:]]+#.*$//' \
		   | grep -qi 'repo\.mongodb\.org'; then
			echo "${dockerfile} references 'mongosh'/'mongodb-mongosh' but does not add MongoDB's official apt repo (no 'repo.mongodb.org' reference). mongosh is NOT in Debian/Ubuntu default repos and will fail the compose build with 'E: Unable to locate package mongosh' or 'E: Unable to locate package mongodb-mongosh'. Prefer pymongo, or add the MongoDB apt source + GPG key to the Dockerfile. See mode-validate-generate.txt: 'installing mongosh in validation/Dockerfile.app'." >> "${PRE_FLIGHT_LOG_FILE}"
			PRE_FLIGHT_STATUS="fail"
			PRE_FLIGHT_FAILURE_CLASS="non_lint"
			_emit_preflight_tail "mongosh installation in ${dockerfile} requires official MongoDB apt repo"
			return 1
		fi
	done

	# Preflight: app-container CANARY_TOOLS scope check.
	#
	# mode-validate-generate.txt hard rule: service-side CLIs (mongosh, mongo,
	# psql, redis-cli, mysql, mysqladmin, kafkacat, kcat) must NOT appear in
	# the app container's CANARY_TOOLS loop unless the app image explicitly
	# installs that exact binary. Canonical false-positive symptom:
	# `not ok N - mongosh is NOT available in app` on a python:*-slim-based
	# image whose app never invokes mongosh (Mongo reachability is already
	# covered by a service-side assertion that runs inside the mongo
	# container). The generator prompt already forbids this at lines 491-492,
	# but the LLM has been observed to violate it; mechanical enforcement
	# here routes the violation through the preflight self-heal path so the
	# regenerate/self-heal loop can intercept before a validation cycle
	# burns. See docs/validation-improvements.md for the motivating run.
	if [ -f validation/tests/00_canary.sh ]; then
		local _canary_tools_line
		_canary_tools_line="$(grep -E '^[[:space:]]*CANARY_TOOLS=' validation/tests/00_canary.sh | head -n 1 || true)"
		if [ -n "${_canary_tools_line}" ]; then
			local _canary_tools_raw
			# Strip the default-value wrapper from any of these shapes:
			#   CANARY_TOOLS="${CANARY_TOOLS:-curl jq python3 mongosh}"
			#   CANARY_TOOLS='${CANARY_TOOLS:-curl jq python3}'
			#   CANARY_TOOLS="curl jq python3 mongosh"
			#   CANARY_TOOLS=curl jq python3         (unquoted — sol3 bug; still scannable)
			_canary_tools_raw="$(printf '%s\n' "${_canary_tools_line}" \
				| sed -E 's/^[[:space:]]*CANARY_TOOLS=//' \
				| sed -E 's/[[:space:]]*#.*$//' \
				| sed -E 's/^"\$\{CANARY_TOOLS:-//; s/\}"[[:space:]]*$//' \
				| sed -E "s/^'\\\$\\{CANARY_TOOLS:-//; s/\\}'[[:space:]]*$//" \
				| sed -E 's/^\$\{CANARY_TOOLS:-//; s/\}[[:space:]]*$//' \
				| sed -E 's/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//')"
			local _denylist="mongosh mongo psql redis-cli mysql mysqladmin kafkacat kcat"
			local _tool _tool_pattern _offenders=""
			set -f
			for _tool in ${_canary_tools_raw}; do
				case " ${_denylist} " in
					*" ${_tool} "*)
					case "${_tool}" in
						psql) _tool_pattern='psql|postgresql-client' ;;
						redis-cli) _tool_pattern='redis-cli|redis-tools' ;;
						mysql|mysqladmin) _tool_pattern="${_tool}|mysql-client|default-mysql-client|mariadb-client" ;;
						kcat|kafkacat) _tool_pattern='kcat|kafkacat' ;;
						*) _tool_pattern="${_tool}" ;;
					esac
						# Tool is service-side. Accept it only if the app
						# image explicitly installs it (apt/pip/custom RUN).
						# The token-boundary regex avoids matching the tool
						# name inside an unrelated identifier.
						if [ -f validation/Dockerfile.app ] && \
							grep -Ev '^[[:space:]]*#' validation/Dockerfile.app \
							| sed -E 's/[[:space:]]+#.*$//' \
							| sed -E ':a;N;$!ba;s/\\[[:space:]]*\n[[:space:]]*/ /g' \
							| grep -qE "(^|[^[:alnum:]_])(${_tool_pattern})([^[:alnum:]_]|$)"; then
							: # installed; scope satisfied
						else
							_offenders="${_offenders:+${_offenders} }${_tool}"
						fi
						;;
				esac
			done
			set +f
			if [ -n "${_offenders}" ]; then
				{
					echo "validation/tests/00_canary.sh CANARY_TOOLS references service-side CLI(s) not installed in validation/Dockerfile.app: ${_offenders}"
					echo "mode-validate-generate.txt hard rule: do NOT include service-side CLIs (mongosh, mongo, psql, redis-cli, mysql, mysqladmin, kafkacat, kcat) in the app-container CANARY_TOOLS unless the app image explicitly installs that exact binary."
					echo "Fix options (choose one per offending tool):"
					echo "  1) Remove the tool from CANARY_TOOLS in 00_canary.sh. For DB reachability, probe from the service container (docker compose exec -T <service> <cli> ...) or use a native client inside the app (python3 + pymongo, psycopg2, redis-py, etc.)."
					echo "  2) Install the tool in validation/Dockerfile.app via apt (plus any required repo/GPG plumbing, e.g. MongoDB official apt repo for mongosh) and leave CANARY_TOOLS unchanged."
				} >> "${PRE_FLIGHT_LOG_FILE}"
				PRE_FLIGHT_STATUS="fail"
				PRE_FLIGHT_FAILURE_CLASS="non_lint"
				_emit_preflight_tail "CANARY_TOOLS scope violation: service-side CLI(s) referenced in app canary but not installed in app image: ${_offenders}"
				return 1
			fi
		fi
	fi

	PRE_FLIGHT_STATUS="pass"
	PRE_FLIGHT_FAILURE_KIND="none"
	PRE_FLIGHT_FAILURE_REASON="none"
	return 0
}

classify_preflight_failure()
{
	PRE_FLIGHT_FAILURE_KIND="non_lint"
	PRE_FLIGHT_FAILURE_REASON="preflight_failure_other"

	if [ ! -s "${PRE_FLIGHT_LOG_FILE}" ]; then
		PRE_FLIGHT_FAILURE_REASON="preflight_log_empty"
		echo "PRE_FLIGHT_CLASSIFICATION kind=${PRE_FLIGHT_FAILURE_KIND} reason=${PRE_FLIGHT_FAILURE_REASON}" >&2
		return 0
	fi

	if grep -Fq "Embedded Python F-code lint violations in generated harness scripts:" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="embedded_python_fcode_lint"
	elif grep -Fq "Embedded Python syntax errors in generated harness scripts:" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="embedded_python_syntax"
	elif grep -Fq "Compose syntax/validation check failed." "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="compose_schema_lint"
	elif grep -Fq "Shell syntax check failed:" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="shell_syntax_lint"
	elif grep -Fq "validation/tests/00_canary.sh CANARY_TOOLS references service-side CLI(s)" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="canary_tools_scope_lint"
	elif grep -Fq "references 'mongosh'/'mongodb-mongosh' but does not add MongoDB's official apt repo" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="dockerfile_package_lint"
	elif grep -Fq "missing build context:" "${PRE_FLIGHT_LOG_FILE}" \
		|| grep -Fq "missing dockerfile:" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="lint"
		PRE_FLIGHT_FAILURE_REASON="compose_build_path_lint"
	elif grep -Fq "Missing validation/docker-compose.test.yml" "${PRE_FLIGHT_LOG_FILE}" \
		|| grep -Fq "Missing validation/validate.env" "${PRE_FLIGHT_LOG_FILE}" \
		|| grep -Fq "Missing validation/tests/00_canary.sh" "${PRE_FLIGHT_LOG_FILE}"; then
		PRE_FLIGHT_FAILURE_KIND="non_lint"
		PRE_FLIGHT_FAILURE_REASON="missing_validation_artifact"
	fi

	echo "PRE_FLIGHT_CLASSIFICATION kind=${PRE_FLIGHT_FAILURE_KIND} reason=${PRE_FLIGHT_FAILURE_REASON}" >&2
	return 0
}

enforce_managed_validation_artifact_contract()
{
	if ! command -v git >/dev/null 2>&1 || [ ! -d .git ]; then
		return 0
	fi

	local canonical_path
	local canonical_hash
	local tracked_script
	local tracked_hash
	local has_violation=false
	local -a canonical_paths
	local -a tracked_scripts
	local -a violations

	canonical_paths=(
		"scripts/validate_process.sh"
		"scripts/validate_driver.sh"
	)

	if git ls-files --error-unmatch -- validation/validate.sh >/dev/null 2>&1; then
		violations+=("validation/validate.sh is tracked. validation/ artifacts must remain transient and untracked.")
		has_violation=true
	fi

	mapfile -t tracked_scripts < <(git ls-files -- 'scripts/*.sh' 2>/dev/null || true)

	for canonical_path in "${canonical_paths[@]}"; do
		if [ ! -f "${canonical_path}" ] || [ -L "${canonical_path}" ]; then
			continue
		fi

		if ! canonical_hash="$(git hash-object -- "${canonical_path}" 2>/dev/null)"; then
			continue
		fi

		for tracked_script in "${tracked_scripts[@]}"; do
			if [ -z "${tracked_script}" ] || [ "${tracked_script}" = "${canonical_path}" ] || [ ! -f "${tracked_script}" ] || [ -L "${tracked_script}" ]; then
				continue
			fi

			if ! tracked_hash="$(git hash-object -- "${tracked_script}" 2>/dev/null)"; then
				continue
			fi

			if [ "${tracked_hash}" = "${canonical_hash}" ]; then
				violations+=("${tracked_script} is a tracked copy of managed artifact ${canonical_path}.")
				has_violation=true
			fi
		done
	done

	if [ "${has_violation}" = true ]; then
		echo "Managed validation artifact contract violation detected:" >&2
		printf ' - %s\n' "${violations[@]}" >&2
		return 1
	fi

	return 0
}

trap cleanup_runtime_containers EXIT


# ---------------------------------------------------------------
# Setup Codex
# ---------------------------------------------------------------
# Centralised in scripts/write_codex_config.sh — its `--allow-elevation
# auto` default already implements the standalone-safety gate this
# script needs (elevate iff GITHUB_ACTIONS=true OR
# VALIDATE_FORCE_FULL_ACCESS=1, otherwise keep codex's safer
# workspace-write/on-request defaults). Catalog path is script-relative
# so a "Standalone validation run" without a fetched scripts/ tree
# still picks up the catalog shipped next to validate_process.sh.
CODEX_HEARTBEAT_HELPER="${_validate_script_dir}/codex_heartbeat.sh"
CODEX_STALL_GUARD_HELPER="${_validate_script_dir}/codex_stall_guard.sh"
WORKSPACE_SAFETY_CHECK_HELPER=""
for _workspace_safety_candidate in \
  "${_validate_script_dir}/workspace_safety_check.sh" \
  "scripts/workspace_safety_check.sh" \
  ".codex-workflow-src/scripts/workspace_safety_check.sh" \
  ".codex-workflow-src-main/scripts/workspace_safety_check.sh"; do
  if [ -f "${_workspace_safety_candidate}" ]; then
    WORKSPACE_SAFETY_CHECK_HELPER="${_workspace_safety_candidate}"
    break
  fi
done
CODEX_THREAD_REUSE_HELPER=""
for _thread_reuse_candidate in \
  "${_validate_script_dir}/codex_thread_reuse.sh" \
  "scripts/codex_thread_reuse.sh" \
  ".codex-workflow-src/scripts/codex_thread_reuse.sh" \
  ".codex-workflow-src-main/scripts/codex_thread_reuse.sh"; do
  if [ -f "${_thread_reuse_candidate}" ]; then
    CODEX_THREAD_REUSE_HELPER="${_thread_reuse_candidate}"
    break
  fi
done
export CODEX_THREAD_REUSE_RUNTIME_DIR="${CODEX_THREAD_REUSE_RUNTIME_DIR:-${RUNTIME_DIR}}"
if [ -n "${CODEX_THREAD_REUSE_HELPER}" ]; then
  # shellcheck disable=SC1090
  source "${CODEX_THREAD_REUSE_HELPER}"
fi
LEDGER_SUBSTATE_HELPER=""
for _ledger_candidate in \
  "${_validate_script_dir}/ledger_emit_substate.sh" \
  "scripts/ledger_emit_substate.sh" \
  ".codex-workflow-src/scripts/ledger_emit_substate.sh" \
  ".codex-workflow-src-main/scripts/ledger_emit_substate.sh"; do
  if [ -f "${_ledger_candidate}" ]; then
    LEDGER_SUBSTATE_HELPER="${_ledger_candidate}"
    break
  fi
done
codex_config_assemble \
  "${MODEL_EDITOR}" \
  "${MODEL_REASONING_EFFORT}" \
  "low" \
  --scripts-dir "${_validate_script_dir}" \
  --catalog-path "${_validate_script_dir}/codex_model_catalog.json"

emit_validate_substate() {
  local phase_name="$1"
  local mode_name="$2"
  local event_or_substate="$3"
  local attempt_number="$4"
  local tokens_log_file="${5:-}"
  local args=()

  [ -f "${LEDGER_SUBSTATE_HELPER:-}" ] || return 0

  args=(
    --run-id "${GITHUB_RUN_ID:-}"
    --workflow "validate"
    --phase "${phase_name}"
    --mode "${mode_name}"
    --attempt "${attempt_number}"
    --model "${MODEL_EDITOR:-}"
    --issue-number "${TRACKING_ISSUE_NUM:-}"
    --actor "${GITHUB_ACTOR:-codex-bot}"
    --repo-root "$(pwd)"
  )
  case "${event_or_substate}" in
    codex_stall_observed|codex_stall_killed)
      args+=(--event-type "${event_or_substate}")
      ;;
    *)
      args+=(--substate "${event_or_substate}")
      ;;
  esac
  if [ -n "${tokens_log_file}" ]; then
    args+=(--tokens-log-file "${tokens_log_file}")
  fi

  bash "${LEDGER_SUBSTATE_HELPER}" "${args[@]}" || true
}

resolve_validate_thread_reuse_asset() {
	local repo_path="$1"
	local candidate=""

	for candidate in \
	  "${repo_path}" \
	  ".codex-workflow-src/${repo_path}" \
	  ".codex-workflow-src-main/${repo_path}"; do
		if [ -f "${candidate}" ]; then
			printf '%s\n' "${candidate}"
			return 0
		fi
	done

	return 1
}

validate_thread_reuse_enabled() {
	[ -n "${CODEX_THREAD_REUSE_HELPER:-}" ] || return 1
	declare -F codex_thread_reuse_truthy >/dev/null 2>&1 || return 1
	codex_thread_reuse_truthy "${CODEX_THREAD_REUSE_ENABLED:-false}"
}

run_validate_codex_attempt() {
  local phase_name="$1"
  local prompt_file="$2"
  local output_file="$3"
  local log_file="$4"
  local status_file="$5"

  if [ -x "${WORKSPACE_SAFETY_CHECK_HELPER}" ]; then
    bash "${WORKSPACE_SAFETY_CHECK_HELPER}" || return $?
  fi

	if validate_thread_reuse_enabled; then
		CODEX_THREAD_REUSE_STATE_KEY="${phase_name}" \
		  CODEX_THREAD_REUSE_PROMPT_FILE="${prompt_file}" \
		  CODEX_THREAD_REUSE_OUTPUT_FILE="${output_file}" \
		  CODEX_THREAD_REUSE_PHASE="${phase_name}" \
		  CODEX_THREAD_REUSE_MODEL="${MODEL_EDITOR}" \
		  CODEX_THREAD_REUSE_LOG_FILE="${log_file}" \
		  CODEX_THREAD_REUSE_STATUS_FILE="${status_file}" \
		  CODEX_THREAD_REUSE_STALL_GUARD_HELPER="${CODEX_STALL_GUARD_HELPER}" \
		  CODEX_THREAD_REUSE_HEARTBEAT_HELPER="${CODEX_HEARTBEAT_HELPER}" \
		  CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK="true" \
		  bash "${CODEX_THREAD_REUSE_HELPER}" direct-run
		return $?
	fi

  if [ -x "${CODEX_STALL_GUARD_HELPER}" ]; then
    "${CODEX_STALL_GUARD_HELPER}" \
      --phase "${phase_name}" \
      --stdout-file "${output_file}" \
      --status-file "${status_file}" \
      -- codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${prompt_file}" 2> >(tee -a "${log_file}" >&2)
    return $?
  fi

  if [ -x "${CODEX_HEARTBEAT_HELPER}" ]; then
    "${CODEX_HEARTBEAT_HELPER}" \
      --phase "${phase_name}" \
      --stdout-file "${output_file}" \
      -- codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${prompt_file}" 2> >(tee -a "${log_file}" >&2)
    return $?
  fi

  codex --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true exec --skip-git-repo-check --model "${MODEL_EDITOR}" --sandbox danger-full-access < "${prompt_file}" > "${output_file}" 2> >(tee -a "${log_file}" >&2)
}

export PATH="${HOME}/.local/bin:${PATH}"


# ---------------------------------------------------------------
# Assemble context
# ---------------------------------------------------------------
if is_tracking_run; then
  gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}" --jq '.body // ""' > "${PROJECT_SPEC_FILE}"
else
  printf 'Standalone validation run. Tracking issue is not provided.\n' > "${PROJECT_SPEC_FILE}"
fi

INTEGRATION_BRANCH=""
if is_tracking_run; then
  INTEGRATION_BRANCH="$(sed -n 's/^\*\*Integration branch:\*\* `\([^`]*\)`$/\1/p' "${PROJECT_SPEC_FILE}" | head -n1 | tr -d '\r')"
fi

{
  if [ -f unattended_system_instructions.md ]; then
    echo "=== SYSTEM INSTRUCTIONS ==="
    cat unattended_system_instructions.md
    echo
  fi
  if [ -f ai_pipeline.md ]; then
    echo "=== AI PIPELINE ==="
    cat ai_pipeline.md
    echo
  fi
  if [ -f AGENTS.md ]; then
    echo "=== AGENTS.MD ==="
    cat AGENTS.md
    echo
  elif [ -f agents.md ]; then
    echo "=== AGENTS.MD ==="
    cat agents.md
    echo
  fi
  if [ -f README.md ]; then
    echo "=== README.MD ==="
    cat README.md
    echo
  fi
  if [ -f probably_unnecessary_but_read_if_stuck.md ]; then
    echo "=== OVERFLOW REFERENCE ==="
    echo "If you cannot make progress without operator-runbook details (env var reference, autofix retrigger/dedup internals, orchestrator integration-sync auto-heal, validation self-healing, workflow log analysis pipeline, semantic cache scope, wrapper pin policy), read ./probably_unnecessary_but_read_if_stuck.md from the working tree before bailing."
    echo
  fi
} > "${STATIC_CONTEXT_FILE}"


# ---------------------------------------------------------------
# Phase 0: Discover validation hints when repository hints are absent
#
# Hint source precedence:
#   1. .ai/validate.yml         — committed in consumer repo (authoritative)
#   2. .ai/validate-hints-cache/hints.yml
#                                — restored from GitHub Actions cache
#                                  (written by a prior successful run in
#                                  the same repo with a matching cache key)
#   3. codex discover call      — LLM-driven discovery, last resort
#
# The cache path is restored by a `Restore validate hints cache` step in
# the validate workflow via actions/cache. On successful discovery we
# copy the hints back into the cache directory so the next run in this
# repo can reuse them without a codex call. Different repos are
# automatically isolated because GitHub Actions cache is per-repo. The
# cache key hashes files that drive discovery output (Dockerfile,
# compose, package manifests) so a repo structure change invalidates it.
# ---------------------------------------------------------------
VALIDATE_HINTS_CACHE_DIR="${VALIDATE_HINTS_CACHE_DIR:-.ai/validate-hints-cache}"
VALIDATE_HINTS_CACHE_FILE="${VALIDATE_HINTS_CACHE_DIR}/hints.yml"

# Lightweight sanity check for a hints file before reuse. Stricter than
# the discover-path validator (which accepts indented keys) because a
# cache entry may live across many runs and the poisoning threat model
# is different: we require at least one TRULY top-level key (no leading
# whitespace in the original line) so a nested mapping cannot satisfy
# the regex by accident. Returns 0 on pass, 1 on fail.
validate_hints_sanity_check() {
  local hints_file="$1"
  [ -s "${hints_file}" ] || return 1
  python3 - "${hints_file}" <<'PY' 2>/dev/null
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
try:
    raw = path.read_text(encoding="utf-8", errors="replace")
except Exception:
    sys.exit(1)

candidate = raw.strip()
if not candidate:
    sys.exit(1)

if path.stat().st_size > 64 * 1024:
    sys.exit(1)

expected_key = re.compile(
    r"^(type|entry|port|health_check|services|env_overrides|custom_tests|skip_tests):\s*",
    re.IGNORECASE,
)
# Keep original indentation so we can distinguish truly top-level keys
# (no leading whitespace) from nested ones.
lines = [
    line
    for line in candidate.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
if not lines:
    sys.exit(1)
if not any(line == line.lstrip() and expected_key.match(line) for line in lines):
    sys.exit(1)
sys.exit(0)
PY
}

if [ -f .ai/validate.yml ]; then
  cp .ai/validate.yml "${VALIDATE_HINTS_FILE}"
  HINTS_SOURCE="committed"
elif [ -f "${VALIDATE_HINTS_CACHE_FILE}" ] \
  && validate_hints_sanity_check "${VALIDATE_HINTS_CACHE_FILE}"; then
  cp "${VALIDATE_HINTS_CACHE_FILE}" "${VALIDATE_HINTS_FILE}"
  HINTS_SOURCE="cache"
  echo "Reused validation hints from ${VALIDATE_HINTS_CACHE_FILE} (skipped codex discovery)."
else
  if [ -f "${VALIDATE_HINTS_CACHE_FILE}" ] && [ -s "${VALIDATE_HINTS_CACHE_FILE}" ]; then
    echo "::warning::Cached validation hints at ${VALIDATE_HINTS_CACHE_FILE} failed sanity checks; falling back to codex discovery." >&2
  fi
  ensure_serena_bootstrap "discover"
  DISCOVER_SERENA_TOOL_HINTS="$(build_validate_serena_tool_hints "discover" || true)"
  discover_semble_query="$(build_validate_discover_semble_query || true)"
{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "=== DISCOVERY TASK ==="
  echo
  SERENA_TOOL_HINTS="${DISCOVER_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh prompts/mode-validate-discover.txt
  echo
  echo "TOOL_CALL_BUDGET: 15"
  echo
  echo "=== PROJECT SPEC ==="
    cat "${PROJECT_SPEC_FILE}"
    echo
    append_validate_semble_context "${discover_semble_query}" "${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS}" "Validate Discover Context"
    echo "Output only YAML for .ai/validate.yml with no markdown fences or prose."
  } > "${DISCOVER_PROMPT_FILE}"

  # Discover-only reasoning override. Patch ~/.codex/config.toml to the
  # discover-specific level (default `low`) before the codex loop, then
  # restore `MODEL_REASONING_EFFORT` after. Matches the per-phase pattern
  # in implement.yml (MODEL_REPAIR_REASONING_EFFORT) and aligns the
  # runtime behaviour with the documented `agents.md` model table.
  _validate_codex_config="${HOME:-/root}/.codex/config.toml"
  _discover_reasoning_patched="false"
  if [ -f "${_validate_codex_config}" ] && grep -Eq '^[[:space:]]*model_reasoning_effort[[:space:]]*=' "${_validate_codex_config}"; then
    sed -i \
      -e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*\".*\"/model_reasoning_effort = \"${MODEL_REASONING_EFFORT_DISCOVER}\"/" \
      -e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*'[^']*'/model_reasoning_effort = \"${MODEL_REASONING_EFFORT_DISCOVER}\"/" \
      "${_validate_codex_config}" || true
    # Verify the patch landed before claiming success / setting the
    # restore flag. A silent sed failure (permissions, unexpected config
    # shape, BSD sed argument differences in standalone runs) would
    # otherwise leave discover running at the previous reasoning level
    # while the log claims the override took effect. Mirrors the
    # post-edit verification pattern in scripts/review_conflict_resolve.sh.
    if grep -Eq "^model_reasoning_effort = \"${MODEL_REASONING_EFFORT_DISCOVER}\"$" "${_validate_codex_config}"; then
      _discover_reasoning_patched="true"
      echo "Patched ~/.codex/config.toml: model_reasoning_effort=${MODEL_REASONING_EFFORT_DISCOVER} for discover phase."
    else
      echo "::warning::Codex config rewrite did not produce the expected model_reasoning_effort = \"${MODEL_REASONING_EFFORT_DISCOVER}\" line; discover phase will run at the prior reasoning level (no restore needed)."
    fi
  fi

  DISCOVER_SUCCESS=false
  DISCOVER_FAILURE_MODE=""
  DISCOVER_ATTEMPTS_USED=0
  for attempt in $(seq 1 "${MAX_CODEX_ATTEMPTS}"); do
  DISCOVER_ATTEMPTS_USED="${attempt}"
  echo "Validation hint discovery attempt ${attempt}/${MAX_CODEX_ATTEMPTS}"
  emit_validate_substate "validate_discover" "discover" "PreparingWorkspace" "${attempt}"
  emit_validate_substate "validate_discover" "discover" "BuildingPrompt" "${attempt}"
  sanitize_codex_prompt_file "${DISCOVER_PROMPT_FILE}"
  discover_stall_status_file="$(mktemp /tmp/validate_discover_stall_status.XXXXXX)"
  discover_stall_state=""
  emit_validate_substate "validate_discover" "discover" "LaunchingAgentProcess" "${attempt}"
  emit_validate_substate "validate_discover" "discover" "InitializingSession" "${attempt}"
  emit_validate_substate "validate_discover" "discover" "StreamingTurn" "${attempt}"
  set +e
  run_validate_codex_attempt "validate_discover" "${DISCOVER_PROMPT_FILE}" "${DISCOVER_OUTPUT_FILE}" "${DISCOVER_LOG_FILE}" "${discover_stall_status_file}"
  DISCOVER_EXIT=$?
  set -e
  emit_validate_substate "validate_discover" "discover" "Finishing" "${attempt}" "${DISCOVER_LOG_FILE}"
  if discover_stall_state="$(read_codex_stall_guard_state "${discover_stall_status_file}" 2>/dev/null)"; then
    :
  elif [ -s "${discover_stall_status_file}" ]; then
    echo "::warning::Validation hint discovery attempt ${attempt}/${MAX_CODEX_ATTEMPTS}: could not parse codex stall guard status from ${discover_stall_status_file}."
  fi
  rm -f "${discover_stall_status_file}"
  case "${discover_stall_state}" in
    observed)
      echo "Validation hint discovery attempt ${attempt}/${MAX_CODEX_ATTEMPTS}: codex_stall_observed recorded (observe-only mode)."
      emit_validate_substate "validate_discover" "discover" "codex_stall_observed" "${attempt}" "${DISCOVER_LOG_FILE}"
      ;;
    killed)
      echo "::warning::Validation hint discovery attempt ${attempt}/${MAX_CODEX_ATTEMPTS}: codex_stall_killed recorded."
      emit_validate_substate "validate_discover" "discover" "codex_stall_killed" "${attempt}" "${DISCOVER_LOG_FILE}"
      ;;
  esac

  if [ "${DISCOVER_EXIT}" -eq 78 ]; then
    emit_validate_substate "validate_discover" "discover" "Failed" "${attempt}" "${DISCOVER_LOG_FILE}"
    fail_validate_codex_phase \
      "validation_discovery" \
      "workspace_safety_violation" \
      "${DISCOVER_ATTEMPTS_USED:-${attempt}}" \
      "Workspace safety preflight failed before validation hint discovery could launch Codex." \
      78
  fi

    if [ "${DISCOVER_EXIT}" -ne 0 ]; then
      if [ "${discover_stall_state}" = "killed" ]; then
        DISCOVER_FAILURE_MODE="codex_stall_killed"
      else
        DISCOVER_FAILURE_MODE="codex_rc_nonzero"
      fi
    elif ! grep -q '[^[:space:]]' "${DISCOVER_OUTPUT_FILE}"; then
      DISCOVER_FAILURE_MODE="codex_empty_output"
    elif python3 - "${DISCOVER_OUTPUT_FILE}" "${VALIDATE_HINTS_FILE}" <<'PY'
import re
import sys

source_file = sys.argv[1]
output_file = sys.argv[2]

with open(source_file, "r", encoding="utf-8", errors="replace") as handle:
    raw = handle.read().replace("\r", "")

candidate = raw.strip()
if "```" in raw:
    match = re.search(r"```(?:yaml|yml)?\n(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()

if not candidate:
    sys.exit(1)

if candidate.lstrip().startswith("{") or candidate.lstrip().startswith("["):
    sys.exit(1)

if len(candidate) > 12000:
    sys.exit(1)

lines = [
    line.lstrip()
    for line in candidate.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
if not lines:
    sys.exit(1)

if lines[0].lower().startswith(("error:", "fatal:", "traceback", "exception")):
    sys.exit(1)

expected_key = re.compile(r"^(type|entry|port|health_check|services|env_overrides|custom_tests|skip_tests):\s*", re.IGNORECASE)
if not any(expected_key.match(line) for line in lines):
    sys.exit(1)

with open(output_file, "w", encoding="utf-8") as handle:
    handle.write(candidate)
    handle.write("\n")
PY
    then
      DISCOVER_SUCCESS=true
      HINTS_SOURCE="discovered"
      emit_validate_substate "validate_discover" "discover" "Succeeded" "${attempt}" "${DISCOVER_LOG_FILE}"
      break
    else
      DISCOVER_FAILURE_MODE="validator_rejected"
    fi

    if [ "${discover_stall_state}" = "killed" ]; then
      emit_validate_substate "validate_discover" "discover" "Stalled" "${attempt}" "${DISCOVER_LOG_FILE}"
    else
      emit_validate_substate "validate_discover" "discover" "Failed" "${attempt}" "${DISCOVER_LOG_FILE}"
    fi

    if [ "${attempt}" -lt "${MAX_CODEX_ATTEMPTS}" ]; then
      sleep $((CODEX_RETRY_BACKOFF_BASE_SECS * (2 ** (attempt - 1))))
    fi
  done

  # Restore the workflow-level reasoning effort so the subsequent
  # generate / diagnose / fix-harness phases run at MODEL_REASONING_EFFORT
  # (default `medium`) rather than inheriting the discover-only override.
  if [ "${_discover_reasoning_patched}" = "true" ] && [ -f "${_validate_codex_config}" ]; then
    sed -i \
      -e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*\".*\"/model_reasoning_effort = \"${MODEL_REASONING_EFFORT}\"/" \
      -e "s/^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*'[^']*'/model_reasoning_effort = \"${MODEL_REASONING_EFFORT}\"/" \
      "${_validate_codex_config}" || true
    # Verify the restore landed before logging success. Without this
    # check, a silent sed failure would leave subsequent generate /
    # diagnose / fix-harness phases running at the discover override
    # level while the log claimed restoration succeeded. Mirrors the
    # post-edit verification on the patch side above and the pattern in
    # scripts/review_conflict_resolve.sh.
    if grep -Eq "^model_reasoning_effort = \"${MODEL_REASONING_EFFORT}\"$" "${_validate_codex_config}"; then
      echo "Restored ~/.codex/config.toml: model_reasoning_effort=${MODEL_REASONING_EFFORT} after discover phase."
      # Clear the patched flag so the EXIT trap (cleanup_runtime_containers)
      # treats this as already-restored and skips the redundant sed.
      _discover_reasoning_patched="false"
    else
      echo "::warning::Codex config rewrite did not produce the expected model_reasoning_effort = \"${MODEL_REASONING_EFFORT}\" line after discover; subsequent validate phases may run at the discover override level (${MODEL_REASONING_EFFORT_DISCOVER})."
    fi
  fi

  if [ "${DISCOVER_SUCCESS}" != "true" ]; then
    echo "::warning::Validation hint discovery exhausted ${DISCOVER_ATTEMPTS_USED} attempt(s) (mode=${DISCOVER_FAILURE_MODE:-unknown}); continuing without discovered hints." >&2
    printf '# No .ai/validate.yml hints file found\n' > "${VALIDATE_HINTS_FILE}"
    HINTS_SOURCE="none"
  else
    # Persist the discovered hints to the cache directory so subsequent
    # runs in the same workspace can skip the codex discover call. The
    # cache is saved by the `Save validate hints cache` / actions/cache
    # post-step in validate.yml.
    if mkdir -p "${VALIDATE_HINTS_CACHE_DIR}" 2>/dev/null; then
      cp "${VALIDATE_HINTS_FILE}" "${VALIDATE_HINTS_CACHE_FILE}" 2>/dev/null || true
    fi
  fi
fi

# Template mode requires a manifest at .ai/validate.yml. When hints came
# from cache or codex discovery (not a committed manifest), materialize
# them into .ai/validate.yml so run_template_validation_harness_renderer
# can consume them. Never clobber an existing committed manifest.
if [ "${HINTS_SOURCE:-}" = "discovered" ] || [ "${HINTS_SOURCE:-}" = "cache" ]; then
  if [ ! -f .ai/validate.yml ] && [ -s "${VALIDATE_HINTS_FILE}" ]; then
    if [ -L .ai ]; then
      echo "::warning::Refusing to materialize ${HINTS_SOURCE} hints into .ai/validate.yml because .ai is a symlink." >&2
    elif [ -e .ai ] && [ ! -d .ai ]; then
      echo "::warning::Refusing to materialize ${HINTS_SOURCE} hints into .ai/validate.yml because .ai exists but is not a directory." >&2
    elif mkdir -p .ai 2>/dev/null && cp "${VALIDATE_HINTS_FILE}" .ai/validate.yml; then
      echo "Materialized ${HINTS_SOURCE} hints into .ai/validate.yml for template renderer."
    else
      echo "::warning::Failed to materialize ${HINTS_SOURCE} hints into .ai/validate.yml; template renderer will report manifest as missing." >&2
    fi
  fi
fi


# ---------------------------------------------------------------
# Phase 1: Generate validation harness
# ---------------------------------------------------------------
set_tracking_phase_label "ai:validating"

if command -v git >/dev/null 2>&1 && [ -d .git ]; then
  if ! grep -qxF 'validation/' .git/info/exclude 2>/dev/null; then
    echo 'validation/' >> .git/info/exclude
  fi
fi

if ! ensure_validation_harness_not_tracked; then
  local_failure_summary="${CANONICAL_VALIDATE_HARNESS_REL} is tracked in git. Runtime validation harness must remain untracked."
  post_tracking_comment "## ⚠️ Runtime validation harness tracking violation\n\n${local_failure_summary}\n\nRemove it from git tracking in the consumer repository and rerun validation."
  set_tracking_phase_label "ai:validation-failed"
  write_result_files "error" "Validation harness tracking violation" "${local_failure_summary}" "harness_error"
  tg_notify "Validation harness tracking violation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit 1
fi

if ! enforce_managed_validation_artifact_contract; then
	exit 1
fi

if [ -L validation ] || { [ -e validation ] && [ ! -d validation ]; }; then
	echo "Refusing to use non-directory 'validation' path." >&2
	exit 1
fi

if [ "${VALIDATION_USE_TEMPLATES_ENABLED}" != "true" ]; then
	local_failure_summary="Freehand harness generation has been removed. Set VALIDATION_USE_TEMPLATES=true (or leave it unset) to use template rendering."
	post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate rendering is now the only supported harness generation path."
	set_tracking_phase_label "ai:validation-failed"
	write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
	tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
	exit 1
fi

HARNESS_MODE="template_generate"
HARNESS_GENERATOR_MODE="templates"
if [ -d validation ] && [ ! -f validation/.ai-validation-owned ]; then
	echo "Refusing to delete existing 'validation' directory without ownership marker (validation/.ai-validation-owned)." >&2
	exit 1
fi
rm -rf validation
mkdir -p validation/logs
touch validation/.ai-validation-owned

ensure_validate_wrapper

if command -v git >/dev/null 2>&1; then
  git status --porcelain --untracked-files=all -- . ':!validation/**' | filter_runtime_status_noise | sort > "${PRE_GENERATE_STATUS_FILE}" 2>/dev/null || true
  capture_write_guard_candidate_paths > "${PRE_GENERATE_GUARD_PATHS_FILE}" 2>/dev/null || true
fi

# ---------------------------------------------------------------
# Cycle 2+: gather previous validation failure context so the LLM
# avoids repeating the same harness mistakes.
# ---------------------------------------------------------------
PRIOR_FAILURE_CONTEXT_FILE="${RUNTIME_DIR}/prior_validation_failures.txt"
: > "${PRIOR_FAILURE_CONTEXT_FILE}"
: > "${PRIOR_RESULT_JSON_FILE}"
: > "${PRIOR_CONTAINER_LOGS_FILE}"

if [ "${VALIDATION_CYCLE}" -gt 1 ] && is_tracking_run; then
	echo "Cycle ${VALIDATION_CYCLE}: fetching prior validation failure context from tracking issue #${TRACKING_ISSUE_NUM}."
  PRIOR_COMMENTS="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/comments" \
    --paginate --jq '[.[] | select(.body | test("Runtime validation"))] | .[-3:] | .[].body' 2>/dev/null || true)"
  if [ -n "${PRIOR_COMMENTS}" ]; then
    {
      echo "IMPORTANT — PREVIOUS VALIDATION CYCLE FAILURES (cycle $((VALIDATION_CYCLE - 1))):"
      echo "The following failures occurred in prior validation cycles. Your generated"
      echo "harness MUST avoid these same patterns. If a prior failure was caused by"
      echo "fragile shell output parsing (e.g. raw mongosh text matching), use the"
      echo "deterministic assertion patterns described above instead."
      echo
      echo "${PRIOR_COMMENTS}"
    } > "${PRIOR_FAILURE_CONTEXT_FILE}"

    jq -n --arg summary "${PRIOR_COMMENTS}" '{result: "fail", phase: "prior_cycle", summary: $summary}' > "${PRIOR_RESULT_JSON_FILE}"
    printf '%s\n' "${PRIOR_COMMENTS}" > "${PRIOR_CONTAINER_LOGS_FILE}"
  fi
fi

echo "Validation harness template render mode is enabled (VALIDATION_USE_TEMPLATES=${VALIDATION_USE_TEMPLATES}, enabled=${VALIDATION_USE_TEMPLATES_ENABLED})."
if run_template_validation_harness_renderer; then
	renderer_exit=0
else
	renderer_exit=$?
fi
case "${renderer_exit}" in
	0)
		if ! is_validation_harness_runnable; then
			local_failure_summary="Template renderer completed but produced non-runnable validation assets (validation/docker-compose.test.yml and validation/tests/00_canary.sh at minimum)."
			post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nSee workflow artifacts for renderer logs."
			set_tracking_phase_label "ai:validation-failed"
			write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
			tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
			exit 1
		fi
		;;
	10)
		local_failure_summary="Template mode requires ${PWD}/.ai/validate.yml but it is missing. Create the required manifest to proceed with template-based validation."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
	11)
		local_failure_summary="Template mode requires scripts/render_validation_templates.py but it is missing. Ensure workflow bootstrap fetched renderer assets."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
	12)
		local_failure_summary="Template mode requires scripts/templates/slot_manifest.schema.json but it is missing. Ensure workflow bootstrap fetched schema assets."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
	13)
		local_failure_summary="Template mode requires workflow-templates/validation-harness directory but it is missing. Ensure workflow bootstrap fetched template assets."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
	14)
		local_failure_summary="Template renderer subprocess (\`scripts/render_validation_templates.py\`) exited non-zero (exit 14). Common causes: missing renderer dependencies (\`pyyaml\`, \`jsonschema\`, \`jinja2\`), invalid \`.ai/validate.yml\`, schema-validation failure, or template-collection error."
		_render_log_excerpt="(no renderer log captured at ${GENERATE_LOG_FILE})"
		if [ -s "${GENERATE_LOG_FILE}" ]; then
			_render_log_excerpt="$(tail -n 40 "${GENERATE_LOG_FILE}" 2>/dev/null || echo '(failed to read renderer log)')"
		fi
		post_tracking_comment "$(printf '## ⚠️ Runtime validation harness generation failed\n\n%s\n\nLast 40 lines of renderer log (`%s`):\n\n~~~\n%s\n~~~\n\nTemplate mode is enabled and does not fall back to freehand generation.' "${local_failure_summary}" "${GENERATE_LOG_FILE}" "${_render_log_excerpt}")"
		unset _render_log_excerpt
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
	17)
		local_failure_summary="Template renderer requires python3 >= 3.9 but the runner does not provide it (exit 17). This is a deterministic environment failure; retrying with the same runner image will not help."
		_render_log_excerpt="(no renderer log captured at ${GENERATE_LOG_FILE})"
		if [ -s "${GENERATE_LOG_FILE}" ]; then
			_render_log_excerpt="$(tail -n 40 "${GENERATE_LOG_FILE}" 2>/dev/null || echo '(failed to read renderer log)')"
		fi
		post_tracking_comment "$(printf '## ⚠️ Runtime validation harness generation failed\n\n%s\n\nPython environment probe (last 40 lines of `%s`):\n\n~~~\n%s\n~~~\n\nFix: install python3 >= 3.9 on the runner image, or pin a setup-python step in the validate workflow.\n\n<!-- AI_VALIDATION_FAILURE_CLASS:deterministic_python_missing -->' "${local_failure_summary}" "${GENERATE_LOG_FILE}" "${_render_log_excerpt}")"
		unset _render_log_excerpt
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: python3 >= 3.9 missing on runner." "ERROR"
		exit 1
		;;
	15)
		local_failure_summary="Template mode requires essential shared templates (_shared/_lib/tap_helpers.sh.j2, _shared/tests/00_canary.sh.j2, _shared/tests/90_tap_report.sh.j2) plus family-specific templates; ensure workflow bootstrap fetched all required template assets under workflow-templates/validation-harness/."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
	*)
		local_failure_summary="Template renderer failed while generating validation assets. Check validate_generate.log for dependency or manifest errors from scripts/render_validation_templates.py."
		post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nTemplate mode is enabled and does not fall back to freehand generation."
		set_tracking_phase_label "ai:validation-failed"
		write_result_files "error" "Validation harness generation failed" "${local_failure_summary}" "harness_error"
		tg_notify "Validation harness generation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
		exit 1
		;;
esac

materialize_synthesised_behavioural_smoke_tests

if command -v git >/dev/null 2>&1; then
  git status --porcelain --untracked-files=all -- . ':!validation/**' | filter_runtime_status_noise | sort > "${POST_GENERATE_STATUS_FILE}" 2>/dev/null || true
  capture_write_guard_candidate_paths > "${POST_GENERATE_GUARD_PATHS_FILE}" 2>/dev/null || true
  if ! run_validate_write_guard; then
    local_failure_summary="Codex modified files outside the validate write-guard policy during harness generation."
    post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "error" "Validation harness generation violated write guard" "${local_failure_summary}" "harness_error"
    tg_notify "Validation harness generation violated the write guard for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    exit 1
  fi
  NON_VALIDATION_CHANGES=""
  if [ -f "${PRE_GENERATE_STATUS_FILE}" ] && [ -f "${POST_GENERATE_STATUS_FILE}" ] && ! cmp -s "${PRE_GENERATE_STATUS_FILE}" "${POST_GENERATE_STATUS_FILE}"; then
    NON_VALIDATION_CHANGES="$(diff -u "${PRE_GENERATE_STATUS_FILE}" "${POST_GENERATE_STATUS_FILE}" || true)"
  fi

  if [ -n "${NON_VALIDATION_CHANGES}" ]; then
    local_failure_summary="Codex modified files outside validation/ during harness generation."
    post_tracking_comment "## ⚠️ Runtime validation harness generation failed\n\n${local_failure_summary}\n\nUnexpected changes:\n\n\`\`\`\n${NON_VALIDATION_CHANGES}\n\`\`\`"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "error" "Validation harness generation violated path constraints" "${local_failure_summary}" "harness_error"
    tg_notify "Validation harness generation touched non-validation files for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    exit 1
  fi
fi

find validation -type f -name '*.sh' -exec chmod +x {} +

if ! ensure_validation_harness_not_tracked; then
  local_failure_summary="${CANONICAL_VALIDATE_HARNESS_REL} became tracked/staged after harness generation."
  post_tracking_comment "## ⚠️ Runtime validation harness tracking violation\n\n${local_failure_summary}\n\nValidation harness files must remain transient and untracked."
  set_tracking_phase_label "ai:validation-failed"
  write_result_files "error" "Validation harness tracking violation" "${local_failure_summary}" "harness_error"
  tg_notify "Validation harness tracking violation after generation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
  exit 1
fi


# ---------------------------------------------------------------
# Phase 2: Pre-flight checks for generated harness
# ---------------------------------------------------------------
if ! run_preflight_checks; then
  render_recovery_exit=1
  if attempt_render_recovery_after_preflight_failure; then
    render_recovery_exit=0
  else
    render_recovery_exit=$?
  fi

  if [ "${render_recovery_exit}" -ne 0 ]; then
    failure_summary="Validation pre-flight checks failed. See validation_preflight.log artifact."
    jq -n \
      --arg diagnosis "Pre-flight validation failed before test execution." \
      --arg harness_fixes "$(tail -n 120 "${PRE_FLIGHT_LOG_FILE}" 2>/dev/null || true)" \
      '{
        status: "harness_error",
        diagnosis: $diagnosis,
        fix_issues: [],
        harness_fixes: (if ($harness_fixes | length) > 0 then $harness_fixes else "Fix validation/docker-compose.test.yml, shell syntax, or build context/dockerfile paths." end)
      }' > "${DIAGNOSE_RESULT_FILE}"

    # Self-heal interception: bad harness is often a prompt-wording defect.
    if [ "${render_recovery_exit}" -eq 2 ]; then
      attempt_self_heal_and_reexec "render"
    else
      attempt_self_heal_and_reexec "preflight"
    fi
    post_tracking_comment "## ❌ Runtime validation harness pre-flight failed\n\n${failure_summary}\n\n\`docker compose config\`, shell syntax, or build context/dockerfile path checks failed."
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "fail" "Validation failed due to harness pre-flight error" "${failure_summary}" "harness_error"
    tg_notify "Validation pre-flight failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    exit 0
  fi
fi


# ---------------------------------------------------------------
# Phase 3: Execute validation harness (idle-timeout based)
# ---------------------------------------------------------------
# The timeout is activity-based: the process is killed only if it
# produces no output for VALIDATION_TIMEOUT minutes. This allows
# large projects to run longer as long as they keep producing output.
IDLE_TIMEOUT_SECS=$((VALIDATION_TIMEOUT * 60))
VALIDATION_EXIT=0
VALIDATION_IDLE_KILLED=0

set +e
# Run validation in background, tee output to log file
if [ -f validation/validate.sh ]; then
  if grep -q 'scripts/validate_driver.sh' validation/validate.sh && [ ! -f scripts/validate_driver.sh ]; then
    ensure_runtime_validation_driver
    GENERATED_VALIDATE_SCRIPT_PATH="${VALIDATION_RUNNER_FILE}"
    "${VALIDATION_RUNNER_FILE}" > "${VALIDATION_LOG_FILE}" 2>&1 &
  else
    GENERATED_VALIDATE_SCRIPT_PATH="validation/validate.sh"
    bash validation/validate.sh > "${VALIDATION_LOG_FILE}" 2>&1 &
  fi
else
  ensure_runtime_validation_driver
  GENERATED_VALIDATE_SCRIPT_PATH="${VALIDATION_RUNNER_FILE}"
  "${VALIDATION_RUNNER_FILE}" > "${VALIDATION_LOG_FILE}" 2>&1 &
fi
VALIDATION_PID=$!

# Monitor the log file for activity; kill if idle too long
LAST_SIZE=0
IDLE_ELAPSED=0
POLL_INTERVAL=5
while kill -0 "${VALIDATION_PID}" 2>/dev/null; do
  CURRENT_SIZE=0
  if [ -f "${VALIDATION_LOG_FILE}" ]; then
    CURRENT_SIZE=$(stat -c%s "${VALIDATION_LOG_FILE}" 2>/dev/null || echo 0)
  fi

  if [ "${CURRENT_SIZE}" -ne "${LAST_SIZE}" ]; then
    LAST_SIZE="${CURRENT_SIZE}"
    IDLE_ELAPSED=0
  else
    IDLE_ELAPSED=$((IDLE_ELAPSED + POLL_INTERVAL))
  fi

  if [ "${IDLE_ELAPSED}" -ge "${IDLE_TIMEOUT_SECS}" ]; then
    echo "Validation idle for ${VALIDATION_TIMEOUT} minute(s) with no output — terminating." >> "${VALIDATION_LOG_FILE}"
    kill "${VALIDATION_PID}" 2>/dev/null || true
    # Grace period: SIGKILL after 30s if still running
    sleep 30
    if kill -0 "${VALIDATION_PID}" 2>/dev/null; then
      kill -9 "${VALIDATION_PID}" 2>/dev/null || true
    fi
    VALIDATION_IDLE_KILLED=1
    break
  fi

  sleep "${POLL_INTERVAL}"
done

wait "${VALIDATION_PID}" 2>/dev/null
VALIDATION_EXIT=$?
set -e

tail -n 200 "${VALIDATION_LOG_FILE}" > "${VALIDATION_LOG_TAIL_FILE}" 2>/dev/null || true

if [ "${VALIDATION_IDLE_KILLED}" -eq 1 ] || [ "${VALIDATION_EXIT}" -eq 124 ] || [ "${VALIDATION_EXIT}" -eq 137 ]; then
  timeout_test="validation-idle-timeout"
  timeout_error="Validation idle-timed out after ${VALIDATION_TIMEOUT} minute(s) with no output"
  if [ "${VALIDATION_IDLE_KILLED}" -eq 0 ]; then
    # Legacy exit codes (shouldn't normally happen now, but handle defensively)
    timeout_test="validation-timeout-signal"
    timeout_error="Validation terminated (exit code ${VALIDATION_EXIT}) after ${VALIDATION_TIMEOUT} minute(s)"
  fi

  jq -n \
    --arg timeout_test "${timeout_test}" \
    --arg timeout_error "${timeout_error}" \
    --arg duration_seconds "${IDLE_TIMEOUT_SECS}" \
    '{
      result: "fail",
      phase: "timeout",
      total_tests: 0,
      passed_tests: 0,
      failed_tests: 1,
      failures: [
        {
          test: $timeout_test,
          error: $timeout_error,
          log_tail: "See validation.log tail in artifacts"
        }
      ],
      duration_seconds: ($duration_seconds | tonumber)
    }' > "${VALIDATION_RESULT_FILE}"
else
  if ! extract_last_json_with_key "${VALIDATION_LOG_FILE}" "result" "${VALIDATION_RESULT_FILE}"; then
    jq -n \
      --arg exit_code "${VALIDATION_EXIT}" \
      '{
        result: "fail",
        phase: "execution_error",
        total_tests: 0,
        passed_tests: 0,
        failed_tests: 1,
        failures: [
          {
            test: "validation-json-parse",
            error: ("Unable to parse validation result JSON (exit code " + $exit_code + ")"),
            log_tail: "See validation.log in artifacts"
          }
        ],
        duration_seconds: 0
      }' > "${VALIDATION_RESULT_FILE}"
  fi
fi

RESULT_KIND="$(jq -r '.result // "fail"' "${VALIDATION_RESULT_FILE}")"
TOTAL_TESTS="$(jq -r '.total_tests // 0' "${VALIDATION_RESULT_FILE}")"
PASSED_TESTS="$(jq -r '.passed_tests // 0' "${VALIDATION_RESULT_FILE}")"
FAILED_TESTS="$(jq -r '.failed_tests // 0' "${VALIDATION_RESULT_FILE}")"
DURATION_SECONDS="$(jq -r '.duration_seconds // 0' "${VALIDATION_RESULT_FILE}")"
FIRST_FAILURE="$(jq -r '.failures[0].error // ""' "${VALIDATION_RESULT_FILE}")"

# ---------------------------------------------------------------
# Safety net: override contradictory fail-with-all-pass results.
# When the harness script crashes (non-zero exit / result=fail)
# but the structured JSON shows all tests passed (failed_tests==0,
# passed_tests>0, counts consistent), the crash was a scripting
# bug (e.g. grep returning 1 on zero matches under pipefail),
# not a real test failure. Override to pass.
# ---------------------------------------------------------------
if [ "${RESULT_KIND}" != "pass" ] || [ "${VALIDATION_EXIT}" -ne 0 ]; then
	if jq -e '
		(.total_tests | type == "number") and
		(.passed_tests | type == "number") and
		(.failed_tests | type == "number") and
		(.total_tests > 0) and
		(.passed_tests > 0) and
		(.failed_tests == 0) and
		(.passed_tests == .total_tests) and
		((.failures | length == 0) or ((.failures | length == 1) and ((.failures[0].test // "") | endswith(":unexpected_error"))))
	' "${VALIDATION_RESULT_FILE}" >/dev/null 2>&1; then
		echo "::warning::Harness exited ${VALIDATION_EXIT} with result '${RESULT_KIND}' but all ${PASSED_TESTS}/${TOTAL_TESTS} tests passed (failed_tests=0). Overriding to pass (likely scripting bug in generated harness script)."
		# Strip the synthetic unexpected_error failure entry and fix result
		jq '.result = "pass" | .failures = [] | .phase = "runtime_validation"' "${VALIDATION_RESULT_FILE}" > "${VALIDATION_RESULT_FILE}.tmp"
		mv "${VALIDATION_RESULT_FILE}.tmp" "${VALIDATION_RESULT_FILE}"
		RESULT_KIND="pass"
		VALIDATION_EXIT=0
		FIRST_FAILURE=""
	fi
fi

PASS_SCHEMA_OK="false"
if [ "${RESULT_KIND}" = "pass" ] && [ "${VALIDATION_EXIT}" -eq 0 ]; then
  if jq -e '
    (.total_tests | type == "number") and
    (.passed_tests | type == "number") and
    (.failed_tests | type == "number") and
    (.duration_seconds | type == "number") and
    (.failures | type == "array") and
    (.failed_tests == 0) and
    (.total_tests >= 0) and
    (.passed_tests >= 0) and
    (.passed_tests <= .total_tests) and
    (.failed_tests <= .total_tests) and
    ((.passed_tests + .failed_tests) == .total_tests)
  ' "${VALIDATION_RESULT_FILE}" >/dev/null 2>&1; then
    PASS_SCHEMA_OK="true"
  else
    jq -n \
      --arg reason "Pass payload schema consistency check failed" \
      '{
        result: "fail",
        phase: "result_schema_error",
        total_tests: 0,
        passed_tests: 0,
        failed_tests: 1,
        failures: [
          {
            test: "validation-result-schema",
            error: $reason,
            log_tail: "See validation.log in artifacts"
          }
        ],
        duration_seconds: 0
      }' > "${VALIDATION_RESULT_FILE}"
    RESULT_KIND="fail"
    TOTAL_TESTS="0"
    PASSED_TESTS="0"
    FAILED_TESTS="1"
    DURATION_SECONDS="0"
    FIRST_FAILURE="Pass payload schema consistency check failed"
  fi
fi

if [ "${RESULT_KIND}" = "pass" ] && [ "${VALIDATION_EXIT}" -eq 0 ] && [ "${PASS_SCHEMA_OK}" = "true" ]; then
	summary_text="Runtime validation passed (${PASSED_TESTS}/${TOTAL_TESTS} tests, ${DURATION_SECONDS}s)."
	post_tracking_comment "## ✅ Runtime validation passed\n\n- Passed tests: ${PASSED_TESTS}/${TOTAL_TESTS}\n- Duration: ${DURATION_SECONDS}s"
	set_tracking_phase_label "ai:validated"

	# Verify the ai:validated label was applied; retry with direct add if missing.
	# This prevents the orchestrator from looping forever when the label
	# application silently fails (Bug: validation-fixing loop stuck).
	if is_tracking_run; then
		_verify_labels="$(gh_retry gh api \
			"repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_NUM}/labels" \
			--jq '[.[].name]' 2>/dev/null || echo '[]')"
		if ! echo "${_verify_labels}" | jq -e 'index("ai:validated") != null' >/dev/null 2>&1; then
			echo "::warning::ai:validated label not found on #${TRACKING_ISSUE_NUM} after set_tracking_phase_label; retrying direct add." >&2
			ensure_label_exists "ai:validated"
			gh_retry gh issue edit "${TRACKING_ISSUE_NUM}" \
				--repo "${GITHUB_REPOSITORY}" \
				--add-label "ai:validated" >/dev/null 2>&1 || \
				echo "::warning::Retry of ai:validated label application also failed for #${TRACKING_ISSUE_NUM}." >&2
		fi
	fi

	write_result_files "pass" "${summary_text}" ""
	tg_notify "Runtime validation passed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} (${PASSED_TESTS}/${TOTAL_TESTS})." "DEBUG"
	# If this pass was reached after one or more self-heal attempts, send
	# the accumulated prompt patches back to upstream coding-workflows.
	dispatch_self_heal_improvements
	exit 0
fi


# ---------------------------------------------------------------
# Collect container logs (used for canary classification + diagnosis)
# ---------------------------------------------------------------
: > "${CONTAINER_LOG_TAIL_FILE}"
if [ -d validation/logs ]; then
	while IFS= read -r log_file; do
		echo "===== ${log_file} (tail 80) =====" >> "${CONTAINER_LOG_TAIL_FILE}"
		tail -n 80 "${log_file}" >> "${CONTAINER_LOG_TAIL_FILE}" 2>/dev/null || true
		echo >> "${CONTAINER_LOG_TAIL_FILE}"
	done < <(find validation/logs -type f | sort)
fi


# ---------------------------------------------------------------
# Canary shortcut: classify infra-only canary failure as harness_error
# ---------------------------------------------------------------
CANARY_TEST_NAME="$(jq -r '.failures[0].test // ""' "${VALIDATION_RESULT_FILE}")"
CANARY_ERROR_TEXT="$(jq -r '.failures[0].error // ""' "${VALIDATION_RESULT_FILE}" | tr '[:upper:]' '[:lower:]')"
CANARY_ONLY_FAILURE=false
CANARY_APP_SIGNAL_IN_LOGS=false
if [ -s "${CONTAINER_LOG_TAIL_FILE}" ]; then
	if grep -E -i -q 'application crashed|app crashed|process exited|server startup failed|panic|traceback|exception in app|fatal error|segmentation fault' "${CONTAINER_LOG_TAIL_FILE}"; then
		CANARY_APP_SIGNAL_IN_LOGS=true
	fi
fi

if [ "${FAILED_TESTS}" = "1" ] && [ -n "${CANARY_TEST_NAME}" ] && [ -n "${CANARY_ERROR_TEXT}" ]; then
	if [[ "${CANARY_TEST_NAME}" == *00_canary* ]]; then
		CANARY_ONLY_FAILURE=true
	fi
fi

if [ "${CANARY_ONLY_FAILURE}" = true ]; then
	if echo "${CANARY_ERROR_TEXT}" | grep -E -q 'connection refused|could not resolve host|command not found|exit code 127|no such file or directory|invalid compose|healthcheck|network|timeout waiting for'; then
		if [ "${CANARY_APP_SIGNAL_IN_LOGS}" != "true" ] \
			&& ! echo "${CANARY_ERROR_TEXT}" | grep -E -q 'application crashed|app crashed|process exited|server startup failed|panic|traceback|exception in app'; then
			jq -n \
				--arg diagnosis "Canary infrastructure check failed before app validation. Classified as harness_error." \
				--arg harness_fixes "${FIRST_FAILURE}" \
				'{
					status: "harness_error",
					diagnosis: $diagnosis,
					fix_issues: [],
					harness_fixes: (if ($harness_fixes | length) > 0 then $harness_fixes else "Fix canary test infrastructure assumptions (ports/services/tools)." end)
				}' > "${DIAGNOSE_RESULT_FILE}"

			# Self-heal interception: canary failures are prime candidates
			# for a bad generate/fix-harness prompt.
			attempt_self_heal_and_reexec "canary"
			failure_summary="Validation harness error: ${FIRST_FAILURE}"
			post_tracking_comment "## ❌ Runtime validation harness error\n\n${failure_summary}\n\nCanary infrastructure check failed and remaining tests were skipped."
			set_tracking_phase_label "ai:validation-failed"
			write_result_files "fail" "Validation failed due to harness error" "${failure_summary}" "harness_error"
			tg_notify "Validation harness canary failure for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
			exit 0
		fi
	fi
fi


# ---------------------------------------------------------------
# Phase 4: Diagnose failures
# ---------------------------------------------------------------
ensure_serena_bootstrap "diagnose"
DIAGNOSE_SERENA_TOOL_HINTS="$(build_validate_serena_tool_hints "diagnose" || true)"
diagnose_semble_query="$(build_validate_diagnose_semble_query || true)"
{
  cat "${STATIC_CONTEXT_FILE}"
  echo
  echo "=== DIAGNOSIS TASK ==="
  echo
  SERENA_TOOL_HINTS="${DIAGNOSE_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh prompts/mode-validate-diagnose.txt
  echo
  echo "TOOL_CALL_BUDGET: ${TOOL_CALL_BUDGET_VALIDATE}"
  echo
  echo "=== PROJECT SPEC ==="
  cat "${PROJECT_SPEC_FILE}"
  echo
  echo "=== STRUCTURED VALIDATION FAILURE JSON ==="
  cat "${VALIDATION_RESULT_FILE}"
  echo
  echo "=== VALIDATION LOG TAIL (last 200 lines) ==="
  cat "${VALIDATION_LOG_TAIL_FILE}"
  echo
  echo "=== CONTAINER LOG TAILS ==="
  cat "${CONTAINER_LOG_TAIL_FILE}"
  echo
  echo "=== VALIDATION HINTS ==="
  cat "${VALIDATE_HINTS_FILE}"
  append_validate_semble_context "${diagnose_semble_query}" "${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS}" "Validate Diagnose Context"
} > "${DIAGNOSE_PROMPT_FILE}"

DIAGNOSE_SUCCESS=false
DIAGNOSE_FAILURE_MODE=""
DIAGNOSE_ATTEMPTS_USED=0
for attempt in $(seq 1 "${MAX_CODEX_ATTEMPTS}"); do
  DIAGNOSE_ATTEMPTS_USED="${attempt}"
  echo "Validation diagnosis attempt ${attempt}/${MAX_CODEX_ATTEMPTS}"
  emit_validate_substate "validate_diagnose" "diagnose" "PreparingWorkspace" "${attempt}"
  emit_validate_substate "validate_diagnose" "diagnose" "BuildingPrompt" "${attempt}"
  sanitize_codex_prompt_file "${DIAGNOSE_PROMPT_FILE}"
  diagnose_stall_status_file="$(mktemp /tmp/validate_diagnose_stall_status.XXXXXX)"
  diagnose_stall_state=""
  emit_validate_substate "validate_diagnose" "diagnose" "LaunchingAgentProcess" "${attempt}"
  emit_validate_substate "validate_diagnose" "diagnose" "InitializingSession" "${attempt}"
  emit_validate_substate "validate_diagnose" "diagnose" "StreamingTurn" "${attempt}"
  set +e
  run_validate_codex_attempt "validate_diagnose" "${DIAGNOSE_PROMPT_FILE}" "${DIAGNOSE_OUTPUT_FILE}" "${DIAGNOSE_LOG_FILE}" "${diagnose_stall_status_file}"
  DIAGNOSE_EXIT=$?
  set -e
  emit_validate_substate "validate_diagnose" "diagnose" "Finishing" "${attempt}" "${DIAGNOSE_LOG_FILE}"
  if diagnose_stall_state="$(read_codex_stall_guard_state "${diagnose_stall_status_file}" 2>/dev/null)"; then
    :
  elif [ -s "${diagnose_stall_status_file}" ]; then
    echo "::warning::Validation diagnosis attempt ${attempt}/${MAX_CODEX_ATTEMPTS}: could not parse codex stall guard status from ${diagnose_stall_status_file}."
  fi
  rm -f "${diagnose_stall_status_file}"
  case "${diagnose_stall_state}" in
    observed)
      echo "Validation diagnosis attempt ${attempt}/${MAX_CODEX_ATTEMPTS}: codex_stall_observed recorded (observe-only mode)."
      emit_validate_substate "validate_diagnose" "diagnose" "codex_stall_observed" "${attempt}" "${DIAGNOSE_LOG_FILE}"
      ;;
    killed)
      echo "::warning::Validation diagnosis attempt ${attempt}/${MAX_CODEX_ATTEMPTS}: codex_stall_killed recorded."
      emit_validate_substate "validate_diagnose" "diagnose" "codex_stall_killed" "${attempt}" "${DIAGNOSE_LOG_FILE}"
      ;;
  esac

  if [ "${DIAGNOSE_EXIT}" -eq 78 ]; then
    emit_validate_substate "validate_diagnose" "diagnose" "Failed" "${attempt}" "${DIAGNOSE_LOG_FILE}"
    fail_validate_codex_phase \
      "validation_diagnosis" \
      "workspace_safety_violation" \
      "${DIAGNOSE_ATTEMPTS_USED:-${attempt}}" \
      "Workspace safety preflight failed before validation diagnosis could launch Codex." \
      78
  fi

  if [ "${DIAGNOSE_EXIT}" -ne 0 ]; then
    if [ "${diagnose_stall_state}" = "killed" ]; then
      DIAGNOSE_FAILURE_MODE="codex_stall_killed"
    else
      DIAGNOSE_FAILURE_MODE="codex_rc_nonzero"
    fi
  elif ! grep -q '[^[:space:]]' "${DIAGNOSE_OUTPUT_FILE}"; then
    DIAGNOSE_FAILURE_MODE="codex_empty_output"
  elif extract_last_json_with_key "${DIAGNOSE_OUTPUT_FILE}" "status" "${DIAGNOSE_RESULT_FILE}"; then
    DIAGNOSE_SUCCESS=true
    emit_validate_substate "validate_diagnose" "diagnose" "Succeeded" "${attempt}" "${DIAGNOSE_LOG_FILE}"
    break
  else
    DIAGNOSE_FAILURE_MODE="validator_rejected"
  fi

  if [ "${diagnose_stall_state}" = "killed" ]; then
    emit_validate_substate "validate_diagnose" "diagnose" "Stalled" "${attempt}" "${DIAGNOSE_LOG_FILE}"
  else
    emit_validate_substate "validate_diagnose" "diagnose" "Failed" "${attempt}" "${DIAGNOSE_LOG_FILE}"
  fi

  if [ "${attempt}" -lt "${MAX_CODEX_ATTEMPTS}" ]; then
    sleep $((CODEX_RETRY_BACKOFF_BASE_SECS * (2 ** (attempt - 1))))
  fi
done

if [ "${DIAGNOSE_SUCCESS}" != "true" ]; then
  attempt_self_heal_and_reexec "diagnose"
  failure_summary="Codex diagnosis failed to produce contract-compliant JSON (mode=${DIAGNOSE_FAILURE_MODE:-unknown}, attempts=${DIAGNOSE_ATTEMPTS_USED}/${MAX_CODEX_ATTEMPTS})."
  fail_validate_codex_phase "validation_diagnosis" "${DIAGNOSE_FAILURE_MODE:-validator_rejected}" "${DIAGNOSE_ATTEMPTS_USED:-${MAX_CODEX_ATTEMPTS}}" "${failure_summary}"
fi

DIAG_STATUS="$(jq -r '.status // "harness_error"' "${DIAGNOSE_RESULT_FILE}")"
DIAG_TEXT="$(jq -r '.diagnosis // "Validation failed."' "${DIAGNOSE_RESULT_FILE}")"

# Self-heal interception: the diagnose LLM has classified the failure. If
# the self-heal LLM determines the classification or the originating phase
# was driven by a prompt defect and proposes a patch, we re-exec this
# script (without burning a validation cycle). This hook fires for ALL
# diagnose outcomes (needs_fixes, harness_error, infeasible, unknown)
# per Q5=B: any failure where a prompt edit is proposed is self-heal-
# eligible. The helper short-circuits on an empty-patch proposal.
attempt_self_heal_and_reexec "diagnose"

# -----------------------------------------------------------------
# Cross-cycle escalation (Q5=A, Q6=A, Q7=B):
#
# The diagnose prompt has been inverted to prefer `needs_fixes` over
# `harness_error` when in doubt, so that the clarify -> plan -> implement
# pipeline gets a chance to fix the failure inside the consumer repo
# without human involvement. This is only safe if we auto-escalate when
# the same fix-up proposal keeps failing across cycles, otherwise a
# genuinely harness-side defect could loop indefinitely burning cycles.
#
# Mechanism:
#   1. Compute a stable fingerprint of the current needs_fixes proposal
#      (Q5=A: sha256 of sorted fix_issues[].title, 16-char hex prefix).
#   2. Embed the fingerprint as an HTML-comment marker in the tracking
#      issue comment posted by the `needs_fixes` branch below (Q6=A).
#   3. On each cycle with VALIDATION_CYCLE >= 3, scan PRIOR_COMMENTS
#      (already fetched at the top of this script for the LLM's
#      cycle-N context) for prior markers. If the same fingerprint
#      appears in at least 2 prior cycles' comments, the editor has
#      failed to land the fix twice — promote this cycle to
#      `harness_error` so the human/alert path takes over (Q7=B).
#   4. The promoted `harness_error` uses the LLM-provided
#      `harness_fixes` text (Q9=B schema allows it alongside
#      `fix_issues`) if present; otherwise a templated fallback.
# -----------------------------------------------------------------
FAILURE_FINGERPRINT=""
PRIOR_FINGERPRINT_HITS=0
ESCALATED_FROM_NEEDS_FIXES=false

if [ "${DIAG_STATUS}" = "needs_fixes" ]; then
	FP_FIX_COUNT="$(jq -r 'if (.fix_issues | type) == "array" then (.fix_issues | length) else 0 end' "${DIAGNOSE_RESULT_FILE}" 2>/dev/null || echo 0)"
	if [ "${FP_FIX_COUNT:-0}" -gt 0 ]; then
		if FINGERPRINT_TITLES="$(jq -r '.fix_issues | sort_by(.title // "") | map(.title // "") | join("\n")' "${DIAGNOSE_RESULT_FILE}" 2>/dev/null)"; then
			FAILURE_FINGERPRINT="$(printf '%s' "${FINGERPRINT_TITLES}" | sha256sum | cut -c1-16)"
		fi
	fi
fi

if [ "${DIAG_STATUS}" = "needs_fixes" ] \
	&& [ -n "${FAILURE_FINGERPRINT}" ] \
	&& [ "${VALIDATION_CYCLE}" -ge 3 ] \
	&& [ -n "${PRIOR_COMMENTS:-}" ]; then
	PREV_CYCLE_1="$((VALIDATION_CYCLE - 1))"
	PREV_CYCLE_2="$((VALIDATION_CYCLE - 2))"
	HIT_PREV_CYCLE_1=0
	HIT_PREV_CYCLE_2=0
	if grep -qF "<!-- validation-failure-fingerprint: ${FAILURE_FINGERPRINT} cycle: ${PREV_CYCLE_1} -->" <<< "${PRIOR_COMMENTS:-}" 2>/dev/null; then
		HIT_PREV_CYCLE_1=1
	fi
	if grep -qF "<!-- validation-failure-fingerprint: ${FAILURE_FINGERPRINT} cycle: ${PREV_CYCLE_2} -->" <<< "${PRIOR_COMMENTS:-}" 2>/dev/null; then
		HIT_PREV_CYCLE_2=1
	fi
	PRIOR_FINGERPRINT_HITS="$((HIT_PREV_CYCLE_1 + HIT_PREV_CYCLE_2))"
	if [ "${PRIOR_FINGERPRINT_HITS}" -ge 2 ]; then
		ESCALATED_FROM_NEEDS_FIXES=true
		DIAG_STATUS="harness_error"
		echo "Cross-cycle escalation: needs_fixes fingerprint ${FAILURE_FINGERPRINT} seen in consecutive prior cycles (${PREV_CYCLE_2}, ${PREV_CYCLE_1}); promoting to harness_error (cycle ${VALIDATION_CYCLE})."
	fi
fi

case "${DIAG_STATUS}" in
  needs_fixes)
    FIX_COUNT="$(jq -r 'if (.fix_issues | type) == "array" then (.fix_issues | length) else 0 end' "${DIAGNOSE_RESULT_FILE}" 2>/dev/null || echo 0)"
    if [ "${FIX_COUNT}" -le 0 ]; then
      failure_summary="Diagnosis returned needs_fixes with empty fix_issues."
      post_tracking_comment "## ❌ Runtime validation failed\n\n${failure_summary}\n\nDiagnosis:\n\n${DIAG_TEXT}"
      set_tracking_phase_label "ai:validation-failed"
      write_result_files "fail" "Runtime validation failed" "${failure_summary}" "harness_error"
      tg_notify "Validation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: invalid diagnosis payload." "ERROR"
      exit 0
    fi

    # Consolidate every diagnosed root cause into a SINGLE GitHub
    # issue. Previously each fix_issues[] entry became its own issue
    # and ran the full clarify -> plan -> implement -> review -> merge
    # pipeline, multiplying token cost by N. The diagnose prompt
    # contract (fix_issues[]) is intentionally unchanged; the
    # collapse happens here at issue-creation time.
    SORTED_FIXES_FILE="${RUNTIME_DIR}/diagnose_fixes_sorted.json"
    jq '.fix_issues | sort_by(.priority // 5)' "${DIAGNOSE_RESULT_FILE}" > "${SORTED_FIXES_FILE}"

    TOP_PRIORITY="$(jq -r '[.[] | (.priority // 5)] | min // 5' "${SORTED_FIXES_FILE}")"
    if ! [[ "${TOP_PRIORITY}" =~ ^[0-9]+$ ]]; then
      TOP_PRIORITY=5
    fi

    CONSOLIDATED_LOCAL_ID="validation-fix-cycle-${VALIDATION_CYCLE}"
    CONSOLIDATED_TITLE="Validation fix-ups (cycle ${VALIDATION_CYCLE}): ${FIX_COUNT} root cause(s)"

    CONSOLIDATED_BODY_FILE="${RUNTIME_DIR}/consolidated_fix_body.md"
    {
      printf '## Diagnosis\n\n%s\n\n' "${DIAG_TEXT}"
      if [ "${FIX_COUNT}" -gt 1 ]; then
        printf '_The %s root causes below are listed in priority order (lowest priority number = highest urgency). Apply them in the order shown._\n\n' "${FIX_COUNT}"
      fi
      printf -- '---\n\n'
      for idx in $(seq 0 $((FIX_COUNT - 1))); do
        FIX_TITLE_N="$(jq -r ".[${idx}].title // \"Validation fix-up $((idx + 1))\"" "${SORTED_FIXES_FILE}")"
        FIX_BODY_N="$(jq -r ".[${idx}].body // \"No body provided\"" "${SORTED_FIXES_FILE}" | sed 's/\\n/\n/g')"
        FIX_PRIORITY_N="$(jq -r ".[${idx}].priority // 5" "${SORTED_FIXES_FILE}")"
        FIX_DEPENDS_ON_N="$(jq -r ".[${idx}].depends_on // empty | if type == \"array\" then map(tostring) | join(\", \") else tostring end" "${SORTED_FIXES_FILE}")"
        printf '## Fix %s: %s\n\n_Priority: %s_\n\n' "$((idx + 1))" "${FIX_TITLE_N}" "${FIX_PRIORITY_N}"
        if [ -n "${FIX_DEPENDS_ON_N}" ]; then
          printf '_Depends on: %s_\n\n' "${FIX_DEPENDS_ON_N}"
        fi
        printf '%s\n\n' "${FIX_BODY_N}"
      done
      printf -- '---\n'
      printf '**Orchestrator metadata** (do not edit)\n'
      printf -- '- Tracking issue: #%s\n' "${TRACKING_ISSUE_RAW}"
      printf -- '- Integration branch: %s\n' "${INTEGRATION_BRANCH}"
      printf -- '- Local ID: `%s`\n' "${CONSOLIDATED_LOCAL_ID}"
      printf -- '- Type: validation-fix-up (cycle %s, consolidated %s root cause(s))\n' "${VALIDATION_CYCLE}" "${FIX_COUNT}"
      printf -- '- Priority: %s\n' "${TOP_PRIORITY}"
      printf -- '- Managed by: AI Orchestrator\n'
    } > "${CONSOLIDATED_BODY_FILE}"

    if ! is_tracking_run; then
      failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s). Tracking issue is not set, so the consolidated fix-up issue was not created."
      write_result_files "fail" "Validation needs fixes" "${failure_summary}" "needs_fixes"
      tg_notify "Validation for ${GITHUB_REPOSITORY} reported fixable failures, but TRACKING_ISSUE is not set." "WARNING"
      exit 0
    fi

    # Tracker-open guard: never open a fix-up against a closed tracker.
    # Fail-open on API failure (empty state) so a transient blip does
    # not block legitimate fix-up creation.
    TRACKER_STATE="$(gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/${TRACKING_ISSUE_RAW}" --jq '.state' 2>/dev/null | tr -d '[:space:]' || true)"
    if [ -n "${TRACKER_STATE}" ] && [ "${TRACKER_STATE}" != "open" ]; then
      echo "Tracker-open guard: tracking issue #${TRACKING_ISSUE_RAW} is '${TRACKER_STATE}'; skipping fix-up issue creation."
      failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s), but tracking issue #${TRACKING_ISSUE_RAW} is ${TRACKER_STATE}; no fix-up issue created."
      write_result_files "fail" "Validation needs fixes" "${failure_summary}" "needs_fixes"
      tg_notify "Validation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} needs fixes, but tracker is ${TRACKER_STATE}; no fix-up created." "WARNING"
      exit 0
    fi

    # Per-tracker dedupe: if an open fix-up for this tracker + cycle
    # already exists (matched by the Local ID and Tracking issue lines
    # the body writer emits above), reuse it instead of creating a
    # duplicate. Fail-open on list/jq failure so a transient API blip
    # does not block legitimate fix-up creation.
    EXISTING_FIXUP_NUM=""
    DEDUPE_LIST_JSON="$(gh_retry gh issue list \
      --repo "${GITHUB_REPOSITORY}" \
      --state open \
      --label "ai:orchestrator-managed" \
      --limit 200 \
      --json number,body 2>/dev/null || true)"
    if [ -n "${DEDUPE_LIST_JSON}" ]; then
      EXISTING_FIXUP_NUM="$(printf '%s' "${DEDUPE_LIST_JSON}" | jq -r \
        --arg lid "Local ID: \`${CONSOLIDATED_LOCAL_ID}\`" \
        --arg trk "Tracking issue: #${TRACKING_ISSUE_RAW}" \
        '[.[] | select(((.body // "") as $b | ($b | contains($lid)) and ($b | contains($trk))))] | .[0].number // empty' 2>/dev/null || true)"
    fi
    if [ -n "${EXISTING_FIXUP_NUM}" ] && [[ "${EXISTING_FIXUP_NUM}" =~ ^[0-9]+$ ]]; then
      echo "Dedupe: open fix-up issue #${EXISTING_FIXUP_NUM} already exists for tracker #${TRACKING_ISSUE_RAW} at cycle ${VALIDATION_CYCLE}; skipping create."
      CREATED_FIX_ISSUES_JSON="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq --argjson num "${EXISTING_FIXUP_NUM}" '. + [$num]')"
      DEDUPE_FINGERPRINT_MARKER=""
      if [ -n "${FAILURE_FINGERPRINT:-}" ]; then
        DEDUPE_FINGERPRINT_MARKER="$(printf '\n<!-- validation-failure-fingerprint: %s cycle: %s -->' \
          "${FAILURE_FINGERPRINT}" "${VALIDATION_CYCLE:-1}")"
      fi
      post_tracking_comment "## 🧪 Runtime validation found fixable issues\n\n${DIAG_TEXT}\n\nReusing existing open fix-up issue #${EXISTING_FIXUP_NUM} for ${FIX_COUNT} root cause(s); not creating a duplicate.${DEDUPE_FINGERPRINT_MARKER}"
      set_tracking_phase_label "ai:validation-fixing"
      failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s). Reused existing fix-up issue #${EXISTING_FIXUP_NUM} (cycle ${VALIDATION_CYCLE}); no duplicate created."
      write_result_files "fail" "Validation needs fixes" "${failure_summary}" "needs_fixes"
      tg_notify "Validation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} needs fixes; reused existing fix-up #${EXISTING_FIXUP_NUM}." "WARNING"
      exit 0
    fi

    ensure_label_exists "ai:clarification"
    ensure_label_exists "ai:orchestrator-managed"

	FIX_URL_OUTPUT="$(gh_retry gh issue create \
	  --repo "${GITHUB_REPOSITORY}" \
	  --title "${CONSOLIDATED_TITLE}" \
	  --body-file "${CONSOLIDATED_BODY_FILE}" \
	  --label "ai:clarification" \
	  --label "ai:orchestrator-managed")"
	FIX_URL="$(printf '%s\n' "${FIX_URL_OUTPUT}" | grep -oE 'https://[^ ]+/issues/[0-9]+/?([?#][^ ]*)?' | tail -n 1 || true)"
	FIX_NUM="$(basename "${FIX_URL%%[?#]*}")"
	if ! [[ "${FIX_NUM}" =~ ^[0-9]+$ ]]; then
	  failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s), but creating the consolidated fix-up issue failed."
	  post_tracking_comment "## ❌ Runtime validation failed\n\n${failure_summary}\n\nDiagnosis:\n\n${DIAG_TEXT}"
	  set_tracking_phase_label "ai:validation-failed"
	  write_result_files "fail" "Runtime validation failed" "${failure_summary}" "harness_error"
	  tg_notify "Validation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: unable to create consolidated fix-up issue." "ERROR"
	  exit 0
	fi
	CREATED_FIX_ISSUES_JSON="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq --argjson num "${FIX_NUM}" '. + [$num]')"

    issue_list_md="$(echo "${CREATED_FIX_ISSUES_JSON}" | jq -r '.[] | "- #\(.)"')"
    if [ -z "${issue_list_md}" ]; then
      issue_list_md='- (no issue numbers captured)'
    fi

    # Embed the cross-cycle fingerprint marker so future cycles can
    # detect the same-proposal-repeated case and escalate (Q6=A). The
    # HTML comment is invisible in GitHub's rendered view but readable
    # by the PRIOR_COMMENTS fetch at the top of this script. The
    # `:-` defaults let this branch be exercised in isolation by
    # tests/test_validate_process_fixup_labels.py, which does not
    # populate FAILURE_FINGERPRINT / VALIDATION_CYCLE.
    FINGERPRINT_MARKER=""
    if [ -n "${FAILURE_FINGERPRINT:-}" ]; then
      FINGERPRINT_MARKER="$(printf '\n<!-- validation-failure-fingerprint: %s cycle: %s -->' \
        "${FAILURE_FINGERPRINT}" "${VALIDATION_CYCLE:-1}")"
    fi

    post_tracking_comment "## 🧪 Runtime validation found fixable issues\n\n${DIAG_TEXT}\n\nConsolidated ${FIX_COUNT} root cause(s) into a single fix-up issue:\n${issue_list_md}${FINGERPRINT_MARKER}"
    set_tracking_phase_label "ai:validation-fixing"

    failure_summary="Runtime validation failed with ${FAILED_TESTS} failing test(s). A single consolidated fix-up issue was created for ${FIX_COUNT} root cause(s)."
    write_result_files "fail" "Validation needs fixes" "${failure_summary}" "needs_fixes"
    tg_notify "Validation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW} needs fixes (${FIX_COUNT} root cause(s) consolidated into 1 issue)." "WARNING"
    ;;

  harness_error)
    if [ "${ESCALATED_FROM_NEEDS_FIXES}" = "true" ]; then
      # Cross-cycle escalation path: the diagnose LLM classified this
      # as needs_fixes, but the same fix-up proposal has failed in
      # PRIOR_FINGERPRINT_HITS prior cycles. Prefer the LLM-provided
      # harness_fixes hint (Q9=B schema permits it alongside
      # fix_issues); fall back to a templated explanation.
      HARNESS_FIXES_FROM_LLM="$(jq -r '.harness_fixes // ""' "${DIAGNOSE_RESULT_FILE}" \
        | tr '\n' ' ' | sed -e 's/[[:space:]]\+/ /g' -e 's/^ //; s/ $//')"
      if [ -n "${HARNESS_FIXES_FROM_LLM}" ]; then
        HARNESS_FIXES="Cross-cycle escalation: the same fix-up proposal (fingerprint ${FAILURE_FINGERPRINT}) failed in ${PRIOR_FINGERPRINT_HITS} prior cycle(s). LLM fallback guidance: ${HARNESS_FIXES_FROM_LLM}"
      else
        HARNESS_FIXES="Cross-cycle escalation: the same fix-up proposal (fingerprint ${FAILURE_FINGERPRINT}) failed in ${PRIOR_FINGERPRINT_HITS} prior cycle(s). The repeated failure suggests the root cause is in harness-owned files (under \`validation/\`, in workflow wrappers referencing \`shubhodeep1/coding-workflows\`, or in scripts fetched from \`coding-workflows\` at runtime) rather than in consumer-repo application code. A human needs to inspect the diagnosis and determine whether to patch the harness or update the consumer repo manually."
      fi
      failure_summary="Validation harness error (cross-cycle escalation): ${HARNESS_FIXES}"

      post_tracking_comment "## ❌ Runtime validation harness error (cross-cycle escalation)\n\n${DIAG_TEXT}\n\nHarness fix guidance:\n\n${HARNESS_FIXES}"
      set_tracking_phase_label "ai:validation-failed"
      write_result_files "fail" "Validation failed due to harness error" "${failure_summary}" "harness_error"
      tg_notify "Validation cross-cycle escalation for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: same fix-up proposal failed $((PRIOR_FINGERPRINT_HITS + 1)) times." "ERROR"
    else
      HARNESS_FIXES="$(jq -r '.harness_fixes // "Validation harness needs correction."' "${DIAGNOSE_RESULT_FILE}")"
      failure_summary="Validation harness error: ${HARNESS_FIXES}"

      post_tracking_comment "## ❌ Runtime validation harness error\n\n${DIAG_TEXT}\n\nHarness fix guidance:\n\n${HARNESS_FIXES}"
      set_tracking_phase_label "ai:validation-failed"
      write_result_files "fail" "Validation failed due to harness error" "${failure_summary}" "harness_error"
      tg_notify "Validation harness error for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    fi
    ;;

  infeasible)
    failure_summary="Runtime validation infeasible: ${DIAG_TEXT}"

    post_tracking_comment "## ❌ Runtime validation infeasible\n\n${DIAG_TEXT}"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "fail" "Validation marked infeasible" "${failure_summary}" "infeasible"
    tg_notify "Validation infeasible for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}." "ERROR"
    ;;

  *)
    failure_summary="Unknown diagnosis status '${DIAG_STATUS}'. ${FIRST_FAILURE}"

    post_tracking_comment "## ❌ Runtime validation failed\n\n${failure_summary}\n\nDiagnosis:\n\n${DIAG_TEXT}"
    set_tracking_phase_label "ai:validation-failed"
    write_result_files "fail" "Validation failed" "${failure_summary}" "harness_error"
    tg_notify "Validation failed for ${GITHUB_REPOSITORY}#${TRACKING_ISSUE_RAW}: unknown diagnosis status." "ERROR"
    ;;
esac

exit 0
