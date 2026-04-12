#!/usr/bin/env bash
# validation/tests/10_runtime_checks.sh
# Runtime checks: guide generation success and determinism.
#
# Outputs TAP-compatible lines to stdout.
# Exits 0 even on failed assertions (TAP convention); exit 1 only on script error.
#
# Usage: bash validation/tests/10_runtime_checks.sh

set -uo pipefail

TESTS_TOTAL=0
TESTS_PASSED=0
TESTS_FAILED=0
FAILED=0

pass() {
  TESTS_TOTAL=$((TESTS_TOTAL + 1))
  TESTS_PASSED=$((TESTS_PASSED + 1))
  echo "ok ${TESTS_TOTAL} - $*"
}

fail() {
  TESTS_TOTAL=$((TESTS_TOTAL + 1))
  TESTS_FAILED=$((TESTS_FAILED + 1))
  FAILED=1
  echo "not ok ${TESTS_TOTAL} - $*"
}

echo "TAP version 13"

# ---------------------------------------------------------------------------
# Assertion A: guide generation command succeeds
# ---------------------------------------------------------------------------

GENERATE_LOG_1=$(mktemp /tmp/generate-guide-first.XXXXXX.log)

if npm run generate-guide >"${GENERATE_LOG_1}" 2>&1; then
  pass "guide generation command succeeds"
else
  fail "guide generation command failed"
  echo "# --- generate-guide output (last 20 lines) ---" >&2
  tail -n 20 "${GENERATE_LOG_1}" >&2
  echo "# --- end of output ---" >&2
fi

# ---------------------------------------------------------------------------
# Assertion B: guide generation is deterministic (only runs if A passed)
# ---------------------------------------------------------------------------

if [ "$FAILED" -eq 0 ]; then
  GUIDE_FIRST=$(mktemp /tmp/GUIDE.first.XXXXXX.md)
  cp GUIDE.md "${GUIDE_FIRST}"

  GENERATE_LOG_2=$(mktemp /tmp/generate-guide-second.XXXXXX.log)

  if npm run generate-guide >"${GENERATE_LOG_2}" 2>&1; then
    if cmp -s GUIDE.md "${GUIDE_FIRST}"; then
      pass "guide generation is deterministic"
    else
      fail "guide generation is non-deterministic"
      echo "# --- diff between first and second generated GUIDE.md ---" >&2
      diff -u "${GUIDE_FIRST}" GUIDE.md >&2 || true
      echo "# --- end of diff ---" >&2
    fi
  else
    fail "second guide generation command failed"
    echo "# --- second generate-guide output (last 20 lines) ---" >&2
    tail -n 20 "${GENERATE_LOG_2}" >&2
    echo "# --- end of output ---" >&2
  fi
fi

echo "1..${TESTS_TOTAL}"

if [ "${TESTS_FAILED}" -gt 0 ]; then
  exit 1
fi
