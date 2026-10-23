"""The review sandbox names the committed build-output directories it hides.

scripts/review_untrusted_workspace.py leaves every dist/, build/ and coverage/
directory out of the sandbox copy. A poller judge that could not see a
committed dist/ filed a fix issue to "restore" it (tele-funtoken-msg-scoring
#4770). snapshot now records the hidden committed directories (names and
counts only) and review_untrusted_sandbox.sh appends a note to every prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PY = REPO_ROOT / "scripts" / "review_untrusted_workspace.py"
SANDBOX_SH = REPO_ROOT / "scripts" / "review_untrusted_sandbox.sh"


def _env():
	env = {key: value for key, value in os.environ.items() if key not in ("GITHUB_WORKSPACE", "GIT_DIR", "GIT_WORK_TREE")}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _git(repo, *args):
	subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
		env={**_env(), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def _write(path, text="x\n"):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _repo(tmp_path):
	host = tmp_path / "host"
	host.mkdir()
	_git(host, "init", "-q")
	_write(host / "src" / "app.py")
	_write(host / "cloudflare" / "worker" / "dist" / "index.html", "<html></html>\n")
	_write(host / "cloudflare" / "worker" / "dist" / "assets" / "index-abc.js")
	_write(host / "cloudflare" / "worker" / "dist" / "assets" / "index-abc.css")
	_write(host / "docs" / "Build" / "notes.md")
	_write(host / "tools" / "node_modules" / "pkg" / "dist" / "x.js")
	_write(host / ".cache" / "dist" / "y.js")
	_git(host, "add", "-A", "-f")
	_git(host, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
	# Untracked build output is not "committed", so it is not reported.
	_write(host / "web" / "dist" / "bundle.js")
	return host


def _snapshot(tmp_path, host):
	workspace = tmp_path / "source"
	manifest = tmp_path / "baseline.json"
	result = subprocess.run([sys.executable, str(WORKSPACE_PY), "snapshot", str(host), str(workspace), str(manifest)],
		capture_output=True, text=True, env=_env(), check=False)
	assert result.returncode == 0, result.stderr
	return workspace, manifest, result.stderr


def _note(manifest):
	result = subprocess.run([sys.executable, str(WORKSPACE_PY), "hidden-note", str(manifest)],
		capture_output=True, text=True, env=_env(), check=False)
	assert result.returncode == 0, result.stderr
	return result.stdout


def test_snapshot_records_hidden_committed_build_dirs(tmp_path):
	host = _repo(tmp_path)
	workspace, manifest, stderr = _snapshot(tmp_path, host)
	# The sandbox copy still leaves the build output out.
	assert (workspace / "src" / "app.py").is_file()
	assert not (workspace / "cloudflare" / "worker" / "dist").exists()
	hidden = json.loads(manifest.with_name(manifest.name + ".hidden_build_dirs.json").read_text(encoding="utf-8"))
	assert hidden == {"cloudflare/worker/dist": 3, "docs/Build": 1}
	# The log carries a count only, never a path.
	assert "REVIEW_ISOLATION_SNAPSHOT hidden_build_dirs=2" in stderr
	assert "cloudflare/worker" not in stderr


def test_hidden_note_lists_the_directories_for_the_prompt(tmp_path):
	host = _repo(tmp_path)
	_, manifest, _ = _snapshot(tmp_path, host)
	note = _note(manifest)
	assert "- `cloudflare/worker/dist/` (3 committed files)" in note
	assert "- `docs/Build/` (1 committed file)" in note
	assert "not evidence that they are missing from the branch" in note
	assert "node_modules" not in note and ".cache" not in note and "web/dist" not in note


def test_hidden_note_is_empty_without_hidden_dirs_or_sidecar(tmp_path):
	host = tmp_path / "host"
	host.mkdir()
	_git(host, "init", "-q")
	_write(host / "src" / "app.py")
	_git(host, "add", "-A")
	_git(host, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
	_, manifest, _ = _snapshot(tmp_path, host)
	assert _note(manifest) == ""
	assert _note(tmp_path / "missing.json") == ""


def test_hidden_note_ignores_unsafe_sidecar_entries_and_caps_the_list(tmp_path):
	manifest = tmp_path / "baseline.json"
	entries = {f"pkg{i:02d}/dist": 1 for i in range(45)}
	entries["bad name/dist"] = 1
	entries["evil`/dist"] = 2
	entries["neg/dist"] = -1
	manifest.with_name(manifest.name + ".hidden_build_dirs.json").write_text(json.dumps(entries), encoding="utf-8")
	note = _note(manifest)
	assert note.count("committed file") == 40
	assert "...and 5 more" in note
	assert "bad name" not in note and "evil" not in note and "neg/dist" not in note


def test_sandbox_appends_the_note_after_every_prompt_install():
	text = SANDBOX_SH.read_text(encoding="utf-8")
	assert 'hidden-note "${root}/baseline.json"' in text
	installs = text.count('install -m 0600 "${prompt}" "${root}/prompt"')
	assert installs == 2
	assert text.count('install -m 0600 "${prompt}" "${root}/prompt"\n\treview_append_hidden_build_note\n') == 1
	assert text.count('install -m 0600 "${prompt}" "${root}/prompt"\nreview_append_hidden_build_note\n') == 1


def test_hidden_build_output_dirs_filters_on_the_directory_path(tmp_path):
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	try:
		import review_untrusted_workspace as workspace_module
	finally:
		sys.path.pop(0)
	tracked = {
		"worker/dist/logo final.js",
		"worker/dist/logo@2x.png",
		"certs.pem/build/x.txt",
		"deploy.key/dist/x.js",
		"pkg.egg-info/dist/x",
		("a" * 250) + "/dist/x.js",
		"bad name/dist/x.js",
	}
	assert workspace_module.hidden_build_output_dirs(tracked) == {"worker/dist": 2}


def test_snapshot_does_not_count_tracked_files_deleted_from_the_worktree(tmp_path):
	host = _repo(tmp_path)
	(host / "cloudflare" / "worker" / "dist" / "index.html").unlink()
	_, manifest, _ = _snapshot(tmp_path, host)
	hidden = json.loads(manifest.with_name(manifest.name + ".hidden_build_dirs.json").read_text(encoding="utf-8"))
	assert hidden["cloudflare/worker/dist"] == 2
