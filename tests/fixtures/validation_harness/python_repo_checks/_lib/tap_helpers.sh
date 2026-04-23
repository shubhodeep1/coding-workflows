#!/usr/bin/env bash
set -euo pipefail

tap_ok() {
	local test_id="$1"
	local message="$2"
	echo "ok ${test_id} - ${message}"
}

tap_not_ok() {
	local test_id="$1"
	local message="$2"
	echo "not ok ${test_id} - ${message}"
}
