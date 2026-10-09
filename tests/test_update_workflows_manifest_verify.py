#!/usr/bin/env python3
"""Contract tests for the updater's release-manifest verification (issue #6960).

The "Verify attested release manifest" step of update_workflows.yml must sit
between the upstream fetch and the first step that writes the consumer
checkout, stay off unless UPDATER_VERIFY_RELEASE_MANIFEST is "true", check the
attestation before running any code from the release tree, and fail the job
on any rejection. The behavioural tests run the step body with bash against a
throwaway git release repository and a fake `gh` under tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update_workflows.yml"
BUILDER = REPO_ROOT / "scripts" / "release_manifest.py"
STEP_NAME = "Verify attested release manifest"
STEP_ID = "verify_release_manifest"
ASSET_NAME = "release-manifest.v1.json"
TAG = "v1.2.3"
SKIP_LINE = "UPDATER_MANIFEST_VERIFY outcome=skip reason=disabled"
MUTATING_STEPS = (
	"Prepare immutable workflow wrappers",
	"Apply canonical audit-gate assets",
	"Sync .claude/ assets from upstream",
	"Remove retired upstream files",
	"Sync top-level CLAUDE.md from upstream",
	"Sync changelog fragment assets from upstream",
	"Assemble changelog fragments",
	"Compare and update workflow wrappers",
	"Commit and push updates",
)
EXECUTED_MODULES = ("verify_release_manifest.py", "release_manifest.py", "workflow_wrapper_refs.py")


def _steps() -> list[dict]:
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["update-wrappers"]["steps"]


def _names() -> list[str]:
	return [step.get("name", "") for step in _steps()]


def _step() -> dict:
	matches = [step for step in _steps() if step.get("name") == STEP_NAME]
	assert len(matches) == 1
	return matches[0]


# ── Structural contract ──────────────────────────────────────────────────


def test_step_sits_between_fetch_and_every_mutating_step() -> None:
	steps = _steps()
	ids = [step.get("id") for step in steps]
	names = _names()
	verify_index = names.index(STEP_NAME)
	assert ids[verify_index] == STEP_ID
	assert ids[verify_index - 1] == "fetch"
	assert names[verify_index + 1] == "Prepare immutable workflow wrappers"
	for name in MUTATING_STEPS:
		assert names.index(name) > verify_index, name


def test_step_is_gated_on_default_off_flag() -> None:
	step = _step()
	assert "if" not in step
	assert "continue-on-error" not in step
	env = step["env"]
	assert env["UPDATER_VERIFY_RELEASE_MANIFEST"] == "${{ vars.UPDATER_VERIFY_RELEASE_MANIFEST || 'false' }}"
	assert env["UPSTREAM_SHA"] == "${{ steps.fetch.outputs.upstream_sha }}"
	assert env["UPSTREAM_DIR"] == "${{ steps.fetch.outputs.upstream_dir }}"
	assert env["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	run = step["run"]
	assert '"${UPDATER_VERIFY_RELEASE_MANIFEST:-false}"' in run
	assert SKIP_LINE in run


def test_no_expression_is_interpolated_into_run() -> None:
	assert "${{" not in _step()["run"]


def test_attestation_and_bootstrap_hash_precede_the_verifier() -> None:
	run = _step()["run"]
	attest = run.index("gh attestation verify")
	bootstrap = run.index("sha256sum --")
	verifier = run.index("scripts/verify_release_manifest.py\" verify")
	assert attest < bootstrap < verifier
	assert "--repo shubhodeep1/coding-workflows" in run
	assert "--cert-identity-regex '^https://github\\.com/shubhodeep1/coding-workflows/\\.github/workflows/(mark-stable|test-and-mark-stable)\\.yml@refs/heads/(main|stable)$'" in run
	assert "--deny-self-hosted-runners" in run
	assert '--expected-sha "${UPSTREAM_SHA}"' in run
	assert "--paths-file" in run
	assert "python3 -I -B" in run


def test_reject_path_fails_the_step() -> None:
	run = _step()["run"]
	reject = run[run.index("reject() {") : run.index("\n}", run.index("reject() {"))]
	assert 'echo "verified=false"' in reject
	assert "exit 1" in reject
	assert "ERR_RELEASE_MANIFEST_VERIFY_FAILED" in reject


def test_only_summary_runs_after_a_failed_verification() -> None:
	steps = _steps()
	names = _names()
	for step in steps[names.index(STEP_NAME) + 1 :]:
		condition = str(step.get("if", ""))
		if step.get("name") == "Summary":
			assert condition == "always()"
			continue
		for marker in ("always()", "failure()", "cancelled()"):
			assert marker not in condition, step.get("name")


def test_summary_reports_verification_failures_first() -> None:
	summary = next(step for step in _steps() if step.get("name") == "Summary")
	assert summary["env"]["VERIFY_OUTCOME"] == "${{ steps.verify_release_manifest.outcome }}"
	run = summary["run"]
	verify_branch = run.index('if [ "${VERIFY_OUTCOME:-}" = "failure" ]; then')
	assert run.index('if [ "${FETCH_OUTCOME}" = "failure" ]; then') < verify_branch
	assert verify_branch < run.index('if [ "${RENDER_OUTCOME}" = "failure" ]; then')
	assert "ERR_RELEASE_MANIFEST_VERIFY_FAILED" in run[verify_branch:]


# ── Behavioural tests ────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		[
			"git",
			"-c", "user.name=test",
			"-c", "user.email=test@example.invalid",
			"-c", "commit.gpgsign=false",
			"-c", "tag.gpgsign=false",
			*args,
		],
		cwd=cwd,
		check=True,
		capture_output=True,
		text=True,
		env=_clean_env(),
	).stdout.strip()


def _clean_env() -> dict[str, str]:
	return {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_", "UPDATER_", "RELEASE_MANIFEST", "UPSTREAM_"))}


def _write(root: Path, rel: str, data: str, mode: int = 0o644) -> None:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(data, encoding="utf-8")
	os.chmod(path, mode)


def _release(tmp_path: Path, tag: bool = True) -> tuple[Path, str]:
	src = tmp_path / "src"
	src.mkdir()
	_write(src, "CLAUDE.md", "# rules\n")
	_write(src, "workflow-templates/ai-update-workflows.yml", "name: update\n")
	_write(src, "workflow-templates/ai-review.yml", "name: review\n")
	_write(src, "workflow-templates/profiles/core.txt", "ai-review.yml\n")
	_write(src, "workflow-templates/retired_files.txt", "# none\n")
	_write(src, "workflow-templates/.claude/settings.json", "{}\n")
	_write(src, "workflow-templates/audit-gate/contract.json", "{}\n")
	(src / "workflow-templates" / "CLAUDE.md").symlink_to("../CLAUDE.md")
	(src / "scripts").mkdir()
	for name in EXECUTED_MODULES:
		shutil.copy2(REPO_ROOT / "scripts" / name, src / "scripts" / name)
	for name in ("apply_audit_gate_assets.py", "assemble_changelog.py"):
		_write(src, f"scripts/{name}", f"# {name}\n", 0o755)
	_git(src, "init", "-q")
	_git(src, "add", "-A")
	_git(src, "commit", "-q", "-m", "release")
	if tag:
		_git(src, "tag", "-a", TAG, "-m", f"Release {TAG}")
	sha = _git(src, "rev-parse", "HEAD")
	upstream = tmp_path / "upstream"
	_git(tmp_path, "clone", "-q", str(src), str(upstream))
	return upstream, sha


def _build_manifest(upstream: Path, sha: str, out: Path) -> None:
	result = subprocess.run(
		["python3", str(BUILDER), "build", "--repo-root", str(upstream), "--release-sha", sha, "--tag", TAG, "--output", str(out)],
		capture_output=True,
		text=True,
		env={**_clean_env(), "PYTHONDONTWRITEBYTECODE": "1"},
	)
	assert result.returncode == 0, result.stderr


def _fake_gh(tmp_path: Path) -> Path:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env bash\n"
		"printf '%s\\n' \"$*\" >> \"${FAKE_GH_LOG}\"\n"
		"if [ \"$1 $2\" = \"attestation verify\" ]; then exit \"${FAKE_ATTEST_RC:-0}\"; fi\n"
		"if [ \"$1\" = \"api\" ]; then\n"
		"  path=\"${@: -1}\"\n"
		"  case \"${path}\" in\n"
		"    */releases/tags/*) printf '%s' \"${FAKE_RELEASE_JSON}\"; exit 0 ;;\n"
		"    */releases/assets/*) cat \"${FAKE_ASSET_FILE}\"; exit 0 ;;\n"
		"  esac\n"
		"fi\n"
		"exit 1\n",
		encoding="utf-8",
	)
	gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
	return bin_dir


def _release_json(asset: Path | None) -> str:
	assets = []
	if asset is not None:
		assets.append({"id": 42, "name": ASSET_NAME, "state": "uploaded", "size": asset.stat().st_size})
	return json.dumps({"draft": False, "prerelease": False, "assets": assets})


def _run(tmp_path: Path, upstream: Path, sha: str, asset: Path | None, flag: str | None = "true", attest_rc: int = 0):
	bin_dir = tmp_path / "bin" if (tmp_path / "bin").exists() else _fake_gh(tmp_path)
	output = tmp_path / "github_output"
	output.write_text("", encoding="utf-8")
	reason_file = tmp_path / "reason.txt"
	log = tmp_path / "gh.log"
	env = {
		**_clean_env(),
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"GITHUB_OUTPUT": str(output),
		"UPDATER_MANIFEST_VERIFY_REASON_FILE": str(reason_file),
		"GH_TOKEN": "dummy",
		"UPSTREAM_DIR": str(upstream / "workflow-templates"),
		"UPSTREAM_SHA": sha,
		"RELEASE_MANIFEST_ASSET_NAME": ASSET_NAME,
		"FAKE_GH_LOG": str(log),
		"FAKE_RELEASE_JSON": _release_json(asset),
		"FAKE_ASSET_FILE": str(asset) if asset is not None else "/nonexistent",
		"FAKE_ATTEST_RC": str(attest_rc),
		"TMPDIR": str(tmp_path),
	}
	if flag is not None:
		env["UPDATER_VERIFY_RELEASE_MANIFEST"] = flag
	result = subprocess.run(["bash", "-c", _step()["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
	outputs = dict(line.partition("=")[::2] for line in output.read_text(encoding="utf-8").splitlines())
	calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
	return result, outputs, calls, reason_file


needs_tools = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("sha256sum") is None, reason="jq/sha256sum unavailable")


@pytest.mark.parametrize("flag", [None, "false", "", "FALSE", "yes"])
def test_flag_off_skips_without_calling_gh(tmp_path: Path, flag: str | None) -> None:
	upstream, sha = _release(tmp_path)
	result, outputs, calls, reason_file = _run(tmp_path, upstream, sha, None, flag=flag)
	assert result.returncode == 0, result.stderr
	assert SKIP_LINE in result.stdout
	assert outputs["verified"] == "skipped"
	assert calls == []
	assert not reason_file.exists()


@pytest.fixture
def signed_release(tmp_path: Path) -> tuple[Path, str, Path]:
	upstream, sha = _release(tmp_path)
	asset = tmp_path / "asset" / ASSET_NAME
	asset.parent.mkdir()
	_build_manifest(upstream, sha, asset)
	return upstream, sha, asset


def _assert_rejected(result, outputs, reason_file: Path, reason: str) -> None:
	assert result.returncode != 0
	assert f"::error::UPDATER_MANIFEST_VERIFY outcome=rejected reason={reason}" in result.stdout
	assert outputs["verified"] == "false"
	assert outputs["reason"] == reason
	assert reason_file.read_text(encoding="utf-8").splitlines()[0] == "ERR_RELEASE_MANIFEST_VERIFY_FAILED"


@needs_tools
def test_clean_release_verifies(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	result, outputs, calls, _ = _run(tmp_path, upstream, sha, asset)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "UPDATER_MANIFEST_VERIFY outcome=ok reason=ok count=" in result.stdout
	assert outputs["verified"] == "true"
	assert outputs["tag"] == TAG
	assert any(call.startswith("attestation verify") and "--repo shubhodeep1/coding-workflows" in call for call in calls)
	assert sum(call.startswith("api ") for call in calls) == 2


@needs_tools
def test_missing_asset_rejects_before_verifier(tmp_path: Path, signed_release) -> None:
	upstream, sha, _ = signed_release
	result, outputs, calls, reason_file = _run(tmp_path, upstream, sha, None)
	_assert_rejected(result, outputs, reason_file, "asset_missing")
	assert "count=" not in result.stdout
	assert not any(call.startswith("attestation") for call in calls)


@needs_tools
def test_failed_attestation_rejects_before_verifier(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	result, outputs, _, reason_file = _run(tmp_path, upstream, sha, asset, attest_rc=1)
	_assert_rejected(result, outputs, reason_file, "attestation_failed")
	assert "count=" not in result.stdout


@needs_tools
def test_missing_release_tag_rejects(tmp_path: Path) -> None:
	upstream, sha = _release(tmp_path, tag=False)
	result, outputs, calls, reason_file = _run(tmp_path, upstream, sha, None)
	_assert_rejected(result, outputs, reason_file, "release_tag_missing")
	assert calls == []


@needs_tools
def test_tampered_template_rejects(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	_write(upstream, "workflow-templates/ai-review.yml", "name: reviev\n")
	result, outputs, _, reason_file = _run(tmp_path, upstream, sha, asset)
	assert "::error::UPDATER_MANIFEST_VERIFY outcome=rejected reason=hash_mismatch" in result.stdout
	_assert_rejected(result, outputs, reason_file, "hash_mismatch")


@needs_tools
def test_injected_claude_file_rejects(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	_write(upstream, "workflow-templates/.claude/injected.md", "evil\n")
	result, outputs, _, reason_file = _run(tmp_path, upstream, sha, asset)
	_assert_rejected(result, outputs, reason_file, "unlisted_path")


@needs_tools
def test_tampered_verifier_rejects_before_running_it(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	with (upstream / "scripts" / "verify_release_manifest.py").open("a", encoding="utf-8") as handle:
		handle.write("# tampered\n")
	result, outputs, _, reason_file = _run(tmp_path, upstream, sha, asset)
	_assert_rejected(result, outputs, reason_file, "verifier_hash_mismatch")
	assert "count=" not in result.stdout


@needs_tools
def test_manifest_without_verifier_entries_rejects(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	doc = json.loads(asset.read_text(encoding="utf-8"))
	doc["files"] = [entry for entry in doc["files"] if entry["path"] != "scripts/verify_release_manifest.py"]
	asset.write_text(json.dumps(doc), encoding="utf-8")
	result, outputs, _, reason_file = _run(tmp_path, upstream, sha, asset)
	_assert_rejected(result, outputs, reason_file, "verifier_unattested")


@needs_tools
def test_manifest_for_other_commit_rejects(tmp_path: Path, signed_release) -> None:
	upstream, sha, asset = signed_release
	doc = json.loads(asset.read_text(encoding="utf-8"))
	doc["release_sha"] = "0" * 40
	asset.write_text(json.dumps(doc), encoding="utf-8")
	result, outputs, _, reason_file = _run(tmp_path, upstream, sha, asset)
	_assert_rejected(result, outputs, reason_file, "manifest_header_mismatch")
