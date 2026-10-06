"""The implement/plan snapshot includes ordinary sources but not runner state."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import review_untrusted_workspace as workspace


def test_editor_profile_and_checked_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
	host = tmp_path / "host"
	host.mkdir()
	subprocess.run(["git", "init", "-q", str(host)], check=True)
	for name in ("Makefile", ".gitignore", "src/example.vue", "src/example.rb", ".env", "secrets/password", ".codex-workflow-src/file", ".claude/settings.json"):
		path = host / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("initial\n")
	big = host / "src/big.vue"
	big.write_bytes(b"x" * (workspace.MAX_FILE + 1))
	source = tmp_path / "source"
	source.mkdir()
	manifest = tmp_path / "baseline.json"
	workspace.snapshot(host, source, manifest, profile="editor")
	for name in ("Makefile", ".gitignore", "src/example.vue", "src/example.rb"):
		assert (source / name).is_file()
	for name in (".env", "secrets/password", ".codex-workflow-src/file", ".claude/settings.json", "src/big.vue"):
		assert not (source / name).exists()
	(source / "src/example.vue").write_text("edited\n")
	workspace.transfer(host, source, manifest)
	assert (host / "src/example.vue").read_text() == "edited\n"
	(source / "src/big.vue").write_text("replacement\n")
	with pytest.raises(ValueError, match="new result conflicts with host path"):
		workspace.transfer(host, source, manifest)
	(source / "src/big.vue").unlink()
	workspace.profile_path(manifest).write_text("untrusted\n")
	with pytest.raises(ValueError, match="profile missing"):
		workspace.transfer(host, source, manifest)
	workspace.profile_path(manifest).write_text("review\n")
	assert subprocess.run(["python3", str(Path(workspace.__file__)), "transfer", str(host), str(source), str(manifest), "--profile", "editor"], capture_output=True).returncode != 0


def test_review_profile_keeps_suffix_filter() -> None:
	assert not workspace.allowed("src/example.vue")
	assert workspace.allowed_editor("src/example.vue")
	assert not workspace.allowed_editor("nested/.npmrc")
