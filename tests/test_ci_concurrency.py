"""Cancel replaced CI runs (cut-ci-runner-time Phases 2 and 4).

`.github/workflows/ci.yml` carries one workflow-level `concurrency` block:

  * a `pull_request` run joins the group `ci-pr-<PR number>` with
    `cancel-in-progress: true`, so a newer push to the PR cancels the older
    queued or running run;
  * a final-CI `workflow_dispatch` run (Phase 4) joins
    `ci-final-pr-<inputs.pr_number>` with cancellation, so a PR's newer final
    run replaces its older one;
  * a quick run (`workflow_call` with `mode: quick`, whose event is the
    caller's `pull_request`) gets `ci-quick-<run id>` and no cancellation;
    its caller workflow cancels replaced quick runs itself;
  * a push run (main, stable and orchestrator/project-*) joins
    `ci-push-<ref>` with no cancellation (run-volume plan, D14): the running
    run always finishes, because check-failure triage compares a failing PR
    against the base branch's newest push run, and GitHub replaces only a
    run still waiting in the group with the newer push;
  * every other run gets the group `ci-<run id>`, unique per run, with no
    cancellation.

The expressions are evaluated here for each event with a small evaluator
that understands exactly the operators the block uses (`==`, `!=`, `&&`,
`||`, parentheses, string literals, `format()` and context lookups), so a
change to the expression shape fails this test instead of silently changing
behaviour.
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
	"${{ inputs.mode == 'quick' && format('ci-quick-{0}', github.run_id)"
	" || github.event_name == 'workflow_dispatch' && format('ci-final-pr-{0}', inputs.pr_number)"
	" || github.event_name == 'pull_request' && format('ci-pr-{0}', github.event.pull_request.number)"
	" || github.event_name == 'push' && format('ci-push-{0}', github.ref)"
	" || format('ci-{0}', github.run_id) }}"
)
CANCEL_EXPR = (
	"${{ inputs.mode != 'quick' && (github.event_name == 'workflow_dispatch' || github.event_name == 'pull_request') }}"
)

_TOKEN = re.compile(r"\s*(?:(?P<str>'[^']*')|(?P<op>==|!=|&&|\|\||\(|\)|,)|(?P<name>[A-Za-z_][A-Za-z0-9_.-]*))")


def _workflow() -> dict:
	return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _concurrency() -> dict:
	block = _workflow().get("concurrency")
	assert isinstance(block, dict), "ci.yml must declare a workflow-level concurrency block"
	return block


def _evaluate(expression: str, context: dict) -> object:
	"""Evaluate a GitHub expression with Actions' `&&` / `||` value semantics."""
	match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expression)
	assert match, expression
	body = match.group(1)
	tokens = []
	pos = 0
	while pos < len(body):
		token = _TOKEN.match(body, pos)
		assert token and token.end() > pos, f"unexpected text at {body[pos:]!r}"
		tokens.append(token)
		pos = token.end()
	index = 0

	def peek() -> str | None:
		return tokens[index].group(0).strip() if index < len(tokens) else None

	def take() -> re.Match:
		nonlocal index
		token = tokens[index]
		index += 1
		return token

	def primary() -> object:
		token = take()
		if token.group("str") is not None:
			return token.group("str")[1:-1]
		if token.group("op") == "(":
			value = disjunction()
			assert take().group("op") == ")"
			return value
		name = token.group("name")
		assert name is not None, token.group(0)
		if name == "format":
			assert take().group("op") == "("
			template = disjunction()
			args = []
			while peek() == ",":
				take()
				args.append(disjunction())
			assert take().group("op") == ")"
			text = str(template)
			for position, arg in enumerate(args):
				text = text.replace("{" + str(position) + "}", "" if arg is None else str(arg))
			return text
		return context.get(name)

	def comparison() -> object:
		left = primary()
		if peek() in ("==", "!="):
			op = take().group("op")
			right = primary()
			return (left == right) if op == "==" else (left != right)
		return left

	def conjunction() -> object:
		value = comparison()
		while peek() == "&&":
			take()
			right = comparison()
			value = right if value else value
		return value

	def disjunction() -> object:
		value = conjunction()
		while peek() == "||":
			take()
			right = conjunction()
			value = value if value else right
		return value

	result = disjunction()
	assert index == len(tokens), f"trailing tokens in {body!r}"
	return result


def _context(event: str, run_id: int = 43781666665, **extra) -> dict:
	context = {"github.event_name": event, "github.run_id": run_id}
	context.update(extra)
	return context


def test_concurrency_block_is_pinned() -> None:
	block = _concurrency()
	assert block["group"] == GROUP_EXPR
	assert block["cancel-in-progress"] == CANCEL_EXPR
	assert set(block) == {"group", "cancel-in-progress"}


def test_pull_request_runs_share_one_cancelling_group_per_pr() -> None:
	block = _concurrency()
	first = _context("pull_request", run_id=1, **{"github.event.pull_request.number": 9544})
	second = _context("pull_request", run_id=2, **{"github.event.pull_request.number": 9544})
	other_pr = _context("pull_request", run_id=3, **{"github.event.pull_request.number": 9535})
	assert _evaluate(block["group"], first) == "ci-pr-9544"
	assert _evaluate(block["group"], second) == _evaluate(block["group"], first)
	assert _evaluate(block["group"], other_pr) == "ci-pr-9535"
	assert _evaluate(block["cancel-in-progress"], first) is True


def test_final_ci_dispatch_runs_share_one_cancelling_group_per_pr() -> None:
	block = _concurrency()
	older = _context("workflow_dispatch", run_id=11, **{"inputs.pr_number": "9544"})
	newer = _context("workflow_dispatch", run_id=12, **{"inputs.pr_number": "9544"})
	other = _context("workflow_dispatch", run_id=13, **{"inputs.pr_number": "9535"})
	assert _evaluate(block["group"], older) == "ci-final-pr-9544"
	assert _evaluate(block["group"], newer) == _evaluate(block["group"], older)
	assert _evaluate(block["group"], other) == "ci-final-pr-9535"
	assert _evaluate(block["cancel-in-progress"], older) is True
	# A final run never shares a group with the PR's pull_request runs.
	pr_run = _context("pull_request", run_id=14, **{"github.event.pull_request.number": 9544})
	assert _evaluate(block["group"], pr_run) != _evaluate(block["group"], older)


def test_quick_runs_never_cancel_here_and_never_share_a_group() -> None:
	# The called workflow sees the caller's pull_request event.
	block = _concurrency()
	quick_a = _context("pull_request", run_id=21, **{"inputs.mode": "quick", "github.event.pull_request.number": 9544})
	quick_b = _context("pull_request", run_id=22, **{"inputs.mode": "quick", "github.event.pull_request.number": 9544})
	assert _evaluate(block["group"], quick_a) == "ci-quick-21"
	assert _evaluate(block["group"], quick_a) != _evaluate(block["group"], quick_b)
	assert _evaluate(block["cancel-in-progress"], quick_a) is False


def test_push_runs_share_one_non_cancelling_group_per_branch() -> None:
	block = _concurrency()
	older = _context("push", run_id=201, **{"github.ref": "refs/heads/main"})
	newer = _context("push", run_id=202, **{"github.ref": "refs/heads/main"})
	integration = _context("push", run_id=203, **{"github.ref": "refs/heads/orchestrator/project-9116"})
	stable = _context("push", run_id=204, **{"github.ref": "refs/heads/stable"})
	assert _evaluate(block["group"], older) == "ci-push-refs/heads/main"
	assert _evaluate(block["group"], newer) == _evaluate(block["group"], older)
	assert _evaluate(block["group"], integration) == "ci-push-refs/heads/orchestrator/project-9116"
	assert _evaluate(block["group"], stable) == "ci-push-refs/heads/stable"
	# The running push run is never cancelled: triage reads its result.
	assert _evaluate(block["cancel-in-progress"], older) is False
	# A push group never collides with a PR, final-CI or per-run group.
	pr_run = _context("pull_request", run_id=205, **{"github.event.pull_request.number": 9116})
	final_run = _context("workflow_dispatch", run_id=206, **{"inputs.pr_number": "9116"})
	assert _evaluate(block["group"], older) not in {
		_evaluate(block["group"], pr_run), _evaluate(block["group"], final_run), "ci-201",
	}


@pytest.mark.parametrize("event", ["workflow_call", "schedule"])
def test_other_runs_never_cancel_and_never_share_a_group(event: str) -> None:
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
