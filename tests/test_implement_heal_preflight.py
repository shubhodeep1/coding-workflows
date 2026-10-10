"""Behaviour tests for scripts/implement_heal_preflight.sh (issue #6981).

The preflight must keep refusing heal implementation whenever its scope or
trusted support checkout cannot be verified, and each refusal must now name
its cause (`detail=`), report a `main` fallback checkout, and log the expected
and actual support ref/SHA without echoing unvalidated input.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/implement_heal_preflight.sh"
HEAL_PY = ROOT / "scripts/workflow_failure_heal.py"
SCOPE_MARKER = "<!-- ai:workflow-heal-scope:v1 paths=scripts/fix.py,tests/**,changelog.d/*.md runs=owner/repo:500 -->"
OTHER_SHA = "f" * 40

needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the heal preflight")

# A small fake `gh`: records every call, and fails or answers per the state file.
_FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = json.loads(open(os.environ["FAKE_GH_STATE"]).read())
with open(os.environ["FAKE_GH_CALLS"], "a") as calls:
	calls.write(" ".join(args) + "\n")
if args[:2] == ["issue", "comment"]:
	sys.exit(1 if state.get("comment_fails") else 0)
if args[:2] == ["issue", "edit"]:
	sys.exit(0)
if args[:2] == ["api", "user"]:
	if state.get("user_fails"):
		sys.exit(1)
	print(state.get("login", "pipeline"))
	sys.exit(0)
if args[:2] == ["api", "graphql"]:
	if state.get("graphql_fails"):
		sys.exit(1)
	if "graphql_raw" in state:
		sys.stdout.write(state["graphql_raw"])
	else:
		print(json.dumps(state["live"]))
	sys.exit(0)
sys.exit(1)
'''


def _live_issue(edited: bool = False) -> dict:
	body = "Heal this.\n" + SCOPE_MARKER + "\n"
	return {"data": {"repository": {"issue": {"body": body, "lastEditedAt": "2026-10-09T00:00:00Z" if edited else None, "author": {"login": "pipeline"}, "labels": {"nodes": [{"name": "ai:workflow-heal"}]}}}}}


def _git(args: list[str], cwd: Path) -> str:
	git_env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=git_env).stdout.strip()


def _make_support(tmp_path: Path, branch: str = "main", detach: bool = True, keep_branch: bool = False) -> str:
	"""Build .codex-workflow-src with a stub evidence helper; return HEAD SHA."""
	support = tmp_path / "workspace/.codex-workflow-src"
	(support / "scripts").mkdir(parents=True)
	shutil.copy(HEAL_PY, support / "scripts/workflow_failure_heal.py")
	evidence = support / "scripts/workflow_failure_heal_evidence.sh"
	evidence.write_text("#!/usr/bin/env bash\necho evidence-ran > \"${HEAL_EVIDENCE_FILE}\"\n")
	evidence.chmod(0o755)
	_git(["init", "-q", "-b", branch, str(support)], tmp_path)
	_git(["add", "--all"], support)
	_git(["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "support"], support)
	sha = _git(["rev-parse", "HEAD"], support)
	if detach:
		_git(["checkout", "-q", "--detach", sha], support)
		if not keep_branch:
			_git(["branch", "-q", "-D", branch], support)
	return sha


def _run(tmp_path: Path, state: dict, script_ref: str | None = None, labels: list[str] | None = None, args: list[str] | None = None) -> tuple[subprocess.CompletedProcess, Path, Path]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(_FAKE_GH)
	gh.chmod(0o755)
	(tmp_path / "gh-state.json").write_text(json.dumps(state))
	calls = tmp_path / "gh-calls"
	calls.touch()
	issue = tmp_path / "issue.json"
	issue.write_text(json.dumps({"body": "", "labels": [{"name": n} for n in (labels if labels is not None else ["ai:workflow-heal"])]}))
	env_file = tmp_path / "github-env"
	env = dict(
		os.environ,
		PATH=f"{bin_dir}:{os.environ['PATH']}",
		FAKE_GH_STATE=str(tmp_path / "gh-state.json"),
		FAKE_GH_CALLS=str(calls),
		PYTHONDONTWRITEBYTECODE="1",
		ISSUE_META_FILE=str(issue),
		ISSUE_NUMBER="42",
		GITHUB_REPOSITORY="owner/repo",
		GITHUB_ENV=str(env_file),
		GITHUB_RUN_ID="91",
		RUNNER_TEMP=str(tmp_path),
		RUNTIME_DIR=str(tmp_path),
		GITHUB_WORKSPACE=str(tmp_path / "workspace"),
		WORKFLOW_HEAL_PY=str(HEAL_PY),
	)
	env.pop("SCRIPT_REF", None)
	if script_ref is not None:
		env["SCRIPT_REF"] = script_ref
	try:
		result = subprocess.run(["bash", str(SCRIPT), *(args or [])], env=env, capture_output=True, text=True, check=False)
	finally:
		for dirpath, _dirs, _files in os.walk(tmp_path):
			os.chmod(dirpath, 0o755)
	if not env_file.exists():
		env_file.touch()
	return result, env_file, calls


def _assert_refused(result: subprocess.CompletedProcess, env_file: Path, calls: Path, reason: str, detail: str | None) -> None:
	assert result.returncode == 0, result.stdout + result.stderr
	refusal = [line for line in result.stdout.splitlines() if line.startswith("HEAL_SCOPE_REFUSED")]
	assert len(refusal) == 1, result.stdout
	assert f"reason={reason}" in refusal[0]
	if detail is None:
		assert "detail=" not in refusal[0]
	else:
		assert f"detail={detail}" in refusal[0]
	env_text = env_file.read_text()
	assert "SKIP_IMPLEMENT=true" in env_text
	assert "HEAL_ROUTE=true" not in env_text
	call_text = calls.read_text()
	marker = f"<!-- ai:workflow-heal-scope-unverified:v1 reason={reason}" + (f" detail={detail}" if detail else "") + " -->"
	assert marker in call_text
	assert call_text.index("issue comment") < call_text.index("issue edit")
	assert "ai:needs-human" in call_text


@needs_jq
def test_success_logs_expected_and_actual_sha(tmp_path: Path) -> None:
	sha = _make_support(tmp_path)
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref=sha)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "HEAL_SCOPE_REFUSED" not in result.stdout
	assert f"expected_sha={sha} actual_sha={sha} fallback=false" in result.stdout
	env_text = env_file.read_text()
	assert "HEAL_ROUTE=true" in env_text
	assert "SKIP_IMPLEMENT=true" not in env_text
	assert (tmp_path / "heal-scope-91.txt").read_text().split() == ["scripts/fix.py", "tests/**", "changelog.d/*.md"]
	assert (tmp_path / "heal_evidence.md").read_text().strip() == "evidence-ran"
	assert "issue comment" not in calls.read_text()


@needs_jq
def test_sha_fallback_to_main_is_reported_and_refused(tmp_path: Path) -> None:
	sha = _make_support(tmp_path, branch="main", detach=False)
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref=OTHER_SHA)
	_assert_refused(result, env_file, calls, "unavailable", "support_ref_fallback")
	assert f"expected_sha={OTHER_SHA} actual_sha={sha} fallback=true" in result.stdout
	assert "detail=support_ref_fallback" in result.stderr


@needs_jq
def test_stable_fallback_to_main_is_reported_and_refused(tmp_path: Path) -> None:
	_make_support(tmp_path, branch="main", detach=False)
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref="stable")
	_assert_refused(result, env_file, calls, "unavailable", "support_ref_fallback")
	assert "expected_ref=stable expected_sha=unresolved" in result.stdout
	assert "fallback=true" in result.stdout


@needs_jq
def test_fallback_main_matching_expected_sha_proceeds_with_warning(tmp_path: Path) -> None:
	sha = _make_support(tmp_path, branch="main", detach=False)
	result, env_file, _calls = _run(tmp_path, {"live": _live_issue()}, script_ref=sha)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "HEAL_SCOPE_REFUSED" not in result.stdout
	assert "::warning::Heal support source came from the main fallback checkout" in result.stdout
	assert "HEAL_ROUTE=true" in env_file.read_text()


@needs_jq
def test_plain_sha_mismatch_without_fallback(tmp_path: Path) -> None:
	sha = _make_support(tmp_path)
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref=OTHER_SHA)
	_assert_refused(result, env_file, calls, "unavailable", "support_ref_mismatch")
	assert f"expected_sha={OTHER_SHA} actual_sha={sha} fallback=false" in result.stdout


@needs_jq
def test_unresolved_stable_without_fallback(tmp_path: Path) -> None:
	_make_support(tmp_path, branch="stable")
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref="stable")
	_assert_refused(result, env_file, calls, "unavailable", "support_ref_unresolved")
	assert "expected_sha=unresolved" in result.stdout
	assert "fallback=false" in result.stdout


@needs_jq
def test_invalid_branch_ref_is_refused(tmp_path: Path) -> None:
	_make_support(tmp_path)
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref="feature/x")
	_assert_refused(result, env_file, calls, "unavailable", "support_ref_invalid")
	assert "expected_ref=feature/x" in result.stdout


@needs_jq
def test_shell_metacharacter_ref_is_never_echoed(tmp_path: Path) -> None:
	_make_support(tmp_path)
	raw = "$(id)"
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref=raw)
	_assert_refused(result, env_file, calls, "unavailable", "support_ref_invalid")
	assert "expected_ref=invalid" in result.stdout
	assert raw not in result.stdout
	assert raw not in result.stderr
	assert raw not in calls.read_text()


@needs_jq
def test_missing_support_checkout_is_refused(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"live": _live_issue()}, script_ref=OTHER_SHA)
	_assert_refused(result, env_file, calls, "unavailable", "support_missing")


def test_identity_read_failure_is_refused(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"user_fails": True, "live": _live_issue()})
	_assert_refused(result, env_file, calls, "unavailable", "identity_unavailable")


def test_empty_identity_is_refused(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"login": "", "live": _live_issue()})
	_assert_refused(result, env_file, calls, "unavailable", "identity_empty")


def test_issue_read_failure_is_refused(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"graphql_fails": True, "live": _live_issue()})
	_assert_refused(result, env_file, calls, "unavailable", "issue_read_failed")


@needs_jq
def test_invalid_issue_payload_is_refused(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"graphql_raw": "{not json"})
	_assert_refused(result, env_file, calls, "unavailable", "scope_verify_failed")


@needs_jq
def test_verify_status_is_passed_through_without_detail(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"live": _live_issue(edited=True)})
	_assert_refused(result, env_file, calls, "edited", None)


def test_out_of_heal_scope_refusal_is_unchanged(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {}, args=["refuse", "out_of_heal_scope"])
	_assert_refused(result, env_file, calls, "out_of_heal_scope", None)


def test_comment_write_failure_fails_the_job_before_labelling(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {"user_fails": True, "comment_fails": True})
	assert result.returncode == 1, result.stdout + result.stderr
	assert "outcome=comment_failed" in result.stderr
	assert "SKIP_IMPLEMENT=true" in env_file.read_text()
	assert "issue edit" not in calls.read_text()


def test_ordinary_issue_skips_heal_route_without_gh(tmp_path: Path) -> None:
	result, env_file, calls = _run(tmp_path, {}, labels=[])
	assert result.returncode == 0, result.stdout + result.stderr
	assert env_file.read_text().splitlines() == ["HEAL_ROUTE=false"]
	assert calls.read_text() == ""
