#!/usr/bin/env bash
set -euo pipefail

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

while IFS= read -r -d '' f; do
  if [ -f "${f}" ] && { [ "${ALLOW_WORKFLOW_EDITS:-true}" = "true" ] || [[ "${f}" != .github/workflows/* ]]; }; then
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

if [ "${ERRORS}" -gt 0 ]; then
  echo "::error::${ERRORS} file(s) failed syntax validation."
  exit 1
fi

echo "All changed files passed syntax validation."
