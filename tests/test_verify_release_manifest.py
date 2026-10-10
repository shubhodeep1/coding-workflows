#!/usr/bin/env python3
"""Tests for scripts/verify_release_manifest.py (release manifest verifier, issue #6957).

Each fixture release is built with scripts/release_manifest.py under tmp_path;
the real-repo test only reads the live checkout. Only the verify-pr-tree tests
(issue #7004) run git, against throwaway repositories under tmp_path.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILDER = REPO_ROOT / "scripts" / "release_manifest.py"
VERIFIER = REPO_ROOT / "scripts" / "verify_release_manifest.py"
SCHEMA_PATH = REPO_ROOT / "ai-memory" / "schemas" / "release_manifest.v1.json"
GOOD_SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
GOOD_TAG = "v1.2.3"
LINE_RE = re.compile(r"^UPDATER_MANIFEST_VERIFY outcome=(ok|rejected) reason=([a-z_]+) count=(\d+)( path=(\S+))?$")
DOCUMENTED_REASONS = {
	"ok",
	"invalid_argument",
	"manifest_unreadable",
	"paths_file_unreadable",
	"schema_invalid",
	"repository_mismatch",
	"sha_mismatch",
	"unlisted_path",
	"missing_file",
	"read_failed",
	"non_regular_file",
	"symlink_mismatch",
	"path_escape",
	"mode_mismatch",
	"size_mismatch",
	"hash_mismatch",
	"internal_error",
	"content_mismatch",
	"retired_hash_mismatch",
	"derived_mismatch",
	"derived_missing",
	"too_many_changes",
	"git_failed",
}
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _write(root: Path, rel: str, data: str, mode: int = 0o644) -> Path:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(data, encoding="utf-8")
	os.chmod(path, mode)
	return path


def _fixture(tmp_path: Path) -> Path:
	root = tmp_path / "repo"
	_write(root, "CLAUDE.md", "# rules\n")
	_write(root, "workflow-templates/ai-update-workflows.yml", "name: update\n")
	_write(root, "workflow-templates/ai-review.yml", "name: review\n")
	_write(root, "workflow-templates/profiles/core.txt", "ai-review.yml\n")
	_write(root, "workflow-templates/retired_files.txt", "# none\n")
	_write(root, "workflow-templates/.claude/settings.json", "{}\n")
	_write(root, "workflow-templates/.claude/hooks/hook.sh", "#!/bin/sh\n", 0o755)
	_write(root, "workflow-templates/audit-gate/contract.json", "{}\n")
	(root / "workflow-templates" / "CLAUDE.md").symlink_to("../CLAUDE.md")
	for name in ("workflow_wrapper_refs.py", "apply_audit_gate_assets.py", "assemble_changelog.py", "verify_release_manifest.py", "release_manifest.py", "lint_pr_body_auto_close.py"):
		_write(root, f"scripts/{name}", f"# {name}\n", 0o755)
	_write(root, "scripts/unrelated.py", "# not attested\n")
	return root


def _build(root: Path, output: Path) -> None:
	result = subprocess.run(
		[sys.executable, str(BUILDER), "build", "--repo-root", str(root), "--release-sha", GOOD_SHA, "--tag", GOOD_TAG, "--output", str(output)],
		capture_output=True,
		text=True,
		env=ENV,
	)
	assert result.returncode == 0, result.stderr


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, Path]:
	root = _fixture(tmp_path)
	manifest = tmp_path / "manifest.json"
	_build(root, manifest)
	return root, manifest


def _verify(manifest: Path, root: Path, *extra: str, sha: str = GOOD_SHA) -> tuple[int, str, str]:
	cmd = [sys.executable, str(VERIFIER), "verify", "--manifest", str(manifest), "--release-root", str(root), "--expected-sha", sha, *extra]
	result = subprocess.run(cmd, capture_output=True, text=True, env=ENV)
	return result.returncode, result.stdout, result.stderr


def _parse(rc: int, stdout: str, stderr: str) -> re.Match:
	assert stderr == ""
	assert rc in (0, 2)
	assert stdout.endswith("\n")
	lines = stdout.splitlines()
	assert len(lines) == 1, stdout
	match = LINE_RE.match(lines[0])
	assert match, lines[0]
	assert (rc == 0) == (match.group(1) == "ok")
	return match


def _assert_rejected(result: tuple[int, str, str], reason: str) -> re.Match:
	match = _parse(*result)
	assert result[0] == 2
	assert match.group(1) == "rejected"
	assert match.group(2) == reason, result[1]
	return match


def _manifest_doc(manifest: Path) -> dict:
	return json.loads(manifest.read_text(encoding="utf-8"))


def _write_doc(manifest: Path, doc) -> None:
	manifest.write_text(json.dumps(doc, indent="\t"), encoding="utf-8")


def _load_module():
	spec = importlib.util.spec_from_file_location("verify_release_manifest_under_test", VERIFIER)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_accepts_fresh_build(release) -> None:
	root, manifest = release
	count = len(_manifest_doc(manifest)["files"])
	match = _parse(*_verify(manifest, root))
	assert match.group(1) == "ok" and match.group(2) == "ok"
	assert int(match.group(3)) == count
	assert match.group(4) is None


def test_accepts_full_paths_file_and_uppercase_sha(release, tmp_path: Path) -> None:
	root, manifest = release
	paths = tmp_path / "paths.txt"
	paths.write_text("\n".join(entry["path"] for entry in _manifest_doc(manifest)["files"]) + "\n\n", encoding="utf-8")
	match = _parse(*_verify(manifest, root, "--paths-file", str(paths), sha=GOOD_SHA.upper()))
	assert match.group(1) == "ok"


def _tamper_same_size(root: Path) -> None:
	_write(root, "workflow-templates/ai-review.yml", "name: reviev\n")


def _append_byte(root: Path) -> None:
	_write(root, "workflow-templates/ai-review.yml", "name: review\n\n")


def _flip_exec_off(root: Path) -> None:
	os.chmod(root / "workflow-templates/.claude/hooks/hook.sh", 0o644)


def _flip_exec_on(root: Path) -> None:
	os.chmod(root / "workflow-templates/.claude/settings.json", 0o755)


def _delete_file(root: Path) -> None:
	(root / "workflow-templates/retired_files.txt").unlink()


def _file_to_symlink(root: Path) -> None:
	target = root / "workflow-templates/.claude/hooks/hook.sh"
	target.unlink()
	target.symlink_to("/etc/passwd")


def _retarget_claude_link(root: Path) -> None:
	link = root / "workflow-templates/CLAUDE.md"
	link.unlink()
	link.symlink_to("../scripts/unrelated.py")


def _claude_link_to_regular(root: Path) -> None:
	link = root / "workflow-templates/CLAUDE.md"
	link.unlink()
	_write(root, "workflow-templates/CLAUDE.md", "../CLAUDE.md")


def _symlinked_parent(root: Path) -> None:
	claude = root / "workflow-templates/.claude"
	elsewhere = root.parent / "elsewhere"
	claude.rename(elsewhere)
	claude.symlink_to(elsewhere)


def _directory_at_file(root: Path) -> None:
	target = root / "workflow-templates/retired_files.txt"
	target.unlink()
	target.mkdir()


def _fifo_at_file(root: Path) -> None:
	target = root / "workflow-templates/retired_files.txt"
	target.unlink()
	os.mkfifo(target)


@pytest.mark.parametrize(
	("mutate", "reason", "path"),
	[
		(_tamper_same_size, "hash_mismatch", "workflow-templates/ai-review.yml"),
		(_append_byte, "size_mismatch", "workflow-templates/ai-review.yml"),
		(_flip_exec_off, "mode_mismatch", "workflow-templates/.claude/hooks/hook.sh"),
		(_flip_exec_on, "mode_mismatch", "workflow-templates/.claude/settings.json"),
		(_delete_file, "missing_file", "workflow-templates/retired_files.txt"),
		(_file_to_symlink, "symlink_mismatch", "workflow-templates/.claude/hooks/hook.sh"),
		(_retarget_claude_link, "symlink_mismatch", "workflow-templates/CLAUDE.md"),
		(_claude_link_to_regular, "symlink_mismatch", "workflow-templates/CLAUDE.md"),
		(_directory_at_file, "non_regular_file", "workflow-templates/retired_files.txt"),
		(_fifo_at_file, "non_regular_file", "workflow-templates/retired_files.txt"),
	],
)
def test_tampered_tree_is_rejected(release, mutate, reason: str, path: str) -> None:
	root, manifest = release
	mutate(root)
	match = _assert_rejected(_verify(manifest, root), reason)
	assert match.group(5) == path
	assert int(match.group(3)) == len(_manifest_doc(manifest)["files"])


def test_symlinked_parent_directory_is_rejected(release) -> None:
	root, manifest = release
	_symlinked_parent(root)
	match = _assert_rejected(_verify(manifest, root), "symlink_mismatch")
	assert match.group(5).startswith("workflow-templates/.claude/")


def test_unlisted_path_is_rejected(release, tmp_path: Path) -> None:
	root, manifest = release
	paths = tmp_path / "paths.txt"
	paths.write_text("CLAUDE.md\nscripts/unrelated.py\n", encoding="utf-8")
	match = _assert_rejected(_verify(manifest, root, "--paths-file", str(paths)), "unlisted_path")
	assert match.group(5) == "scripts/unrelated.py"


@pytest.mark.parametrize("line", ["./CLAUDE.md", "CLAUDE.md\r", "/CLAUDE.md"])
def test_paths_file_is_not_normalised(release, tmp_path: Path, line: str) -> None:
	root, manifest = release
	paths = tmp_path / "paths.txt"
	paths.write_bytes((line + "\n").encode("utf-8"))
	_assert_rejected(_verify(manifest, root, "--paths-file", str(paths)), "unlisted_path")


def test_path_is_neutralised(release, tmp_path: Path) -> None:
	root, manifest = release
	paths = tmp_path / "paths.txt"
	paths.write_text("evil\x1b[31m nameé\n", encoding="utf-8")
	match = _assert_rejected(_verify(manifest, root, "--paths-file", str(paths)), "unlisted_path")
	assert match.group(5) == "evil?[31m?name?"


def test_wrong_expected_sha(release) -> None:
	root, manifest = release
	_assert_rejected(_verify(manifest, root, sha=OTHER_SHA), "sha_mismatch")


def test_wrong_expected_repository(release) -> None:
	root, manifest = release
	_assert_rejected(_verify(manifest, root, "--expected-repository", "other/repo"), "repository_mismatch")


def test_edited_repository_with_matching_expectation_is_schema_invalid(release) -> None:
	root, manifest = release
	doc = _manifest_doc(manifest)
	doc["repository"] = "other/repo"
	_write_doc(manifest, doc)
	_assert_rejected(_verify(manifest, root, "--expected-repository", "other/repo"), "schema_invalid")


def _entry(doc: dict, path: str) -> dict:
	return next(entry for entry in doc["files"] if entry["path"] == path)


def _set(key, value):
	def mutate(doc):
		doc[key] = value

	return mutate


def _set_entry(path, key, value):
	def mutate(doc):
		_entry(doc, path)[key] = value

	return mutate


def _del_entry_key(path, key):
	def mutate(doc):
		del _entry(doc, path)[key]

	return mutate


def _append_entry(entry):
	def mutate(doc):
		doc["files"].append(entry)

	return mutate


_ZERO = "0" * 64
# Documents the checked-in JSON Schema also rejects (shared with the parity test).
SCHEMA_MALFORMED = {
	"wrong_schema_version": _set("schema_version", "release_manifest.v2"),
	"extra_top_level_key": _set("generated_at", "now"),
	"missing_tag": lambda doc: doc.pop("tag"),
	"bad_tag": _set("tag", "1.2.3"),
	"tag_not_string": _set("tag", 123),
	"empty_files": _set("files", []),
	"files_not_list": _set("files", {}),
	"size_true": _set_entry("CLAUDE.md", "size", True),
	"size_negative": _set_entry("CLAUDE.md", "size", -1),
	"size_string": _set_entry("CLAUDE.md", "size", "8"),
	"bad_sha256": _set_entry("CLAUDE.md", "sha256", "A" * 64),
	"short_sha256": _set_entry("CLAUDE.md", "sha256", "0" * 63),
	"bad_mode": _set_entry("CLAUDE.md", "mode", "100600"),
	"extra_entry_key": _set_entry("CLAUDE.md", "owner", "root"),
	"missing_entry_key": _del_entry_key("CLAUDE.md", "sha256"),
	"dotdot_segment": _append_entry({"path": "zz/../x", "sha256": _ZERO, "size": 0, "mode": "100644"}),
	"absolute_path": _append_entry({"path": "/zz", "sha256": _ZERO, "size": 0, "mode": "100644"}),
	"symlink_mode_not_allowlisted": _append_entry({"path": "zz", "sha256": _ZERO, "size": 0, "mode": "120000", "link_target": "../CLAUDE.md"}),
	"known_symlink_as_file": lambda doc: [_entry(doc, "workflow-templates/CLAUDE.md").pop("link_target"), _entry(doc, "workflow-templates/CLAUDE.md").update(mode="100644")],
	"symlink_wrong_target": _set_entry("workflow-templates/CLAUDE.md", "link_target", "../../CLAUDE.md"),
	"entry_not_object": _append_entry("zz"),
	"top_level_list": None,
	"bad_release_sha": _set("release_sha", "g" * 40),
	"other_repository": _set("repository", "other/repo"),
}


def _malformed_doc(manifest: Path, name: str):
	if name == "top_level_list":
		return [_manifest_doc(manifest)]
	doc = copy.deepcopy(_manifest_doc(manifest))
	SCHEMA_MALFORMED[name](doc)
	return doc


@pytest.mark.parametrize("name", sorted(SCHEMA_MALFORMED))
def test_malformed_manifest_is_rejected(release, name: str) -> None:
	root, manifest = release
	_write_doc(manifest, _malformed_doc(manifest, name))
	expected = {"bad_release_sha": "sha_mismatch", "other_repository": "repository_mismatch"}.get(name, "schema_invalid")
	_assert_rejected(_verify(manifest, root), expected)


def test_unsorted_and_duplicate_entries_are_schema_invalid(release) -> None:
	root, manifest = release
	doc = _manifest_doc(manifest)
	doc["files"] = list(reversed(doc["files"]))
	_write_doc(manifest, doc)
	_assert_rejected(_verify(manifest, root), "schema_invalid")
	doc = _manifest_doc(manifest)
	doc["files"] = list(reversed(doc["files"]))
	doc["files"].insert(1, copy.deepcopy(doc["files"][0]))
	_write_doc(manifest, doc)
	_assert_rejected(_verify(manifest, root), "schema_invalid")


@pytest.mark.parametrize(
	"text",
	[
		"not json",
		'{"schema_version": "release_manifest.v1"',
		"",
		'{"size": NaN}',
	],
)
def test_malformed_json_is_schema_invalid(release, text: str) -> None:
	root, manifest = release
	manifest.write_text(text, encoding="utf-8")
	_assert_rejected(_verify(manifest, root), "schema_invalid")


def test_truncated_manifest_is_schema_invalid(release) -> None:
	root, manifest = release
	data = manifest.read_bytes()
	manifest.write_bytes(data[: len(data) // 2])
	_assert_rejected(_verify(manifest, root), "schema_invalid")


def test_duplicate_key_is_schema_invalid(release) -> None:
	root, manifest = release
	text = manifest.read_text(encoding="utf-8")
	assert '"tag": "v1.2.3"' in text
	manifest.write_text(text.replace('"tag": "v1.2.3"', '"tag": "v9.9.9",\n\t"tag": "v1.2.3"', 1), encoding="utf-8")
	_assert_rejected(_verify(manifest, root), "schema_invalid")


def test_invalid_utf8_manifest_is_schema_invalid(release) -> None:
	root, manifest = release
	manifest.write_bytes(b"\xff\xfe{}")
	_assert_rejected(_verify(manifest, root), "schema_invalid")


def test_missing_manifest(release, tmp_path: Path) -> None:
	root, _ = release
	match = _assert_rejected(_verify(tmp_path / "absent.json", root), "manifest_unreadable")
	assert match.group(3) == "0"


def test_symlinked_manifest_is_unreadable(release, tmp_path: Path) -> None:
	root, manifest = release
	link = tmp_path / "link.json"
	link.symlink_to(manifest)
	_assert_rejected(_verify(link, root), "manifest_unreadable")


def test_missing_paths_file(release, tmp_path: Path) -> None:
	root, manifest = release
	_assert_rejected(_verify(manifest, root, "--paths-file", str(tmp_path / "absent.txt")), "paths_file_unreadable")


@pytest.mark.parametrize("sha", [GOOD_SHA[:39], "g" * 40, ""])
def test_invalid_expected_sha(release, sha: str) -> None:
	root, manifest = release
	_assert_rejected(_verify(manifest, root, sha=sha), "invalid_argument")


def test_unknown_flag_and_bad_root(release, tmp_path: Path) -> None:
	root, manifest = release
	_assert_rejected(_verify(manifest, root, "--bogus"), "invalid_argument")
	_assert_rejected(_verify(manifest, tmp_path / "absent"), "invalid_argument")
	result = subprocess.run([sys.executable, str(VERIFIER)], capture_output=True, text=True, env=ENV)
	_assert_rejected((result.returncode, result.stdout, result.stderr), "invalid_argument")
	result = subprocess.run([sys.executable, str(VERIFIER), "build"], capture_output=True, text=True, env=ENV)
	_assert_rejected((result.returncode, result.stdout, result.stderr), "invalid_argument")


def test_internal_error_is_structured(release, monkeypatch, capsys) -> None:
	root, manifest = release
	module = _load_module()

	def boom(*_args, **_kwargs):
		raise RuntimeError("secret detail")

	monkeypatch.setattr(module, "verify_entry", boom)
	rc = module.main(["verify", "--manifest", str(manifest), "--release-root", str(root), "--expected-sha", GOOD_SHA])
	captured = capsys.readouterr()
	assert rc == 2
	assert captured.err == ""
	assert "secret detail" not in captured.out
	assert "Traceback" not in captured.out
	_assert_rejected((rc, captured.out, captured.err), "internal_error")


def _run_in_process(module, manifest: Path, root: Path, capsys) -> tuple[int, str, str]:
	rc = module.main(["verify", "--manifest", str(manifest), "--release-root", str(root), "--expected-sha", GOOD_SHA])
	captured = capsys.readouterr()
	return rc, captured.out, captured.err


def test_unreadable_symlink_is_read_failed(release, monkeypatch, capsys) -> None:
	root, manifest = release
	module = _load_module()

	def deny(*_args, **_kwargs):
		raise PermissionError("denied")

	monkeypatch.setattr(module.os, "readlink", deny)
	match = _assert_rejected(_run_in_process(module, manifest, root, capsys), "read_failed")
	assert match.group(5) == "workflow-templates/CLAUDE.md"


def test_safe_rel_path_escape_maps_to_path_escape(release, monkeypatch, capsys) -> None:
	root, manifest = release
	module = _load_module()
	module._load_deps()

	def escape(*_args, **_kwargs):
		raise module._rm.ManifestError("path_escape")

	monkeypatch.setattr(module._rm, "_safe_rel", escape)
	match = _assert_rejected(_run_in_process(module, manifest, root, capsys), "path_escape")
	assert match.group(5) is not None


def test_reason_tokens_are_the_documented_set() -> None:
	module = _load_module()
	assert set(module.REASONS) == DOCUMENTED_REASONS


def test_builder_helpers_used_by_the_verifier_exist() -> None:
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	try:
		import release_manifest  # noqa: PLC0415
	finally:
		sys.path.pop(0)
	for name in ("SCHEMA_VERSION", "RELEASE_MANIFEST_REPOSITORY", "TAG_RE", "PATH_SEGMENT_RE", "KNOWN_SYMLINKS", "ManifestError", "_safe_rel"):
		assert hasattr(release_manifest, name), name


def _validator() -> jsonschema.Draft202012Validator:
	schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
	return jsonschema.Draft202012Validator(schema)


def test_schema_parity(release) -> None:
	"""The stdlib check and the checked-in JSON Schema agree on every case."""
	root, manifest = release
	original = manifest.read_bytes()
	validator = _validator()
	assert validator.is_valid(_manifest_doc(manifest))
	assert _parse(*_verify(manifest, root)).group(1) == "ok"
	for name in sorted(SCHEMA_MALFORMED):
		manifest.write_bytes(original)
		doc = _malformed_doc(manifest, name)
		assert not validator.is_valid(doc), name
		_write_doc(manifest, doc)
		match = _parse(*_verify(manifest, root))
		assert match.group(1) == "rejected", name
		assert match.group(2) in ("schema_invalid", "repository_mismatch", "sha_mismatch"), name


def test_real_repo_tree(tmp_path: Path) -> None:
	manifest = tmp_path / "manifest.json"
	_build(REPO_ROOT, manifest)
	paths = tmp_path / "paths.txt"
	paths.write_text("\n".join(entry["path"] for entry in _manifest_doc(manifest)["files"]) + "\n", encoding="utf-8")
	match = _parse(*_verify(manifest, REPO_ROOT, "--paths-file", str(paths)))
	assert match.group(1) == "ok", match.group(0)


# ── verify-pr-tree (issue #7004) ─────────────────────────────────────────

WRAPPER_TEMPLATE = (
	"name: AI Review\n"
	"jobs:\n"
	"  review:\n"
	"    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable\n"
)
RETIRED_OLD = "old hook\n"


def _git_env() -> dict:
	env = {k: v for k, v in ENV.items() if not k.startswith("GIT_")}
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	env["GIT_CONFIG_GLOBAL"] = os.devnull
	return env


def _g(cwd: Path, *args: str) -> str:
	result = subprocess.run(
		["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
		cwd=cwd,
		capture_output=True,
		text=True,
		env=_git_env(),
		check=True,
	)
	return result.stdout.strip()


class PrTree:
	"""A release fixture, a consumer repo with a base commit and a PR commit on top."""

	def __init__(self, tmp_path: Path):
		self.tmp = tmp_path
		self.root = _fixture(tmp_path)
		_write(self.root, "workflow-templates/ai-review.yml", WRAPPER_TEMPLATE)
		retired_sha = hashlib.sha256(RETIRED_OLD.encode("utf-8")).hexdigest()
		_write(self.root, "workflow-templates/retired_files.txt", f"# retired\n.claude/old.sh {retired_sha}\n")
		self.manifest = tmp_path / "manifest.json"
		_build(self.root, self.manifest)
		self.repo = tmp_path / "consumer"
		self.repo.mkdir()
		_g(self.repo, "init", "-q")
		_write(self.repo, ".github/workflows/ai-review.yml", "name: old\n")
		_write(self.repo, ".claude/old.sh", RETIRED_OLD)
		_write(self.repo, "README.md", "# consumer\n")
		_write(self.repo, "CHANGELOG.md", "# Changelog\n")
		_g(self.repo, "add", "-A")
		_g(self.repo, "commit", "-q", "-m", "base")
		self.base = _g(self.repo, "rev-parse", "HEAD")
		self.reproduced = tmp_path / "base-tree"
		_g(self.repo, "worktree", "add", "-q", "--detach", str(self.reproduced), self.base)

	def rendered_wrapper(self) -> str:
		return WRAPPER_TEMPLATE.replace("@stable", f"@{GOOD_SHA} # stable")

	def stage_clean_update(self) -> None:
		_write(self.repo, ".github/workflows/ai-review.yml", self.rendered_wrapper())
		_write(self.repo, ".claude/settings.json", "{}\n")
		_write(self.repo, "CLAUDE.md", "# rules\n")
		(self.repo / ".claude" / "old.sh").unlink()

	def commit(self) -> str:
		_g(self.repo, "add", "-A")
		_g(self.repo, "commit", "-q", "-m", "update")
		return _g(self.repo, "rev-parse", "HEAD")

	def args(self, head: str, base: str | None = None) -> list[str]:
		return [
			"verify-pr-tree",
			"--manifest", str(self.manifest),
			"--release-root", str(self.root),
			"--expected-sha", GOOD_SHA,
			"--git-dir", str(self.repo / ".git"),
			"--base", base or self.base,
			"--head", head,
			"--reproduced-root", str(self.reproduced),
		]

	def run(self, head: str, base: str | None = None) -> tuple[int, str, str]:
		result = subprocess.run([sys.executable, str(VERIFIER), *self.args(head, base)], capture_output=True, text=True, env=ENV)
		return result.returncode, result.stdout, result.stderr


@pytest.fixture
def pr(tmp_path: Path) -> PrTree:
	return PrTree(tmp_path)


def test_pr_tree_clean_update_is_accepted(pr: PrTree) -> None:
	pr.stage_clean_update()
	match = _parse(*pr.run(pr.commit()))
	assert match.group(1) == "ok", match.group(0)
	assert match.group(3) == "4"


def test_pr_tree_extra_file_is_unlisted(pr: PrTree) -> None:
	pr.stage_clean_update()
	_write(pr.repo, "evil.txt", "payload\n")
	match = _assert_rejected(pr.run(pr.commit()), "unlisted_path")
	assert match.group(5) == "evil.txt"


def test_pr_tree_tampered_wrapper_is_rejected(pr: PrTree) -> None:
	pr.stage_clean_update()
	_write(pr.repo, ".github/workflows/ai-review.yml", pr.rendered_wrapper().replace(GOOD_SHA, OTHER_SHA))
	match = _assert_rejected(pr.run(pr.commit()), "content_mismatch")
	assert match.group(5) == ".github/workflows/ai-review.yml"


def test_pr_tree_wrapper_without_template_is_unlisted(pr: PrTree) -> None:
	pr.stage_clean_update()
	_write(pr.repo, ".github/workflows/ai-extra.yml", pr.rendered_wrapper())
	_assert_rejected(pr.run(pr.commit()), "unlisted_path")


def test_pr_tree_tampered_claude_asset_is_rejected(pr: PrTree) -> None:
	pr.stage_clean_update()
	_write(pr.repo, ".claude/settings.json", '{"hooks": "evil"}\n')
	_assert_rejected(pr.run(pr.commit()), "content_mismatch")


def test_pr_tree_symlink_is_mode_mismatch(pr: PrTree) -> None:
	pr.stage_clean_update()
	(pr.repo / ".claude" / "settings.json").unlink()
	(pr.repo / ".claude" / "settings.json").symlink_to("../README.md")
	_assert_rejected(pr.run(pr.commit()), "mode_mismatch")


def test_pr_tree_added_file_with_wrong_mode_is_rejected(pr: PrTree) -> None:
	pr.stage_clean_update()
	_write(pr.repo, ".claude/settings.json", "{}\n", 0o755)
	match = _assert_rejected(pr.run(pr.commit()), "mode_mismatch")
	assert match.group(5) == ".claude/settings.json"


def test_pr_tree_mode_only_change_is_rejected(pr: PrTree) -> None:
	_write(pr.repo, ".claude/settings.json", "{}\n")
	pr.base = pr.commit()
	pr.stage_clean_update()
	_write(pr.repo, ".claude/settings.json", "{}\n", 0o755)
	match = _assert_rejected(pr.run(pr.commit()), "mode_mismatch")
	assert match.group(5) == ".claude/settings.json"


def test_pr_tree_existing_consumer_mode_is_kept(pr: PrTree) -> None:
	# The updater's cp onto an existing file keeps the consumer's mode.
	_write(pr.repo, ".claude/settings.json", "{\"old\": 1}\n", 0o755)
	pr.base = pr.commit()
	pr.stage_clean_update()
	_write(pr.repo, ".claude/settings.json", "{}\n", 0o755)
	assert _parse(*pr.run(pr.commit())).group(1) == "ok"


def test_pr_tree_deleting_unlisted_file_is_rejected(pr: PrTree) -> None:
	pr.stage_clean_update()
	(pr.repo / "README.md").unlink()
	match = _assert_rejected(pr.run(pr.commit()), "unlisted_path")
	assert match.group(5) == "README.md"


def test_pr_tree_deleting_modified_retired_file_is_rejected(pr: PrTree) -> None:
	_write(pr.repo, ".claude/old.sh", "locally modified\n")
	pr.base = pr.commit()
	pr.stage_clean_update()
	_assert_rejected(pr.run(pr.commit()), "retired_hash_mismatch")


def test_pr_tree_reproduced_changelog_must_match(pr: PrTree) -> None:
	_write(pr.reproduced, "CHANGELOG.md", "# Changelog\n\n## 2026-10-10\n- entry\n")
	pr.stage_clean_update()
	_write(pr.repo, "CHANGELOG.md", "# Changelog\n\n## 2026-10-10\n- entry\n")
	assert _parse(*pr.run(pr.commit())).group(1) == "ok"


def test_pr_tree_reproduced_changelog_mismatch_is_rejected(pr: PrTree) -> None:
	_write(pr.reproduced, "CHANGELOG.md", "# Changelog\n\n## 2026-10-10\n- entry\n")
	pr.stage_clean_update()
	_write(pr.repo, "CHANGELOG.md", "# Changelog\n\n## 2026-10-10\n- injected\n")
	match = _assert_rejected(pr.run(pr.commit()), "derived_mismatch")
	assert match.group(5) == "CHANGELOG.md"


def test_pr_tree_missing_reproduced_change_is_rejected(pr: PrTree) -> None:
	_write(pr.reproduced, "changelog.d/.gitkeep", "")
	pr.stage_clean_update()
	match = _assert_rejected(pr.run(pr.commit()), "derived_missing")
	assert match.group(5) == "changelog.d/.gitkeep"


def test_pr_tree_reproduced_deletion_must_match(pr: PrTree) -> None:
	_write(pr.repo, "changelog.d/1-x.md", "- fragment\n")
	pr.base = pr.commit()
	_g(pr.reproduced, "checkout", "-q", "--detach", pr.base)
	(pr.reproduced / "changelog.d" / "1-x.md").unlink()
	pr.stage_clean_update()
	(pr.repo / "changelog.d" / "1-x.md").unlink()
	assert _parse(*pr.run(pr.commit())).group(1) == "ok"


def test_pr_tree_change_cap(pr: PrTree, capsys) -> None:
	pr.stage_clean_update()
	head = pr.commit()
	module = _load_module()
	module.MAX_PR_CHANGES = 1
	rc = module.main(pr.args(head))
	captured = capsys.readouterr()
	_assert_rejected((rc, captured.out, captured.err), "too_many_changes")


def test_pr_tree_invalid_arguments(pr: PrTree, tmp_path: Path) -> None:
	pr.stage_clean_update()
	head = pr.commit()
	_assert_rejected(pr.run(head, base="HEAD~1"), "invalid_argument")
	_assert_rejected(pr.run("refs/heads/main"), "invalid_argument")


def test_pr_tree_unknown_commit_is_git_failed(pr: PrTree) -> None:
	_assert_rejected(pr.run(OTHER_SHA), "git_failed")


def test_pr_tree_tampered_release_is_rejected(pr: PrTree) -> None:
	pr.stage_clean_update()
	head = pr.commit()
	_write(pr.root, "workflow-templates/ai-review.yml", WRAPPER_TEMPLATE + "# tampered\n")
	_assert_rejected(pr.run(head), "size_mismatch")
