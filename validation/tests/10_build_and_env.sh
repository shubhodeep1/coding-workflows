#!/usr/bin/env bash
# validation/tests/10_build_and_env.sh
# Build verification and environment variable validation.
#
# TAP-like output: each assertion emits "ok N - description" or
# "not ok N - description" so the harness can count pass/fail.
#
# set -euo pipefail is intentional: unexpected errors abort early with a
# non-zero exit rather than silently continuing.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-validation/docker-compose.test.yml}"
APP_SERVICE="${APP_SERVICE:-app}"
ASSERTION=1

# ── Assertion 1: docker image was built ────────────────────────────────────
CID=$(docker compose -f "$COMPOSE_FILE" ps -q "$APP_SERVICE" 2>/dev/null | head -n1 || true)
if [ -n "$CID" ]; then
  echo "ok $ASSERTION - docker image built and container exists"
else
  echo "not ok $ASSERTION - docker image not found or container is not running"
  exit 1
fi
ASSERTION=$((ASSERTION + 1))

# ── Assertion 2: container is running ──────────────────────────────────────
RUNNING=$(docker inspect -f '{{.State.Running}}' "$CID" 2>/dev/null || echo "false")
STATUS=$(docker inspect -f '{{.State.Status}}' "$CID" 2>/dev/null || echo "unknown")
if [ "$RUNNING" = "true" ] || [ "$STATUS" = "running" ]; then
  echo "ok $ASSERTION - $APP_SERVICE container is running"
else
  echo "not ok $ASSERTION - $APP_SERVICE container is not running (status=$STATUS)"
  exit 1
fi
ASSERTION=$((ASSERTION + 1))

# ── Assertion 3: synthetic env vars are configured as expected ─────────────
# Docker Compose uses the override-or-default expression for test credentials:
#   VALIDATION_TEST_API_KEY: "${VALIDATION_TEST_API_KEY:-validation-api-key}"
# The container therefore receives EITHER the CI-injected override OR the
# compose-defined default — both are valid.  We mirror that logic here so the
# assertion does not produce a false negative regardless of which branch fires.

actual="$(docker compose -f "$COMPOSE_FILE" exec -T "$APP_SERVICE" \
            /bin/sh -c 'printf "%s" "${VALIDATION_TEST_API_KEY:-}"' 2>/dev/null || true)"

# Compute expected: honour the explicit CI override when present, otherwise
# fall back to the same default that the compose file uses.
if [ -n "${VALIDATION_TEST_API_KEY:-}" ]; then
  expected="${VALIDATION_TEST_API_KEY}"
else
  expected="validation-api-key"
fi

if [ "$actual" = "$expected" ]; then
  echo "ok $ASSERTION - synthetic validation env vars are configured as expected"
else
  echo "not ok $ASSERTION - synthetic validation env vars are not configured as expected (expected='$expected', got='$actual')"
  exit 1
fi
ASSERTION=$((ASSERTION + 1))
