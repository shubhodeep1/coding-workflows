"""Signed sync manifest verification in update_workflows.yml (issue #7128)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_sync_manifest import BUILDER, make_upstream_repo  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_WORKFLOWS = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
IDENTITY_REGEX = "'^https://github\\.com/shubhodeep1/coding-workflows/\\.github/workflows/(test-and-mark-stable|mark-stable)\\.yml@refs/heads/stable$'"


def workflow_text() -> str:
	return UPDATE_WORKFLOWS.read_text(encoding="utf-8")


def step_names(text: str) -> list:
	return re.findall(r"^      - name: (.+)$", text, re.M)


def step_block(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	nxt = text.find("\n      - name: ", start + 1)
	return text[start:] if nxt == -1 else text[start:nxt]


def extract_verifier(tmp_path: Path) -> Path:
	text = workflow_text()
	body = text.split("<<'SYNC_VERIFY_PY'\n", 1)[1].split("\n          SYNC_VERIFY_PY\n", 1)[0]
	source = "\n".join(line[10:] if line.strip() else "" for line in body.split("\n"))
	path = tmp_path / "sync_manifest_verify.py"
	path.write_text(source + "\n", encoding="utf-8")
	return path


def test_verify_step_runs_before_any_upstream_code():
	names = step_names(workflow_text())
	fetch = names.index("Fetch latest workflow templates from coding-workflows@stable")
	verify = names.index("Verify signed sync manifest")
	render = names.index("Prepare immutable workflow wrappers")
	assert fetch < verify < render
	assert verify == fetch + 1
	block = step_block(workflow_text(), "Verify signed sync manifest")
	assert "id: verify_manifest" in block


def test_attestation_identity_is_exact():
	block = step_block(workflow_text(), "Verify signed sync manifest")
	assert f"--cert-identity-regex {IDENTITY_REGEX}" in block
	assert "--deny-self-hosted-runners" in block
	assert '--repo "${SYNC_UPSTREAM_REPO}"' in block
	assert "SYNC_UPSTREAM_REPO: shubhodeep1/coding-workflows" in block
	code_lines = [line for line in workflow_text().splitlines() if not line.strip().startswith("#")]
	assert not any("--source-digest" in line for line in code_lines)
	assert "reason=gh_flags_missing" in block
	for code in ("ERR_SYNC_MANIFEST_NO_VERSION_TAG", "ERR_SYNC_MANIFEST_MISSING", "ERR_SYNC_MANIFEST_SIGNATURE"):
		assert code in block


def test_only_summary_runs_unconditionally():
	# Scoped to the update-wrappers job: the separate `verify` job (#7004)
	# has its own always() bookkeeping steps.
	full = workflow_text()
	text = full[full.index("\n  update-wrappers:\n"):full.index("\n  verify:\n")]
	always = [name for name in step_names(text) if "always()" in step_block(text, name).split("run:", 1)[0]]
	assert always == ["Summary"]
	alert = step_block(text, "Alert on sync manifest verification failure")
	assert alert in full
	assert "failure() && steps.verify_manifest.outcome == 'failure'" in alert
	assert "source " not in alert.split("run: |", 1)[1]


def test_tg_helpers_come_from_the_verified_checkout():
	text = workflow_text()
	assert "contents/scripts/tg_helpers.sh" not in text
	telegram = step_block(text, "Send Telegram notification")
	assert 'pushd "${UPSTREAM_ROOT}"' in telegram
	assert "source scripts/tg_helpers.sh" in telegram
	assert "> scripts/.gitignore" not in telegram


def _sparse_checkout(repo: Path, sha: str, dest: Path) -> Path:
	subprocess.run(["git", "clone", "-q", "--no-checkout", str(repo), str(dest)], check=True)
	subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", sha], check=True)
	subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", "workflow-templates", "scripts"], check=True)
	return dest


@pytest.fixture()
def verify_env(tmp_path):
	repo, sha = make_upstream_repo(tmp_path)
	manifest = tmp_path / "sync-manifest.json"
	subprocess.run([sys.executable, str(BUILDER), "build", "--repo-root", str(repo), "--commit", sha, "--version", "v2.0.1", "--out", str(manifest)], check=True, capture_output=True)
	root = _sparse_checkout(repo, sha, tmp_path / "checkout")
	assert not (root / "docs").exists()
	verifier = extract_verifier(tmp_path)
	env = {
		**os.environ,
		"MANIFEST_FILE": str(manifest),
		"UPSTREAM_ROOT": str(root),
		"EXPECTED_COMMIT": sha,
		"EXPECTED_VERSION": "v2.0.1",
		"ALLOWED_OUT": str(tmp_path / "allowed.txt"),
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	return {"env": env, "verifier": verifier, "root": root, "tmp": tmp_path, "manifest": manifest}


def _verify(ctx, **overrides):
	env = {**ctx["env"], **overrides}
	return subprocess.run([sys.executable, "-I", str(ctx["verifier"]), "verify"], env=env, capture_output=True, text=True)


def test_verifier_accepts_a_matching_checkout(verify_env):
	result = _verify(verify_env)
	assert result.returncode == 0, result.stdout + result.stderr
	assert int(result.stdout.strip()) > 0
	allowed = (verify_env["tmp"] / "allowed.txt").read_text(encoding="utf-8").splitlines()
	assert ".github/workflows/ai-review.yml" in allowed


def test_verifier_rejects_missing_file(verify_env):
	(verify_env["root"] / "scripts" / "tool.py").unlink()
	result = _verify(verify_env)
	assert result.returncode == 1
	assert result.stdout.strip() == "ERR_SYNC_MANIFEST_FILE_MISMATCH path=scripts/tool.py"


def test_verifier_rejects_extra_file(verify_env):
	(verify_env["root"] / "scripts" / "extra.sh").write_text("echo\n", encoding="utf-8")
	assert _verify(verify_env).stdout.strip() == "ERR_SYNC_MANIFEST_FILE_MISMATCH path=scripts/extra.sh"


def test_verifier_rejects_hash_mismatch(verify_env):
	(verify_env["root"] / "README.md").write_text("tampered\n", encoding="utf-8")
	assert _verify(verify_env).stdout.strip() == "ERR_SYNC_MANIFEST_FILE_MISMATCH path=README.md"


def test_verifier_rejects_symlink_target_change(verify_env):
	link = verify_env["root"] / "workflow-templates" / "CLAUDE.md"
	link.unlink()
	link.symlink_to("../README.md")
	assert _verify(verify_env).stdout.strip() == "ERR_SYNC_MANIFEST_FILE_MISMATCH path=workflow-templates/CLAUDE.md"


def test_verifier_rejects_commit_and_version_mismatch(verify_env):
	assert _verify(verify_env, EXPECTED_COMMIT="0" * 40).stdout.strip() == "ERR_SYNC_MANIFEST_COMMIT_MISMATCH"
	assert _verify(verify_env, EXPECTED_VERSION="v2.0.2").stdout.strip() == "ERR_SYNC_MANIFEST_COMMIT_MISMATCH"


def test_verifier_rejects_wrong_schema_and_unreadable_manifest(verify_env):
	manifest = json.loads(verify_env["manifest"].read_text(encoding="utf-8"))
	manifest["repository"] = "someone/else"
	other = verify_env["tmp"] / "other.json"
	other.write_text(json.dumps(manifest), encoding="utf-8")
	assert _verify(verify_env, MANIFEST_FILE=str(other)).stdout.strip() == "ERR_SYNC_MANIFEST_COMMIT_MISMATCH"
	assert _verify(verify_env, MANIFEST_FILE=str(verify_env["tmp"] / "absent.json")).stdout.strip() == "ERR_SYNC_MANIFEST_MISSING"


def test_path_policy_rejects_off_list_paths(tmp_path):
	verifier = extract_verifier(tmp_path)
	allowed = tmp_path / "allowed.txt"
	allowed.write_text(".github/workflows/ai-review.yml\nchangelog.d/*.md\n", encoding="utf-8")
	staged = tmp_path / "staged"
	staged.write_bytes(b".github/workflows/ai-review.yml\0changelog.d/1-x.md\0")
	ok = subprocess.run([sys.executable, "-I", str(verifier), "check-paths", str(allowed), str(staged)], capture_output=True, text=True)
	assert ok.returncode == 0, ok.stdout
	staged.write_bytes(b".github/workflows/ai-review.yml\0.github/workflows/evil.yml\0")
	bad = subprocess.run([sys.executable, "-I", str(verifier), "check-paths", str(allowed), str(staged)], capture_output=True, text=True)
	assert bad.returncode == 3
	assert bad.stdout.strip() == ".github/workflows/evil.yml"


def test_commit_step_enforces_the_path_policy_before_commit():
	block = step_block(workflow_text(), "Commit and push updates")
	policy = block.index('check-paths "${SYNC_ALLOWED_FILE}" "${SYNC_STAGED_PATHS_FILE}"')
	assert block.index("git diff --cached --name-only --no-renames -z") < policy
	assert policy < block.index("if git diff --cached --quiet; then")
	assert policy < block.index("git commit -m")
	assert "ERR_SYNC_PATH_NOT_ALLOWED reason=allowlist_missing" in block
	assert "ERR_SYNC_PATH_NOT_ALLOWED path=${SYNC_DENIED_PATH}" in block
	assert block.count("git reset -q") >= 2


def test_inline_matcher_agrees_with_builder(tmp_path):
	spec = importlib.util.spec_from_file_location("build_sync_manifest", BUILDER)
	builder = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(builder)
	verifier_spec = importlib.util.spec_from_file_location("sync_manifest_verify", extract_verifier(tmp_path))
	verifier = importlib.util.module_from_spec(verifier_spec)
	verifier_spec.loader.exec_module(verifier)
	cases = [
		("changelog.d/*.md", "changelog.d/1-x.md"),
		("changelog.d/*.md", "changelog.d/x/y.md"),
		("changelog.d/*.md", "changelog.d/.md"),
		("agents.d/*.md", "agents.d/7128-x.md"),
		(".github/workflows/ai-review.yml", ".github/workflows/ai-reviewXyml"),
		("a+b(c)[d].yml", "a+b(c)[d].yml"),
		("*", "dir/file"),
		("*", "file"),
	]
	for pattern, path in cases:
		assert bool(builder.glob_to_regex(pattern).match(path)) == bool(verifier.glob_to_regex(pattern).match(path)), (pattern, path)


def test_summary_reports_verification_and_delivery():
	block = step_block(workflow_text(), "Summary")
	assert 'SYNC_VERIFY_OUTCOME="${{ steps.verify_manifest.outcome }}"' in block
	assert "steps.verify_manifest.outputs.failure_reason_code" in block
	# VERIFY_OUTCOME keeps its release-manifest meaning (#6960).
	assert 'VERIFY_OUTCOME: ${{ steps.verify_release_manifest.outcome }}' in block
	assert 'VERIFY_OUTCOME="${{ steps.verify_manifest.outcome }}"' not in block.replace("SYNC_VERIFY_OUTCOME=", "")
	assert "steps.commit_push.outputs.sync_pr_state" in block
	assert "steps.commit_push.outputs.sync_pr_url" in block
	assert "source " not in block


if __name__ == "__main__":
	if shutil.which("git") is None:
		sys.exit(0)
	sys.exit(pytest.main([__file__, "-q"]))
