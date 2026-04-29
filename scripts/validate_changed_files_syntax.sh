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
    #
    # SECURITY: this snippet is later embedded into prompts AND posted
    # to GitHub issues by the diagnose fallback path. Two layered
    # guards prevent secret exfiltration:
    # (a) Path denylist: skip the dump entirely for files whose names
    #     match common secret-bearing patterns (.env, *secret*, *token*,
    #     *.pem, *credential*, *key, etc.). The original stderr (which
    #     just names a line+column) still appears in the capture.
    # (b) Per-line redaction: lines matching a key=value / key: value
    #     pattern with high-entropy or known-credential markers are
    #     emitted as "<redacted>" with the same line number, so the
    #     repair model still sees the file structure but never the
    #     credential.
    if [ -f "${file}" ] && [ -s "${stderr_file}" ]; then
      local lineno start end basename_lc skip_dump=0
      basename_lc="$(printf '%s' "$(basename "${file}")" | tr '[:upper:]' '[:lower:]')"
      # Path denylist: every `.<ext>*` pattern (.env*, .pem*, .key*,
      # .cer*, .crt*, .p12*, .pfx*) deliberately includes a trailing
      # `*` so backup-style names (`.key.bak`, `.pem.old`,
      # `.crt.archived`) and compound-extension credential files
      # (`.keystore`, `.keychain`, `.crt.pem`) are also suppressed.
      # This is over-redaction by design — false positives only cost
      # ~5 lines of diagnostic context (the underlying syntax error
      # still surfaces in stderr), whereas false negatives leak file
      # content into prompts and public issue comments. The same
      # trade-off applies to `.env*` (matches `.env.example`) and
      # `.pem*` (matches `.pem.bak`); `.key*`/`.cer*`/`.crt*` are
      # kept consistent with the rest. Don't tighten any of these
      # to exact-suffix-only without also loosening the others to
      # match — the precision/coverage choice is unified across
      # the alternation.
      case "${file},${basename_lc}" in
        *.env*|*.pem*|*.p12*|*.pfx*|*.key*|*.cer*|*.crt*|\
        *,*secret*|*,*credential*|*,*password*|*,*token*|\
        *,*.envrc|*,.env*)
          skip_dump=1
          ;;
      esac
      if [ "${skip_dump}" = "0" ]; then
        lineno="$(grep -oE 'line[[:space:]]+[0-9]+' "${stderr_file}" | tail -n 1 | grep -oE '[0-9]+' | tail -n 1 || true)"
        if [ -n "${lineno}" ] && [[ "${lineno}" =~ ^[0-9]+$ ]] && [ "${lineno}" -gt 0 ]; then
          start=$(( lineno > 2 ? lineno - 2 : 1 ))
          end=$(( lineno + 2 ))
          printf -- '----- Offending bytes (lines %d-%d; error reported at line %d) -----\n' "${start}" "${end}" "${lineno}"
          # Per-line redaction: any line whose key contains common
          # secret indicators (token/secret/password/credential/api[_-]?key/
          # private[_-]?key/etc.) is replaced with "<redacted: secret-like
          # key>" but keeps its line number. Lines that look like raw
          # high-entropy values (>40 chars of base64/hex on a single line)
          # also redact. Everything else passes through unchanged.
          #
          # Portability: case-insensitive matching is implemented via
          # `tolower(line)` rather than `BEGIN { IGNORECASE = 1 }`,
          # because IGNORECASE is a GNU awk extension and is silently
          # ignored on POSIX awk / BSD awk / mawk (which would let
          # mixed-case secrets like `Api_Token` evade redaction). The
          # key-name regex uses lowercase letters only and matches
          # against the lowercased copy.
          #
          # Key-name regex: matches the keyword anywhere in the leading
          # key-portion of a line (allowing leading whitespace, dashes
          # for YAML list syntax, and surrounding key-name characters).
          # The trailing `[:=]` requirement was DROPPED so that bare
          # YAML keys (`api_token:` at end of line, `api_token: |`
          # block-scalar header) also redact — false positives on
          # innocuous matches are safer than silent secret leaks.
          sed -n "${start},${end}p" "${file}" | awk -v start="${start}" '
            {
              line = $0
              lower = tolower(line)
              # Key-name based redaction (case-insensitive via tolower).
              if (match(lower, /^[[:space:]-]*[a-z0-9_.-]*(secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|bearer)[a-z0-9_.-]*/)) {
                printf "  %d: <redacted: secret-like key>\n", start + NR - 1
                next
              }
              # High-entropy value redaction: a long unbroken token
              # (>=40 chars, no whitespace) on the line — typical of
              # base64/hex secrets even when the key is innocent.
              if (match(line, /[A-Za-z0-9+\/=_-]{40,}/)) {
                printf "  %d: <redacted: long opaque token>\n", start + NR - 1
                next
              }
              printf "  %d: %s\n", start + NR - 1, line
            }
          '
        fi
      else
        printf -- '----- Offending bytes (suppressed: file path matches secret-bearing pattern) -----\n'
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
