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


def _merged_pr(head_ref="feature/approved", **overrides):
	pr = {
		"number": 12,
		"merged_at": "2026-10-01T00:00:00Z",
		"head": {"ref": head_ref, "repo": {"full_name": "octo/repo"}},
		"base": {"ref": "main"},
		"user": {"login": "octo", "type": "User"},
		"author_association": "OWNER",
	}
	pr.update(overrides)
	return pr


def _sync_pr(number: int, *, repo: str = "octo/repo", base: str = "main", branch: str = "ai/sync-claude-live-copies") -> dict:
	return {
		"number": number,
		"state": "open",
		"head": {"ref": branch, "repo": {"full_name": repo}},
		"base": {"ref": base},
	}


def _authorized_api(*args):
	endpoint = args[0]
	if "/commits/" in endpoint and "/pulls?" in endpoint:
		return [_merged_pr()]
	if "/pulls/12/commits?" in endpoint:
		return [{"commit": {"message": "feat: human change"}}]
	if "-X" in args:
		return {"number": 7}
	return [] if "-held" in endpoint else [_sync_pr(7)]


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


def test_pr_dry_run_never_looks_up_provenance(tmp_path: Path, monkeypatch) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	def unexpected_api(*args):
		raise AssertionError("dry-run must not call the API")
	monkeypatch.setattr(sync_mod, "_gh_json", unexpected_api)
	assert sync_mod.sync(root, before, after, dry_run=True) == 0


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
	monkeypatch.setattr(sync_mod, "_gh_json", _authorized_api)
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
	gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "' + str(calls) + '"\ncase "$*" in *"/commits/"*"/pulls?"*) echo \'[{"number":12,"merged_at":"2026-10-01T00:00:00Z","head":{"ref":"feature/approved","repo":{"full_name":"octo/repo"}},"base":{"ref":"main"},"user":{"login":"octo","type":"User"},"author_association":"OWNER"}]\' ;; *"/pulls/12/commits?"*) echo \'[{"commit":{"message":"human change"}}]\' ;; *"-X POST"*) echo \'{"number":7}\' ;; *) echo "[]" ;; esac\n', encoding="utf-8")
	gh.chmod(0o755)
	env = {key: value for key, value in os.environ.items() if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_PAT"}}
	env.update(PATH=f"{bin_dir}:{env['PATH']}", GITHUB_REPOSITORY="octo/repo", PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "sync", "--before", before, "--after", after], env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "CLAUDE_LIVE_SYNC opened pr=7 branch=ai/sync-claude-live-copies paths=1" in result.stderr
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/guard.py") == "v2"
	lines = calls.read_text(encoding="utf-8").splitlines()
	assert any(line.startswith("api repos/octo/repo/commits/") for line in lines)
	assert any(line.startswith("api repos/octo/repo/pulls?state=open&head=octo:ai/sync-claude-live-copies") and "&base=main&per_page=100" in line for line in lines)
	post = next(line for line in lines if line.startswith("api -X POST repos/octo/repo/pulls -f title=chore(.claude): sync live copies"))
	assert "-f head=ai/sync-claude-live-copies -f base=main" in post


def test_sync_ignores_open_pr_into_other_base(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	calls = []

	def gh_json(*args):
		if "/commits/" in args[0] or "/pulls/12/commits?" in args[0]:
			return _authorized_api(*args)
		calls.append(args)
		return {"number": 7} if "-X" in args else [_sync_pr(5, base="stable")]

	monkeypatch.setattr(sync_mod, "_gh_json", gh_json)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert len(calls) == 2
	assert "&base=main&per_page=100" in calls[0][0]
	assert "base=main" in calls[1]
	output = capsys.readouterr().err
	assert "ignored_pr pr=5 reason=base_mismatch" in output
	assert "opened pr=7" in output
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/guard.py") == "v2"


def test_sync_ignores_fork_and_deleted_head_repo(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	calls = []
	deleted_head = _sync_pr(6)
	deleted_head["head"]["repo"] = None

	def gh_json(*args):
		if "/commits/" in args[0] or "/pulls/12/commits?" in args[0]:
			return _authorized_api(*args)
		calls.append(args)
		return {"number": 7} if "-X" in args else [_sync_pr(5, repo="evil/repo"), deleted_head]

	monkeypatch.setattr(sync_mod, "_gh_json", gh_json)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert len(calls) == 2
	assert "base=main" in calls[1]
	output = capsys.readouterr().err
	assert "ignored_pr pr=5 reason=head_repo_mismatch" in output
	assert "ignored_pr pr=6 reason=head_repo_mismatch" in output
	assert "opened pr=7" in output


def test_sync_updates_exact_match_after_decoy(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "Octo/Repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	calls = []

	def gh_json(*args):
		if "/commits/" in args[0] or "/pulls/12/commits?" in args[0]:
			return _authorized_api(*args)
		calls.append(args)
		return [_sync_pr(5, base="stable"), _sync_pr(7)]

	monkeypatch.setattr(sync_mod, "_gh_json", gh_json)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert len(calls) == 1
	assert "&base=main&per_page=100" in calls[0][0]
	output = capsys.readouterr().err
	assert "ignored_pr pr=5 reason=base_mismatch" in output
	assert "updated pr=7 branch=ai/sync-claude-live-copies base=main paths=1" in output


def test_sync_pr_rejects_malformed_entries() -> None:
	for pr in (None, [], {"number": 1}, {"head": {"repo": None}, "base": {"ref": "main"}},
	           {"head": {"repo": {"full_name": "octo/repo"}, "ref": "ai/sync-claude-live-copies"}, "base": "main"}):
		assert not sync_mod._is_sync_pr(pr, "octo/repo", "ai/sync-claude-live-copies", "main")
	closed_pr = _sync_pr(1)
	closed_pr["state"] = "closed"
	assert not sync_mod._is_sync_pr(closed_pr, "octo/repo", "ai/sync-claude-live-copies", "main")


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
	monkeypatch.setattr(sync_mod, "_gh_json", _authorized_api)
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


def test_carry_forward_reports_template_read_failure(tmp_path: Path, monkeypatch, capsys) -> None:
	root, _remote = _sync_repo(tmp_path, monkeypatch)
	after = _git(root, "rev-parse", "HEAD")
	branch = "ai/sync-claude-live-copies"
	_git(root, "checkout", "-q", "-b", branch)
	(root / ".claude/hooks/guard.py").write_text("v2\n", encoding="utf-8")
	_git(root, "add", ".claude/hooks/guard.py")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sync")
	_git(root, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
	_git(root, "checkout", "-q", "main")
	(root / "workflow-templates/.claude/hooks/guard.py").unlink()

	assert sync_mod._carry_forward(root, after, branch, [], {"hooks/guard.py"}) is None
	assert "reason=template_read_failed path=workflow-templates/.claude/hooks/guard.py" in capsys.readouterr().err


def test_sync_refuses_base_branch_override(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	monkeypatch.setenv("SYNC_BRANCH", "main")
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert "reason=unsafe_sync_branch" in capsys.readouterr().err
	assert _git(root, "rev-parse", "main") == after


def test_sync_refuses_held_branch_override(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	monkeypatch.setenv("SYNC_BRANCH", "ai/sync-claude-live-copies-held")
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert "reason=unsafe_sync_branch" in capsys.readouterr().err


def test_sync_reports_api_error_after_push(tmp_path: Path, monkeypatch, capsys) -> None:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	def api_unavailable(*args):
		if "/commits/" in args[0] or "/pulls/12/commits?" in args[0]:
			return _authorized_api(*args)
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
	monkeypatch.setattr(sync_mod, "_gh_json", _authorized_api)

	def fail_checkout(*args, **kwargs):
		if args[1] == "checkout":
			raise subprocess.CalledProcessError(1, ["git", "checkout"])
		return git_sync_call(*args, **kwargs)

	monkeypatch.setattr(sync_mod, "_git", fail_checkout)
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert "CLAUDE_LIVE_SYNC error reason=git_stage_failed" in capsys.readouterr().err


def _sync_repo(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
	root = _scratch_repo(tmp_path)
	remote = tmp_path / "remote.git"
	_git(tmp_path, "init", "-q", "--bare", str(remote))
	_git(root, "remote", "add", "origin", str(remote))
	monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
	monkeypatch.delenv("SYNC_BRANCH", raising=False)
	monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
	return root, remote


def test_unassociated_change_goes_only_to_draft_and_reports_source(tmp_path: Path, monkeypatch, capsys) -> None:
	root, remote = _sync_repo(tmp_path, monkeypatch)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	output = tmp_path / "output"
	monkeypatch.setenv("GITHUB_OUTPUT", str(output))
	calls = []
	def api(*args):
		calls.append(args)
		if "/commits/" in args[0]:
			return []
		if "-X" in args:
			return {"number": 9}
		return []
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert _git(remote, "show", "ai/sync-claude-live-copies-held:.claude/hooks/guard.py") == "v2"
	assert _git(remote, "branch", "--list", "ai/sync-claude-live-copies") == ""
	assert "reason=no_merged_pr commit=" in capsys.readouterr().err
	assert any("-F" in call and "draft=true" in call for call in calls)
	assert "held=true\nheld_pr=9\n" in output.read_text(encoding="utf-8")
	assert "commit " + after[:12] in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
	("head", "subject", "pr_subject", "reason"),
	[
		("ai/issue-5", "normal", "normal", "pipeline_branch"),
		("auto/forward-merge-stable-1", "normal", "normal", "pipeline_branch"),
		("feature/x", "[claude-intervention] edit", "normal", "pipeline_commit_marker"),
		("feature/x", "normal", "[ai-autofix] edit", "pr_commit_marker"),
		("feature/x", "normal", "feat (#12)", "squashed_pr_commit"),
	],
)
def test_pipeline_provenance_is_held(head, subject, pr_subject, reason, monkeypatch) -> None:
	def api(*args):
		if "/commits/" in args[0]:
			return [_merged_pr(head_ref=head)]
		return [{"commit": {"message": pr_subject}}]
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	ok, actual, _pr = sync_mod._commit_authorization("octo/repo", "a" * 40, subject, {}, {})
	assert not ok and actual == reason


@pytest.mark.parametrize(
	("overrides", "reason"),
	[
		({"base": {"ref": "stable"}}, "base_mismatch"),
		({"head": {"ref": "claude/fix-x", "repo": None}}, "foreign_head_repo"),
		({"head": {"ref": "claude/fix-x", "repo": "unknown"}}, "foreign_head_repo"),
		({"head": {"ref": "claude/fix-x", "repo": {"full_name": "evil/repo"}}}, "foreign_head_repo"),
		({"user": {"login": "octo", "type": "Bot"}}, "bot_author"),
		({"user": {"login": "octo"}}, "bot_author"),
		({"user": {"login": "octo", "type": "Organization"}}, "bot_author"),
		({"user": {"login": "renovate[bot]", "type": "User"}}, "bot_author"),
		({"author_association": "CONTRIBUTOR"}, "untrusted_author"),
		({"author_association": "NONE"}, "untrusted_author"),
		({"author_association": None}, "untrusted_author"),
	],
)
def test_untrusted_pr_provenance_is_held_before_commit_fetch(overrides, reason, monkeypatch) -> None:
	calls = []
	def api(*args):
		calls.append(args)
		return [_merged_pr(**overrides)]
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {}) == (False, reason, "12")
	assert len(calls) == 1
	assert "/commits/" in calls[0][0] and "/pulls?" in calls[0][0]


@pytest.mark.parametrize("overrides", [
	{"base": None},
	{"base": "main"},
	{"user": None},
	{"head": {"ref": "feature/x", "repo": {"full_name": 1}}},
])
def test_malformed_pr_provenance_fails_closed(overrides, monkeypatch) -> None:
	monkeypatch.setattr(sync_mod, "_gh_json", lambda *args: [_merged_pr(**overrides)])
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == "api_failed"


@pytest.mark.parametrize("missing_key,reason", [
	("base", "api_failed"),
	("user", "api_failed"),
	("author_association", "untrusted_author"),
])
def test_missing_pr_provenance_is_held(missing_key, reason, monkeypatch) -> None:
	pr = _merged_pr()
	pr.pop(missing_key)
	monkeypatch.setattr(sync_mod, "_gh_json", lambda *args: [pr])
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == reason


def test_trusted_claude_pr_and_case_insensitive_repo_match(monkeypatch) -> None:
	def api(*args):
		if "/commits/" in args[0] and "/pulls?" in args[0]:
			return [_merged_pr(head={"ref": "claude/fix-x", "repo": {"full_name": "Octo/Repo"}})]
		return [{"commit": {"message": "human change"}}]
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {}) == (True, "", "12")


def test_all_associated_merged_prs_must_be_trusted(monkeypatch) -> None:
	calls = []
	def api(*args):
		calls.append(args)
		return [_merged_pr(), _merged_pr(number=13, head={"ref": "claude/x", "repo": {"full_name": "fork/repo"}})]
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {}) == (False, "foreign_head_repo", "13")
	assert len(calls) == 1


def test_provenance_truncation_and_malformed_api_fail_closed(monkeypatch) -> None:
	def too_many(*args):
		if "/commits/" in args[0]:
			return [_merged_pr()]
		return [{"commit": {"message": "normal"}}] * 100
	monkeypatch.setattr(sync_mod, "_gh_json", too_many)
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == "pr_commits_truncated"
	monkeypatch.setattr(sync_mod, "_gh_json", lambda *args: {"error": "unavailable"})
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == "api_failed"
	monkeypatch.setattr(sync_mod, "_gh_json", lambda *args: [_merged_pr(merged_at=True)])
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == "api_failed"
	for invalid_timestamp in ("invalid", "2026-13-01T00:00:00Z", "2026-10-01T00:00:00", ""):
		monkeypatch.setattr(sync_mod, "_gh_json", lambda *args, timestamp=invalid_timestamp: [_merged_pr(merged_at=timestamp)])
		assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == "api_failed"


def test_pr_commit_pagination_inspects_later_pages(monkeypatch) -> None:
	def api(*args):
		if "/commits/" in args[0] and "/pulls?" in args[0]:
			return [_merged_pr()]
		if "&page=1" in args[0]:
			return [{"commit": {"message": "human"}}] * 100
		return [{"commit": {"message": "[judge-fix] hidden edit"}}]
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod._commit_authorization("octo/repo", "a" * 40, "normal", {}, {})[1] == "pr_commit_marker"


def test_provenance_outage_still_opens_draft(tmp_path: Path, monkeypatch) -> None:
	root, remote = _sync_repo(tmp_path, monkeypatch)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	def api(*args):
		if "/commits/" in args[0]:
			raise subprocess.CalledProcessError(1, "gh")
		return {"number": 9} if "-X" in args else []
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert _git(remote, "show", "ai/sync-claude-live-copies-held:.claude/hooks/guard.py") == "v2"


def test_mixed_paths_are_isolated_by_branch(tmp_path: Path, monkeypatch) -> None:
	root, remote = _sync_repo(tmp_path, monkeypatch)
	for prefix in (".claude", "workflow-templates/.claude"):
		(root / prefix / "hooks/second.py").write_text("v1\n", encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "second pair")
	before = _git(root, "rev-parse", "HEAD")
	_commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	_commit(root, "workflow-templates/.claude/hooks/second.py", "v2\n")
	after = _git(root, "rev-parse", "HEAD")
	def api(*args):
		if "/commits/" in args[0] and "/pulls?" in args[0]:
			return [] if _git(root, "show", "--format=", args[0].split("/commits/")[1].split("/")[0], "--", "workflow-templates/.claude/hooks/second.py") else _authorized_api(*args)
		if "-X" in args:
			return {"number": 9}
		return _authorized_api(*args) if "/pulls/12/commits?" in args[0] else []
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/guard.py") == "v2"
	assert _git(remote, "show", "ai/sync-claude-live-copies:.claude/hooks/second.py") == "v1"
	assert _git(remote, "show", "ai/sync-claude-live-copies-held:.claude/hooks/second.py") == "v2"
	assert _git(remote, "show", "ai/sync-claude-live-copies-held:.claude/hooks/guard.py") == "v1"


@pytest.mark.parametrize("pr_base,authorized", [("develop", True), ("main", False)])
def test_sync_threads_base_branch_into_provenance(tmp_path: Path, monkeypatch, pr_base: str, authorized: bool) -> None:
	root, remote = _sync_repo(tmp_path, monkeypatch)
	monkeypatch.setenv("BASE_BRANCH", "develop")
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	def api(*args):
		if "/commits/" in args[0] and "/pulls?" in args[0]:
			return [_merged_pr(base={"ref": pr_base})]
		if "/pulls/12/commits?" in args[0]:
			return [{"commit": {"message": "human change"}}]
		return {"number": 7} if "-X" in args else []
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	branch = "ai/sync-claude-live-copies" if authorized else "ai/sync-claude-live-copies-held"
	assert _git(remote, "show", f"{branch}:.claude/hooks/guard.py") == "v2"


def test_held_ready_pr_converted_before_push_and_failure_does_not_push(tmp_path: Path, monkeypatch, capsys) -> None:
	root, remote = _sync_repo(tmp_path, monkeypatch)
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	converted = []
	def api(*args):
		if "/commits/" in args[0]:
			return []
		if args[0] == "graphql":
			converted.append(_git(remote, "branch", "--list", "ai/sync-claude-live-copies-held"))
			return {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}}
		return [{**_sync_pr(9, branch="ai/sync-claude-live-copies-held"), "draft": False, "node_id": "PR_9"}]
	monkeypatch.setattr(sync_mod, "_gh_json", api)
	assert sync_mod.sync(root, before, after, dry_run=False) == 0
	assert converted == [""]
	assert "converted_to_draft pr=9" in capsys.readouterr().err
	_git(root, "checkout", "main")
	before, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v3\n")
	old_head = _git(remote, "rev-parse", "ai/sync-claude-live-copies-held")
	def conversion_fails(*args):
		if args[0] == "graphql":
			raise subprocess.CalledProcessError(1, "gh")
		return api(*args)
	monkeypatch.setattr(sync_mod, "_gh_json", conversion_fails)
	assert sync_mod.sync(root, before, after, dry_run=False) == 1
	assert _git(remote, "rev-parse", "ai/sync-claude-live-copies-held") == old_head


def test_provenance_cap_and_sanitized_outputs(tmp_path: Path, monkeypatch) -> None:
	root = _scratch_repo(tmp_path)
	_commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	_, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v3\n")
	monkeypatch.setenv("CLAUDE_LIVE_SYNC_MAX_PROVENANCE_COMMITS", "1")
	authorized, held = sync_mod._classify_paths(root, ["hooks/guard.py"], after, "octo/repo")
	assert authorized == [] and held["hooks/guard.py"][0] == "provenance_cap_exceeded"
	output = tmp_path / "output"
	monkeypatch.setenv("GITHUB_OUTPUT", str(output))
	sync_mod._write_outputs(9, {"hooks/guard.py\nheld=false": ("api_failed", "a" * 12, "none")})
	assert output.read_text(encoding="utf-8").splitlines()[0:2] == ["held=true", "held_pr=9"]
	assert len(output.read_text(encoding="utf-8").splitlines()) == 3


def test_history_failure_is_held(tmp_path: Path, monkeypatch) -> None:
	root = _scratch_repo(tmp_path)
	_, after = _commit(root, "workflow-templates/.claude/hooks/guard.py", "v2\n")
	previous = sync_mod._git
	def fail_history(*args, **kwargs):
		if args[1] == "log":
			raise OSError("git unavailable")
		return previous(*args, **kwargs)
	monkeypatch.setattr(sync_mod, "_git", fail_history)
	assert sync_mod._classify_paths(root, ["hooks/guard.py"], after, "octo/repo")[1]["hooks/guard.py"][0] == "history_failed"


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
	assert step["id"] == "sync"
	alert = next(step for step in data["jobs"]["sync"]["steps"] if step.get("name") == "Alert on held .claude live-copy sync")
	assert "steps.sync.outputs.held == 'true'" in alert["if"]
	assert '"WARNING"' in alert["run"] and "tg_send_msg" in alert["run"]
	assert "${{" not in alert["run"]
	assert all(key in alert["env"] for key in ("TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID", "ALERT_MSG_LEVEL"))
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
