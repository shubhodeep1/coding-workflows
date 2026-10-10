#!/usr/bin/env python3
"""Check-triage target-branch guard in plan.yml (issue #7064).

The guard is default off. When CHECK_TRIAGE_BRANCH_GUARD_ENABLED is on, an
``ai:check-triage`` issue whose body declares no verified target branch is
blocked (a synthetic BLOCKED line for the existing parse/blocked-handler path)
instead of being planned on the default branch. It reads only the resolver's
status token and the issue labels, never issue prose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WF = REPO_ROOT / ".github" / "workflows" / "plan.yml"
RESOLVER = REPO_ROOT / "scripts" / "resolve_integration_ref.sh"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver"
GUARD_STEP = "Check-triage target-branch guard"
# Same detection regex as the "Parse planning output" step (perl, /i).
NEEDS_JQ = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required to read issue labels")
BLOCKED_RE = re.compile(r"^\s*BLOCKED:\s*(.*\S)\s*$", re.IGNORECASE)


def _workflow_text() -> str:
	return PLAN_WF.read_text(encoding="utf-8")


def _step_text(name: str) -> str:
	text = _workflow_text()
	marker = f"      - name: {name}\n"
	start = text.find(marker)
	assert start != -1, f"missing workflow step: {name}"
	end = text.find("\n      - name:", start + len(marker))
	# Drop trailing comment lines that belong to the next step.
	block = text[start:] if end == -1 else text[start:end]
	return block


def _step_names() -> list[str]:
	return re.findall(r"^      - name: (.+)$", _workflow_text(), re.MULTILINE)


def _run_body(name: str) -> str:
	step = _step_text(name)
	_, body = step.split("        run: |\n", 1)
	lines = []
	for line in body.split("\n"):
		if line.strip() and not line.startswith("          ") and not line.startswith("      #"):
			break
		if line.startswith("      #"):
			break
		lines.append(line)
	return textwrap.dedent("\n".join(lines))


def _run_guard(
	tmp_path: Path,
	*,
	enabled: str | None,
	labels: list[str] | None,
	status: str | None,
	body: str = "",
) -> dict:
	meta = tmp_path / "issue_meta.json"
	if labels is None:
		meta.write_text("not json", encoding="utf-8")
	else:
		meta.write_text(
			json.dumps({"number": 7064, "body": body, "labels": [{"name": n} for n in labels]}),
			encoding="utf-8",
		)
	codex_output = tmp_path / "codex_output.txt"
	github_env = tmp_path / "github_env"
	github_output = tmp_path / "github_output"
	github_env.write_text("", encoding="utf-8")
	github_output.write_text("", encoding="utf-8")
	script = tmp_path / "guard.sh"
	script.write_text(_run_body(GUARD_STEP), encoding="utf-8")
	env = {
		"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
		"ISSUE_META_FILE": str(meta),
		"CODEX_OUTPUT_FILE": str(codex_output),
		"GITHUB_ENV": str(github_env),
		"GITHUB_OUTPUT": str(github_output),
		"ISSUE_NUMBER": "7064",
	}
	if enabled is not None:
		env["CHECK_TRIAGE_BRANCH_GUARD_ENABLED"] = enabled
	if status is not None:
		env["CHECK_TRIAGE_RESOLVER_STATUS"] = status
	result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
	return {
		"rc": result.returncode,
		"stdout": result.stdout,
		"stderr": result.stderr,
		"output": codex_output.read_text(encoding="utf-8") if codex_output.exists() else None,
		"env": github_env.read_text(encoding="utf-8"),
		"gh_output": github_output.read_text(encoding="utf-8"),
	}


def _assert_blocked(result: dict, reason_fragment: str) -> None:
	assert result["rc"] == 0, result["stderr"]
	assert result["output"] is not None
	lines = [line for line in result["output"].splitlines() if line.strip()]
	assert len(lines) == 1, lines
	match = BLOCKED_RE.match(lines[0])
	assert match, lines[0]
	assert reason_fragment in match.group(1)
	assert "CHECK_TRIAGE_BRANCH_GUARD_BLOCKED=true" in result["env"].splitlines()
	assert "blocked=true" in result["gh_output"].splitlines()
	assert "outcome=blocked" in result["stdout"]


def _assert_not_blocked(result: dict, outcome: str) -> None:
	assert result["rc"] == 0, result["stderr"]
	assert result["output"] is None
	assert "CHECK_TRIAGE_BRANCH_GUARD_BLOCKED" not in result["env"]
	assert "blocked=true" not in result["gh_output"]
	assert f"outcome={outcome}" in result["stdout"]


@pytest.mark.parametrize("enabled", [None, "false", "garbage", ""])
def test_disabled_guard_never_blocks(tmp_path: Path, enabled: str | None) -> None:
	result = _run_guard(tmp_path, enabled=enabled, labels=["ai:check-triage"], status="none")
	_assert_not_blocked(result, "skip")
	assert "reason=disabled" in result["stdout"]


@NEEDS_JQ
@pytest.mark.parametrize("enabled", ["true", "TRUE", "1", "on", "yes"])
def test_enabled_guard_ignores_issues_without_check_triage_label(tmp_path: Path, enabled: str) -> None:
	for status in ("none", "failed", "unavailable", "resolved", ""):
		sub = tmp_path / (status or "empty")
		sub.mkdir()
		result = _run_guard(sub, enabled=enabled, labels=["ai:planning"], status=status)
		_assert_not_blocked(result, "pass")
		assert "reason=not_check_triage" in result["stdout"]


@NEEDS_JQ
def test_enabled_guard_passes_with_resolved_metadata(tmp_path: Path) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage", "ai:planning"], status="resolved")
	_assert_not_blocked(result, "pass")
	assert "reason=metadata_present" in result["stdout"]


@NEEDS_JQ
def test_enabled_guard_blocks_without_metadata(tmp_path: Path) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status="none")
	_assert_blocked(result, "no explicit target-branch metadata")
	assert "reason=no_target_branch_metadata" in result["stdout"]


@NEEDS_JQ
@pytest.mark.parametrize("status", ["failed", "unavailable", "", "resolved\nINJECTED=1", "bogus"])
def test_enabled_guard_blocks_when_branch_unverified(tmp_path: Path, status: str) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status=status)
	_assert_blocked(result, "could not be verified")
	assert "reason=target_branch_unverified" in result["stdout"]
	assert "INJECTED" not in result["output"]
	assert "INJECTED" not in result["env"]


def test_enabled_guard_fails_closed_on_unreadable_labels(tmp_path: Path) -> None:
	result = _run_guard(tmp_path, enabled="true", labels=None, status="resolved")
	_assert_blocked(result, "could not be verified")
	assert "reason=labels_unavailable" in result["stdout"]


@NEEDS_JQ
def test_guard_never_infers_branch_from_issue_prose(tmp_path: Path) -> None:
	body = "The failing PR targets orchestrator/project-77.\nBase branch: orchestrator/project-77\nTarget branch: main\n"
	result = _run_guard(tmp_path, enabled="true", labels=["ai:check-triage"], status="none", body=body)
	_assert_blocked(result, "no explicit target-branch metadata")
	assert "orchestrator/project-77" not in result["output"]
	assert "project-77" not in _run_body(GUARD_STEP)


def test_guard_step_wiring_contract() -> None:
	step = _step_text(GUARD_STEP)
	body = _run_body(GUARD_STEP)
	assert "${{" not in body
	assert "id: check_triage_branch_guard" in step
	assert "if: env.SKIP_PLAN != 'true'" in step
	assert "CHECK_TRIAGE_BRANCH_GUARD_ENABLED: ${{ vars.CHECK_TRIAGE_BRANCH_GUARD_ENABLED || 'false' }}" in step
	assert "CHECK_TRIAGE_RESOLVER_STATUS: ${{ steps.refctx.outputs.resolver_status }}" in step
	names = _step_names()
	guard = names.index(GUARD_STEP)
	assert names.index("Validate planning phase label") < guard
	assert names.index("Check and claim /answer command") < guard
	assert guard < names.index("Capture pre-Codex plan write-guard snapshot")
	assert guard < names.index("Run Codex planning") < names.index("Parse planning output")
	codex = _step_text("Run Codex planning")
	assert "if: env.SKIP_PLAN != 'true' && env.CHECK_TRIAGE_BRANCH_GUARD_BLOCKED != 'true'" in codex


def test_refctx_emits_resolver_status_on_every_exit_path() -> None:
	body = _run_body("Resolve integration ref")
	assert body.count('echo "resolver_status=unavailable" >> "$GITHUB_OUTPUT"') == 3
	for status in ("resolved", "none", "failed"):
		assert body.count(f'echo "resolver_status={status}" >> "$GITHUB_OUTPUT"') == 1
	assert body.count("exit 0") == body.count('echo "resolver_status=unavailable"')
	# Checkout ref selection is unchanged.
	assert "ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}" in _workflow_text()


def test_resolver_fixtures_for_check_triage_bodies() -> None:
	prose = json.loads((FIXTURE_DIR / "check_triage_prose_branch_not_inferred.json").read_text(encoding="utf-8"))
	target = json.loads((FIXTURE_DIR / "check_triage_target_branch_main.json").read_text(encoding="utf-8"))
	assert prose["expected_stdout"] == "" and prose["expected_exit_code"] == 0
	assert "orchestrator/project-77" in prose["issues"]["101"]["body"]
	assert target["expected_stdout"] == "main" and target["expected_exit_code"] == 0
	result = subprocess.run(["bash", str(RESOLVER), "--self-test"], capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "self-test passed" in result.stdout
