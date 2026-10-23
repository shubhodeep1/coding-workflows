#!/usr/bin/env python3
"""Unit tests for scripts/codex_isolated_workspace.py.

The module builds the disposable copy an isolated Codex agent works on and
carries the agent's file changes back. These tests pin the security
properties: no .git (and so no GH_PAT remote URL or checkout extraheader)
reaches the copy, secret-looking and untracked files stay out of read-only
snapshots, symlinks and special files never come back, and a transfer is
all-or-nothing.
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE = REPO_ROOT / "scripts" / "codex_isolated_workspace.py"
TOKEN = "ghp_isolationtesttoken000000000000000000"


def run(command, *args, check=True, hide=None):
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") and key != "CODEX_ISOLATED_HIDE"}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	if hide is not None:
		env["CODEX_ISOLATED_HIDE"] = hide
	proc = subprocess.run(
		[sys.executable, str(MODULE), command, *[str(a) for a in args]],
		capture_output=True,
		text=True,
		env=env,
	)
	if check and proc.returncode != 0:
		raise AssertionError(f"{command} failed: {proc.stderr}")
	return proc


def git(repo, *args):
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
	return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
	"""A checkout whose .git config carries a token, like actions/checkout leaves it."""
	host = tmp_path / "host"
	host.mkdir()
	git(host, "init", "-q")
	git(host, "remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r")
	git(host, "config", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {TOKEN}")
	(host / "src").mkdir()
	(host / "src" / "app.py").write_text("print('app')\n")
	(host / "frontend").mkdir()
	(host / "frontend" / "index.ts").write_text("export {}\n")
	(host / "run.sh").write_text("#!/bin/sh\necho run\n")
	(host / "run.sh").chmod(0o755)
	(host / ".env").write_text("SECRET=1\n")
	(host / "deploy.pem").write_text("-----BEGIN-----\n")
	(host / "big.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	os.symlink("/etc/passwd", host / "link")
	git(host, "add", "-A")
	git(host, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
	(host / "untracked.txt").write_text("not tracked\n")
	return host


def tree(root):
	return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink())


def test_readonly_snapshot_takes_tracked_files_only_and_skips_secrets(repo, tmp_path):
	dest = tmp_path / "copy"
	proc = run("snapshot-readonly", repo, dest)
	assert tree(dest) == ["frontend/index.ts", "run.sh", "src/app.py"]
	assert "skipped_large=1" in proc.stderr
	assert os.access(dest / "run.sh", os.X_OK)


def test_credential_paths_are_absent_from_snapshots_and_synthetic_git(repo, tmp_path):
	hidden = (
		".npmrc", ".netrc", ".pypirc", ".ssh/id_ed25519", ".config/gh/hosts.yml",
		"client_secret.json", "secrets.json", "credentials.yaml", "secrets.yml",
		"credentials.toml", "credentials.ini", "aws_credentials", "aws_credentials.json",
		"client-secret.json", "client-secret.yaml", "secret.json", "credentials.txt",
		"credentials.conf", "secret.cfg", "client_secret.properties",
		"credentials.xml", "secrets.xml", "oauth.secret.properties", "app.credentials.json",
	)
	for name in hidden:
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("credential value\n")
	git(repo, "add", "-A")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add credential fixtures")
	readonly = tmp_path / "readonly"
	workspace, _manifest = snapshot_ws(repo, tmp_path)
	run("snapshot-readonly", repo, readonly)
	run("seed-git", repo, readonly)
	run("seed-git", repo, workspace)
	for copied in (readonly, workspace):
		for name in hidden:
			assert not (copied / name).exists()
			assert subprocess.run(["git", "show", f"HEAD:{name}"], cwd=copied, capture_output=True).returncode != 0


def test_source_files_about_secrets_remain_editable(repo, tmp_path):
	paths = (
		"src/secret_manager.py", "src/credential_provider.py", "tests/test_secrets.py",
		"src/secret.py", "src/secrets.py", "src/credentials.ts", "src/credential.go",
	)
	for name in paths:
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("original\n")
	git(repo, "add", "-A")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add source")
	readonly = tmp_path / "readonly"
	workspace, manifest = snapshot_ws(repo, tmp_path)
	run("snapshot-readonly", repo, readonly)
	for name in paths:
		assert (readonly / name).read_text() == "original\n"
		(workspace / name).write_text("edited\n")
	run("transfer", repo, workspace, manifest)
	for name in paths:
		assert (repo / name).read_text() == "edited\n"


def test_readonly_snapshot_limits_fail_closed(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	monkeypatch.setattr(module, "MAX_READONLY_FILES", 1)
	with pytest.raises(module.Rejected):
		module.snapshot_readonly(repo, tmp_path / "copy")


def test_export_oversized_chunks_only_scoped_safe_tracked_files(repo, tmp_path):
	data = (b"line one\n" * 300000) + b"last line"
	(repo / "large.txt").write_bytes(data)
	(repo / "unscoped.txt").write_bytes(b"x" * len(data))
	(repo / "secret.json").write_bytes(data)
	(repo / "CLAUDE.md").write_bytes(data)
	os.symlink(repo / "large.txt", repo / "large-link.txt")
	git(repo, "add", "large.txt", "unscoped.txt", "secret.json", "CLAUDE.md", "large-link.txt")
	scope = tmp_path / "scope.txt"
	scope.write_text("large.txt\nsrc/app.py\n")
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, len(data), len(data), hide="CLAUDE.md")
	assert "scoped=1" in proc.stderr and "unscoped=2" in proc.stderr
	manifest = json.loads((dest / "manifest.json").read_text())
	assert manifest["scope_mode"] == "explicit"
	assert [item["path"] for item in manifest["unscoped_oversized"]] == ["big.bin", "unscoped.txt"]
	assert manifest["unscoped_oversized_count"] == 2
	assert manifest["unscoped_text_capped_count"] == 0
	assert manifest["unscoped_text_capped"] == []
	assert len(manifest["scoped"]) == 1
	item = manifest["scoped"][0]
	assert item["path"] == "large.txt" and item["sha256"] == hashlib.sha256(data).hexdigest()
	assert b"".join((dest / chunk["file"]).read_bytes() for chunk in item["chunks"]) == data
	assert all(chunk["bytes"] <= 1024 * 1024 for chunk in item["chunks"])
	assert all(left["end_line"] + 1 == right["start_line"] for left, right in zip(item["chunks"], item["chunks"][1:]))
	assert tree(dest) == sorted([chunk["file"] for chunk in item["chunks"]] + ["manifest.json"])


def test_export_oversized_all_chunks_every_eligible_tracked_file(repo, tmp_path):
	data = b"source\n" * 300000
	(repo / "large.txt").write_bytes(data)
	(repo / "unscoped.txt").write_bytes(data)
	(repo / "secret.json").write_bytes(data)
	(repo / "CLAUDE.md").write_bytes(data)
	(repo / "large-link.txt").symlink_to("large.txt")
	git(repo, "add", "large.txt", "unscoped.txt", "secret.json", "CLAUDE.md", "large-link.txt")
	scope = tmp_path / "scope.txt"
	scope.write_text("")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, 16777216, 67108864, "all", hide="CLAUDE.md")
	manifest = json.loads((dest / "manifest.json").read_text())
	assert manifest["scope_mode"] == "all"
	assert [item["path"] for item in manifest["scoped"]] == ["big.bin", "large.txt", "unscoped.txt"]
	assert manifest["unscoped_oversized"] == []
	assert manifest["unscoped_oversized_count"] == 0
	assert manifest["unscoped_text_capped_count"] == 0
	for item in manifest["scoped"]:
		assert b"".join((dest / chunk["file"]).read_bytes() for chunk in item["chunks"]) == (data if item["path"] != "big.bin" else b"x" * (2 * 1024 * 1024 + 1))


@pytest.mark.parametrize("file_cap,total_cap,expected", [
	("2097152", "67108864", {"big.bin": "over_file_cap", "large.txt": "over_file_cap"}),
	("16777216", "4194304", {"large.txt": "over_total_cap"}),
])
def test_export_oversized_all_reports_unlisted_cap_breach(repo, tmp_path, file_cap, total_cap, expected):
	# Q24: a full audit lists unscoped files past the caps as not inspected.
	(repo / "large.txt").write_bytes(b"a" * (2 * 1024 * 1024 + 1))
	(repo / "photo.jpg").write_bytes(b"\x00" + b"a" * (2 * 1024 * 1024))
	git(repo, "add", "large.txt", "photo.jpg")
	scope = tmp_path / "scope.txt"
	scope.write_text("")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, file_cap, total_cap, "all")
	manifest = json.loads((dest / "manifest.json").read_text())
	assert {item["path"]: item["reason"] for item in manifest["unscoped_oversized"]} == {
		**expected, "photo.jpg": "over_file_cap" if file_cap == "2097152" else "over_total_cap",
	}
	assert manifest["unscoped_oversized_count"] == len(expected) + 1
	assert manifest["unscoped_text_capped_count"] == len(expected)
	assert {item["path"]: item["reason"] for item in manifest["unscoped_text_capped"]} == expected
	assert {item["path"] for item in manifest["scoped"]} == {"big.bin", "large.txt"} - set(expected)


def test_export_oversized_all_lists_binary_files_without_chunking(repo, tmp_path):
	(repo / "photo.jpg").write_bytes(b"\xff\xd8\x00" + b"a" * (2 * 1024 * 1024))
	git(repo, "add", "photo.jpg")
	scope = tmp_path / "scope.txt"
	scope.write_text("")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, 16777216, 67108864, "all")
	manifest = json.loads((dest / "manifest.json").read_text())
	assert [item["path"] for item in manifest["scoped"]] == ["big.bin"]
	assert manifest["unscoped_oversized"] == [{"path": "photo.jpg", "size": 2 * 1024 * 1024 + 3, "reason": "binary"}]
	assert manifest["unscoped_text_capped_count"] == 0
	assert not any(path.startswith("f002") for path in tree(dest))


def test_export_oversized_text_cap_count_survives_truncated_list(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	for index in range(51):
		(repo / f"large-{index:02d}.txt").write_text("abc")
	monkeypatch.setattr(module, "tracked_paths", lambda _host: [f"large-{index:02d}.txt" for index in range(51)])
	monkeypatch.setattr(module, "MAX_READONLY_FILE", 1)
	scope = tmp_path / "scope.txt"
	scope.write_text("")
	dest = tmp_path / "export"
	module.export_oversized(repo, scope, dest, 2, 2, "all")
	manifest = json.loads((dest / "manifest.json").read_text())
	assert manifest["unscoped_text_capped_count"] == 51
	assert len(manifest["unscoped_text_capped"]) == 50
	assert manifest["unscoped_text_capped"][0]["reason"] == "over_file_cap"


def test_export_oversized_all_gives_explicit_scope_the_cap_budget(repo, tmp_path):
	# An explicit file that sorts after an extra still fits; the extra yields.
	(repo / "z-listed.txt").write_bytes(b"z" * (2 * 1024 * 1024 + 1))
	git(repo, "add", "z-listed.txt")
	scope = tmp_path / "scope.txt"
	scope.write_text("z-listed.txt\n")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, 16777216, 2 * 1024 * 1024 + 1, "all")
	manifest = json.loads((dest / "manifest.json").read_text())
	assert [item["path"] for item in manifest["scoped"]] == ["z-listed.txt"]
	assert manifest["unscoped_oversized"] == [{"path": "big.bin", "size": 2 * 1024 * 1024 + 1, "reason": "over_total_cap"}]
	assert manifest["unscoped_text_capped_count"] == 1


@pytest.mark.parametrize("file_cap,total_cap", [("2097152", "67108864"), ("16777216", "2097152")])
def test_export_oversized_all_still_rejects_explicit_cap_breach(repo, tmp_path, file_cap, total_cap):
	scope = tmp_path / "scope.txt"
	scope.write_text("big.bin\n")
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, file_cap, total_cap, "all", check=False)
	assert proc.returncode == 1 and "exceeds cap" in proc.stderr
	assert not dest.exists()


def test_export_oversized_all_records_explicit_filtered_file_as_not_inspected(repo, tmp_path):
	# fun-token-multi-chain#579: a project diff that touches a committed
	# `.env.example` must not stall the audit; the file stays out of the
	# export and is reported as not inspected.
	scope = tmp_path / "scope.txt"
	scope.write_text(".env\ndeploy.pem\n")
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, 16777216, 67108864, "all")
	assert "filtered_scoped=2" in proc.stderr
	manifest = json.loads((dest / "manifest.json").read_text())
	assert manifest["filtered_scoped"] == [
		{"path": ".env", "reason": "credential_filter"},
		{"path": "deploy.pem", "reason": "credential_filter"},
	]
	assert manifest["filtered_scoped_count"] == 2
	assert [item["path"] for item in manifest["scoped"]] == ["big.bin"]
	exported = "".join(part.read_text(errors="replace") for part in dest.rglob("part-*.txt"))
	assert "SECRET=1" not in exported and "BEGIN" not in exported


def test_export_oversized_rejects_invalid_scope_mode(repo, tmp_path):
	scope = tmp_path / "scope.txt"
	scope.write_text("")
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, 16777216, 67108864, "other", check=False)
	assert proc.returncode == 1 and "invalid oversized scope mode" in proc.stderr
	assert not dest.exists()


@pytest.mark.parametrize("filtered_name,hide,reason", [("secret.json", None, "credential_filter"), ("CLAUDE.md", "CLAUDE.md", "hidden"), (".env.example", None, "credential_filter")])
def test_export_oversized_skips_and_records_filtered_scoped_file(repo, tmp_path, filtered_name, hide, reason):
	(repo / filtered_name).write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	git(repo, "add", filtered_name)
	scope = tmp_path / "scope.txt"
	scope.write_text(f"{filtered_name}\n")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, 16777216, 67108864, hide=hide)
	manifest = json.loads((dest / "manifest.json").read_text())
	assert manifest["filtered_scoped"] == [{"path": filtered_name, "reason": reason}]
	assert manifest["filtered_scoped_count"] == 1
	assert manifest["scoped"] == []
	assert not list(dest.rglob("part-*.txt"))


def test_export_oversized_ignores_deleted_filtered_scoped_file(repo, tmp_path):
	(repo / ".env").unlink()  # Still in the index, gone from the work tree.
	scope = tmp_path / "scope.txt"
	scope.write_text(".env\n")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, 16777216, 67108864)
	manifest = json.loads((dest / "manifest.json").read_text())
	assert manifest["filtered_scoped"] == [] and manifest["filtered_scoped_count"] == 0


@pytest.mark.parametrize("excluded_name", ["large-link.txt", "setuid.txt"])
def test_export_oversized_refuses_scoped_files_missing_from_snapshot(repo, tmp_path, excluded_name):
	(repo / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	if excluded_name == "large-link.txt":
		(repo / excluded_name).symlink_to("large.txt")
	else:
		(repo / excluded_name).write_bytes(b"x" * (2 * 1024 * 1024 + 1))
		(repo / excluded_name).chmod(0o4755)
	git(repo, "add", "large.txt", excluded_name)
	scope = tmp_path / "scope.txt"
	scope.write_text(f"{excluded_name}\n")
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, 16777216, 67108864, check=False)
	assert proc.returncode == 1 and "scoped file excluded by read-only snapshot" in proc.stderr
	assert not dest.exists()


@pytest.mark.parametrize("file_cap,total_cap", [
	("1048576", "67108864"), ("16777216", "1048576"), ("0", "67108864"), ("invalid", "67108864"),
])
def test_export_oversized_rejects_caps_before_writing(repo, tmp_path, file_cap, total_cap):
	(repo / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
	git(repo, "add", "large.txt")
	scope = tmp_path / "scope.txt"
	scope.write_text("large.txt\n")
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, file_cap, total_cap, check=False)
	assert proc.returncode == 1 and "CODEX_ISOLATION export-oversized rejected" in proc.stderr
	assert not dest.exists()
	if total_cap == "1048576":
		assert "files=1 total=2097153 cap=1048576 last_path=large.txt" in proc.stderr


def test_export_oversized_hard_cuts_single_long_line(repo, tmp_path):
	data = b"a" * (2 * 1024 * 1024 + 10)
	(repo / "large.bin").write_bytes(data)
	git(repo, "add", "large.bin")
	scope = tmp_path / "scope.txt"
	scope.write_text("large.bin\n")
	dest = tmp_path / "export"
	run("export-oversized", repo, scope, dest, len(data), len(data))
	chunks = json.loads((dest / "manifest.json").read_text())["scoped"][0]["chunks"]
	assert [chunk["bytes"] for chunk in chunks] == [1024 * 1024, 1024 * 1024, 10]
	assert all(chunk["start_line"] == chunk["end_line"] == 1 for chunk in chunks)


def test_export_oversized_rejects_unrepresentable_scope_path(repo, tmp_path):
	scope = tmp_path / "scope.txt"
	scope.write_text('"large\\nfile.py"\n')
	dest = tmp_path / "export"
	proc = run("export-oversized", repo, scope, dest, 16777216, 67108864, check=False)
	assert proc.returncode == 1 and "unrepresentable oversized scope path" in proc.stderr
	assert not dest.exists()


def test_seed_git_copies_objects_but_no_config_or_credentials(repo, tmp_path):
	dest = tmp_path / "copy"
	run("snapshot-readonly", repo, dest)
	run("seed-git", repo, dest)
	config = (dest / ".git" / "config").read_text()
	assert TOKEN not in config
	assert "extraheader" not in config and "remote" not in config
	assert not list((dest / ".git").rglob("FETCH_HEAD"))
	# Neither the worktree nor the synthetic HEAD may expose excluded blobs.
	status = git(dest, "status", "--porcelain")
	assert status == ""
	for hidden in (".env", "deploy.pem", "big.bin"):
		assert subprocess.run(["git", "show", f"HEAD:{hidden}"], cwd=dest, capture_output=True).returncode != 0
	assert git(dest, "show", "HEAD:src/app.py") == "print('app')\n"


def snapshot_ws(repo, tmp_path):
	dest = tmp_path / "work"
	manifest = tmp_path / "manifest.json"
	run("snapshot-workspace", repo, dest, manifest)
	return dest, manifest


@pytest.mark.parametrize("lock,mode", [
	("package-lock.json", "npm-ci"), ("yarn.lock", "yarn"),
	("pnpm-lock.yaml", "pnpm"), (None, "npm-install"),
])
def test_stage_deps_allowlists_node_manifests(repo, tmp_path, lock, mode):
	work = tmp_path / "work"
	work.mkdir()
	(work / "package.json").write_text('{"name":"example"}')
	for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", "pnpm-workspace.yaml", "app.py"):
		(work / name).write_text("not source for the networked container\n")
	if lock:
		for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
			if name != lock:
				(work / name).unlink()
	else:
		for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
			(work / name).unlink()
	stage = tmp_path / "stage"
	run("stage-deps", work, stage)
	assert (stage / ".codex-deps" / "node").read_text().strip() == mode
	assert (stage / ".codex-deps" / "python").read_text().strip() == "none"
	assert tree(stage) == sorted([".codex-deps/node", ".codex-deps/python", "package.json"] + ([lock] if lock else []))
	(work / "node_modules").mkdir()
	other_stage = tmp_path / "other-stage"
	run("stage-deps", work, other_stage)
	assert (other_stage / ".codex-deps" / "node").read_text().strip() == "present"
	assert "package.json" not in tree(other_stage)


def _node_project(root, rel, lock="package-lock.json"):
	project = root / rel
	project.mkdir(parents=True, exist_ok=True)
	(project / "package.json").write_text('{"name":"p"}')
	if lock:
		(project / lock).write_text("lock\n")
	(project / "index.js").write_text("source, never staged\n")
	return project


def test_stage_deps_stages_nested_node_projects_with_a_lockfile(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	_node_project(work, "cloudflare/worker")
	_node_project(work, "apps/web", lock="pnpm-lock.yaml")
	_node_project(work, "fixtures/no-lock", lock=None)
	_node_project(work, ".hidden/app")
	_node_project(work, "a/b/c/d/too-deep")
	present = _node_project(work, "has/modules")
	(present / "node_modules").mkdir()
	_node_project(work, "secrets/app")
	(work / "linked").symlink_to(work / "cloudflare")
	stage = tmp_path / "stage"
	proc = run("stage-deps", work, stage)
	listed = json.loads((stage / ".codex-deps" / "nested.json").read_text())
	assert listed == [
		{"index": 0, "path": "apps/web", "mode": "pnpm"},
		{"index": 1, "path": "cloudflare/worker", "mode": "npm-ci"},
	]
	assert sorted(p.name for p in (stage / ".codex-deps" / "nested" / "1").iterdir()) == ["mode", "package-lock.json", "package.json"]
	assert not any(p.name == "index.js" for p in stage.rglob("*"))
	assert "stage-deps nested_node=2 nested_over_limit=0" in proc.stderr
	assert "nested_skipped=present" in proc.stderr


def test_stage_deps_caps_nested_projects_and_writes_no_list_without_any(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	empty_stage = tmp_path / "empty-stage"
	run("stage-deps", work, empty_stage)
	assert not (empty_stage / ".codex-deps" / "nested.json").exists()
	for index in range(8):
		_node_project(work, f"pkgs/p{index}")
	stage = tmp_path / "stage"
	proc = run("stage-deps", work, stage)
	assert len(json.loads((stage / ".codex-deps" / "nested.json").read_text())) == 6
	assert "nested_node=6 nested_over_limit=2" in proc.stderr


def test_handoff_nested_deps_moves_only_into_listed_safe_projects(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	_node_project(work, "cloudflare/worker")
	_node_project(work, "present")
	(work / "present" / "node_modules").mkdir()
	(work / "real").mkdir()
	(work / "link").symlink_to(work / "real", target_is_directory=True)
	(work / "real" / "package.json").write_text("{}")
	stage = tmp_path / "stage"
	entries = [
		{"index": 0, "path": "cloudflare/worker"},
		{"index": 1, "path": "present"},
		{"index": 2, "path": "../escape"},
		{"index": 3, "path": "link"},
		{"index": "4", "path": "missing"},
		{"index": 5, "path": ".git"},
	]
	for entry in entries:
		modules = stage / ".codex-deps" / "nested" / str(entry["index"]) / "node_modules"
		modules.mkdir(parents=True)
		(modules / "marker").write_text("x")
	listing = tmp_path / "list.json"
	listing.write_text(json.dumps(entries))
	proc = run("handoff-nested-deps", stage, work, listing)
	assert (work / "cloudflare" / "worker" / "node_modules" / "marker").is_file()
	assert list((work / "present" / "node_modules").iterdir()) == []
	assert not (tmp_path / "escape").exists() and not (work / "real" / "node_modules").exists()
	assert not (work / "missing").exists()
	assert "handoff-nested-deps moved=1" in proc.stderr
	assert "skipped=1 reason=present" in proc.stderr and "skipped=2 reason=unsafe_path" in proc.stderr
	assert "skipped=3 reason=missing_project" in proc.stderr, proc.stderr
	assert "skipped=entry reason=malformed" in proc.stderr and "skipped=5 reason=unsafe_path" in proc.stderr
	missing_list = run("handoff-nested-deps", stage, work, tmp_path / "absent.json")
	assert "handoff-nested-deps moved=0" in missing_list.stderr


def test_handoff_nested_deps_never_follows_a_swapped_stage_directory(tmp_path):
	# The install container can write the stage: a nested/<n> directory it
	# replaced with a symlink to a directory outside the stage is not followed.
	work = tmp_path / "work"
	work.mkdir()
	_node_project(work, "swapped")
	_node_project(work, "not-installed")
	outside = tmp_path / "outside"
	(outside / "node_modules").mkdir(parents=True)
	nested = tmp_path / "stage" / ".codex-deps" / "nested"
	nested.mkdir(parents=True)
	(nested / "0").symlink_to(outside, target_is_directory=True)
	listing = tmp_path / "list.json"
	listing.write_text(json.dumps([{"index": 0, "path": "swapped"}, {"index": 1, "path": "not-installed"}]))
	proc = run("handoff-nested-deps", tmp_path / "stage", work, listing)
	assert (outside / "node_modules").is_dir() and not (work / "swapped" / "node_modules").exists()
	assert "skipped=0 reason=unsafe_source" in proc.stderr
	assert "skipped=1 reason=install_missing" in proc.stderr
	assert "handoff-nested-deps moved=0" in proc.stderr


@pytest.mark.parametrize("bad_kind", ["symlink", "oversized"])
def test_stage_deps_skips_unsafe_manifests(repo, tmp_path, bad_kind):
	work = tmp_path / "work"
	work.mkdir()
	if bad_kind == "symlink":
		(work / "package.json").symlink_to(repo / "src" / "app.py")
	else:
		(work / "package.json").write_bytes(b"x" * (64 * 1024 * 1024 + 1))
	stage = tmp_path / "stage"
	proc = run("stage-deps", work, stage)
	assert "skipped=package.json" in proc.stderr
	assert (stage / ".codex-deps" / "node").read_text().strip() == "none"
	assert not (stage / "package.json").exists()


def test_stage_deps_filters_requirements_and_preserves_precedence(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	(work / "requirements.txt").write_text("\n".join([
		"requests==2.31", 'foo[bar]>=1; python_version>"3.8"',
		"x @ https://example.org/x.whl", "valid==1 --hash=sha256:abc123",
		"-e .", "-r extras.txt", "-c constraints.txt", "-i https://evil.invalid",
		"--extra-index-url https://evil.invalid", "-f /tmp", "file:/tmp/pkg",
		"./pkg", "/abs", "../x", "my-pkg[test]>=1",
		"other @ file:///tmp/other", "other @ http://evil.invalid/pkg",
		"other --index-url=https://evil.invalid", "other @ https://user:pass@evil.invalid/pkg",
	]) + "\n")
	(work / "pyproject.toml").write_text('[project]\nname = "my.pkg"\ndependencies = ["from-project==1"]\n[project.optional-dependencies]\ndev = ["dev-tool==1"]\n[build-system]\nrequires = ["setuptools==68"]\n')
	(work / "pytest.ini").write_text("[pytest]\n")
	stage = tmp_path / "stage"
	run("stage-deps", work, stage)
	meta = stage / ".codex-deps"
	assert (meta / "python").read_text().strip() == "both"
	assert (meta / "requirements.txt").read_text().splitlines() == [
		"requests==2.31", 'foo[bar]>=1; python_version>"3.8"',
		"x @ https://example.org/x.whl", "valid==1 --hash=sha256:abc123",
	]
	assert (meta / "base.txt").read_text().splitlines() == ["from-project==1"]
	assert (meta / "dev.txt").read_text().splitlines() == ["dev-tool==1"]
	assert (meta / "build.txt").read_text().splitlines() == ["setuptools==68"]
	assert (meta / "source-install").exists()
	assert (meta / "want-pytest").exists()
	assert "evil.invalid" not in (meta / "requirements.txt").read_text()


def test_stage_deps_pyproject_build_and_offline_marker(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	(work / "pyproject.toml").write_text('''[project]
name = "my_pkg"
dependencies = ["requests>=2", "my.pkg[test]", "local @ file:///tmp/local"]
[project.optional-dependencies]
dev = ["pytest>=7", "my-pkg[dev]"]
[build-system]
requires = ["setuptools>=68", "wheel"]
[tool.pytest.ini_options]
addopts = "-q"
''')
	stage = tmp_path / "stage"
	run("stage-deps", work, stage)
	meta = stage / ".codex-deps"
	assert (meta / "python").read_text().strip() == "pyproject"
	assert (meta / "base.txt").read_text().splitlines() == ["requests>=2"]
	assert (meta / "dev.txt").read_text().splitlines() == ["pytest>=7"]
	assert (meta / "build.txt").read_text().splitlines() == ["setuptools>=68", "wheel"]
	assert (meta / "source-install").exists() and (meta / "want-pytest").exists()
	(work / "package.json").write_text("{}")
	stage2 = tmp_path / "stage2"
	run("stage-deps", work, stage2)
	assert (stage2 / ".codex-deps" / "python").read_text().strip() == "pyproject"
	assert (stage2 / ".codex-deps" / "source-install").exists()


@pytest.mark.parametrize("installable", [False, True])
def test_stage_deps_requirements_only_marks_installable_source(tmp_path, installable):
	work = tmp_path / "work"
	work.mkdir()
	(work / "requirements.txt").write_text("requests==2\n-e .\n")
	if installable:
		(work / "setup.py").write_text("from setuptools import setup\nsetup(name='sample')\n")
	stage = tmp_path / "stage"
	run("stage-deps", work, stage)
	meta = stage / ".codex-deps"
	assert (meta / "python").read_text().strip() == "requirements"
	assert (meta / "requirements.txt").read_text().splitlines() == ["requests==2"]
	assert (meta / "source-install").exists() == installable
	assert not (stage / "setup.py").exists()


def test_stage_deps_bad_pyproject_degrades_without_install(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	(work / "pyproject.toml").write_text("[project\n")
	stage = tmp_path / "stage"
	proc = run("stage-deps", work, stage)
	assert proc.returncode == 0 and "parse_failed" in proc.stderr
	assert (stage / ".codex-deps" / "python").read_text().strip() == "none"
	assert not (stage / ".codex-deps" / "source-install").exists()


@pytest.mark.parametrize("with_requirements", [False, True])
def test_stage_deps_config_only_pyproject_skips_source_install(tmp_path, with_requirements):
	work = tmp_path / "work"
	work.mkdir()
	(work / "pyproject.toml").write_text('[tool.ruff]\nline-length = 100\n')
	if with_requirements:
		(work / "requirements.txt").write_text("requests==2\n")
	stage = tmp_path / "stage"
	run("stage-deps", work, stage)
	meta = stage / ".codex-deps"
	assert (meta / "python").read_text().strip() == ("both" if with_requirements else "pyproject")
	assert not (meta / "source-install").exists()
	assert (meta / "base.txt").read_text() == "\n"
	if with_requirements:
		assert (meta / "requirements.txt").read_text().splitlines() == ["requests==2"]


def test_stage_deps_never_follows_nested_symlinks_for_pytest_marker(tmp_path):
	work = tmp_path / "work"
	work.mkdir()
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / "conftest.py").write_text("private\n")
	(work / "tests").symlink_to(outside, target_is_directory=True)
	stage = tmp_path / "stage"
	run("stage-deps", work, stage)
	assert not (stage / ".codex-deps" / "want-pytest").exists()
	assert not list(stage.rglob("conftest.py"))


def test_workspace_snapshot_excludes_git_and_keeps_symlinks(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	assert not (dest / ".git").exists()
	assert not (dest / ".env").exists() and not (dest / "deploy.pem").exists()
	assert (dest / "link").is_symlink() and os.readlink(dest / "link") == "/etc/passwd"
	assert (dest / "untracked.txt").exists()
	entries = json.loads(manifest.read_text())["entries"]
	assert entries["link"] == ["l", "/etc/passwd"]
	assert not any(name == ".git" or name.startswith(".git/") for name in entries)
	assert ".env" not in entries and "deploy.pem" not in entries


def test_workspace_snapshot_and_transfer_exclude_nested_credentials(repo, tmp_path):
	(repo / "secrets").mkdir()
	(repo / "secrets" / "token.txt").write_text("sensitive\n")
	dest, manifest = snapshot_ws(repo, tmp_path)
	assert not (dest / "secrets").exists()
	(dest / ".env").write_text("injected\n")
	(dest / "src" / "app.py").write_text("changed\n")
	run("transfer", repo, dest, manifest)
	assert (repo / ".env").read_text() == "SECRET=1\n"
	assert (repo / "secrets" / "token.txt").read_text() == "sensitive\n"
	assert (repo / "src" / "app.py").read_text() == "changed\n"


def test_workspace_snapshot_skips_worktree_git_file(repo, tmp_path):
	(repo / "nested").mkdir()
	(repo / "nested" / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
	dest, manifest = snapshot_ws(repo, tmp_path)
	assert not (dest / "nested" / ".git").exists()


def test_transfer_applies_new_changed_deleted_files_and_modes(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "src" / "app.py").write_text("print('changed')\n")
	(dest / "src" / "new.py").write_text("x = 1\n")
	(dest / "frontend" / "index.ts").unlink()
	(dest / "tool.sh").write_text("#!/bin/sh\n")
	(dest / "tool.sh").chmod(0o755)
	(dest / "deep" / "er").mkdir(parents=True)
	(dest / "deep" / "er" / "f.txt").write_text("f\n")
	(dest / ".git").mkdir()
	(dest / ".git" / "config").write_text("[core]\n")
	proc = run("transfer", repo, dest, manifest)
	assert (repo / "src" / "app.py").read_text() == "print('changed')\n"
	assert (repo / "src" / "new.py").read_text() == "x = 1\n"
	assert not (repo / "frontend" / "index.ts").exists()
	assert os.access(repo / "tool.sh", os.X_OK)
	assert (repo / "deep" / "er" / "f.txt").exists()
	assert TOKEN in (repo / ".git" / "config").read_text(), "host .git must never be touched"
	assert "written=4 deleted=1" in proc.stderr


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_transfer_refuses_symlink_and_special_results_without_host_writes(repo, tmp_path, kind):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "src" / "app.py").write_text("print('changed')\n")
	if kind == "symlink":
		os.symlink(str(Path.home() / ".ssh" / "id_rsa"), dest / "evil")
	else:
		os.mkfifo(dest / "evil")
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1
	assert "CODEX_ISOLATION transfer rejected" in proc.stderr
	assert (repo / "src" / "app.py").read_text() == "print('app')\n"
	assert not (repo / "evil").exists() and not (repo / "evil").is_symlink()


def test_transfer_refuses_changed_symlink(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "link").unlink()
	os.symlink("/etc/shadow", dest / "link")
	assert run("transfer", repo, dest, manifest, check=False).returncode == 1
	assert os.readlink(repo / "link") == "/etc/passwd"


def test_transfer_refuses_when_host_changed_since_snapshot(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "src" / "app.py").write_text("agent\n")
	(repo / "src" / "app.py").write_text("host moved on\n")
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1 and "host path changed since snapshot" in proc.stderr
	assert (repo / "src" / "app.py").read_text() == "host moved on\n"


def test_transfer_refuses_writes_through_a_host_symlinked_parent(repo, tmp_path):
	outside = tmp_path / "outside"
	outside.mkdir()
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "pkg").mkdir()
	(dest / "pkg" / "x.py").write_text("x\n")
	os.symlink(outside, repo / "pkg")  # host gains a symlink where the agent created a directory
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1
	assert not (outside / "x.py").exists()


def test_transfer_handles_directory_replaced_by_file(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "frontend" / "index.ts").unlink()
	(dest / "frontend").rmdir()
	(dest / "frontend").write_text("now a file\n")
	run("transfer", repo, dest, manifest)
	assert (repo / "frontend").read_text() == "now a file\n"


def test_transfer_rejects_host_only_file_before_deleting_directory(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "frontend" / "index.ts").unlink()
	(dest / "frontend").rmdir()
	(dest / "frontend").write_text("replacement\n")
	(repo / "frontend" / "host-only.txt").write_text("keep\n")
	proc = run("transfer", repo, dest, manifest, check=False)
	assert proc.returncode == 1
	assert (repo / "frontend" / "index.ts").read_text() == "export {}\n"
	assert (repo / "frontend" / "host-only.txt").read_text() == "keep\n"


def test_transfer_rolls_back_prior_writes_and_deletes_on_late_failure(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "frontend" / "index.ts").unlink()
	(dest / "src" / "app.py").write_text("changed\n")
	(dest / "src" / "new.py").write_text("new\n")
	real_replace = module.os.replace

	def fail_new_file(source, target):
		if str(target).endswith("/src/new.py"):
			raise OSError("simulated late write failure")
		return real_replace(source, target)

	monkeypatch.setattr(module.os, "replace", fail_new_file)
	with pytest.raises(OSError):
		module.transfer(repo, dest, manifest)
	assert (repo / "frontend" / "index.ts").read_text() == "export {}\n"
	assert (repo / "src" / "app.py").read_text() == "print('app')\n"
	assert not (repo / "src" / "new.py").exists()


def test_transfer_restores_file_replaced_by_directory_on_failure(repo, tmp_path, monkeypatch):
	sys.path.insert(0, str(MODULE.parent))
	import codex_isolated_workspace as module

	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "run.sh").unlink()
	(dest / "run.sh").mkdir()
	(dest / "run.sh" / "new.py").write_text("new\n")
	real_replace = module.os.replace

	def fail_new_file(source, target):
		if str(target).endswith("/run.sh/new.py"):
			raise OSError("simulated late write failure")
		return real_replace(source, target)

	monkeypatch.setattr(module.os, "replace", fail_new_file)
	with pytest.raises(OSError):
		module.transfer(repo, dest, manifest)
	assert (repo / "run.sh").read_text() == "#!/bin/sh\necho run\n"
	assert (repo / "run.sh").is_file()


def test_prep_roots_are_kept_and_never_transferred(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "node_modules" / "pkg").mkdir(parents=True)
	(dest / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
	(dest / "src" / "app.py").write_text("build backend wrote this\n")
	(dest / "src" / "app.egg-info").mkdir()
	(dest / "src" / "app.egg-info" / "PKG-INFO").write_text("x\n")
	run("prep-finalize", repo, dest, manifest)
	roots = json.loads(manifest.read_text())["prep_roots"]
	assert roots == ["node_modules", "src/app.egg-info"]
	# Build-backend writes to snapshotted files are undone before the agent runs.
	assert (dest / "src" / "app.py").read_text() == "print('app')\n"
	assert (dest / "node_modules" / "pkg" / "index.js").exists()
	# A later attempt keeps prep roots, and agent writes inside them stay put.
	run("snapshot-workspace", repo, dest, manifest)
	assert (dest / "node_modules" / "pkg" / "index.js").exists()
	(dest / "node_modules" / ".cache").mkdir()
	(dest / "node_modules" / ".cache" / "x").write_text("cache\n")
	run("transfer", repo, dest, manifest)
	assert not (repo / "node_modules").exists()
	assert not (repo / "src" / "app.egg-info").exists()


def test_new_snapshot_discards_leftovers_from_an_earlier_attempt(repo, tmp_path):
	dest, manifest = snapshot_ws(repo, tmp_path)
	(dest / "leftover.txt").write_text("from a killed attempt\n")
	run("snapshot-workspace", repo, dest, manifest)
	assert not (dest / "leftover.txt").exists()


def test_copy_include_copies_regular_files_only(tmp_path):
	src = tmp_path / "runtime"
	(src / "sub").mkdir(parents=True)
	(src / "sub" / "a.txt").write_text("a\n")
	(src / ".git").write_text("gitdir: /x\n")
	(src / "secrets").mkdir()
	(src / "secrets" / "token.txt").write_text("secret\n")
	(src / ".env").write_text("secret\n")
	os.symlink("/etc/passwd", src / "sub" / "link")
	target = tmp_path / "out" / "runtime"
	run("copy-include", src, target)
	assert tree(target) == ["sub/a.txt"]


def test_copy_include_rejects_symlink_source(tmp_path):
	real = tmp_path / "real.txt"
	real.write_text("x\n")
	os.symlink(real, tmp_path / "alias.txt")
	assert run("copy-include", tmp_path / "alias.txt", tmp_path / "out", check=False).returncode == 1


# --- hidden paths (CODEX_ISOLATED_HIDE; the Claude engine's hide_claude_md) ------------


def test_hidden_claude_md_is_never_copied_shown_or_written_back(repo, tmp_path):
	(repo / "CLAUDE.md").write_text("repository instructions\n")
	git(repo, "add", "CLAUDE.md")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "claude md")
	readonly = tmp_path / "ro"
	run("snapshot-readonly", repo, readonly, hide="CLAUDE.md")
	run("seed-git", repo, readonly, hide="CLAUDE.md")
	assert not (readonly / "CLAUDE.md").exists() and (readonly / "src" / "app.py").exists()
	assert subprocess.run(["git", "show", "HEAD:CLAUDE.md"], cwd=readonly, capture_output=True).returncode != 0
	dest = tmp_path / "work"
	manifest = tmp_path / "manifest.json"
	run("snapshot-workspace", repo, dest, manifest, hide="CLAUDE.md")
	assert not (dest / "CLAUDE.md").exists()
	(dest / "CLAUDE.md").write_text("agent instructions\n")
	(dest / "src" / "app.py").write_text("print('edited')\n")
	proc = run("transfer", repo, dest, manifest, hide="CLAUDE.md")
	assert "transfer ignored=CLAUDE.md reason=hidden" in proc.stderr
	assert (repo / "CLAUDE.md").read_text() == "repository instructions\n"
	assert (repo / "src" / "app.py").read_text() == "print('edited')\n"


def test_hidden_path_does_not_delete_the_host_file(repo, tmp_path):
	(repo / "CLAUDE.md").write_text("repository instructions\n")
	dest = tmp_path / "work"
	manifest = tmp_path / "manifest.json"
	run("snapshot-workspace", repo, dest, manifest, hide="CLAUDE.md")
	run("transfer", repo, dest, manifest, hide="CLAUDE.md")
	assert (repo / "CLAUDE.md").read_text() == "repository instructions\n"


@pytest.mark.parametrize("value", ["../CLAUDE.md", "docs/CLAUDE.md", ".", "a b"])
def test_invalid_hidden_path_is_refused(repo, tmp_path, value):
	proc = run("snapshot-readonly", repo, tmp_path / "ro", check=False, hide=value)
	assert proc.returncode == 1
	assert "invalid hidden path" in proc.stderr


def test_seed_git_adds_the_filtered_default_branch_as_isolated_base(repo, tmp_path):
	"""fun-token-multi-chain#639: the agent can read the default branch offline."""
	git(repo, "branch", "base-tip")
	(repo / "src" / "app.py").write_text("print('changed on the project branch')\n")
	(repo / "src" / "new.py").write_text("NEW = 1\n")
	git(repo, "add", "src/app.py", "src/new.py")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "project change")
	dest = tmp_path / "copy"
	run("snapshot-readonly", repo, dest)
	proc = run("seed-git", repo, dest, "base-tip")
	assert "seed-git outcome=ok base=present" in proc.stderr
	assert git(dest, "show", "isolated-base:src/app.py") == "print('app')\n"
	assert git(dest, "show", "HEAD:src/app.py") == "print('changed on the project branch')\n"
	assert git(dest, "rev-parse", "HEAD^").strip() == git(dest, "rev-parse", "isolated-base").strip()
	assert git(dest, "diff", "--name-only", "isolated-base", "HEAD").split() == ["src/app.py", "src/new.py"]
	assert git(dest, "status", "--porcelain") == ""
	assert git(dest, "rev-parse", "--abbrev-ref", "HEAD").strip() == "isolated"
	for hidden in (".env", "deploy.pem", "big.bin"):
		assert subprocess.run(["git", "show", f"isolated-base:{hidden}"], cwd=dest, capture_output=True).returncode != 0
	config = (dest / ".git" / "config").read_text()
	assert TOKEN not in config and "remote" not in config


def test_seed_git_base_never_exposes_a_credential_only_present_on_the_base(repo, tmp_path):
	(repo / "client_secret.json").write_text("credential value\n")
	git(repo, "add", "client_secret.json")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base with a credential")
	git(repo, "branch", "base-tip")
	git(repo, "rm", "-q", "client_secret.json")
	git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "drop it")
	dest = tmp_path / "copy"
	run("snapshot-readonly", repo, dest)
	run("seed-git", repo, dest, "base-tip")
	assert subprocess.run(["git", "show", "isolated-base:client_secret.json"], cwd=dest, capture_output=True).returncode != 0
	objects = subprocess.run(["git", "cat-file", "--batch-all-objects", "--batch"], cwd=dest, capture_output=True).stdout
	assert b"credential value" not in objects


@pytest.mark.parametrize("base_ref", ["no-such-ref", "", "--output=/tmp/x"])
def test_seed_git_without_a_resolvable_base_keeps_a_single_commit(repo, tmp_path, base_ref):
	dest = tmp_path / "copy"
	run("snapshot-readonly", repo, dest)
	proc = run("seed-git", repo, dest, base_ref)
	expected = "base=none" if base_ref == "" else "base=unresolved"
	assert f"seed-git outcome=ok {expected}" in proc.stderr
	assert subprocess.run(["git", "rev-parse", "--verify", "HEAD^"], cwd=dest, capture_output=True).returncode != 0
	assert git(dest, "branch", "--list", "isolated-base") == ""
	assert git(dest, "show", "HEAD:src/app.py") == "print('app')\n"


def test_isolated_runner_and_implement_step_pass_the_default_branch_base():
	exec_script = (REPO_ROOT / "scripts" / "codex_isolated_exec.sh").read_text()
	assert 'workspace_py seed-git "${workdir}" "${root}/work" "${CODEX_ISOLATED_GIT_BASE_REF:-}" || true' in exec_script
	implement = (REPO_ROOT / ".github" / "workflows" / "implement.yml").read_text()
	assert "IMPLEMENT_ISOLATED_BASE_BRANCH: ${{ github.event.repository.default_branch }}" in implement
	assert '"+refs/heads/${IMPLEMENT_ISOLATED_BASE_BRANCH}:refs/codex-isolated/base"' in implement
	assert implement.index("export CODEX_ISOLATED_GIT_BASE_REF") < implement.index('prepare --workdir "$(pwd)" --deps')
	prompt = (REPO_ROOT / "prompts" / "mode-implement.txt").read_text()
	assert "git show isolated-base:<path>" in prompt
	# The inline heredoc is the prompt implement.yml actually sends.
	inline = implement[implement.index('mode-implement-inline.txt"\n'):]
	inline = inline[: inline.index("\n          EOF\n")]
	assert "git show isolated-base:<path>" in inline
	assert "`git diff HEAD` shows the edits from this run that you have not" in inline
	assert "`git log isolated-base..HEAD`." in inline
