#!/usr/bin/env bash
# shellcheck shell=bash
# Body of the "Attempt post-Codex syntax repair" step (id post_codex_syntax_repair) of .github/workflows/implement.yml,
# moved out of the workflow for #9678 to keep implement.yml well under the
# 480,000-byte workflow file size guard (tests/test_workflow_file_size_limit.py).
# Runs after the editor's writes are copied back when changed-file syntax
# validation failed: it asks the isolated repair agent to fix the syntax
# errors and writes the step output repaired=true|false.
#
# The step keeps its name:, id:, if:, env: and continue-on-error: in the
# workflow and sources this file in the step shell, only from the trusted
# pre-editor copy in IMPLEMENT_STAGED_SUPPORT_RUN_DIR; it never runs a
# workspace copy. Pass values through the step env: (GitHub does not
# substitute workflow expressions inside a script file).
set -euo pipefail
echo "repaired=false" >> "$GITHUB_OUTPUT"

# Source sanitize_codex_prompt_file (best-effort) so the
# repair-prompt pipe to codex strips invalid UTF-8 first.
# shellcheck source=/dev/null
source "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/gh_helpers.sh" 2>/dev/null || true

REPAIR_PROMPT_TEMPLATE="prompts/mode-implement-repair.txt"
REPAIR_VALIDATOR_SCRIPT="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/validate_changed_files_syntax.sh"
LEDGER_SUBSTATE_HELPER="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/ledger_emit_substate.sh"

# The repair agent runs in the same credential-free, network-isolated
# container as the implementation attempts (scripts/codex_isolated_exec.sh
# from the trusted IMPLEMENT_STAGED_SUPPORT_RUN_DIR copy), reusing the
# sandbox the implementation step prepared when it is still there.
CODEX_ISOLATED_EXEC="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}/codex_isolated_exec.sh"
if [ -z "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-}" ] || [ ! -f "${CODEX_ISOLATED_EXEC}" ]; then
  echo "::error::CODEX_ISOLATION trusted support copy missing (IMPLEMENT_STAGED_SUPPORT_RUN_DIR=${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-<unset>}); Codex is never run on the host."
  exit 1
fi
CODEX_ISOLATED_ROOT="${CODEX_ISOLATED_IMPLEMENT_ROOT:-}"
if [ -z "${CODEX_ISOLATED_ROOT}" ] || [ ! -d "${CODEX_ISOLATED_ROOT}" ]; then
  # Only the sandbox image build is retried (bounded, #7196); every
  # other isolation failure still fails the step on its first try.
  if ! CODEX_ISOLATED_ROOT="$(CODEX_ISOLATED_IMAGE_BUILD_ATTEMPTS=3 bash "${CODEX_ISOLATED_EXEC}" prepare --workdir "$(pwd)" --deps)"; then
    echo "::error::CODEX_ISOLATION sandbox preparation failed; Codex is never run on the host."
    exit 1
  fi
fi
CODEX_ISOLATED_MODE="workspace"
export CODEX_ISOLATED_EXEC CODEX_ISOLATED_ROOT CODEX_ISOLATED_MODE
emit_implement_substate() {
  local phase_name="$1"
  local mode_name="$2"
  local event_or_substate="$3"
  local attempt_number="$4"
  local tokens_log_file="${5:-}"
  local args=()

  [ -f "${LEDGER_SUBSTATE_HELPER}" ] || return 0

  args=(
    --run-id "${GITHUB_RUN_ID}"
    --workflow "implement"
    --phase "${phase_name}"
    --mode "${mode_name}"
    --attempt "${attempt_number}"
    --model "${MODEL_EDITOR:-}"
    --issue-number "${ISSUE_NUMBER:-}"
    --actor "${GITHUB_ACTOR:-codex-bot}"
    --repo-root "${GITHUB_WORKSPACE:-$(pwd)}"
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
CAPTURE_FILE="${RUNTIME_DIR}/post_codex_validation_errors.txt"
INITIAL_CHANGED_FILE="${RUNTIME_DIR}/post_codex_initial_changed_files.txt"
CAPTURED_FILES_FILE="${RUNTIME_DIR}/post_codex_captured_files.txt"
ALLOW_LIST_FILE="${RUNTIME_DIR}/post_codex_repair_allow_list.txt"

if [ ! -s "${REPAIR_PROMPT_TEMPLATE}" ] || [ ! -s "${REPAIR_VALIDATOR_SCRIPT}" ]; then
  echo "::error::Required repair artifacts are missing from repair-prompt-and-validator-split dependency."
  echo "Expected prompt: ${REPAIR_PROMPT_TEMPLATE} (non-empty)"
  echo "Expected validator: ${REPAIR_VALIDATOR_SCRIPT} (non-empty)"
  exit 1
fi

max_attempts_raw="${MAX_POST_CODEX_REPAIR_ATTEMPTS:-3}"
if ! [[ "${max_attempts_raw}" =~ ^[0-9]+$ ]] || [ "${max_attempts_raw}" -lt 0 ]; then
  echo "::warning::Invalid MAX_POST_CODEX_REPAIR_ATTEMPTS='${max_attempts_raw}'. Falling back to 3."
  max_attempts=3
else
  max_attempts="${max_attempts_raw}"
fi

if [ "${max_attempts}" -eq 0 ]; then
  echo "::notice::MAX_POST_CODEX_REPAIR_ATTEMPTS=0; skipping in-job post-Codex syntax repair attempts."
  exit 1
fi

# Repair phases honour MODEL_REPAIR_REASONING_EFFORT (default
# "high"), separate from the editor's MODEL_REASONING_EFFORT so
# operators can drop repair effort independently of the main
# editor. Patch ~/.codex/config.toml in-place so `codex exec`
# honors it (the CLI has no --reasoning-effort flag). Validate
# against the known set first; fall back with a warning on a
# typo.
REPAIR_REASONING="${MODEL_REPAIR_REASONING_EFFORT:-high}"
case "${REPAIR_REASONING}" in
  xhigh|high|medium|low|none) ;;
  *)
    echo "::warning::Invalid MODEL_REPAIR_REASONING_EFFORT='${REPAIR_REASONING}'. Falling back to 'high'."
    REPAIR_REASONING="high"
    ;;
esac
REPAIR_CODEX_CONFIG="${HOME:-/root}/.codex/config.toml"
upsert_repair_reasoning_into_config() {
  local cfg="$1"
  PYTHONDONTWRITEBYTECODE=1 python3 - "${cfg}" "${REPAIR_REASONING}" <<'PY' || return 1
from pathlib import Path
import re
import sys

config_path = Path(sys.argv[1])
reasoning = sys.argv[2]
replacement = f'model_reasoning_effort = "{reasoning}"\n'

if not config_path.exists():
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(replacement, encoding="utf-8")
    raise SystemExit(0)

text = config_path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

first_table_idx = len(lines)
for idx, line in enumerate(lines):
    stripped = line.lstrip()
    if re.match(r"^(\[[^\]]+\]|\[\[[^\]]+\]\])(?:[ \t]+#.*)?$", stripped):
        first_table_idx = idx
        break

top_level_lines = lines[:first_table_idx]
rest_lines = lines[first_table_idx:]
pattern = re.compile(r"^[ \t]*model_reasoning_effort[ \t]*=")

updated_top = []
replaced = False
for line in top_level_lines:
    if pattern.match(line):
        if not replaced:
            updated_top.append(replacement)
            replaced = True
        continue
    updated_top.append(line)

if not replaced:
    if updated_top and not updated_top[-1].endswith("\n"):
        updated_top[-1] += "\n"
    updated_top.append(replacement)

config_path.write_text("".join(updated_top + rest_lines), encoding="utf-8")
PY
}
if ! upsert_repair_reasoning_into_config "${REPAIR_CODEX_CONFIG}"; then
  echo "::warning::Failed to patch ~/.codex/config.toml for repair reasoning; leaving existing config unchanged."
else
  echo "Patched ~/.codex/config.toml: model_reasoning_effort=${REPAIR_REASONING} for repair phase."
fi

# No Serena hints: the agent runs in the isolated container
# (scripts/codex_isolated_exec.sh), where no MCP server is
# configured, so a hint would only send it to tools that are not
# there. Semble results are unaffected: they are rendered into the
# prompt on the host.
REPAIR_SERENA_TOOL_HINTS=""
REPAIR_RENDERED_PROMPT_TEMPLATE="${RUNTIME_DIR}/mode-implement-repair.rendered.txt"
if SERENA_TOOL_HINTS="${REPAIR_SERENA_TOOL_HINTS}" bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/render_prompt.sh" "${REPAIR_PROMPT_TEMPLATE}" > "${REPAIR_RENDERED_PROMPT_TEMPLATE}"; then
  REPAIR_PROMPT_TEMPLATE="${REPAIR_RENDERED_PROMPT_TEMPLATE}"
else
  echo "::warning::Failed to render repair prompt template ${REPAIR_PROMPT_TEMPLATE}; using raw prompt."
fi
REPAIR_CONTINUATION_PROMPT=""
if [ -s "prompts/mode-implement-repair-continuation.txt" ]; then
  REPAIR_CONTINUATION_PROMPT="${RUNTIME_DIR}/mode-implement-repair-continuation.rendered.txt"
  if ! SERENA_TOOL_HINTS="${REPAIR_SERENA_TOOL_HINTS}" bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/render_prompt.sh" "prompts/mode-implement-repair-continuation.txt" > "${REPAIR_CONTINUATION_PROMPT}"; then
    echo "::warning::Failed to render repair continuation prompt; later repair attempts will fall back to the full prompt path."
    REPAIR_CONTINUATION_PROMPT=""
  fi
fi

: > "${INITIAL_CHANGED_FILE}"
while IFS= read -r -d '' path; do
  printf '%s\n' "${path}" >> "${INITIAL_CHANGED_FILE}"
done < <(git diff --name-only --diff-filter=ACMR -z HEAD)
while IFS= read -r -d '' path; do
  printf '%s\n' "${path}" >> "${INITIAL_CHANGED_FILE}"
done < <(git ls-files --others --exclude-standard -z -- '*.py' '*.js' '*.sh' '*.yml' '*.yaml' '*.json')
sort -u "${INITIAL_CHANGED_FILE}" -o "${INITIAL_CHANGED_FILE}" || true

: > "${CAPTURED_FILES_FILE}"
if [ -s "${CAPTURE_FILE}" ]; then
  awk '/^===== / && / \([^)]*\) =====$/ { line=$0; sub(/^===== /, "", line); sub(/ \([^)]*\) =====$/, "", line); print line }' "${CAPTURE_FILE}" > "${CAPTURED_FILES_FILE}" || true
  sort -u "${CAPTURED_FILES_FILE}" -o "${CAPTURED_FILES_FILE}" || true
fi

: > "${ALLOW_LIST_FILE}"
if [ -s "${CAPTURED_FILES_FILE}" ]; then
  comm -12 "${INITIAL_CHANGED_FILE}" "${CAPTURED_FILES_FILE}" > "${ALLOW_LIST_FILE}" || true
else
  cp "${INITIAL_CHANGED_FILE}" "${ALLOW_LIST_FILE}"
fi

if [ ! -s "${ALLOW_LIST_FILE}" ]; then
  echo "::error::Effective post-Codex repair allow-list is empty. Failing repair path deterministically."
  exit 1
fi

is_allowed_path() {
  local p="$1"
  grep -Fqx -- "${p}" "${ALLOW_LIST_FILE}" 2>/dev/null
}

if [ -f "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/semble_helpers.sh" ]; then
  # shellcheck source=/dev/null
  source "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/semble_helpers.sh" 2>/dev/null || true
fi

  build_repair_semble_query() {
    local output_file="$1"
    : > "${output_file}"
    python3 - "${CAPTURE_FILE}" "${ALLOW_LIST_FILE}" "${CAPTURED_FILES_FILE}" "${output_file}" <<'PY' || { echo "::warning::Failed to build repair Semble query" >&2; :; }
import os
import re
import sys

capture_path, allow_list_path, captured_files_path, output_path = sys.argv[1:5]


def read_lines(path: str) -> list[str]:
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as handle:
			return [line.rstrip("\n") for line in handle]
	except OSError:
		return []


capture_lines = read_lines(capture_path)
allow_list_lines = [line.strip() for line in read_lines(allow_list_path) if line.strip()]
captured_file_lines = [line.strip() for line in read_lines(captured_files_path) if line.strip()]
tail_text = "\n".join(capture_lines[-120:])

seen: set[str] = set()
identifiers: list[str] = []
stop_words = {
	"and",
	"bash",
	"captured",
	"command",
	"diff",
	"error",
	"errors",
	"failed",
	"failure",
	"file",
	"files",
	"from",
	"line",
	"lines",
	"list",
	"path",
	"repair",
	"syntax",
	"the",
	"traceback",
	"validate",
	"validation",
	"warning",
	"with",
	"yaml",
}


def add_identifier(value: str) -> None:
	value = value.strip()
	if not value or value in seen:
		return
	seen.add(value)
	identifiers.append(value)


for path in allow_list_lines[:20]:
	add_identifier(path)
	add_identifier(os.path.basename(path))

for path in captured_file_lines[:20]:
	add_identifier(path)
	add_identifier(os.path.basename(path))

for match in re.findall(r"(?:[A-Za-z0-9_.-]+/)*([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)", tail_text):
	add_identifier(match)

for match in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", tail_text):
	if match.lower() in stop_words:
		continue
	add_identifier(match)
	if len(identifiers) >= 40:
		break

parts: list[str] = ["Implement post-Codex syntax repair"]
if allow_list_lines:
	parts.append("Allow-list files:\n" + "\n".join(allow_list_lines[:20]))
if captured_file_lines:
	parts.append("Captured syntax-failure files:\n" + "\n".join(captured_file_lines[:20]))
if identifiers:
	parts.append("Captured diagnostics identifiers:\n" + "\n".join(identifiers[:40]))

query_text = "\n\n".join(part for part in parts if part).strip()
if query_text:
	with open(output_path, "w", encoding="utf-8") as handle:
		handle.write(query_text)
		handle.write("\n")
PY
  }

REPAIR_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/post_codex_repair_semble_query.txt"
build_repair_semble_query "${REPAIR_SEMBLE_QUERY_FILE}"

# Same editor view as the implementation step: the repair agent
# edits the branch's own helper copies, never SCRIPT_REF's.
STAGED_SUPPORT_WORKSPACE_HELPER="${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/implement_staged_support_workspace.sh"
if [ -f "${STAGED_SUPPORT_WORKSPACE_HELPER}" ]; then
  bash "${STAGED_SUPPORT_WORKSPACE_HELPER}" restore
fi

repair_succeeded=false
for attempt in $(seq 1 "${max_attempts}"); do
  echo "Post-Codex syntax repair attempt ${attempt}/${max_attempts}"
  emit_implement_substate "implement_repair" "implement_repair" "PreparingWorkspace" "${attempt}"

  BASELINE_COMMIT="$(git stash create "post-codex-repair-attempt-${attempt}" || true)"
  if [ -z "${BASELINE_COMMIT}" ]; then
    BASELINE_COMMIT="HEAD"
  fi

  PRE_UNTRACKED_FILE="${RUNTIME_DIR}/post_codex_pre_untracked_attempt_${attempt}.txt"
  : > "${PRE_UNTRACKED_FILE}"
  while IFS= read -r -d '' pre_untracked; do
    [ -n "${pre_untracked}" ] || continue
    if ! is_allowed_path "${pre_untracked}"; then
      printf '%s\n' "${pre_untracked}" >> "${PRE_UNTRACKED_FILE}"
    fi
  done < <(git ls-files --others --exclude-standard -z)
  sort -u "${PRE_UNTRACKED_FILE}" -o "${PRE_UNTRACKED_FILE}" || true

  get_blob_for_source() {
    local source_ref="$1"
    local file_path="$2"
    if git cat-file -e "${source_ref}:${file_path}" >/dev/null 2>&1; then
      git rev-parse "${source_ref}:${file_path}" 2>/dev/null || true
    else
      echo "__MISSING__"
    fi
  }

  REPAIR_PROMPT_FILE="${RUNTIME_DIR}/mode-implement-repair-attempt-${attempt}.txt"
  {
    cat "${REPAIR_PROMPT_TEMPLATE}"
    echo
    echo "=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ==="
    if [ -s "${CAPTURE_FILE}" ]; then
      cat "${CAPTURE_FILE}"
    else
      echo "(capture file missing or empty)"
    fi
    echo
    echo "=== INITIAL POST-CODEX CHANGED FILES ==="
    cat "${INITIAL_CHANGED_FILE}"
    echo
    echo "=== EFFECTIVE REPAIR ALLOW-LIST ==="
    cat "${ALLOW_LIST_FILE}"
    echo
    echo "=== CURRENT DIFF FOR ALLOW-LIST FILES ==="
    while IFS= read -r allowed_file; do
      [ -n "${allowed_file}" ] || continue
      echo "----- ${allowed_file} -----"
      if git ls-files --error-unmatch -- "${allowed_file}" >/dev/null 2>&1; then
        git diff -- "${allowed_file}" || true
      elif [ -f "${allowed_file}" ]; then
        git diff --no-index -- /dev/null "${allowed_file}" || true
      else
        echo "(file not found in working tree)"
      fi
    done < "${ALLOW_LIST_FILE}"
    echo
    if [ -s "${REPAIR_SEMBLE_QUERY_FILE}" ] && declare -F semble_query_block >/dev/null 2>&1; then
      SEMBLE_STATIC_CONTEXT_FILE=./pre_assembled_static.txt semble_query_block "$(cat "${REPAIR_SEMBLE_QUERY_FILE}")" 6 "Implement Repair Context" || true
    fi
  } > "${REPAIR_PROMPT_FILE}"
  emit_implement_substate "implement_repair" "implement_repair" "BuildingPrompt" "${attempt}"

  REPAIR_OUTPUT_FILE="${RUNTIME_DIR}/post_codex_repair_output_attempt_${attempt}.txt"
  REPAIR_LOG_FILE="${RUNTIME_DIR}/post_codex_repair_log_attempt_${attempt}.txt"
  repair_thread_reuse_continuation_file=""
  if [ "${attempt}" -gt 1 ] && [ -n "${REPAIR_CONTINUATION_PROMPT}" ]; then
    repair_thread_reuse_continuation_file="${REPAIR_CONTINUATION_PROMPT}"
  fi
  if command -v sanitize_codex_prompt_file >/dev/null 2>&1; then
    sanitize_codex_prompt_file "${REPAIR_PROMPT_FILE}"
  fi
  bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/workspace_safety_check.sh"
  emit_implement_substate "implement_repair" "implement_repair" "LaunchingAgentProcess" "${attempt}" "${REPAIR_LOG_FILE}"
  emit_implement_substate "implement_repair" "implement_repair" "InitializingSession" "${attempt}" "${REPAIR_LOG_FILE}"
  emit_implement_substate "implement_repair" "implement_repair" "StreamingTurn" "${attempt}" "${REPAIR_LOG_FILE}"
  # Same ledger scrub as the implementation launch above.
  repair_direct_rc=0
  CODEX_THREAD_REUSE_STATE_KEY="implement-repair" \
    CODEX_THREAD_REUSE_ENGINE="${AI_ENGINE_RESOLVED_IMPLEMENT_REPAIR:-codex}" \
    CODEX_THREAD_REUSE_ENGINE_ROLE="IMPLEMENT_REPAIR" \
    AI_ENGINE_EFFORT_HINT="${REPAIR_REASONING}" \
    CODEX_THREAD_REUSE_PROMPT_FILE="${REPAIR_PROMPT_FILE}" \
    CODEX_THREAD_REUSE_OUTPUT_FILE="${REPAIR_OUTPUT_FILE}" \
    CODEX_THREAD_REUSE_PHASE="implement_repair" \
    CODEX_THREAD_REUSE_MODEL="${MODEL_EDITOR}" \
    CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK="true" \
    CODEX_THREAD_REUSE_LOG_FILE="${REPAIR_LOG_FILE}" \
    CODEX_THREAD_REUSE_CONTINUATION_FILE="${repair_thread_reuse_continuation_file}" \
    CODEX_THREAD_REUSE_TRANSFORM_MODE="replace-prefix" \
    CODEX_THREAD_REUSE_MARKER_START="=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ===" \
    env -u STAGED_SUPPORT_LEDGER -u STAGED_SUPPORT_BASE_DIR -u STAGED_SUPPORT_EDITOR_HEAD_LEDGER -u IMPLEMENT_STAGED_SUPPORT_RUN_DIR \
    bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/codex_thread_reuse.sh" direct-run || repair_direct_rc=$?
  if [ "${repair_direct_rc}" -eq 80 ]; then
    # Claude capacity wait (issue #7370): stop without codex.
    echo "AI_ENGINE_CAPACITY_WAIT role=IMPLEMENT_REPAIR outcome=deferred stage=implement"
    { echo "SKIP_IMPLEMENT=true"; echo "RESTORE_AWAITING_APPROVAL_ON_SKIP=true"; echo "CLAUDE_CAPACITY_WAIT=true"; } >> "${GITHUB_ENV}"
    exit 0
  fi
  if [ "${repair_direct_rc}" -ne 0 ]; then
    emit_implement_substate "implement_repair" "implement_repair" "Finishing" "${attempt}" "${REPAIR_LOG_FILE}"
    echo "::warning::Post-Codex repair attempt ${attempt} failed while invoking Codex."
    emit_implement_substate "implement_repair" "implement_repair" "Failed" "${attempt}" "${REPAIR_LOG_FILE}"
    continue
  fi
  emit_implement_substate "implement_repair" "implement_repair" "Finishing" "${attempt}" "${REPAIR_LOG_FILE}"

  tracked_strays=()
  while IFS= read -r -d '' changed_path; do
    [ -n "${changed_path}" ] || continue
    if ! is_allowed_path "${changed_path}"; then
      baseline_blob="$(get_blob_for_source "${BASELINE_COMMIT}" "${changed_path}")"
      if [ -f "${changed_path}" ]; then
        current_blob="$(git hash-object -- "${changed_path}" 2>/dev/null || echo "__MISSING__")"
      else
        current_blob="__MISSING__"
      fi
      if [ "${baseline_blob}" != "${current_blob}" ]; then
        tracked_strays+=("${changed_path}")
      fi
    fi
  done < <(git diff --name-only -z HEAD)

  untracked_strays=()
  while IFS= read -r -d '' untracked_path; do
    [ -n "${untracked_path}" ] || continue
    if ! is_allowed_path "${untracked_path}"; then
      if ! grep -Fqx -- "${untracked_path}" "${PRE_UNTRACKED_FILE}" 2>/dev/null; then
        untracked_strays+=("${untracked_path}")
      fi
    fi
  done < <(git ls-files --others --exclude-standard -z)

  if [ "${#tracked_strays[@]}" -gt 0 ] || [ "${#untracked_strays[@]}" -gt 0 ]; then
    echo "::warning::Repair attempt ${attempt} produced out-of-scope edits. Rolling back stray files."
    if [ "${#tracked_strays[@]}" -gt 0 ]; then
      restore_from_baseline=()
      remove_from_baseline=()
      for stray_tracked in "${tracked_strays[@]}"; do
        if git cat-file -e "${BASELINE_COMMIT}:${stray_tracked}" >/dev/null 2>&1; then
          restore_from_baseline+=("${stray_tracked}")
        else
          remove_from_baseline+=("${stray_tracked}")
        fi
      done
      if [ "${#restore_from_baseline[@]}" -gt 0 ]; then
        git restore --source="${BASELINE_COMMIT}" --staged --worktree -- "${restore_from_baseline[@]}"
      fi
      if [ "${#remove_from_baseline[@]}" -gt 0 ]; then
        git rm -r -f --ignore-unmatch -- "${remove_from_baseline[@]}"
      fi
    fi
    if [ "${#untracked_strays[@]}" -gt 0 ]; then
      safe_untracked=()
      for stray in "${untracked_strays[@]}"; do
        case "${stray}" in
          /*|../*|*/../*|*/..)
            echo "::error::Refusing to delete unsafe untracked stray path: ${stray}"
            ;;
          *)
            safe_untracked+=("${stray}")
            ;;
        esac
      done
      if [ "${#safe_untracked[@]}" -gt 0 ]; then
        rm -rf -- "${safe_untracked[@]}"
      fi
    fi
    emit_implement_substate "implement_repair" "implement_repair" "Failed" "${attempt}" "${REPAIR_LOG_FILE}"
    continue
  fi

  rm -f "${CAPTURE_FILE}"
  if CAPTURE_FILE="${CAPTURE_FILE}" RUNTIME_DIR="${RUNTIME_DIR}" bash "${REPAIR_VALIDATOR_SCRIPT}"; then
    repair_succeeded=true
    emit_implement_substate "implement_repair" "implement_repair" "Succeeded" "${attempt}" "${REPAIR_LOG_FILE}"
    break
  fi
  emit_implement_substate "implement_repair" "implement_repair" "Failed" "${attempt}" "${REPAIR_LOG_FILE}"
done

if [ -f "${STAGED_SUPPORT_WORKSPACE_HELPER}" ]; then
  bash "${STAGED_SUPPORT_WORKSPACE_HELPER}" reinstall
fi

if [ "${repair_succeeded}" != 'true' ]; then
  echo "::error::Post-Codex syntax repair did not succeed after ${max_attempts} attempt(s)."
  exit 1
fi

rm -f "${CAPTURE_FILE}"
echo "repaired=true" >> "$GITHUB_OUTPUT"
