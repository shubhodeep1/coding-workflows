"""Pin the trusted support and write-guard boundaries of live-copy sync."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/sync-claude-live-copies.yml"
POLICY = ROOT / ".github/ai/write_guards.v1.json"
GUARDED_PATHS = ("scripts/sync_claude_live_copies.py", ".github/workflows/sync-claude-live-copies.yml")
PHASES = ("implement", "review_editor", "validate_fix_harness", "conflict_resolver")


def _steps():
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["sync"]["steps"]


def _step(name):
	return next(step for step in _steps() if step.get("name") == name)


def test_checkouts_do_not_persist_pat():
	checkouts = [step for step in _steps() if step.get("uses", "").startswith("actions/checkout@")]
	assert len(checkouts) == 2
	assert all(step["with"]["persist-credentials"] is False for step in checkouts)
	assert all("token" not in step["with"] for step in checkouts)
	support = [step for step in checkouts if step["with"].get("path") == ".codex-workflow-src"]
	assert len(support) == 1
	assert support[0]["with"]["ref"] == "${{ steps.resolve_support.outputs.support_sha }}"
	assert support[0]["with"]["fetch-depth"] == 1
	assert any(step["with"].get("path") == "target" and step["with"]["fetch-depth"] == 0 for step in checkouts)


def test_stable_is_peeled_verified_and_missing_support_skips_sync():
	resolve = _step("Resolve trusted sync support")
	assert resolve["id"] == "resolve_support"
	assert resolve["env"]["GH_TOKEN"] == "${{ github.token }}"
	assert "git/ref/tags/stable" in resolve["run"]
	assert "git/tags/${support_sha}" in resolve["run"]
	assert '"${support_type}" != commit' in resolve["run"]
	assert "^[0-9a-f]{40}$" in resolve["run"]
	verify = _step("Verify trusted sync support")
	assert verify["id"] == "verify_support"
	assert verify["env"]["SUPPORT_SHA"] == "${{ steps.resolve_support.outputs.support_sha }}"
	assert '"$(git -C .codex-workflow-src rev-parse HEAD)" != "${SUPPORT_SHA}"' in verify["run"]
	assert "support_script_missing" in verify["run"]
	assert "available=false" in verify["run"]
	assert _step("Sync template-only .claude changes to the live copy")["if"] == "steps.verify_support.outputs.available == 'true'"


def test_pat_only_enters_pinned_sync_step():
	steps = _steps()
	sync = _step("Sync template-only .claude changes to the live copy")
	assert sync["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert sum(json.dumps(step).count("secrets.GH_PAT") for step in steps) == 1
	assert sync["working-directory"] == "target"
	assert "${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/sync_claude_live_copies.py" in sync["run"]
	assert '--root "${GITHUB_WORKSPACE}/target"' in sync["run"]
	assert "python3 scripts/sync_claude_live_copies.py" not in sync["run"]
	assert 'if [ -z "${GH_TOKEN}" ]' in sync["run"]
	assert sync["run"].index("::add-mask::") < sync["run"].index("GIT_CONFIG_VALUE_0=")
	assert "source \"${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/tg_helpers.sh\"" in _step("Alert on held .claude live-copy sync")["run"]


def test_policy_and_resolver_guard_both_paths():
	policy = json.loads(POLICY.read_text(encoding="utf-8"))
	for phase in PHASES:
		assert set(GUARDED_PATHS) <= set(policy["phases"][phase]["blocked_globs"])
	resolver = (ROOT / "scripts/review_conflict_resolve.sh").read_text(encoding="utf-8")
	assert resolver.count("write_guard_check conflict_resolver") == 2


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("protected_path", GUARDED_PATHS)
def test_committed_policy_blocks_protected_paths(tmp_path, phase, protected_path):
	(tmp_path / "scripts").mkdir()
	(tmp_path / ".github/ai").mkdir(parents=True)
	shutil.copy2(ROOT / "scripts/write_guard.sh", tmp_path / "scripts/write_guard.sh")
	shutil.copy2(POLICY, tmp_path / ".github/ai/write_guards.v1.json")
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.pop("WRITE_GUARDS_ENABLED", None)
	subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
	subprocess.run(["git", "add", "scripts/write_guard.sh", ".github/ai/write_guards.v1.json"], cwd=tmp_path, env=env, check=True)
	subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], cwd=tmp_path, env=env, check=True)
	paths = tmp_path / "paths.txt"
	paths.write_text(protected_path + "\n", encoding="utf-8")
	result = subprocess.run(["bash", "scripts/write_guard.sh", phase, str(paths)], cwd=tmp_path, env=env, capture_output=True, text=True)
	assert result.returncode == 1, result.stdout + result.stderr
	assert f"WRITE_GUARD_BLOCK: phase={phase} path={protected_path}" in result.stdout
	if phase == "implement":
		paths.write_text(".claude/commands/x.md\n", encoding="utf-8")
		allowed = subprocess.run(["bash", "scripts/write_guard.sh", phase, str(paths)], cwd=tmp_path, env=env, capture_output=True, text=True)
		assert allowed.returncode == 0, allowed.stdout + allowed.stderr
