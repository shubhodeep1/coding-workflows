"""CI failures reach triage and heal through workflow_run (Q50, Q51, Q55, Q56).

GitHub sends no check_run event for a check created by GitHub Actions, so the
check_run job of the triage wrappers never fires for Actions CI. These tests
pin the workflow_run jobs that replace it and the main-only CI filter of the
heal intake, and evaluate the job conditions against sample events.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
INTERNAL = REPO_ROOT / ".github" / "workflows" / "internal-check-failure-triage.yml"
CONSUMER = REPO_ROOT / "workflow-templates" / "ai-check-failure-triage.yml"
INTAKE = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
TRIAGE_SCRIPT = REPO_ROOT / "scripts" / "check_failure_triage.sh"


def _load(path: Path) -> dict:
	return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(data: dict) -> dict:
	return data.get("on", data.get(True))


def _evaluate(expression: str, event_name: str, event: dict, variables: dict | None = None) -> bool:
	"""Evaluate the small GitHub expression subset these `if:` blocks use."""
	text = " ".join(expression.split())

	def contains_ci(haystack, needle):
		return str(needle).lower() in (str(item).lower() for item in haystack)

	def lookup(path: str):
		root, *rest = path.split(".")
		value = {"github": {"event_name": event_name, "event": event}, "vars": variables or {}}[root]
		for part in rest:
			match = re.fullmatch(r"(\w+)\[(\d+)\]", part)
			if match:
				value = (value or {}).get(match.group(1)) or []
				value = value[int(match.group(2))] if len(value) > int(match.group(2)) else None
			else:
				value = (value or {}).get(part) if isinstance(value, dict) else None
		return value

	python = text
	python = re.sub(r"!contains\(fromJson\('(\[[^\]]*\])'\), ([\w.\[\]]+)\)", lambda m: f"(not contains_ci({m.group(1)}, lookup('{m.group(2)}')))", python)
	python = re.sub(r"contains\(fromJson\('(\[[^\]]*\])'\), ([\w.\[\]]+)\)", lambda m: f"contains_ci({m.group(1)}, lookup('{m.group(2)}'))", python)
	python = re.sub(r"!startsWith\(([\w.\[\]]+), '([^']*)'\)", lambda m: f"(not str(lookup('{m.group(1)}') or '').startswith('{m.group(2)}'))", python)
	python = re.sub(r"!contains\(([\w.\[\]]+), '([^']*)'\)", lambda m: f"(not ('{m.group(2)}' in str(lookup('{m.group(1)}') or '')))", python)
	python = re.sub(r"(?<!lookup\(')\b(github\.[\w.\[\]]+)", lambda m: f"lookup('{m.group(1)}')", python)
	python = python.replace("&&", " and ").replace("||", " or ").replace("!= null", "is not None")
	python = python.replace("'false'", "'false'")
	return bool(eval(python, {"lookup": lookup, "contains_ci": contains_ci}))  # noqa: S307 - test-only evaluator over checked-in YAML


def _run_event(name: str, *, conclusion: str = "failure", event: str = "pull_request", prs: int = 1, branch: str = "feature", path: str = ".github/workflows/test.yml") -> dict:
	return {
		"workflow": {"path": path.split("@", 1)[0]},
		"workflow_run": {
			"name": name,
			"path": path,
			"conclusion": conclusion,
			"event": event,
			"head_branch": branch,
			"head_sha": "a" * 40,
			"html_url": "https://github.com/o/r/actions/runs/1",
			"pull_requests": [{"number": 42}] * prs,
		},
		"repository": {"default_branch": "main"},
	}


def test_internal_wrapper_listens_to_ci_and_keeps_check_run() -> None:
	data = _load(INTERNAL)
	assert _on(data)["workflow_run"] == {"workflows": ["CI"], "types": ["completed"]}
	assert _on(data)["check_run"] == {"types": ["completed"]}
	assert data["jobs"]["triage"]["if"].startswith("github.event_name == 'check_run' &&")


@pytest.mark.parametrize("path, ref", [(INTERNAL, "main"), (CONSUMER, "stable")])
def test_workflow_run_job_triages_one_failed_pull_request_run(path: Path, ref: str) -> None:
	job = _load(path)["jobs"]["triage-workflow-run"]
	assert job["uses"] == f"shubhodeep1/coding-workflows/.github/workflows/check_failure_triage.yml@{ref}"
	assert job["secrets"] == {
		name: "${{ secrets." + name + " }}"
		for name in ("GH_PAT", "CHECK_TRIAGE_ISSUES_TOKEN", "OPENROUTER_API_KEY", "TG_BOT_SECRET")
	}
	assert _load(path)["jobs"]["triage"]["secrets"] == job["secrets"]
	assert "inherit" not in path.read_text(encoding="utf-8")
	assert job["with"] == {
		"pr_number": "${{ github.event.workflow_run.pull_requests[0].number }}",
		"check_run_id": "",
		"check_name": "${{ github.event.workflow_run.name }}",
		"check_conclusion": "${{ github.event.workflow_run.conclusion }}",
		"head_sha": "${{ github.event.workflow_run.head_sha }}",
		"details_url": "${{ github.event.workflow_run.html_url }}",
	}
	condition = job["if"]
	assert _evaluate(condition, "workflow_run", _run_event("CI"))
	assert _evaluate(condition, "workflow_run", _run_event("CI", conclusion="timed_out"))
	assert not _evaluate(condition, "workflow_run", _run_event("CI", conclusion="success"))
	assert not _evaluate(condition, "workflow_run", _run_event("CI", event="push"))
	assert not _evaluate(condition, "workflow_run", _run_event("CI", prs=0))
	assert not _evaluate(condition, "workflow_run", _run_event("CI"), {"CHECK_FAILURE_TRIAGE_ENABLED": "false"})
	assert not _evaluate(condition, "check_run", {"check_run": {}})


def test_workflow_run_dedup_reuses_open_issue_per_pr_and_workflow() -> None:
	script = TRIAGE_SCRIPT.read_text(encoding="utf-8")
	assert 'FP="$(printf \'%s\' "${REPO}|pr=${PR_NUMBER}|check=${CHECK_NAME}"' in script
	assert 'log "skip reason=duplicate_open_issue' in script


def test_consumer_wrapper_listens_to_every_workflow_but_its_own() -> None:
	data = _load(CONSUMER)
	assert _on(data)["workflow_run"] == {"workflows": ["*"], "types": ["completed"]}
	condition = data["jobs"]["triage-workflow-run"]["if"]
	assert _evaluate(condition, "workflow_run", _run_event("Tests"))
	assert _evaluate(condition, "workflow_run", _run_event("AI Integration Tests"))
	names = {_load(path)["name"] for path in (REPO_ROOT / "workflow-templates").glob("*.yml")}
	assert names and all(name.startswith("AI ") for name in names), sorted(names)
	paths = {f".github/workflows/{path.name}" for path in (REPO_ROOT / "workflow-templates").glob("*.yml")}
	excluded_names_match = re.search(
		r"!contains\(fromJson\('([^']+)'\), github\.event\.workflow\.path\)", condition,
	)
	assert excluded_names_match
	assert set(json.loads(excluded_names_match.group(1))) == paths
	for own in paths:
		assert not _evaluate(condition, "workflow_run", _run_event("Any name", path=own)), own
		assert not _evaluate(condition, "workflow_run", _run_event("Any name", path=f"{own}@refs/heads/main")), own
	assert not _evaluate(condition, "workflow_run", {**_run_event("Tests"), "workflow": {}})
	assert _evaluate(condition, "workflow_run", _run_event("AI Clarify"))
	assert _evaluate(condition, "workflow_run", _run_event("ai clarify"))
	assert _evaluate(condition, "workflow_run", _run_event("AI Review", path=".github/workflows/custom.yml@refs/heads/main"))
	assert not _evaluate("!contains(fromJson('[\"AI Clarify\"]'), github.event.workflow_run.name)", "workflow_run", _run_event("ai clarify"))


def test_heal_intake_takes_ci_only_for_pushes_to_the_default_branch() -> None:
	data = _load(INTAKE)
	assert "CI" in _on(data)["workflow_run"]["workflows"]
	condition = data["jobs"]["intake"]["if"]
	assert _evaluate(condition, "workflow_run", _run_event("CI", event="push", branch="main", prs=0))
	assert not _evaluate(condition, "workflow_run", _run_event("CI", event="pull_request"))
	assert not _evaluate(condition, "workflow_run", _run_event("CI", event="push", branch="stable", prs=0))
	assert not _evaluate(condition, "workflow_run", _run_event("CI", event="push", branch="main", conclusion="success", prs=0))
	# The release workflows keep their branch-agnostic behaviour.
	assert _evaluate(condition, "workflow_run", _run_event("Promote main to stable", event="schedule", branch="main", prs=0))
	assert _evaluate(condition, "workflow_dispatch", {})
