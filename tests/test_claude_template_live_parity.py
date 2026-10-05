"""Live `.claude/` copies match their `workflow-templates/.claude/` templates (Q57).

The pipeline's editors cannot edit `.claude/**`, so an AI fix that changes a
template leaves this repo's own copy behind; #6133 and #6176 broke main that
way. Every template file needs a live copy matching its content and executable
mode, except files `.github/ai/claude_template_divergence.json` lists as
maintained separately.
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

import pytest
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
		assert (REPO_ROOT / sync_mod.LIVE_PREFIX / relative).is_file(), f"{relative} has no live copy"
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
	assert sync_mod.plan(root, before, after) == ["hooks/guard.py"]
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


def test_pr_dry_run_prepares_parity_without_changing_the_pr_commit(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.mismatched(root) == ["hooks/guard.py"]
	assert sync_mod.sync(root, before, after, dry_run=True) == 0
	assert sync_mod.mismatched(root) == []
	assert _git(root, "rev-parse", "HEAD") == after
	assert _git(root, "show", "HEAD:.claude/hooks/guard.py") == "v1"


def test_pr_dry_run_keeps_committed_security_hook(tmp_path: Path, capsys) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=True, keep_committed_security_paths=True) == 0
	assert sync_mod.mismatched(root) == ["hooks/guard.py"]
	assert (root / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v1\n"
	assert _git(root, "rev-parse", "HEAD") == after
	assert "dry_run_kept_committed path=.claude/hooks/guard.py reason=security_path" in capsys.readouterr().err


def test_pr_dry_run_prepares_commands_but_not_hooks(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	(root / "workflow-templates/.claude/commands").mkdir()
	before, _after = _commit(root, "workflow-templates/.claude/commands/new.md", "new command\n")
	_before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=True, keep_committed_security_paths=True) == 0
	assert (root / ".claude/commands/new.md").read_text(encoding="utf-8") == "new command\n"
	assert (root / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v1\n"
	assert sync_mod.mismatched(root) == ["hooks/guard.py"]


def test_pr_dry_run_keeps_committed_settings_json(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	for prefix in ("workflow-templates/.claude", ".claude"):
		(root / prefix / "settings.json").write_text('{"hooks": []}\n', encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "settings pair")
	before, after = _commit(root, "workflow-templates/.claude/settings.json", '{"hooks": ["guard"]}\n')
	assert sync_mod.sync(root, before, after, dry_run=True, keep_committed_security_paths=True) == 0
	assert sync_mod.mismatched(root) == ["settings.json"]
	assert (root / ".claude/settings.json").read_text(encoding="utf-8") == '{"hooks": []}\n'


def test_keep_committed_flag_requires_dry_run(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	with pytest.raises(SystemExit) as error:
		sync_mod.main(["--root", str(root), "sync", "--before", before, "--after", after, "--keep-committed-security-paths"])
	assert error.value.code == 2
	assert (root / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v1\n"


def test_pr_dry_run_does_not_mask_live_edits(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before, _after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	_commit(root, ".claude/hooks/guard.py", "custom\n")
	assert sync_mod.sync(root, before, _git(root, "rev-parse", "HEAD"), dry_run=True) == 0
	assert sync_mod.mismatched(root) == ["hooks/guard.py"]


def test_sync_refuses_template_symlink_into_git_credentials(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before = _git(root, "rev-parse", "HEAD")
	template = root / "workflow-templates/.claude/hooks/guard.py"
	template.unlink()
	template.symlink_to("../../../.git/config")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "symlink")
	with pytest.raises(ValueError, match="unsafe template symlink"):
		sync_mod.sync(root, before, _git(root, "rev-parse", "HEAD"), dry_run=True)
	assert (root / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v1\n"


def test_sync_refuses_template_directory_symlink(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before = _git(root, "rev-parse", "HEAD")
	linked_directory = root / "workflow-templates/.claude/credentials"
	linked_directory.symlink_to("../../.git", target_is_directory=True)
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "directory symlink")
	with pytest.raises(ValueError, match="unsafe template symlink"):
		sync_mod.sync(root, before, _git(root, "rev-parse", "HEAD"), dry_run=True)
	assert not (root / ".claude/credentials").exists()


def test_sync_refuses_live_symlink_destination(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	live = root / ".claude/hooks/guard.py"
	live.unlink()
	live.symlink_to("../../.git/config")
	with pytest.raises(ValueError, match="unsafe live symlink"):
		sync_mod.sync(root, before, after, dry_run=True)
	assert "v2" not in (root / ".git/config").read_text(encoding="utf-8")


def test_sync_creates_missing_live_copy(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	template = root / "workflow-templates/.claude/commands/new.md"
	template.parent.mkdir(parents=True)
	before, after = _commit(root, "workflow-templates/.claude/commands/new.md", "new command\n")
	assert "commands/new.md" in sync_mod.mismatched(root)
	assert sync_mod.plan(root, before, after) == ["commands/new.md"]
	before, after = _commit(root, "workflow-templates/.claude/hooks/own.py", "changed\n")
	assert sync_mod.plan(root, before, after) == ["commands/new.md"]
	assert sync_mod.sync(root, before, after, dry_run=True) == 0
	assert (root / ".claude/commands/new.md").read_text(encoding="utf-8") == "new command\n"


def test_sync_commits_missing_live_copy(tmp_path: Path, monkeypatch) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	monkeypatch.setattr(sync_mod, "_gh_json", lambda *args: [{"number": 7}])
	(root / "workflow-templates/.claude/commands").mkdir()
	before, after = _commit(root, "workflow-templates/.claude/commands/new.md", "new command\n")
	(root / "workflow-templates/.claude/commands/new.md").chmod(0o755)
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "executable template")
	after = _git(root, "rev-parse", "HEAD")
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/commands/new.md") == "new command"
	assert _git(remote, "ls-tree", "ai/sync-claude-live-copies", ".claude/commands/new.md").startswith("100755 blob ")


def test_executable_mode_only_drift_is_detected_and_repaired(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	template = root / "workflow-templates/.claude/hooks/guard.py"
	live = root / ".claude/hooks/guard.py"
	before = _git(root, "rev-parse", "HEAD")
	template.chmod(0o755)
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "executable template")
	after = _git(root, "rev-parse", "HEAD")
	assert sync_mod.mismatched(root) == ["hooks/guard.py"]
	assert sync_mod.plan(root, before, after) == ["hooks/guard.py"]
	assert sync_mod.sync(root, before, after, dry_run=True) == 0
	assert template.stat().st_mode & 0o111 == live.stat().st_mode & 0o111
	assert sync_mod.mismatched(root) == []


def test_plan_recovers_superseded_push_without_overwriting_live_edits(tmp_path: Path) -> None:
	root = _scratch_repo(tmp_path)
	for prefix in (".claude", "workflow-templates/.claude"):
		(root / prefix / "hooks/second.py").write_text("v1\n", encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "second pair")
	_commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	before, after = _commit(root, "workflow-templates/.claude/hooks/second.py", "v2\n")
	assert sync_mod.plan(root, before, after) == ["hooks/guard.py", "hooks/second.py"]
	assert sync_mod.sync(root, before, after, dry_run=True) == 0
	assert (root / ".claude/hooks/guard.py").read_text(encoding="utf-8") == "v2\n"
	_commit(root, ".claude/hooks/guard.py", "custom\n")
	before, after = _commit(root, "workflow-templates/.claude/hooks/second.py", "v3\n")
	assert sync_mod.plan(root, before, after) == ["hooks/second.py"]


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
	assert sync_mod.plan(root, before, after) == ["hooks/guard.py", "hooks/second.py"]
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


def test_sync_reports_git_stage_error(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	git_sync_call = sync_mod._git

	def fail_checkout(*args, **kwargs):
		if args[1] == "checkout":
			raise subprocess.CalledProcessError(1, ["git", "checkout"])
		return git_sync_call(*args, **kwargs)

	monkeypatch.setattr(sync_mod, "_git", fail_checkout)
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert "CLAUDE_LIVE_SYNC error reason=git_stage_failed" in capsys.readouterr().err


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
	ci_jobs = yaml.safe_load(ci)["jobs"]
	prepare_jobs = [
		job_name for job_name, job in ci_jobs.items()
		if any(step.get("name") == "Prepare template-only PR live copies for parity tests" for step in job.get("steps", []))
	]
	assert prepare_jobs == ["tests-hooks-and-orchestrator"]
	steps = ci_jobs["tests-hooks-and-orchestrator"]["steps"]
	assert steps[0]["with"]["fetch-depth"] == 0
	prepare = next(step for step in steps if step["name"] == "Prepare template-only PR live copies for parity tests")
	assert prepare["if"] == "github.event_name == 'pull_request' && github.base_ref == 'main'"
	assert prepare["env"]["PR_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
	assert 'sync --dry-run --before "${PR_BASE_SHA}" --after HEAD' in prepare["run"]
	assert "--keep-committed-security-paths" in prepare["run"]
