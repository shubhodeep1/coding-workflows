#!/usr/bin/env python3
"""Retarget after a merged base (plan Phase 8d, port P6)."""

from __future__ import annotations

import json
import os
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
import json, os, sys, urllib.parse
fixture = json.load(open(os.environ["FAKE_GH_FIXTURE"]))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
	log.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if fixture.get("fail"):
	sys.exit(1)
if args[:3] == ["api", "-X", "PATCH"]:
	sys.exit(1 if fixture.get("patch_fails") else 0)
endpoint = args[1]
jq = args[args.index("--jq") + 1] if "--jq" in args else None
path, _, query = endpoint.partition("?")
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
		sys.exit(1)
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


def test_chains_are_followed(tmp_path: Path) -> None:
	fixture = {
		"merged": {"stack/b": _merged(8, "stack/a", SHA_B), "stack/a": _merged(7, "main", SHA_A)},
		"tips": {"stack/a": SHA_A, "stack/b": SHA_B},
	}
	result, _ = _run(tmp_path, fixture, "resolve", "o/r", "stack/b", "main")
	assert result.stdout.strip() == "main"
	assert "merged_pr=8" in result.stderr


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


def _steps(path: Path, job: str) -> dict[str, dict]:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def test_implement_maps_the_integration_branch_before_checkout() -> None:
	step = _steps(IMPLEMENT, "implement")["Resolve integration ref"]
	assert step["env"]["RETARGET_MERGED_BASE_ENABLED"] == "${{ vars.RETARGET_MERGED_BASE_ENABLED || 'true' }}"
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
