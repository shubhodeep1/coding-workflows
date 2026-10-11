"""Q64: consumer wrappers pin each reusable workflow to a commit
(`<file>@<sha> # stable`). The "Resolve workflow support ref" steps of
plan.yml, clarify.yml, implement.yml and orchestrate_clarify_respond.yml
stage their support scripts from that same commit instead of a newer
`stable`; validate.yml does the same in its own step (#7123). Every such step
is run here for a pin, a tag, look-alike identities and this repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ("plan.yml", "clarify.yml", "implement.yml", "orchestrate_clarify_respond.yml")
PIN = "de7fef2fce27d8e45ff8451eeba7fb980f13ee66"


def _resolve_steps(workflow: str) -> list[dict]:
	data = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
	return [
		step
		for job in data["jobs"].values()
		for step in job.get("steps", [])
		if str(step.get("name", "")).startswith("Resolve workflow support ref")
	]


def _run(step: dict, repository: str, workflow_ref: str, tmp_path: Path) -> dict[str, str]:
	env_file = tmp_path / "github_env"
	env_file.write_text("", encoding="utf-8")
	script = (
		step["run"]
		.replace("${{ github.repository }}", repository)
		.replace("${{ github.sha }}", "1" * 40)
	)
	assert "${{" not in script
	env = {
		**os.environ,
		"GITHUB_ENV": str(env_file),
		"SUPPORT_REF_WORKFLOW_JOB_JSON": json.dumps({"workflow_ref": workflow_ref}),
	}
	subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
	return dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if "=" in line)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_support_ref_follows_the_consumer_pin(workflow: str, tmp_path: Path) -> None:
	steps = _resolve_steps(workflow)
	assert steps, workflow
	reusable = f"shubhodeep1/coding-workflows/.github/workflows/{workflow}"
	other = "validate.yml" if workflow != "validate.yml" else "plan.yml"
	for step in steps:
		assert step["env"]["SUPPORT_REF_WORKFLOW_JOB_JSON"] == "${{ toJSON(job) }}"
		cases = (
			("shubhodeep1/binance-blessings", f"{reusable}@{PIN}", PIN),
			("shubhodeep1/binance-blessings", f"{reusable}@refs/tags/stable", "stable"),
			("shubhodeep1/binance-blessings", "", "stable"),
			("shubhodeep1/binance-blessings", f"evil/coding-workflows/.github/workflows/{workflow}@{PIN}", "stable"),
			("shubhodeep1/binance-blessings", f"shubhodeep1/coding-workflows/.github/workflows/{other}@{PIN}", "stable"),
			("shubhodeep1/binance-blessings", f"{reusable}@{PIN[:-1]}", "stable"),
			("shubhodeep1/coding-workflows", f"{reusable}@{PIN}", "1" * 40),
		)
		for repository, workflow_ref, expected in cases:
			values = _run(step, repository, workflow_ref, tmp_path)
			assert len(values) == 1, (step["name"], values)
			assert list(values.values()) == [expected], (workflow, step["name"], repository, workflow_ref, values)
