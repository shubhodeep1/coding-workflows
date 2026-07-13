#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

with_temp_workspace()
{
	local callback="$1"
	local tmpdir=""
	tmpdir="$(mktemp -d)"
	trap 'rm -rf "${tmpdir}"' RETURN
	"${callback}" "${tmpdir}"
	trap - RETURN
	rm -rf "${tmpdir}"
}

test_round_trip()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local index_path="${workspace}/.worktrees/index.json"
	mkdir -p "${workspace}"

	WORKTREE_REGISTRY_ROOT="${workspace}" \
	GITHUB_RUN_ID="run-123" \
	bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
		"wt-one" \
		"${workspace}/wt-one" \
		"refs/heads/main" \
		"task-1" \
		"orchestrate-poll" >/dev/null

	WORKTREE_REGISTRY_ROOT="${workspace}" \
	GITHUB_RUN_ID="run-123" \
	bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
		"wt-two" \
		"${workspace}/wt-two" \
		"refs/heads/feature" \
		"task-2" \
		"review-autofix" >/dev/null

	list_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list)"
	task_filtered_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list --task "task-2")"
	phase_filtered_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list --owner-phase "review-autofix")"

	PYTHONDONTWRITEBYTECODE=1 python3 - "${index_path}" "${list_json}" "${task_filtered_json}" "${phase_filtered_json}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


index_path = Path(sys.argv[1])
registry = json.loads(index_path.read_text(encoding="utf-8"))
assert registry["schema_version"] == "worktree_registry.v1.json", registry
entries = registry["entries"]
assert len(entries) == 2, entries

list_entries = json.loads(sys.argv[2])
assert {entry["name"] for entry in list_entries} == {"wt-one", "wt-two"}, list_entries
assert {entry["owner_run_id"] for entry in list_entries} == {"run-123"}, list_entries

task_entries = json.loads(sys.argv[3])
assert len(task_entries) == 1, task_entries
assert task_entries[0]["name"] == "wt-two", task_entries

phase_entries = json.loads(sys.argv[4])
assert len(phase_entries) == 1, phase_entries
assert phase_entries[0]["task_id"] == "task-2", phase_entries
PY

	WORKTREE_REGISTRY_ROOT="${workspace}" \
	bash "${REPO_ROOT}/scripts/worktree_registry.sh" deregister "wt-one" >/dev/null

	remaining_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list)"
	PYTHONDONTWRITEBYTECODE=1 python3 - "${remaining_json}" <<'PY'
from __future__ import annotations

import json
import sys


entries = json.loads(sys.argv[1])
assert len(entries) == 1, entries
assert entries[0]["name"] == "wt-two", entries
PY
}

test_concurrent_registers()
{
	local tmpdir="$1"
	local workspace="${tmpdir}/workspace"
	local index_path="${workspace}/.worktrees/index.json"
	mkdir -p "${workspace}"

	for worker in $(seq 1 12); do
		(
			WORKTREE_REGISTRY_ROOT="${workspace}" \
			GITHUB_RUN_ID="concurrent-${worker}" \
			bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
				"wt-${worker}" \
				"${workspace}/wt-${worker}" \
				"refs/heads/branch-${worker}" \
				"task-${worker}" \
				"orchestrate-poll" >/dev/null
		) &
	done
	wait

	all_entries_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list)"
	PYTHONDONTWRITEBYTECODE=1 python3 - "${index_path}" "${all_entries_json}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


index_path = Path(sys.argv[1])
registry = json.loads(index_path.read_text(encoding="utf-8"))
entries = registry["entries"]
assert len(entries) == 12, entries
assert len({entry["name"] for entry in entries}) == 12, entries

listed = json.loads(sys.argv[2])
assert len(listed) == 12, listed
assert {entry["name"] for entry in listed} == {f"wt-{i}" for i in range(1, 13)}, listed
PY
}

with_temp_workspace test_round_trip
with_temp_workspace test_concurrent_registers
echo "test_worktree_registry.sh: PASS"
