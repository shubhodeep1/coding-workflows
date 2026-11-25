#!/usr/bin/env python3
"""Consumer pipeline jobs pick their runner from the caller's PIPELINE_RUNS_ON.

docs/plans/digitalocean-runners-for-consumer-pipelines-plan.md, Phase 1 (goals
G1, G2 and G8). Every job of the reusable workflows that consumers call runs on
`do-runner` (single-use DigitalOcean droplets) only when the calling
repository's variable PIPELINE_RUNS_ON is exactly `do-runner`; any other value,
or none, keeps the job's previous GitHub-hosted runner. The heal and triage
workflows, `ci.yml` (called only by this repository) and every workflow that is
not reusable never read the variable, so the repair path and coding-workflows'
own jobs never depend on the droplet fleet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
TEMPLATES_DIR = ROOT / "workflow-templates"
VARIABLE = "PIPELINE_RUNS_ON"
# Reusable workflows that keep GitHub-hosted runners whatever the caller sets.
UNROUTED_REUSABLE = {"ci.yml", "workflow_failure_heal.yml", "check_failure_triage.yml"}
# A job's previous runner, kept as the fallback; every other routed job used ubuntu-latest.
FALLBACK_RUNNER = {("security-audit.yml", "security-audit"): "ubuntu-24.04"}


def _expression(fallback: str) -> str:
	return f"${{{{ vars.{VARIABLE} == 'do-runner' && 'do-runner' || '{fallback}' }}}}"


def _load(path: Path) -> dict:
	return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _triggers(doc: dict) -> dict:
	# PyYAML reads an unquoted `on:` key as True.
	on = doc.get("on", doc.get(True))
	if isinstance(on, dict):
		return on
	if isinstance(on, list):
		return {name: None for name in on}
	if isinstance(on, str):
		return {on: None}
	return {}


def _reusable_workflows() -> list[Path]:
	return sorted(path for path in WORKFLOWS_DIR.glob("*.yml") if "workflow_call" in _triggers(_load(path)))


ROUTED = [path for path in _reusable_workflows() if path.name not in UNROUTED_REUSABLE]


def test_the_routed_set_is_the_planned_one() -> None:
	names = {path.name for path in ROUTED}
	assert len(names) == 17, sorted(names)
	assert not names & UNROUTED_REUSABLE
	# Every unrouted reusable workflow still exists, so the exclusion list cannot go stale.
	assert UNROUTED_REUSABLE <= {path.name for path in _reusable_workflows()}


@pytest.mark.parametrize("path", ROUTED, ids=lambda path: path.name)
def test_every_routed_job_reads_the_caller_variable(path: Path) -> None:
	jobs = _load(path).get("jobs") or {}
	assert jobs, path.name
	for job_id, job in jobs.items():
		if "uses" in job:
			# A job that calls another reusable workflow has no runner of its own.
			continue
		fallback = FALLBACK_RUNNER.get((path.name, job_id), "ubuntu-latest")
		assert job.get("runs-on") == _expression(fallback), (path.name, job_id, job.get("runs-on"))


def test_the_fallback_map_names_real_jobs() -> None:
	for (name, job_id), _ in FALLBACK_RUNNER.items():
		assert job_id in (_load(WORKFLOWS_DIR / name).get("jobs") or {}), (name, job_id)


@pytest.mark.parametrize(
	"path",
	[path for path in sorted(WORKFLOWS_DIR.glob("*.yml")) if path not in ROUTED] + sorted(TEMPLATES_DIR.glob("*.yml")),
	ids=lambda path: f"{path.parent.name}/{path.name}",
)
def test_unrouted_workflows_never_read_the_variable(path: Path) -> None:
	assert VARIABLE not in path.read_text(encoding="utf-8"), path
