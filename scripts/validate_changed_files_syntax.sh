#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

ERRORS=0

CAPTURE_FILE="${CAPTURE_FILE:-}"
if [ -z "${CAPTURE_FILE}" ] && [ -n "${RUNTIME_DIR:-}" ] && [ -d "${RUNTIME_DIR}" ] && [ -w "${RUNTIME_DIR}" ]; then
  CAPTURE_FILE="${RUNTIME_DIR}/post_codex_validation_errors.txt"
fi

append_checker_error() {
  local file="$1"
  local checker="$2"
  local stderr_file="$3"
  [ -n "${CAPTURE_FILE}" ] || return 0
  {
    printf '===== %s (%s) =====\n' "${file}" "${checker}"
    if [ -s "${stderr_file}" ]; then
      cat "${stderr_file}"
    else
      echo "(no stderr output)"
    fi
    # Surface the offending bytes inline so the post-Codex repair model
    # doesn't have to shell out to rg/grep with literal special
    # characters — that has burned repair turns in production
    # (tele-funtoken-msg-scoring run 25099535242: the model tried
    # `rg "...\`..."` with an unescapable backtick and lost its only
    # repair attempt). yaml.scanner.ScannerError, json.JSONDecodeError,
    # py_compile, and bash -n all surface "line <N>" in stderr; pick
    # the LAST match (Python tracebacks list stack frames first and
    # the actual error location last, so head would land on the wrong
    # line number) and dump <N-2>..<N+2> with line numbers so the
    # repair prompt sees the exact bytes inline. node --check uses a
    # different `<file>:<line>:<col>` format and won't match this
    # regex; that's fine — the offending-bytes block just won't appear
    # for node, and the original stderr is still in the capture.
    if [ -f "${file}" ] && [ -s "${stderr_file}" ]; then
      local lineno start end
      lineno="$(grep -oE 'line[[:space:]]+[0-9]+' "${stderr_file}" | tail -n 1 | grep -oE '[0-9]+' | tail -n 1 || true)"
      if [ -n "${lineno}" ] && [[ "${lineno}" =~ ^[0-9]+$ ]] && [ "${lineno}" -gt 0 ]; then
        start=$(( lineno > 2 ? lineno - 2 : 1 ))
        end=$(( lineno + 2 ))
        printf -- '----- Offending bytes (lines %d-%d; error reported at line %d) -----\n' "${start}" "${end}" "${lineno}"
        sed -n "${start},${end}p" "${file}" | awk -v start="${start}" '{ printf "  %d: %s\n", start + NR - 1, $0 }'
      fi
    fi
    printf '\n'
  } >> "${CAPTURE_FILE}" || true
}

if [ -n "${CAPTURE_FILE}" ]; then
  rm -f "${CAPTURE_FILE}" || true
fi

while IFS= read -r -d '' f; do
  if [ -f "${f}" ] && { [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ] || [[ "${f}" != .github/workflows/* ]]; }; then
    checker_stderr="$(mktemp)"
    if ! python3 -m py_compile "${f}" 2>"${checker_stderr}"; then
      echo "::error file=${f}::Syntax error in ${f}"
      cat "${checker_stderr}" >&2
      append_checker_error "${f}" "python3 -m py_compile" "${checker_stderr}"
      ERRORS=$((ERRORS + 1))
    fi
    rm -f "${checker_stderr}"
  fi
done < <({ git diff --name-only --diff-filter=ACMR -z HEAD -- '*.py'; git ls-files --others --exclude-standard -z -- '*.py'; })

while IFS= read -r -d '' f; do
  if [ -f "${f}" ] && { [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ] || [[ "${f}" != .github/workflows/* ]]; }; then
    checker_stderr="$(mktemp)"
    if ! node --check "${f}" 2>"${checker_stderr}"; then
      echo "::error file=${f}::Syntax error in ${f}"
      cat "${checker_stderr}" >&2
      append_checker_error "${f}" "node --check" "${checker_stderr}"
      ERRORS=$((ERRORS + 1))
    fi
    rm -f "${checker_stderr}"
  fi
done < <({ git diff --name-only --diff-filter=ACMR -z HEAD -- '*.js'; git ls-files --others --exclude-standard -z -- '*.js'; })

while IFS= read -r -d '' f; do
  if [ -f "${f}" ] && { [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ] || [[ "${f}" != .github/workflows/* ]]; }; then
    checker_stderr="$(mktemp)"
    if ! bash -n "${f}" 2>"${checker_stderr}"; then
      echo "::error file=${f}::Shell syntax error in ${f}"
      cat "${checker_stderr}" >&2
      append_checker_error "${f}" "bash -n" "${checker_stderr}"
      ERRORS=$((ERRORS + 1))
    fi
    rm -f "${checker_stderr}"
  fi
done < <({ git diff --name-only --diff-filter=ACMR -z HEAD -- '*.sh'; git ls-files --others --exclude-standard -z -- '*.sh'; })

# Strip a fully-fence-wrapped LLM artifact in place: the *entire* file
# is a single ```/``` block (optionally with a language tag like ```yaml).
# Only fires when the first non-blank line opens a fence and the last
# non-blank line is exactly ```. Files containing inline fenced blocks
# inside multi-document YAML or string values are left untouched.
strip_full_file_fence() {
  local target="$1"
  python3 - "${target}" <<'PY' || true
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
except (OSError, UnicodeDecodeError):
    sys.exit(0)

lines = raw.splitlines()
non_blank_idx = [i for i, line in enumerate(lines) if line.strip()]
if len(non_blank_idx) < 2:
    sys.exit(0)

first, last = non_blank_idx[0], non_blank_idx[-1]
opener = lines[first].strip()
closer = lines[last].strip()
if not opener.startswith("```") or closer != "```":
    sys.exit(0)

stripped = lines[first + 1 : last]
trailing_nl = "\n" if raw.endswith("\n") else ""
with open(path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(stripped))
    if stripped:
        handle.write(trailing_nl)
PY
}

while IFS= read -r -d '' f; do
  if [ -f "${f}" ] && { [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ] || [[ "${f}" != .github/workflows/* ]]; }; then
    strip_full_file_fence "${f}"
    checker_stderr="$(mktemp)"
    if ! python3 -c "import yaml, sys; f=open(sys.argv[1], 'rb'); list(yaml.safe_load_all(f)); f.close()" "${f}" 2>"${checker_stderr}"; then
      echo "::error file=${f}::YAML syntax error in ${f}"
      cat "${checker_stderr}" >&2
      append_checker_error "${f}" "python3 yaml.safe_load" "${checker_stderr}"
      ERRORS=$((ERRORS + 1))
    fi
    rm -f "${checker_stderr}"
  fi
done < <({ git diff --name-only --diff-filter=ACMR -z HEAD -- '*.yml' '*.yaml'; git ls-files --others --exclude-standard -z -- '*.yml' '*.yaml'; })

while IFS= read -r -d '' f; do
  if [ -f "${f}" ] && { [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ] || [[ "${f}" != .github/workflows/* ]]; }; then
    strip_full_file_fence "${f}"
    checker_stderr="$(mktemp)"
    if ! python3 -c "import json, sys; f=open(sys.argv[1], 'rb'); json.load(f); f.close()" "${f}" 2>"${checker_stderr}"; then
      echo "::error file=${f}::JSON syntax error in ${f}"
      cat "${checker_stderr}" >&2
      append_checker_error "${f}" "python3 json.load" "${checker_stderr}"
      ERRORS=$((ERRORS + 1))
    fi
    rm -f "${checker_stderr}"
  fi
done < <({ git diff --name-only --diff-filter=ACMR -z HEAD -- '*.json'; git ls-files --others --exclude-standard -z -- '*.json'; })

if [ "${ERRORS}" -gt 0 ]; then
  echo "::error::${ERRORS} file(s) failed syntax validation."
  exit 1
fi

echo "All changed files passed syntax validation."
