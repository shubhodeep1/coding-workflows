#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

unset GIT_DIR GIT_WORK_TREE

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

workspace="${tmpdir}/workspace"
mkdir -p "${workspace}"

set +e
invalid_output="$({
	WORKTREE_REGISTRY_ROOT="${workspace}" \
	bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
		"bad name" \
		"${workspace}/bad-name" \
		"refs/heads/main" \
		"task-invalid" \
		"orchestrate-poll"
} 2>&1)"
invalid_rc=$?
set -e

[ "${invalid_rc}" -ne 0 ]
printf '%s\n' "${invalid_output}" | grep -F "WORKTREE_REGISTER_INVALID_NAME name=bad name" >/dev/null
[ ! -e "${workspace}/.worktrees/index.json" ]

for valid_name in \
	"premerge-int-sync.ABC123" \
	"resolver-tooling-refresh-wt-12345-1" \
	"branch-rebuild-wt.ABC123"
do
	WORKTREE_REGISTRY_ROOT="${workspace}" \
	GITHUB_RUN_ID="valid-run" \
	bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
		"${valid_name}" \
		"${workspace}/${valid_name}" \
		"refs/heads/${valid_name}" \
		"task-${valid_name}" \
		"orchestrate-poll" >/dev/null
done

list_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list)"
PYTHONDONTWRITEBYTECODE=1 python3 - "${list_json}" <<'PY'
from __future__ import annotations

import json
import sys


entries = json.loads(sys.argv[1])
assert {entry["name"] for entry in entries} == {
	"premerge-int-sync.ABC123",
	"resolver-tooling-refresh-wt-12345-1",
	"branch-rebuild-wt.ABC123",
}, entries
PY

echo "test_worktree_name_validation.sh: PASS"
