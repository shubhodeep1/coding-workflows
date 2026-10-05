"""Live `.claude/` copies match their `workflow-templates/.claude/` templates (Q57).

The pipeline's editors cannot edit `.claude/**`, so an AI fix that changes a
template leaves this repo's own copy behind; #6133 and #6176 broke main that
way. Every template file with a live copy must match it, except the files
`.github/ai/claude_template_divergence.json` lists as maintained separately.
The tests also run scripts/sync_claude_live_copies.py's `plan` and `sync`
against a scratch repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sync_claude_live_copies.py"
_spec = importlib.util.spec_from_file_location("sync_claude_live_copies", SCRIPT)
assert _spec is not None and _spec.loader is not None
sync_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync_mod
_spec.loader.exec_module(sync_mod)


def test_live_copies_match_their_templates() -> None:
	drift = sync_mod.mismatched(REPO_ROOT)
	assert drift == [], (
		"Live .claude/ copies differ from workflow-templates/.claude/ for: "
		+ ", ".join(drift)
		+ ". Copy the template over the live file, or list the file in "
		".github/ai/claude_template_divergence.json with a reason."
	)


def test_allowlist_names_real_divergent_pairs() -> None:
	pairs = set(sync_mod.paired_paths(REPO_ROOT))
	divergent = json.loads((REPO_ROOT / sync_mod.ALLOWLIST_PATH).read_text(encoding="utf-8"))["divergent"]
	for relative, reason in divergent.items():
		assert relative in pairs, f"{relative} has no template/live pair"
		assert isinstance(reason, str) and reason.strip(), relative


def _git(root: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _scratch_repo(tmp_path: Path) -> Path:
	root = tmp_path / "repo"
	for base in ("workflow-templates/.claude", ".claude"):
		(root / base / "hooks").mkdir(parents=True)
		(root / base / "hooks" / "guard.py").write_text("v1\n", encoding="utf-8")
		(root / base / "hooks" / "own.py").write_text(f"{base}\n", encoding="utf-8")
	(root / ".github" / "ai").mkdir(parents=True)
	(root / ".github" / "ai" / "claude_template_divergence.json").write_text(json.dumps({"divergent": {"hooks/own.py": "repo-specific"}}), encoding="utf-8")
	_git(root.parent, "init", "-q", "-b", "main", str(root))
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "root")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
	return root


def _commit(root: Path, path: str, text: str) -> tuple[str, str]:
	before = _git(root, "rev-parse", "HEAD")
	(root / path).write_text(text, encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
	return before, _git(root, "rev-parse", "HEAD")


def test_plan_picks_template_only_changes(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.plan(root, before, after) == ["hooks/guard.py"]
	# An allowlisted file never syncs.
	before, after = _commit(root, "workflow-templates/.claude/hooks/own.py", "changed\n")
	assert sync_mod.plan(root, before, after) == []
	# A push that changed the live copy too is left to the parity test.
	before = _git(root, "rev-parse", "HEAD")
	(root / ".claude" / "hooks" / "guard.py").write_text("v3\n", encoding="utf-8")
	(root / "workflow-templates" / ".claude" / "hooks" / "guard.py").write_text("v4\n", encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "both")
	assert sync_mod.plan(root, before, _git(root, "rev-parse", "HEAD")) == []


def test_plan_fails_open_without_a_usable_before_commit(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	_before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.plan(root, "0" * 40, after) == []
	assert sync_mod.plan(root, "f" * 40, after) == []


def test_sync_pushes_a_branch_and_opens_one_pr(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	calls = tmp_path / "gh_calls"
	gh = bin_dir / "gh"
	gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "' + str(calls) + '"\ncase "$*" in *"-X POST"*) echo \'{"number": 7}\' ;; *) echo "[]" ;; esac\n', encoding="utf-8")
	gh.chmod(0o755)
	env = {key: value for key, value in os.environ.items() if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_PAT"}}
	env.update(PATH=f"{bin_dir}:{env['PATH']}", GITHUB_REPOSITORY="octo/repo", PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "sync", "--before", before, "--after", after], env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "CLAUDE_LIVE_SYNC opened pr=7 branch=ai/sync-claude-live-copies paths=1" in result.stderr
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/guard.py") == "v2"
	lines = calls.read_text(encoding="utf-8").splitlines()
	assert lines[0].startswith("api repos/octo/repo/pulls?state=open&head=octo:ai/sync-claude-live-copies")
	assert lines[1].startswith("api -X POST repos/octo/repo/pulls -f title=chore(.claude): sync live copies")
	assert "-f head=ai/sync-claude-live-copies -f base=main" in lines[1]


def test_sync_refresh_keeps_earlier_unmerged_live_copies(tmp_path: Path, monkeypatch) -> None:
	root = _scratch_repo(tmp_path)
	for prefix in (".claude", "workflow-templates/.claude"):
		(root / prefix / "hooks" / "second.py").write_text("v1\n", encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "second pair")
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	monkeypatch.setattr(sync_mod, "_gh_json", lambda *args: [{"number": 7}])
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	_git(root, "checkout", "main")
	before, after = _commit(root, "workflow-templates/.claude/hooks/second.py", "v2\n")
	assert sync_mod.plan(root, before, after) == ["hooks/second.py"]
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	for name in ("guard.py", "second.py"):
		assert _git(remote, "show", f"ai/sync-claude-live-copies:.claude/hooks/{name}") == "v2"
	_git(root, "checkout", "main")
	_commit(root, ".claude/hooks/guard.py", "custom\n")
	before, after = _commit(root, "workflow-templates/.claude/hooks/second.py", "v3\n")
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/guard.py") == "custom"
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/second.py") == "v3"


def test_sync_refuses_base_branch_override(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	monkeypatch.setenv("SYNC_BRANCH", "main")
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert "reason=unsafe_sync_branch" in capsys.readouterr().err
	assert _git(root, "rev-parse", "main") == after


def test_sync_reports_api_error_after_push(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	def api_unavailable(*args):
		raise subprocess.CalledProcessError(1, ["gh", "api"])
	monkeypatch.setattr(sync_mod, "_gh_json", api_unavailable)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert "CLAUDE_LIVE_SYNC error reason=api_failed stage=lookup" in capsys.readouterr().err
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/guard.py") == "v2"


def test_sync_workflow_runs_on_template_pushes_to_main() -> None:
	data = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "sync-claude-live-copies.yml").read_text(encoding="utf-8"))
	on = data.get("on", data.get(True))
	assert on == {"push": {"branches": ["main"], "paths": ["workflow-templates/.claude/**"]}}
	assert data["permissions"] == {"contents": "read"}
	step = next(step for step in data["jobs"]["sync"]["steps"] if "sync_claude_live_copies.py sync" in step.get("run", ""))
	assert 'python3 scripts/sync_claude_live_copies.py sync --before "${PUSH_BEFORE}" --after "${PUSH_AFTER}"' in step["run"]
	assert step["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert step["env"]["PUSH_BEFORE"] == "${{ github.event.before }}"
	assert step["env"]["PUSH_AFTER"] == "${{ github.sha }}"
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_claude_template_live_parity.py" in ci
