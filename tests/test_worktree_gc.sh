#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

unset GIT_DIR GIT_WORK_TREE

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

workspace="${tmpdir}/workspace"
runtime_dir="${tmpdir}/runtime"
mkdir -p "${workspace}" "${runtime_dir}"

git -C "${workspace}" init --quiet
git -C "${workspace}" config user.email "test@example.com"
git -C "${workspace}" config user.name "Test User"
printf 'root\n' > "${workspace}/README.md"
git -C "${workspace}" add README.md
git -C "${workspace}" commit --quiet -m "init"

stale_dead_path="${tmpdir}/stale-dead-wt"
stale_live_path="${tmpdir}/stale-live-wt"
fresh_path="${tmpdir}/fresh-wt"

git -C "${workspace}" worktree add --quiet --detach "${stale_dead_path}" HEAD
git -C "${workspace}" worktree add --quiet --detach "${stale_live_path}" HEAD
git -C "${workspace}" worktree add --quiet --detach "${fresh_path}" HEAD

WORKTREE_REGISTRY_ROOT="${workspace}" \
GITHUB_RUN_ID="100" \
bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
	"stale-dead-wt" \
	"${stale_dead_path}" \
	"HEAD" \
	"task-stale-dead" \
	"orchestrate-poll" >/dev/null

WORKTREE_REGISTRY_ROOT="${workspace}" \
GITHUB_RUN_ID="200" \
bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
	"stale-live-wt" \
	"${stale_live_path}" \
	"HEAD" \
	"task-stale-live" \
	"orchestrate-poll" >/dev/null

WORKTREE_REGISTRY_ROOT="${workspace}" \
GITHUB_RUN_ID="300" \
bash "${REPO_ROOT}/scripts/worktree_registry.sh" register \
	"fresh-wt" \
	"${fresh_path}" \
	"HEAD" \
	"task-fresh" \
	"orchestrate-poll" >/dev/null

PYTHONDONTWRITEBYTECODE=1 python3 - "${workspace}/.worktrees/index.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


index_path = Path(sys.argv[1])
payload = json.loads(index_path.read_text(encoding="utf-8"))
for entry in payload["entries"]:
	if entry["name"] in {"stale-dead-wt", "stale-live-wt"}:
		entry["created_at"] = "2000-01-01T00:00:00Z"
index_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
PY

cat > "${runtime_dir}/state_snapshot_actions_runs.json" <<'JSON'
{"workflow_runs":[{"id":200,"status":"queued"},{"id":999,"status":"completed"}]}
JSON

WORKTREE_REGISTRY_ROOT="${workspace}" \
ORCH_WORKTREE_REGISTRY_ENABLED="true" \
ORCH_WORKTREE_TTL_SECS="300" \
RUNTIME_DIR="${runtime_dir}" \
GITHUB_REPOSITORY="owner/repo" \
bash "${REPO_ROOT}/scripts/worktree_gc.sh" >/dev/null

[ ! -e "${stale_dead_path}" ]
[ -d "${stale_live_path}" ]
[ -d "${fresh_path}" ]

remaining_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list)"
PYTHONDONTWRITEBYTECODE=1 python3 - "${remaining_json}" <<'PY'
from __future__ import annotations

import json
import sys


entries = json.loads(sys.argv[1])
assert {entry["name"] for entry in entries} == {"stale-live-wt", "fresh-wt"}, entries
PY

bogus_path="${tmpdir}/not-a-worktree"
mkdir -p "${bogus_path}"
printf 'sentinel\n' > "${bogus_path}/sentinel.txt"

PYTHONDONTWRITEBYTECODE=1 python3 - "${workspace}/.worktrees/index.json" "${bogus_path}" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path


index_path = Path(sys.argv[1])
bogus_path = sys.argv[2]
payload = json.loads(index_path.read_text(encoding="utf-8"))
payload["entries"].append(
	{
		"name": "bogus-wt",
		"path": bogus_path,
		"branch": "HEAD",
		"task_id": "task-bogus",
		"created_at": "2000-01-01T00:00:00Z",
		"owner_phase": "orchestrate-poll",
		"owner_run_id": "400",
	}
)
index_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
PY

WORKTREE_REGISTRY_ROOT="${workspace}" \
ORCH_WORKTREE_REGISTRY_ENABLED="true" \
ORCH_WORKTREE_TTL_SECS="300" \
RUNTIME_DIR="${runtime_dir}" \
GITHUB_REPOSITORY="owner/repo" \
bash "${REPO_ROOT}/scripts/worktree_gc.sh" >/dev/null

[ -d "${bogus_path}" ]
[ -f "${bogus_path}/sentinel.txt" ]

remaining_json="$(WORKTREE_REGISTRY_ROOT="${workspace}" bash "${REPO_ROOT}/scripts/worktree_registry.sh" list)"
PYTHONDONTWRITEBYTECODE=1 python3 - "${remaining_json}" <<'PY'
from __future__ import annotations

import json
import sys


entries = json.loads(sys.argv[1])
assert {entry["name"] for entry in entries} == {"stale-live-wt", "fresh-wt", "bogus-wt"}, entries
PY

echo "test_worktree_gc.sh: PASS"
