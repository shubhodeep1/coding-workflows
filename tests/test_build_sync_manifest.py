"""Tests for scripts/build_sync_manifest.py (issue #7128)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "scripts" / "build_sync_manifest.py"
UPDATE_WORKFLOWS = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
ASSEMBLE_CHANGELOG = REPO_ROOT / "scripts" / "assemble_changelog.py"

GIT_ENV = {
	"GIT_AUTHOR_NAME": "test",
	"GIT_AUTHOR_EMAIL": "test@example.invalid",
	"GIT_COMMITTER_NAME": "test",
	"GIT_COMMITTER_EMAIL": "test@example.invalid",
	"GIT_CONFIG_NOSYSTEM": "1",
	"HOME": "/nonexistent",
}


def _load_builder():
	spec = importlib.util.spec_from_file_location("build_sync_manifest", BUILDER)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _git(repo: Path, *args: str) -> str:
	env = {**os.environ, **GIT_ENV}
	result = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], env=env, check=True, capture_output=True, text=True)
	return result.stdout.strip()


def make_upstream_repo(root: Path) -> tuple[Path, str]:
	"""A scratch coding-workflows tree with every kind of entry the builder reads."""
	repo = root / "upstream"
	files = {
		"CLAUDE.md": "rules\n",
		"README.md": "readme\n",
		"workflow-templates/ai-review.yml": "name: AI Review\n",
		"workflow-templates/profiles/full.txt": "ai-review.yml\n",
		"workflow-templates/.claude/commands/do.md": "do\n",
		"workflow-templates/audit-gate/contract.json": json.dumps({"managed_files": [{"source": "assets/x.js", "target": "scripts/security/x.js"}]}),
		"workflow-templates/retired_files.txt": "# comment\n.claude/commands/old.md abc123\n",
		"scripts/tool.py": "print('x')\n",
		"docs/outside.md": "not in cone\n",
	}
	for rel, text in files.items():
		path = repo / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")
	(repo / "workflow-templates" / "CLAUDE.md").symlink_to("../CLAUDE.md")
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	return repo, _git(repo, "rev-parse", "HEAD")


def run_builder(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run([sys.executable, str(BUILDER), *args], capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def test_build_is_deterministic_and_covers_the_cone(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	out_a, out_b = tmp_path / "a.json", tmp_path / "b.json"
	for out in (out_a, out_b):
		result = run_builder("build", "--repo-root", str(repo), "--commit", sha, "--version", "v1.2.3", "--out", str(out))
		assert result.returncode == 0, result.stderr
		assert "SYNC_MANIFEST outcome=built" in result.stderr
	assert out_a.read_bytes() == out_b.read_bytes()
	manifest = json.loads(out_a.read_text(encoding="utf-8"))
	assert manifest["schema"] == "coding-workflows.sync-manifest.v1"
	assert manifest["repository"] == "shubhodeep1/coding-workflows"
	assert manifest["commit"] == sha
	assert manifest["version"] == "v1.2.3"
	files = manifest["files"]
	assert files["workflow-templates/CLAUDE.md"] == "symlink:../CLAUDE.md"
	assert "CLAUDE.md" in files and "README.md" in files
	assert "scripts/tool.py" in files
	assert "docs/outside.md" not in files
	assert re.fullmatch(r"[0-9a-f]{64}", files["README.md"])
	allowed = manifest["allowed_consumer_paths"]
	for expected in (
		".github/workflows/ai-review.yml",
		".claude/commands/do.md",
		"scripts/security/x.js",
		".claude/commands/old.md",
		"CLAUDE.md",
		".gitattributes",
		"changelog.d/*.md",
		"agents.d/*.md",
		"package.json",
	):
		assert expected in allowed
	assert ".github/workflows/full.txt" not in allowed
	assert allowed == sorted(allowed)


def test_managed_paths_are_the_allowlist_minus_consumer_edited_entries(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	result = run_builder("managed-paths", "--repo-root", str(repo), "--commit", sha)
	assert result.returncode == 0, result.stderr
	managed = result.stdout.splitlines()
	assert managed == sorted(managed)
	assert set(managed) == {
		".github/workflows/ai-review.yml",
		".claude/commands/do.md",
		"scripts/security/x.js",
		".claude/commands/old.md",
		"CLAUDE.md",
	}
	allowed = run_builder("allowed-paths", "--repo-root", str(repo), "--commit", sha).stdout.splitlines()
	assert set(managed) < set(allowed)
	# Files the updater edits in place stay consumer-owned.
	for consumer_owned in ("CHANGELOG.md", "changelog.d/*.md", "agents.md", "AGENTS.md", "agents.d/*.md", "package.json", ".gitattributes", "scripts/.gitignore"):
		assert consumer_owned in allowed
		assert consumer_owned not in managed


def test_non_boolean_seed_only_is_rejected(tmp_path):
	repo, _sha = make_upstream_repo(tmp_path)
	contract = {"managed_files": [{"source": "assets/allow.json", "target": "security/allow.json", "seed_only": "true"}]}
	assets = repo / "workflow-templates" / "audit-gate" / "assets"
	assets.mkdir(parents=True)
	(assets / "allow.json").write_text("[]\n", encoding="utf-8")
	(repo / "workflow-templates" / "audit-gate" / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "bad seed-only")
	sha = _git(repo, "rev-parse", "HEAD")
	for mode in ("allowed-paths", "managed-paths", "managed-digests"):
		assert run_builder(mode, "--repo-root", str(repo), "--commit", sha).returncode != 0


def test_canonical_contract_marks_the_allowlist_seed_only():
	"""Issue shubhodeep1/multi-user-ai-agent#322: overwriting the allowlist on
	every sync emptied consumer allowlists and failed their npm Audit Gate."""
	contract = json.loads((REPO_ROOT / "workflow-templates" / "audit-gate" / "contract.json").read_text(encoding="utf-8"))
	by_target = {item["target"]: item for item in contract["managed_files"]}
	assert by_target["security/dependency-audit-allowlist.json"].get("seed_only") is True
	assert by_target["scripts/security/check-npm-audit.js"].get("seed_only", False) is False


def test_managed_paths_defaults_to_head_and_rejects_a_missing_commit(tmp_path):
	repo, _sha = make_upstream_repo(tmp_path)
	assert run_builder("managed-paths", "--repo-root", str(repo)).returncode == 0
	assert run_builder("managed-paths", "--repo-root", str(repo), "--commit", "0" * 40).returncode == 1
	assert run_builder("managed-paths").returncode == 2


def test_managed_digests_hold_the_bytes_a_sync_writes(tmp_path):
	import hashlib

	repo, sha = make_upstream_repo(tmp_path)
	wrapper = "name: AI Review\njobs:\n  review:\n    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@stable\n"
	(repo / "workflow-templates" / "ai-review.yml").write_text(wrapper, encoding="utf-8")
	(repo / "workflow-templates" / "audit-gate" / "assets").mkdir(parents=True)
	(repo / "workflow-templates" / "audit-gate" / "assets" / "x.js").write_text("audit()\n", encoding="utf-8")
	retired_hash = "a" * 64
	(repo / "workflow-templates" / "retired_files.txt").write_text(f"# comment\n.claude/commands/old.md {retired_hash} not-a-hash\n", encoding="utf-8")
	(repo / "workflow-templates" / ".claude" / "commands" / "link.md").symlink_to("do.md")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "digests")
	sha = _git(repo, "rev-parse", "HEAD")
	result = run_builder("managed-digests", "--repo-root", str(repo), "--commit", sha)
	assert result.returncode == 0, result.stderr
	digests = json.loads(result.stdout)

	def sha256(text: str) -> list:
		return [hashlib.sha256(text.encode("utf-8")).hexdigest()]

	rendered = wrapper.replace("@stable", f"@{sha} # stable")
	normalized = wrapper.replace("@stable", f"@{'0' * 40} # stable")
	# The release's own bytes plus the pin-normalized form an older release's
	# wrapper reduces to after the audit replaces its pinned SHA.
	assert digests[".github/workflows/ai-review.yml"] == sorted(sha256(rendered) + sha256(normalized))
	assert digests[".claude/commands/do.md"] == sha256("do\n")
	assert digests["CLAUDE.md"] == sha256("rules\n")
	assert digests["scripts/security/x.js"] == sha256("audit()\n")
	assert digests[".claude/commands/old.md"] == [retired_hash]
	# Symlinks have no digest (the sync never writes one as a regular file).
	assert ".claude/commands/link.md" not in digests
	# Consumer-edited paths are never managed.
	assert not {"CHANGELOG.md", "agents.md", "package.json"} & set(digests)


def test_seed_only_audit_gate_target_is_allowed_but_consumer_owned(tmp_path):
	"""The sync creates a seed_only target (the advisory allowlist) once and then leaves it to the consumer."""
	repo, _sha = make_upstream_repo(tmp_path)
	contract = {"managed_files": [
		{"source": "assets/x.js", "target": "scripts/security/x.js"},
		{"source": "assets/allow.json", "target": "security/allow.json", "seed_only": True},
	]}
	(repo / "workflow-templates" / "audit-gate" / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
	(repo / "workflow-templates" / "audit-gate" / "assets").mkdir(parents=True)
	(repo / "workflow-templates" / "audit-gate" / "assets" / "x.js").write_text("audit()\n", encoding="utf-8")
	(repo / "workflow-templates" / "audit-gate" / "assets" / "allow.json").write_text("[]\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "seed only")
	sha = _git(repo, "rev-parse", "HEAD")

	allowed = run_builder("allowed-paths", "--repo-root", str(repo), "--commit", sha).stdout.splitlines()
	managed = run_builder("managed-paths", "--repo-root", str(repo), "--commit", sha).stdout.splitlines()
	result = run_builder("managed-digests", "--repo-root", str(repo), "--commit", sha)
	assert result.returncode == 0, result.stderr
	digests = json.loads(result.stdout)
	assert "security/allow.json" in allowed
	assert "security/allow.json" not in managed
	assert "security/allow.json" not in digests
	assert "scripts/security/x.js" in managed
	assert "scripts/security/x.js" in digests


def test_managed_digests_skip_a_wrapper_the_renderer_rejects(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	result = run_builder("managed-digests", "--repo-root", str(repo), "--commit", sha)
	assert result.returncode == 0, result.stderr
	digests = json.loads(result.stdout)
	# "name: AI Review" has no @stable reference, so the updater could not render it either.
	assert ".github/workflows/ai-review.yml" not in digests
	assert ".claude/commands/do.md" in digests


def test_build_rejects_bad_arguments(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	out = str(tmp_path / "m.json")
	assert run_builder("build", "--repo-root", str(repo), "--commit", sha, "--version", "1.2.3", "--out", out).returncode == 2
	assert run_builder("build", "--repo-root", str(repo), "--commit", sha.upper(), "--version", "v1.2.3", "--out", out).returncode == 2
	assert run_builder("build", "--repo-root", str(repo)).returncode == 2
	assert run_builder().returncode == 2


def test_build_rejects_submodule_entries(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	_git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},scripts/sub")
	_git(repo, "commit", "-q", "-m", "gitlink")
	new_sha = _git(repo, "rev-parse", "HEAD")
	result = run_builder("build", "--repo-root", str(repo), "--commit", new_sha, "--version", "v1.2.3", "--out", str(tmp_path / "m.json"))
	assert result.returncode == 1
	assert "unsupported tree entry" in result.stderr


def test_glob_matcher_does_not_cross_directories():
	module = _load_builder()
	regex = module.glob_to_regex("changelog.d/*.md")
	assert regex.match("changelog.d/123-x.md")
	assert not regex.match("changelog.d/x/y.md")
	assert not regex.match("changelog.dXy.md")
	assert module.glob_to_regex("a+b(c).yml").match("a+b(c).yml")


def test_check_paths_exit_codes(tmp_path):
	allowed = tmp_path / "allowed.txt"
	allowed.write_text("CHANGELOG.md\nchangelog.d/*.md\n", encoding="utf-8")
	ok = tmp_path / "ok.txt"
	ok.write_text("CHANGELOG.md\nchangelog.d/1-x.md\n", encoding="utf-8")
	bad = tmp_path / "bad.txt"
	bad.write_text("CHANGELOG.md\nchangelog.d/x/y.md\n", encoding="utf-8")
	assert run_builder("check-paths", "--allowed-file", str(allowed), "--paths-file", str(ok)).returncode == 0
	result = run_builder("check-paths", "--allowed-file", str(allowed), "--paths-file", str(bad))
	assert result.returncode == 3
	assert result.stdout.strip() == "changelog.d/x/y.md"
	assert run_builder("check-paths", "--allowed-file", str(tmp_path / "missing"), "--paths-file", str(ok)).returncode == 1


def test_allowed_paths_subcommand_matches_build(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	result = run_builder("allowed-paths", "--repo-root", str(repo))
	assert result.returncode == 0, result.stderr
	module = _load_builder()
	assert result.stdout.splitlines() == module.derive_allowed_paths(repo, sha)


def _commit_step_body() -> str:
	text = UPDATE_WORKFLOWS.read_text(encoding="utf-8")
	start = text.index("      - name: Commit and push updates\n")
	end = text.index("      - name: Send Telegram notification\n", start)
	return text[start:end]


def test_every_git_add_source_maps_to_an_allowlist_class():
	"""Each staging source of the commit step has a matching allowlist class."""
	body = _commit_step_body()
	sources = set(re.findall(r"done < (/tmp/[a-z_]+\.txt)", body))
	sources |= set(re.findall(r'git add (?:-A )?(?:-- )?("?[^\n"]+"?(?: [^\n]+)?)$', body, re.M))
	expected_classes = {
		"/tmp/audit_gate_changed_files.txt": "audit-gate managed_files targets and package.json",
		"/tmp/claude_changed_files.txt": ".claude/<rel> for workflow-templates/.claude/",
		"/tmp/claude_md_changed_file.txt": "CLAUDE.md",
		"/tmp/retired_removed_files.txt": "retired_files.txt first tokens",
		"/tmp/changelog_asset_files.txt": "changelog.d/.gitkeep and .gitattributes",
	}
	for source in expected_classes:
		assert source in sources, source
	file_sources = {item for item in sources if item.startswith("/tmp/")}
	assert file_sources == set(expected_classes), file_sources - set(expected_classes)
	assert "git add .github/workflows/" in body
	assert "git add -A -- CHANGELOG.md changelog.d" in body
	assert 'git add -A -- "${AGENTS_FILE}" agents.d' in body
	allowed = _load_builder().STATIC_ALLOWED_PATHS
	for entry in ("CLAUDE.md", "CHANGELOG.md", "changelog.d/*.md", "changelog.d/.gitkeep", ".gitattributes", "agents.md", "AGENTS.md", "agents.d/*.md", "package.json"):
		assert entry in allowed


def test_changelog_ensure_assets_paths_are_allowed(tmp_path):
	consumer = tmp_path / "consumer"
	consumer.mkdir()
	changed = tmp_path / "changed.txt"
	result = subprocess.run(
		[sys.executable, str(ASSEMBLE_CHANGELOG), "ensure-assets", "--repo-root", str(consumer), "--changed-paths-file", str(changed)],
		capture_output=True,
		text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert result.returncode == 0, result.stderr
	module = _load_builder()
	compiled = [module.glob_to_regex(entry) for entry in module.STATIC_ALLOWED_PATHS]
	paths = [line for line in changed.read_text(encoding="utf-8").splitlines() if line]
	assert paths, "ensure-assets reported no changed paths in an empty consumer"
	for path in paths:
		assert module.path_allowed(path, compiled), path



# ---------------------------------------------------------------------------
# Issue #8265: classify-paths and parity with the security audit's inline copy
# ---------------------------------------------------------------------------

SECURITY_AUDIT = REPO_ROOT / "scripts" / "security_audit.sh"
_PIN_A = "1" * 40
_PIN_B = "2" * 40
_WRAPPER_TEMPLATE = (
	"jobs:\n  review:\n    uses: shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@{pin} # stable\n"
	"      - uses: actions/checkout@v5\n        with:\n          ref: {pin} # stable\n"
)


def _audit_namespace(repo_root: Path, digests: dict) -> dict:
	"""Exec the audit's inline WRAPPER_PIN_PATTERNS / normalize_wrapper_pins /
	upstream_managed_status from scripts/security_audit.sh."""
	text = SECURITY_AUDIT.read_text(encoding="utf-8")
	start = text.index("WRAPPER_PIN_PATTERNS = (")
	end = text.index("\n\n\nraw_findings = ", start)
	namespace = {"re": re, "hashlib": hashlib, "Path": Path, "repo_root": repo_root.resolve(), "upstream_managed_digests": digests}
	exec(compile(text[start:end], "security_audit_inline", "exec"), namespace)  # noqa: S102 - trusted repository source
	return namespace


def _classify_fixture(tmp_path: Path) -> tuple[Path, dict, list]:
	module = _load_builder()
	repo = tmp_path / "consumer"
	(repo / ".github" / "workflows").mkdir(parents=True)
	(repo / ".claude" / "commands").mkdir(parents=True)
	files = {
		".github/workflows/same-pin.yml": _WRAPPER_TEMPLATE.format(pin=_PIN_A),
		".github/workflows/other-pin.yml": _WRAPPER_TEMPLATE.format(pin=_PIN_B),
		".github/workflows/edited.yml": _WRAPPER_TEMPLATE.format(pin=_PIN_B) + "# edit\n",
		".claude/commands/do.md": "do\n",
		".claude/commands/changed.md": "changed\n",
		"app.js": "x\n",
	}
	for rel, content in files.items():
		(repo / rel).write_text(content, encoding="utf-8")
	(repo / ".github" / "workflows" / "linked.yml").symlink_to("same-pin.yml")
	subprocess.run(["git", "init", "-q", str(repo)], check=True)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "consumer")
	rendered = _WRAPPER_TEMPLATE.format(pin=_PIN_A).encode("utf-8")
	normalized = module.normalize_wrapper_pins(rendered)
	wrapper_digests = sorted({hashlib.sha256(rendered).hexdigest(), hashlib.sha256(normalized).hexdigest()})
	digests = {
		".github/workflows/same-pin.yml": wrapper_digests,
		".github/workflows/other-pin.yml": wrapper_digests,
		".github/workflows/edited.yml": wrapper_digests,
		".github/workflows/linked.yml": wrapper_digests,
		".github/workflows/missing.yml": wrapper_digests,
		".claude/commands/do.md": [hashlib.sha256(b"do\n").hexdigest()],
		".claude/commands/changed.md": [hashlib.sha256(b"do\n").hexdigest()],
	}
	paths = [*files, ".github/workflows/linked.yml", ".github/workflows/missing.yml", "unknown.txt"]
	return repo, digests, paths


def test_classify_paths_reads_git_objects(tmp_path):
	module = _load_builder()
	repo, digests, paths = _classify_fixture(tmp_path)
	# A working-tree edit after the commit must not change the result.
	(repo / ".claude" / "commands" / "do.md").write_text("edited in the working tree\n", encoding="utf-8")
	result = module.classify_paths(repo, _git(repo, "rev-parse", "HEAD"), digests, paths)
	assert result == {
		".github/workflows/same-pin.yml": "unmodified",
		".github/workflows/other-pin.yml": "unmodified",
		".github/workflows/edited.yml": "modified",
		".github/workflows/linked.yml": "modified",
		".github/workflows/missing.yml": "modified",
		".claude/commands/do.md": "unmodified",
		".claude/commands/changed.md": "modified",
		"app.js": "unmanaged",
		"unknown.txt": "unmanaged",
	}


def test_classify_paths_cli(tmp_path):
	repo, digests, paths = _classify_fixture(tmp_path)
	digests_file = tmp_path / "digests.json"
	digests_file.write_text(json.dumps(digests), encoding="utf-8")
	paths_file = tmp_path / "paths.txt"
	paths_file.write_text("\n".join(paths) + "\n", encoding="utf-8")
	result = run_builder("classify-paths", "--digests-json", str(digests_file), "--repo-root", str(repo), "--commit", "HEAD", "--paths-file", str(paths_file))
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout)["app.js"] == "unmanaged"
	digests_file.write_text("{}", encoding="utf-8")
	assert run_builder("classify-paths", "--digests-json", str(digests_file), "--repo-root", str(repo), "--commit", "HEAD", "--paths-file", str(paths_file)).returncode == 1
	assert run_builder("classify-paths", "--digests-json", str(tmp_path / "missing.json"), "--repo-root", str(repo), "--commit", "HEAD", "--paths-file", str(paths_file)).returncode == 1


def test_classifier_matches_the_security_audit_inline_copy(tmp_path):
	"""The audit keeps its own inline copy (D1 of the #8265 plan); both must agree."""
	module = _load_builder()
	repo, digests, paths = _classify_fixture(tmp_path)
	audit = _audit_namespace(repo, digests)
	assert [pattern.pattern for pattern in audit["WRAPPER_PIN_PATTERNS"]] == [pattern.pattern for pattern in module.WRAPPER_PIN_PATTERNS]
	for pin in (_PIN_A, _PIN_B):
		data = _WRAPPER_TEMPLATE.format(pin=pin).encode("utf-8")
		assert audit["normalize_wrapper_pins"](data) == module.normalize_wrapper_pins(data)
	shared = module.classify_paths(repo, _git(repo, "rev-parse", "HEAD"), digests, paths)
	for path in paths:
		assert audit["upstream_managed_status"](path) == shared[path], path


if __name__ == "__main__":
	sys.exit(pytest.main([__file__, "-q"]))
