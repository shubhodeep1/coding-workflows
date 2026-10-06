#!/usr/bin/env python3
"""Retarget after a merged base (plan Phase 8d, port P6)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "retarget_merged_base.sh"
IMPLEMENT = ROOT / ".github" / "workflows" / "implement.yml"
REVIEW = ROOT / ".github" / "workflows" / "review_autofix.yml"

SHA_A = "a" * 40
SHA_B = "b" * 40

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, urllib.parse
fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
	log.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if fixture.get("fail"):
	sys.exit(1)
if args[:3] == ["api", "-X", "PATCH"]:
	sys.exit(1 if fixture.get("patch_fails") else 0)
endpoint = next((arg for arg in args[1:] if arg.startswith("repos/") or arg == "user"), "")
jq = args[args.index("--jq") + 1] if "--jq" in args else None
path, _, query = endpoint.partition("?")
if path.endswith("/pulls/42"):
	pr = fixture["pr"]
	if jq:
		pr = json.loads(subprocess.run(["jq", "-c", jq], input=json.dumps(pr), text=True, capture_output=True, check=True).stdout)
	print(json.dumps(pr))
	sys.exit(0)
if path == "user":
	print("bot" if jq else json.dumps({"login": "bot"}))
	sys.exit(0)
if path.endswith("/comments"):
	comments = fixture.get("comments", [])
	if jq:
		comments = json.loads(subprocess.run(["jq", "-c", jq], input=json.dumps(comments), text=True, capture_output=True, check=True).stdout)
	print(json.dumps(comments))
	sys.exit(0)
if path.endswith("/files"):
	print(json.dumps(fixture.get("files", [{"filename": "docs/guide.md", "status": "modified"}])))
	sys.exit(0)
if path.endswith("/pulls"):
	head = urllib.parse.parse_qs(query)["head"][0].split(":", 1)[1]
	pr = fixture.get("merged", {}).get(head)
	if pr:
		print(json.dumps(pr))
	sys.exit(0)
if "/git/ref/heads/" in path:
	branch = urllib.parse.unquote(path.split("/git/ref/heads/", 1)[1])
	tip = fixture.get("tips", {}).get(branch)
	if tip is None:
		print(fixture.get("ref_error", "gh: Not Found (HTTP 404)"), file=sys.stderr)
		sys.exit(1)
	if fixture.get("tip_warning"):
		print("gh: warning on successful ref read", file=sys.stderr)
	print(tip)
	sys.exit(0)
sys.exit(2)
'''


def _run(tmp_path: Path, fixture: dict, *args: str, env: dict | None = None) -> tuple[subprocess.CompletedProcess, list]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	fixture_file = tmp_path / "fixture.json"
	fixture_file.write_text(json.dumps(fixture), encoding="utf-8")
	log = tmp_path / "calls.log"
	log.write_text("", encoding="utf-8")
	run_env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_GH_FIXTURE=str(fixture_file), FAKE_GH_LOG=str(log))
	run_env.pop("RETARGET_MERGED_BASE_ENABLED", None)
	run_env.update(env or {})
	result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=run_env, check=False)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, calls


def _merged(number: int, base: str, head_sha: str) -> dict:
	return {"number": number, "base": base, "head_sha": head_sha}


def test_default_branch_is_never_looked_up(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, {}, "resolve", "o/r", "main", "main")
	assert result.stdout.strip() == "main" and calls == []


def test_merged_branch_resolves_to_its_base(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_A}}
	result, calls = _run(tmp_path, fixture, "resolve", "o/r", "stack/a", "main")
	assert result.returncode == 0 and result.stdout.strip() == "main"
	assert "RETARGET_MERGED_BASE mode=resolve repo=o/r from=stack/a to=main merged_pr=7 outcome=retargeted" in result.stderr
	assert len(calls) == 2


def test_a_reused_branch_is_not_retargeted(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_B}}
	result, _ = _run(tmp_path, fixture, "resolve", "o/r", "stack/a", "main")
	assert result.stdout.strip() == "stack/a"


def test_successful_ref_read_ignores_gh_stderr(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_A}, "tip_warning": True}
	result, _ = _run(tmp_path, fixture, "resolve", "o/r", "stack/a", "main")
	assert result.returncode == 0 and result.stdout.strip() == "main"


def test_deleted_merged_branch_resolves_to_base_but_other_ref_errors_do_not(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}}
	result, _ = _run(tmp_path, fixture, "resolve", "o/r", "stack/a", "main")
	assert result.returncode == 0 and result.stdout.strip() == "main"
	result, _ = _run(tmp_path, {**fixture, "ref_error": "gh: Service Unavailable (HTTP 503)"}, "resolve", "o/r", "stack/a", "main")
	assert result.returncode == 0 and result.stdout.strip() == "stack/a"
	assert "reason=lookup_failed" in result.stderr


def test_chains_are_followed(tmp_path: Path) -> None:
	fixture = {
		"merged": {"stack/b": _merged(8, "stack/a", SHA_B), "stack/a": _merged(7, "main", SHA_A)},
		"tips": {"stack/a": SHA_A, "stack/b": SHA_B},
	}
	result, _ = _run(tmp_path, fixture, "resolve", "o/r", "stack/b", "main")
	assert result.stdout.strip() == "main"
	assert "merged_pr=8" in result.stderr
	result, calls = _run(tmp_path, fixture, "resolve", "o/r", "stack/b", "main", env={"RETARGET_MERGED_BASE_MAX_HOPS": "1"})
	assert result.stdout.strip() == "stack/a" and len(calls) == 2


def test_disabled_or_failed_lookup_keeps_the_branch(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_A}}
	result, calls = _run(tmp_path, fixture, "resolve", "o/r", "stack/a", "main", env={"RETARGET_MERGED_BASE_ENABLED": "false"})
	assert result.stdout.strip() == "stack/a" and calls == []
	result, _ = _run(tmp_path, {"fail": True}, "resolve", "o/r", "stack/a", "main")
	assert result.returncode == 0 and result.stdout.strip() == "stack/a"
	assert "reason=lookup_failed" in result.stderr


def test_pr_mode_patches_the_base(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_A}}
	result, calls = _run(tmp_path, fixture, "pr", "o/r", "42", "stack/a", "main")
	assert result.stdout.strip() == "main"
	assert ["api", "-X", "PATCH", "repos/o/r/pulls/42", "-f", "base=main"] in calls
	assert "mode=pr repo=o/r pr=42 from=stack/a to=main merged_pr=7 outcome=retargeted" in result.stderr


def test_pr_mode_keeps_the_base_when_the_patch_fails(tmp_path: Path) -> None:
	fixture = {"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_A}, "patch_fails": True}
	result, _ = _run(tmp_path, fixture, "pr", "o/r", "42", "stack/a", "main")
	assert result.returncode == 0 and result.stdout.strip() == "stack/a"
	assert "reason=patch_failed" in result.stderr


def test_pr_mode_on_the_default_branch_makes_no_call(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, {}, "pr", "o/r", "42", "main", "main")
	assert result.stdout.strip() == "main" and calls == []


def test_retargeted_gate_never_skips_on_stale_mergeability_or_terminal_marker(tmp_path: Path) -> None:
	work = tmp_path / "work"
	helper = work / ".codex-retarget-src" / "scripts" / SCRIPT.name
	helper.parent.mkdir(parents=True)
	shutil.copyfile(SCRIPT, helper)
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	fixture_file = tmp_path / "fixture.json"
	gate_fixture = {
		"merged": {"stack/a": _merged(7, "main", SHA_A)}, "tips": {"stack/a": SHA_A},
		"pr": {"state": "open", "merged": False, "head": {"ref": "ai/issue-42", "sha": SHA_B, "repo": {"full_name": "o/r"}},
			"base": {"ref": "stack/a"}, "updated_at": "2026-09-22T08:00:00Z", "labels": [],
			"additions": 1, "deletions": 1, "changed_files": 1, "mergeable": True,
			"mergeable_state": "clean", "title": "Test", "body": ""},
		"comments": [{"id": 1, "user": {"login": "bot"}, "created_at": "2026-09-22T08:01:00Z",
			"body": f"<!-- REVIEW_AUTOFIX_PARTIAL_V1 -->\n```text\npartial_finalize=true\nhead_sha={SHA_B}\nresume_should_continue=false\n```"}],
	}
	fixture_file.write_text(json.dumps(gate_fixture), encoding="utf-8")
	log = tmp_path / "calls.log"
	log.write_text("", encoding="utf-8")
	output_file = tmp_path / "output.txt"
	output_file.write_text("", encoding="utf-8")
	gate = _steps(REVIEW, "gate")["Evaluate review gate"]["run"]
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_GH_FIXTURE=str(fixture_file),
		FAKE_GH_LOG=str(log), GITHUB_OUTPUT=str(output_file), RUNNER_TEMP=str(tmp_path),
		REPOSITORY="o/r", PR_NUMBER="42", PR_IS_DRAFT="false", PR_SKIP_AI="false",
		PR_TITLE="Test", PR_BODY="", EVENT_NAME="workflow_dispatch", EVENT_ACTION="",
		RETARGET_HELPER_VERIFIED="true", RETARGET_DEFAULT_BRANCH="main", RETARGET_MERGED_BASE_ENABLED="true",
		FINGERPRINT_CAP_SUPPORT_VERIFIED="false", AUTOFIX_SKIP_DOC_ONLY="true")
	result = subprocess.run(["bash", "-c", gate], env=env, cwd=work, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	assert result.returncode == 0, result.stderr
	assert outputs["base_retargeted"] == "true" and outputs["retargeted_base_ref"] == "main"
	assert outputs["should_run"] == "true" and outputs["deterministic_skip"] == "false"
	assert "reason=terminal_same_head" not in result.stdout
	assert "reason=base_retargeted" in result.stdout
	assert sum(call[:3] == ["api", "-X", "PATCH"] for call in [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]) == 1

	# A subsequent dispatch on the same head cannot attribute a legacy marker
	# to the new base; PR updated_at is not evidence of a base change.
	gate_fixture["pr"]["base"]["ref"] = "main"
	gate_fixture["pr"]["additions"] = 400
	gate_fixture["pr"]["deletions"] = 50
	gate_fixture["files"] = [{"filename": "scripts/helper.sh", "status": "modified"}]
	fixture_file.write_text(json.dumps(gate_fixture), encoding="utf-8")
	output_file.write_text("", encoding="utf-8")
	result = subprocess.run(["bash", "-c", gate], env=env, cwd=work, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	assert result.returncode == 0, result.stderr
	assert outputs["should_run"] == "true" and outputs["deterministic_skip"] == "false"
	gate_fixture["pr"]["updated_at"] = "2026-09-22T08:02:00Z"
	fixture_file.write_text(json.dumps(gate_fixture), encoding="utf-8")
	output_file.write_text("", encoding="utf-8")
	result = subprocess.run(["bash", "-c", gate], env=env, cwd=work, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	assert result.returncode == 0, result.stderr
	assert outputs["base_retargeted"] == "false" and outputs["should_run"] == "true"
	assert outputs["deterministic_skip"] == "false" and "reason=terminal_same_head" not in result.stdout
	# Base-bound markers remain valid after unrelated PR activity.
	gate_fixture["comments"][0]["body"] = gate_fixture["comments"][0]["body"].replace(
		f"head_sha={SHA_B}\n", f"head_sha={SHA_B}\nbase_ref=main\n")
	gate_fixture["pr"]["updated_at"] = "2026-09-22T08:03:00Z"
	fixture_file.write_text(json.dumps(gate_fixture), encoding="utf-8")
	output_file.write_text("", encoding="utf-8")
	result = subprocess.run(["bash", "-c", gate], env=env, cwd=work, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	assert result.returncode == 0, result.stderr
	assert outputs["skip_reason"] == "terminal_same_head"


def test_resume_state_rejects_unbound_legacy_marker_even_with_new_mtime(tmp_path: Path) -> None:
	marker_root = tmp_path / ".ai" / "review_runtime" / "pr-42"
	marker_root.mkdir(parents=True)
	legacy_dir = marker_root / "round-2"
	legacy_dir.mkdir()
	legacy_path = legacy_dir / "partial_finalize.json"
	legacy_path.write_text(json.dumps({"head_sha": SHA_B, "resume_round": 2,
		"resume_state": "round_budget_exhausted", "resume_should_continue": False}), encoding="utf-8")
	os.utime(legacy_path, (2000000000, 2000000000))
	restore = _steps(REVIEW, "codex-agent")["Restore same-head partial resume state"]["run"]
	script = restore.split("python3 - <<'PY' > \"${resume_env_file}\"\n", 1)[1].split("\nPY\n", 1)[0]
	env = dict(os.environ, CURRENT_HEAD_SHA=SHA_B, CURRENT_BASE_REF="main", CURRENT_BASE_RETARGETED="false",
		CURRENT_BASE_UPDATED_AT="2026-09-22T08:03:00Z", PR_NUMBER="42")
	result = subprocess.run(["python3", "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert "AUTOFIX_RESUME_RESTORED=false" in result.stdout
	bound_dir = marker_root / "round-1"
	bound_dir.mkdir()
	(bound_dir / "partial_finalize.json").write_text(json.dumps({"head_sha": SHA_B, "base_ref": "main",
		"resume_round": 1, "resume_state": "resumable", "resume_should_continue": True}), encoding="utf-8")
	result = subprocess.run(["python3", "-c", script], env=env, cwd=tmp_path, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert "AUTOFIX_RESUME_RESTORED=true" in result.stdout
	assert "AUTOFIX_RESUME_ROUND=1" in result.stdout
	assert "AUTOFIX_RESUME_TERMINAL=false" in result.stdout


def test_verified_retarget_helper_identity_mismatch_fails_gate(tmp_path: Path) -> None:
	verify = _steps(REVIEW, "gate")["Verify retarget helper identity"]["run"].replace(
		"${{ steps.checkout_retarget_helper.outcome }}", "success")
	helper = tmp_path / ".codex-retarget-src" / "scripts" / SCRIPT.name
	helper.parent.mkdir(parents=True)
	helper.write_text("#!/bin/sh\n", encoding="utf-8")
	env_file = tmp_path / "env"
	env = dict(os.environ, WORKFLOW_REPOSITORY="shubhodeep1/coding-workflows",
		WORKFLOW_REF="refs/heads/main", WORKFLOW_SHA="not-a-verified-sha", GITHUB_ENV=str(env_file))
	result = subprocess.run(["bash", "-c", verify], env=env, cwd=tmp_path, capture_output=True, text=True)
	assert result.returncode != 0
	assert "::error::Unverified retarget helper checkout." in result.stdout
	assert not env_file.exists()


def _steps(path: Path, job: str) -> dict[str, dict]:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def test_implement_maps_the_integration_branch_before_checkout() -> None:
	step = _steps(IMPLEMENT, "implement")["Resolve integration ref"]
	assert step["env"]["RETARGET_MERGED_BASE_ENABLED"] == "${{ vars.RETARGET_MERGED_BASE_ENABLED || 'true' }}"
	assert step["env"]["RETARGET_MERGED_BASE_MAX_HOPS"] == "${{ vars.RETARGET_MERGED_BASE_MAX_HOPS || '3' }}"
	assert 'retarget_helper="$(dirname "${resolver_script}")/retarget_merged_base.sh"' in step["run"]
	assert 'if [ -n "${retarget_branch}" ]; then' in step["run"]


def test_review_gate_retargets_with_the_verified_helper() -> None:
	steps = _steps(REVIEW, "gate")
	names = list(steps)
	assert names.index("Checkout retarget helper") < names.index("Verify retarget helper identity") < names.index("Evaluate review gate")
	assert steps["Checkout retarget helper"]["with"]["sparse-checkout"] == "scripts/retarget_merged_base.sh"
	evaluate = steps["Evaluate review gate"]["run"]
	assert "base_ref: (.base.ref // \"\")" in evaluate
	assert 'bash .codex-retarget-src/scripts/retarget_merged_base.sh pr "${REPOSITORY}" "${PR_NUMBER}"' in evaluate
	assert 'RETARGET_HELPER_VERIFIED:-false}" = "true"' in evaluate
	assert steps["Evaluate review gate"]["env"]["RETARGET_MERGED_BASE_MAX_HOPS"] == "${{ vars.RETARGET_MERGED_BASE_MAX_HOPS || '3' }}"
	assert 'GATE_BASE_REF="${pr_base_ref_gate}"' in evaluate
	assert '"${pr_base_retargeted}" != "true"' in evaluate
	assert 'base_ref=${RETARGETED_BASE_REF:-}' in (ROOT / "scripts" / "review_autofix_step_partial_finalize.sh").read_text(encoding="utf-8")
	assert 'CURRENT_BASE_RETARGETED="${RETARGETED_BASE_THIS_RUN:-false}"' in _steps(REVIEW, "codex-agent")["Restore same-head partial resume state"]["run"]
