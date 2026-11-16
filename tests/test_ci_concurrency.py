"""Cancel replaced PR CI (cut-ci-runner-time Phase 2).

`.github/workflows/ci.yml` carries one workflow-level `concurrency` block:

  * a `pull_request` run joins the group `ci-pr-<PR number>` with
    `cancel-in-progress: true`, so a newer push to the PR cancels the older
    queued or running run;
  * every other run (pushes to main, stable and orchestrator/project-*) gets
    the group `ci-<run id>`, unique per run, with no cancellation, because
    check-failure triage compares a failing PR against the base branch's
    newest push run.

The expressions are evaluated here for each event with a small evaluator
that understands exactly the operators the block uses, so a change to the
expression shape fails this test instead of silently changing behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_RERUN_SCRIPT = REPO_ROOT / "scripts" / "ci_cancelled_rerun.py"

GROUP_EXPR = (
	"${{ github.event_name == 'pull_request' && format('ci-pr-{0}', github.event.pull_request.number)"
	" || format('ci-{0}', github.run_id) }}"
)
CANCEL_EXPR = "${{ github.event_name == 'pull_request' }}"


def _workflow() -> dict:
	return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _concurrency() -> dict:
	block = _workflow().get("concurrency")
	assert isinstance(block, dict), "ci.yml must declare a workflow-level concurrency block"
	return block


def _evaluate(expression: str, context: dict) -> object:
	"""Evaluate the two expression shapes the block uses for one event."""
	match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expression)
	assert match, expression
	body = match.group(1)
	is_pr = context["github.event_name"] == "pull_request"
	if body == "github.event_name == 'pull_request'":
		return is_pr
	shape = re.fullmatch(
		r"github\.event_name == 'pull_request' && format\('([^']*)', ([a-z_.]+)\) \|\| format\('([^']*)', ([a-z_.]+)\)",
		body,
	)
	assert shape, f"unexpected concurrency expression shape: {body}"
	pr_format, pr_field, other_format, other_field = shape.groups()
	pr_value = pr_format.replace("{0}", str(context.get(pr_field, "")))
	# `a && b || c` returns b when a is true and b is truthy, else c.
	if is_pr and pr_value:
		return pr_value
	return other_format.replace("{0}", str(context[other_field]))


def _context(event: str, pr_number: int | None = None, run_id: int = 43781666665) -> dict:
	context = {"github.event_name": event, "github.run_id": run_id}
	if pr_number is not None:
		context["github.event.pull_request.number"] = pr_number
	return context


def test_concurrency_block_is_pinned() -> None:
	block = _concurrency()
	assert block["group"] == GROUP_EXPR
	assert block["cancel-in-progress"] == CANCEL_EXPR
	assert set(block) == {"group", "cancel-in-progress"}


def test_pull_request_runs_share_one_cancelling_group_per_pr() -> None:
	block = _concurrency()
	first = _context("pull_request", pr_number=9544, run_id=1)
	second = _context("pull_request", pr_number=9544, run_id=2)
	other_pr = _context("pull_request", pr_number=9535, run_id=3)
	assert _evaluate(block["group"], first) == "ci-pr-9544"
	assert _evaluate(block["group"], second) == _evaluate(block["group"], first)
	assert _evaluate(block["group"], other_pr) == "ci-pr-9535"
	assert _evaluate(block["cancel-in-progress"], first) is True


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "workflow_call", "schedule"])
def test_non_pull_request_runs_never_cancel_and_never_share_a_group(event: str) -> None:
	block = _concurrency()
	run_a = _context(event, run_id=101)
	run_b = _context(event, run_id=102)
	assert _evaluate(block["group"], run_a) == "ci-101"
	assert _evaluate(block["group"], run_a) != _evaluate(block["group"], run_b)
	assert _evaluate(block["cancel-in-progress"], run_a) is False


def test_push_triggers_still_cover_the_triage_base_branches() -> None:
	on = _workflow()["on"]
	assert on["push"]["branches"] == ["main", "stable", "orchestrator/project-*"]


def test_no_job_overrides_the_workflow_concurrency() -> None:
	for job_id, job in _workflow()["jobs"].items():
		assert "concurrency" not in job, f"{job_id} must not carry its own concurrency block"


def test_rerun_sweep_skips_superseded_heads() -> None:
	# A run this block cancels belongs to a head the PR no longer has; the
	# cancelled-CI rerun sweep must keep skipping it, or it would re-run it.
	text = CI_RERUN_SCRIPT.read_text(encoding="utf-8")
	assert 'decision(number, sha, None, "skip", "superseded_head")' in text


def test_contract_is_wired_into_static_checks() -> None:
	steps = _workflow()["jobs"]["static-checks"]["steps"]
	runs = [str(step.get("run", "")) for step in steps]
	assert any("tests/test_ci_concurrency.py" in run for run in runs)
