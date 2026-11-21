#!/usr/bin/env python3
"""PR_CLOSED gate contract for codex-agent steps after the PR-state check (#9419).

"Check PR state (defense-in-depth)" writes ``PR_CLOSED=true`` to
``$GITHUB_ENV`` when the PR merged or closed between the ``gate`` job and
the start of ``codex-agent``. The workspace steps ("Finalize workspace
contents", "Activate workspace shell context") are then skipped, so
``WORKSPACE_PATH`` stays unset. A later step that reads ``WORKSPACE_PATH``
without its own ``PR_CLOSED`` gate fails the run: in run 43147436812 (PR
#9287) "Checkout PR head branch" hit its ``workspace_guard`` and turned a
merged PR's review red. These checks pin the gate on that step and fail CI
if a new ungated workspace-dependent step brings the race back.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import (  # noqa: E402
	REVIEW_AUTOFIX_WORKFLOW_PATH,
	expanded_review_autofix_text,
)

PR_STATE_CHECK_STEP = "Check PR state (defense-in-depth)"
CHECKOUT_STEP = "Checkout PR head branch"
CHECKOUT_STEP_SCRIPT = "review_autofix_step_checkout_pr_head_branch.sh"
PR_CLOSED_GATE = "env.PR_CLOSED != 'true'"
WORKSPACE_PATH_RE = re.compile(r"(\$\{?|env\.)WORKSPACE_PATH\b")
WORKSPACE_PATH_GUARD_RE = re.compile(r"\[\[?\s+-[nz]\s+\"?\$\{?WORKSPACE_PATH\b")
FAIL_CLOSED_GUARD_TEXT = "Review workspace is unavailable"
RACE_REFERENCE = (
	"a PR merged or closed before codex-agent starts leaves WORKSPACE_PATH unset "
	"(#9419, run 43147436812)"
)


CODEX_AGENT_JOB_LINE = "  codex-agent:"
STEP_START_PREFIX = "      - "
STEP_KEY_RE = re.compile(r"^        (?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<value>.*)$")


def _unquote(value: str) -> str:
	value = value.strip()
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
		return value[1:-1]
	return value


def _parse_step(block: list[str]) -> dict:
	# Line-based parse of one step (sibling review_autofix tests avoid PyYAML,
	# which the pre-push gate container does not ship). Keys sit at 8 spaces;
	# any deeper-indented or blank line continues the current key's value.
	lines = ["        " + block[0][len(STEP_START_PREFIX):]] + block[1:]
	step: dict = {}
	key = None
	values: list[str] = []
	for line in lines + ["        __end__:"]:
		stripped = line.lstrip(" ")
		if stripped.startswith("#") and len(line) - len(stripped) <= 8:
			continue  # YAML comment at key level, not part of any value
		match = STEP_KEY_RE.match(line)
		if match:
			if key is not None:
				step[key] = "\n".join(values).strip()
			key = match.group("key")
			values = [match.group("value")]
		elif key is not None:
			values.append(line)
	for key_name in ("name", "id", "if"):
		if key_name in step:
			step[key_name] = _unquote(" ".join(step[key_name].split()))
	return step


def _codex_agent_steps(text: str) -> list[dict]:
	lines = text.split("\n")
	assert CODEX_AGENT_JOB_LINE in lines, "review_autofix.yml has no codex-agent job"
	start = lines.index(CODEX_AGENT_JOB_LINE) + 1
	end = len(lines)
	for idx in range(start, len(lines)):
		line = lines[idx]
		stripped = line.lstrip(" ")
		if stripped and not stripped.startswith("#") and len(line) - len(stripped) <= 2:
			end = idx
			break
	steps: list[dict] = []
	block: list[str] = []
	for line in lines[start:end]:
		if line.startswith(STEP_START_PREFIX):
			if block:
				steps.append(_parse_step(block))
			block = [line]
		elif block:
			block.append(line)
	if block:
		steps.append(_parse_step(block))
	return steps


def _codex_agent_steps_after_pr_state_check(text: str) -> list[dict]:
	steps = _codex_agent_steps(text)
	names = [step.get("name") for step in steps]
	assert PR_STATE_CHECK_STEP in names, (
		f"codex-agent no longer has a step named {PR_STATE_CHECK_STEP!r}; "
		"update this contract to the step that sets PR_CLOSED"
	)
	return steps[names.index(PR_STATE_CHECK_STEP) + 1:]


def _step_if(step: dict) -> str:
	return str(step.get("if") or "")


def test_checkout_pr_head_branch_is_gated_on_pr_closed():
	steps = _codex_agent_steps_after_pr_state_check(
		REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8")
	)
	matches = [step for step in steps if step.get("name") == CHECKOUT_STEP]
	assert len(matches) == 1, f"expected one {CHECKOUT_STEP!r} step after {PR_STATE_CHECK_STEP!r}"
	assert PR_CLOSED_GATE in _step_if(matches[0]), (
		f"{CHECKOUT_STEP!r} must carry `if: {PR_CLOSED_GATE}`: {RACE_REFERENCE}, "
		"and its workspace_guard then fails the run"
	)


def test_workspace_dependent_steps_after_pr_state_check_are_pr_closed_gated():
	steps = _codex_agent_steps_after_pr_state_check(expanded_review_autofix_text())
	checked: list[str] = []
	ungated: list[str] = []
	for step in steps:
		body = "\n".join(str(step.get(key) or "") for key in ("run", "with", "env"))
		if not (WORKSPACE_PATH_RE.search(body) or CHECKOUT_STEP_SCRIPT in body):
			continue
		name = str(step.get("name") or step.get("id") or "<unnamed step>")
		checked.append(name)
		if PR_CLOSED_GATE in _step_if(step):
			continue
		# Issue #9419 step 3: an always() step is allowed only if its run body
		# checks WORKSPACE_PATH (`[ -n/-z "${WORKSPACE_PATH...` ) before any
		# other use, and is not the checkout script's fail-closed guard (which
		# exits 1 when WORKSPACE_PATH is unset and would reintroduce the race).
		run_body = str(step.get("run") or "")
		guard = WORKSPACE_PATH_GUARD_RE.search(run_body)
		first_use = WORKSPACE_PATH_RE.search(run_body)
		fail_closed = CHECKOUT_STEP_SCRIPT in body or FAIL_CLOSED_GUARD_TEXT in body
		if "always()" in _step_if(step) and guard and first_use and guard.start() < first_use.start() and not fail_closed:
			continue
		ungated.append(name)
	assert CHECKOUT_STEP in checked, (
		f"{CHECKOUT_STEP!r} no longer reads WORKSPACE_PATH after expansion; "
		"this contract would stop covering the step it was written for"
	)
	assert not ungated, (
		f"codex-agent steps after {PR_STATE_CHECK_STEP!r} read WORKSPACE_PATH without "
		f"`if: ... {PR_CLOSED_GATE}`: {ungated}. {RACE_REFERENCE}; gate them like "
		"'Detect preexisting Serena project config'."
	)
