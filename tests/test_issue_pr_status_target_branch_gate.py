#!/usr/bin/env python3
"""Runtime tests for the target-branch gate in issue_pr_status.yml (issue #4813).

Incident: #4688's completion PR #4748 merged into #4688's own project branch
(`claude/implement-plan-issue-4688-...`). The step `Update linked issue labels
when PR closes` found #4688 through the PR body/title fallback and labelled it
`ai:merged` with no base-branch check, and `close_merged_issues_sweep` closed
the issue on that label before the chain's final merge.

The step now labels a linked issue on a merged PR only when the PR's base is the
issue's target branch: the default branch, the branch the issue body names on
its `Integration branch:` / `Target branch:` line, or, for an
orchestrator-managed child, its `orchestrator/project-<T>` branch. Issue #4957:
an issue is a managed child only when it carries the `ai:orchestrator-managed`
label; the "Managed by: AI Orchestrator" body text alone never counts. Issue
#5619: an orchestrator-tracking issue's AI-memory lineage is finalized only
by its completion PR (merged into the default branch from
`orchestrator/project-<T>` in this repository). These
tests run the step's real `run:` script under
bash with a stub `gh` on PATH and record the label and close calls it makes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"
GH_HELPERS = REPO_ROOT / "scripts" / "gh_helpers.sh"
STEP_NAME = "Update linked issue labels when PR closes"
REPOSITORY = "acme/widgets"


def _step_script(step_name: str) -> str:
	text = WORKFLOW.read_text(encoding="utf-8")
	step_marker = f"      - name: {step_name}\n"
	start = text.find(step_marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	run_marker = "\n        run: |\n"
	run_start = text.find(run_marker, start)
	assert run_start != -1, f"Missing run block for workflow step: {step_name}"
	run_start += len(run_marker)
	next_step = text.find("\n      - name: ", run_start)
	if next_step == -1:
		next_step = len(text)
	block = text[run_start:next_step]
	return "\n".join(
		line[10:] if line.startswith("          ") else line
		for line in block.splitlines()
	) + "\n"


GH_STUB = r'''#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["GH_STUB_STATE"]
with open(state_path, encoding="utf-8") as fh:
	state = json.load(fh)
args = sys.argv[1:]

def save():
	with open(state_path, "w", encoding="utf-8") as fh:
		json.dump(state, fh)

if args[:2] == ["api", "graphql"]:
	query = ""
	for idx, arg in enumerate(args):
		if arg == "-f" and idx + 1 < len(args) and args[idx + 1].startswith("query="):
			query = args[idx + 1][len("query="):]
	if "closingIssuesReferences" in query:
		nodes = [
			{
				"number": num,
				"body": state["issues"][str(num)]["body"],
				"labels": {"nodes": [{"name": name} for name in state["issues"][str(num)]["labels"]]},
			}
			for num in state["closing_refs"]
		]
		print(json.dumps({"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}}))
		sys.exit(0)
	if state.get("orch_graphql_fail"):
		print("graphql unavailable", file=sys.stderr)
		sys.exit(1)
	repo = {}
	import re
	for alias, num in re.findall(r"(i\d+): issue\(number: (\d+)\)", query):
		issue = state["issues"][num]
		repo[alias] = {
			"number": int(num),
			"labels": {"nodes": [{"name": name} for name in issue["labels"]]},
			"body": issue["body"],
		}
	print(json.dumps({"data": {"repository": repo}}))
	sys.exit(0)

if len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/") and "/issues/" in args[1] and "--jq" in args:
	# Serve the real REST shape (labels are objects, not names) and apply the
	# caller's own --jq filter with jq, as gh does, so the test exercises the
	# workflow's label transform instead of a pre-flattened payload.
	import subprocess
	if state.get("orch_rest_fail"):
		print("rest unavailable", file=sys.stderr)
		sys.exit(1)
	num = args[1].rsplit("/", 1)[1]
	issue = state["issues"][num]
	rest_issue = {
		"number": int(num),
		"labels": [{"id": 1000 + idx, "name": name, "color": "ededed"} for idx, name in enumerate(issue["labels"])],
		"body": issue["body"],
	}
	jq_filter = args[args.index("--jq") + 1]
	proc = subprocess.run(["jq", "-c", jq_filter], input=json.dumps(rest_issue), capture_output=True, text=True)
	if proc.returncode != 0:
		sys.stderr.write(proc.stderr)
		sys.exit(proc.returncode)
	sys.stdout.write(proc.stdout)
	sys.exit(0)

if args[:2] == ["issue", "close"]:
	state["closed"].append(int(args[2]))
	save()
	sys.exit(0)

print("unexpected gh call: " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''

LABEL_HELPERS_STUB = r'''#!/usr/bin/env bash
ensure_label_exists() { :; }
set_issue_phase_label_resilient() {
	printf '%s %s\n' "$1" "$2" >> "${LABEL_CALLS_FILE}"
}
'''


def _run_step(
	*,
	issues: dict[int, dict],
	pr_base_ref: str,
	pr_body: str,
	closing_refs: list[int] | None = None,
	pr_merged: bool = True,
	default_branch: str = "main",
	pr_head_ref: str = "claude/some-feature",
	pr_head_repo: str = REPOSITORY,
	orch_graphql_fail: bool = False,
	orch_rest_fail: bool = False,
) -> dict:
	tmp = Path(tempfile.mkdtemp(prefix="issue-pr-status-gate-"))
	try:
		(tmp / "scripts").mkdir()
		shutil.copy(GH_HELPERS, tmp / "scripts" / "gh_helpers.sh")
		(tmp / "scripts" / "label_helpers.sh").write_text(LABEL_HELPERS_STUB, encoding="utf-8")
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		gh = bin_dir / "gh"
		gh.write_text(GH_STUB, encoding="utf-8")
		gh.chmod(0o755)
		state_path = tmp / "gh_state.json"
		state_path.write_text(json.dumps({
			"issues": {
				str(num): {"body": data.get("body", ""), "labels": list(data.get("labels", []))}
				for num, data in issues.items()
			},
			"closing_refs": list(closing_refs or []),
			"closed": [],
			"orch_graphql_fail": orch_graphql_fail,
			"orch_rest_fail": orch_rest_fail,
		}), encoding="utf-8")
		label_calls = tmp / "label_calls.txt"
		label_calls.write_text("", encoding="utf-8")
		github_env = tmp / "github_env"
		github_env.write_text("", encoding="utf-8")
		script = tmp / "step.sh"
		script.write_text(_step_script(STEP_NAME), encoding="utf-8")

		env = {
			"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
			"HOME": str(tmp),
			"GH_STUB_STATE": str(state_path),
			"LABEL_CALLS_FILE": str(label_calls),
			"GITHUB_ENV": str(github_env),
			"GH_TOKEN": "stub",
			"REPOSITORY": REPOSITORY,
			"PR_NUMBER": "4748",
			"PR_HEAD_REF": pr_head_ref,
			"PR_HEAD_REPO_FULL_NAME": pr_head_repo,
			"PR_BASE_REF": pr_base_ref,
			"PR_BASE_DEFAULT_BRANCH": default_branch,
			"PR_MERGED": "true" if pr_merged else "false",
			"PR_TITLE": "Completion PR",
			"PR_BODY": pr_body,
			"FINAL_LABEL": "ai:merged" if pr_merged else "ai:closed",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
		result = subprocess.run(
			["bash", str(script)],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			timeout=120,
		)
		assert result.returncode == 0, f"step failed rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
		state = json.loads(state_path.read_text(encoding="utf-8"))
		labels = [
			tuple(line.split(" ", 1))
			for line in label_calls.read_text(encoding="utf-8").splitlines()
			if line.strip()
		]
		return {
			"stdout": result.stdout,
			"closed": state["closed"],
			"labels": labels,
			"env": _parse_github_env(github_env.read_text(encoding="utf-8")),
		}
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def _parse_github_env(text: str) -> dict[str, str]:
	"""Parse `$GITHUB_ENV` the way the runner does: `NAME=value` lines and
	`NAME<<EOF` … `EOF` heredocs. A later write of the same name wins."""
	parsed: dict[str, str] = {}
	lines = text.splitlines()
	idx = 0
	while idx < len(lines):
		line = lines[idx]
		if "<<" in line and "=" not in line.split("<<", 1)[0]:
			name, delimiter = line.split("<<", 1)
			idx += 1
			value_lines = []
			while idx < len(lines) and lines[idx] != delimiter:
				value_lines.append(lines[idx])
				idx += 1
			parsed[name] = "\n".join(value_lines)
		elif "=" in line:
			name, value = line.split("=", 1)
			parsed[name] = value
		idx += 1
	return parsed


def _issue_list(value: str) -> list[int]:
	return [int(item) for item in value.split() if item.strip()]


SECURITY_FOLLOW_UP_BODY = (
	"Refs #3576\n\n"
	"Security follow-up.\n\n"
	"- Integration branch: `claude/implement-plan-issue-4586-parent`\n"
)
INCIDENT_PR_BODY = (
	"Completion PR. After the final PR merges, the chain closes #4688 explicitly.\n\n"
	"Refs #4688\n"
)


def test_merge_into_another_project_branch_leaves_issue_untouched() -> None:
	"""The #4748 shape: closing-keyword prose found by the body fallback, base is
	the issue's own project branch (neither default nor its integration branch)."""
	result = _run_step(
		issues={4688: {"body": SECURITY_FOLLOW_UP_BODY, "labels": ["ai:security", "ai:claude"]}},
		pr_base_ref="claude/implement-plan-issue-4688-own-project",
		pr_body=INCIDENT_PR_BODY,
	)
	assert result["labels"] == [], result
	assert result["closed"] == [], result
	assert (
		"PR merged into claude/implement-plan-issue-4688-own-project, which is not issue #4688's target branch "
		"(default main, integration claude/implement-plan-issue-4586-parent); leaving its labels and state unchanged."
	) in result["stdout"], result["stdout"]


def test_default_branch_merge_labels_and_closes() -> None:
	result = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": ["ai:claude"]}},
		pr_base_ref="main",
		pr_body="Fixes #10\n",
		closing_refs=[10],
	)
	assert result["labels"] == [("10", "ai:merged")], result
	assert result["closed"] == [10], result


def test_declared_integration_branch_merge_labels_without_closing() -> None:
	"""A security follow-up merged into the branch its body names keeps today's
	behaviour: `ai:merged` (the parent chain's checker waits for it), issue left
	open for close_merged_issues_sweep. Exercises both body sources."""
	for closing_refs in ([10], []):
		result = _run_step(
			issues={10: {"body": "- Integration branch: `claude/implement-plan-parent`\n", "labels": ["ai:security"]}},
			pr_base_ref="claude/implement-plan-parent",
			pr_body="Fixes #10\n",
			closing_refs=closing_refs,
			pr_head_ref="ai/issue-10",
		)
		assert result["labels"] == [("10", "ai:merged")], (closing_refs, result)
		assert result["closed"] == [], (closing_refs, result)
		assert "PR merged into claude/implement-plan-parent; issue #10 remains open." in result["stdout"], result["stdout"]


def test_target_branch_alias_counts() -> None:
	result = _run_step(
		issues={10: {"body": "**Target branch:** `stable` (heal)\n", "labels": ["ai:workflow-heal"]}},
		pr_base_ref="stable",
		pr_body="Fixes #10\n",
		pr_head_ref="ai/issue-10",
	)
	assert result["labels"] == [("10", "ai:merged")], result


MANAGED_CHILD_BODY = (
	"Implement the thing.\n\n"
	"---\n"
	"**Orchestrator metadata** (do not edit)\n"
	"- Tracking issue: #5\n"
	"- Integration branch: orchestrator/project-5\n"
	"- Local ID: `issue-1`\n"
	"- Priority: 1\n"
	"- Managed by: AI Orchestrator\n"
)
# Legacy child metadata with no integration-branch value: only the tracking
# line ties the child to its project branch.
MANAGED_CHILD_TRACKING_ONLY_BODY = (
	"Implement the thing.\n\n"
	"---\n"
	"**Orchestrator metadata** (do not edit)\n"
	"- Tracking issue: #5\n"
	"- Local ID: `issue-1`\n"
	"- Managed by: AI Orchestrator\n"
)
# Issue #4957: a standalone issue that quotes the marker, in prose and even
# on a line of its own, without the automation-applied label.
SPOOFED_MARKER_BODY = (
	"A standalone issue body containing \u201cManaged by: AI Orchestrator,\u201d even in prose.\n\n"
	"- Managed by: AI Orchestrator\n"
)


def test_orchestrator_managed_child_closes_on_integration_branch_merge() -> None:
	"""Exercises both payload sources (closingIssuesReferences and the batched
	alias lookup) and both lineage lines (declared integration branch, and
	the tracking-issue line alone)."""
	for closing_refs in ([10], []):
		for body in (MANAGED_CHILD_BODY, MANAGED_CHILD_TRACKING_ONLY_BODY):
			result = _run_step(
				issues={10: {"body": body, "labels": ["ai:orchestrator-managed"]}},
				pr_base_ref="orchestrator/project-5",
				pr_body="Fixes #10\n",
				closing_refs=closing_refs,
				pr_head_ref="ai/issue-10",
			)
			assert result["labels"] == [("10", "ai:merged")], (closing_refs, body, result)
			assert result["closed"] == [10], (closing_refs, body, result)
			assert "Closing orchestrator-managed child issue #10" in result["stdout"], result["stdout"]


def test_body_marker_without_label_is_not_managed() -> None:
	"""Issue #4957: the marker text alone does not make an issue managed, so a
	PR merged into an unrelated branch neither labels nor closes it. Exercises
	both payload sources."""
	for closing_refs in ([10], []):
		result = _run_step(
			issues={10: {"body": SPOOFED_MARKER_BODY, "labels": ["ai:claude"]}},
			pr_base_ref="feature/unrelated",
			pr_body="Fixes #10\n",
			closing_refs=closing_refs,
			pr_head_ref="ai/issue-10",
		)
		assert result["labels"] == [], (closing_refs, result)
		assert result["closed"] == [], (closing_refs, result)
		assert (
			'Issue #10 has the "Managed by: AI Orchestrator" text but not the ai:orchestrator-managed label; '
			"treating it as a standalone issue."
		) in result["stdout"], result["stdout"]
		assert (
			"PR merged into feature/unrelated, which is not issue #10's target branch "
			"(default main, integration none); leaving its labels and state unchanged."
		) in result["stdout"], result["stdout"]


def test_body_marker_without_label_still_closes_on_default_branch() -> None:
	"""The spoof fix only narrows non-default merges: a default-branch merge
	closes the issue exactly as it closes any standalone issue."""
	result = _run_step(
		issues={10: {"body": SPOOFED_MARKER_BODY, "labels": []}},
		pr_base_ref="main",
		pr_body="Fixes #10\n",
		closing_refs=[10],
	)
	assert result["labels"] == [("10", "ai:merged")], result
	assert result["closed"] == [10], result


def test_labelled_child_on_unrelated_base_is_left_untouched() -> None:
	"""Issue #4957: a labelled child finishes only on its own project branch,
	not on any base (here another project's branch)."""
	result = _run_step(
		issues={10: {"body": MANAGED_CHILD_BODY, "labels": ["ai:orchestrator-managed"]}},
		pr_base_ref="orchestrator/project-6",
		pr_body="Fixes #10\n",
		pr_head_ref="ai/issue-10",
	)
	assert result["labels"] == [], result
	assert result["closed"] == [], result
	assert (
		"PR merged into orchestrator/project-6, which is not orchestrator-managed issue #10's project branch "
		"(default main, integration orchestrator/project-5, project orchestrator/project-5); "
		"leaving its labels and state unchanged."
	) in result["stdout"], result["stdout"]


def test_rest_fallback_applies_the_same_managed_rule() -> None:
	"""When the batched GraphQL lookup fails, the per-issue REST fallback also
	requires the label and the project branch."""
	child = _run_step(
		issues={10: {"body": MANAGED_CHILD_TRACKING_ONLY_BODY, "labels": ["ai:orchestrator-managed"]}},
		pr_base_ref="orchestrator/project-5",
		pr_body="Fixes #10\n",
		pr_head_ref="ai/issue-10",
		orch_graphql_fail=True,
	)
	assert "falling back to per-issue REST" in child["stdout"], child["stdout"]
	assert child["labels"] == [("10", "ai:merged")], child
	assert child["closed"] == [10], child

	spoof = _run_step(
		issues={10: {"body": SPOOFED_MARKER_BODY, "labels": []}},
		pr_base_ref="orchestrator/project-5",
		pr_body="Fixes #10\n",
		pr_head_ref="ai/issue-10",
		orch_graphql_fail=True,
	)
	assert spoof["labels"] == [], spoof
	assert spoof["closed"] == [], spoof


def test_non_main_default_branch_is_resolved_from_payload() -> None:
	result = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": []}},
		pr_base_ref="master",
		pr_body="Fixes #10\n",
		closing_refs=[10],
		default_branch="master",
	)
	assert result["labels"] == [("10", "ai:merged")], result
	assert result["closed"] == [10], result


def test_empty_default_branch_falls_back_to_main() -> None:
	result = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": []}},
		pr_base_ref="main",
		pr_body="Fixes #10\n",
		closing_refs=[10],
		default_branch="",
	)
	assert result["labels"] == [("10", "ai:merged")], result
	assert result["closed"] == [10], result


def test_non_automation_head_on_integration_branch_is_left_untouched() -> None:
	"""Issue #5226: an unrelated PR saying `Fixes #10` merged into the branch
	#10's editable `Integration branch:` line names is not #10's assigned
	fix. Off the default branch only an automation head counts, so the issue
	gets no label. Exercises both body sources."""
	for closing_refs in ([10], []):
		result = _run_step(
			issues={10: {"body": "- Integration branch: `claude/implement-plan-parent`\n", "labels": ["ai:security"]}},
			pr_base_ref="claude/implement-plan-parent",
			pr_body="Fixes #10\n",
			closing_refs=closing_refs,
			pr_head_ref="feature/unrelated",
		)
		assert result["labels"] == [], (closing_refs, result)
		assert result["closed"] == [], (closing_refs, result)
		assert (
			"PR #4748 merged into claude/implement-plan-parent is not an automation PR for issue #10 "
			f"(head feature/unrelated, head repo {REPOSITORY}); leaving its labels and state unchanged."
		) in result["stdout"], result["stdout"]


def test_fork_head_on_managed_child_project_branch_is_left_untouched() -> None:
	"""Issue #5226: a fork PR whose head is named `ai/issue-10` must not close
	a labelled child on its project branch; only a same-repository head
	proves the automation created it. An empty head repo fails closed too."""
	for head_repo in ("attacker/widgets", ""):
		result = _run_step(
			issues={10: {"body": MANAGED_CHILD_BODY, "labels": ["ai:orchestrator-managed"]}},
			pr_base_ref="orchestrator/project-5",
			pr_body="Fixes #10\n",
			closing_refs=[10],
			pr_head_ref="ai/issue-10",
			pr_head_repo=head_repo,
		)
		assert result["labels"] == [], (head_repo, result)
		assert result["closed"] == [], (head_repo, result)
		assert (
			"PR #4748 merged into orchestrator/project-5 is not an automation PR for issue #10 "
			f"(head ai/issue-10, head repo {head_repo or 'none'}); leaving its labels and state unchanged."
		) in result["stdout"], result["stdout"]


def test_judge_followup_head_on_integration_branch_labels() -> None:
	"""Issue #5226: the orchestrator judge's `fix/<n>-followup-<epoch>` PR is
	an automation head and keeps labelling its issue on the integration
	branch."""
	result = _run_step(
		issues={10: {"body": "- Integration branch: `claude/implement-plan-parent`\n", "labels": ["ai:security"]}},
		pr_base_ref="claude/implement-plan-parent",
		pr_body="Closes #10\n",
		closing_refs=[10],
		pr_head_ref="fix/10-followup-1790000000",
	)
	assert result["labels"] == [("10", "ai:merged")], result
	assert result["closed"] == [], result


def test_default_branch_merge_from_fork_still_labels_and_closes() -> None:
	"""Issue #5226 only narrows non-default merges: GitHub closes the issue on
	a default-branch merge whatever the head."""
	result = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": []}},
		pr_base_ref="main",
		pr_body="Fixes #10\n",
		closing_refs=[10],
		pr_head_ref="patch-1",
		pr_head_repo="contributor/widgets",
	)
	assert result["labels"] == [("10", "ai:merged")], result
	assert result["closed"] == [10], result


def test_unmerged_close_behaviour_is_unchanged() -> None:
	"""Out of scope for #4813: the issue's own automation PR closed without
	merging keeps labelling the linked issue `ai:closed` and closing it,
	whatever its base (issue #5617 gates every other head)."""
	result = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": []}},
		pr_base_ref="claude/implement-plan-issue-10-own-project",
		pr_body="Fixes #10\n",
		pr_merged=False,
		pr_head_ref="ai/issue-10",
	)
	assert result["labels"] == [("10", "ai:closed")], result
	assert result["closed"] == [10], result


# Issue #5227: the lineage step finalizes only the issues the gate accepted.

LINEAGE_STEP_NAME = "Finalize linked issue lineage state"
MEMORY_HELPERS_STUB = r'''#!/usr/bin/env bash
memory_ensure_branch() { :; }
memory_finalize_task() {
	printf '%s\n' "$*" >> "${FINALIZE_CALLS_FILE}"
}
'''


def _run_lineage_step(gate_env: dict[str, str], *, pr_merged: bool = True) -> dict:
	"""Run the lineage step's real `run:` script with the lists the gate step
	exported and a stub memory_helpers.sh that records memory_finalize_task."""
	tmp = Path(tempfile.mkdtemp(prefix="issue-pr-status-lineage-"))
	try:
		(tmp / "scripts").mkdir()
		(tmp / "scripts" / "memory_helpers.sh").write_text(MEMORY_HELPERS_STUB, encoding="utf-8")
		calls = tmp / "finalize_calls.txt"
		calls.write_text("", encoding="utf-8")
		script_text = _step_script(LINEAGE_STEP_NAME)
		script_text = script_text.replace("${{ github.event.pull_request.merged }}", "true" if pr_merged else "false")
		script_text = script_text.replace("${{ github.server_url }}", "https://github.com")
		assert "${{" not in script_text, "unsubstituted workflow expression in lineage step"
		script = tmp / "step.sh"
		script.write_text(script_text, encoding="utf-8")
		env = {
			"PATH": os.environ.get("PATH", ""),
			"HOME": str(tmp),
			"FINALIZE_CALLS_FILE": str(calls),
			"AI_MEMORY_ENABLED": "true",
			"MEMORY_HELPERS_READY": "1",
			"REPOSITORY": REPOSITORY,
			"PR_NUMBER": "4748",
			"PR_URL": f"https://github.com/{REPOSITORY}/pull/4748",
			"WORKFLOW_NAME": "AI Issue PR Status Sync",
			"RUN_ID": "1",
			"RUN_ATTEMPT": "1",
			"ACTOR": "someone",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
		for name in ("LINKED_ISSUE_NUMBERS", "LINEAGE_FINALIZE_ISSUE_NUMBERS"):
			if name in gate_env:
				env[name] = gate_env[name]
		result = subprocess.run(
			["bash", str(script)],
			cwd=tmp,
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)
		assert result.returncode == 0, f"lineage step failed rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
		finalized = []
		for line in calls.read_text(encoding="utf-8").splitlines():
			parts = line.split()
			finalized.append((
				int(parts[parts.index("--issue-number") + 1]),
				parts[parts.index("--final-state") + 1],
			))
		return {"stdout": result.stdout, "finalized": finalized}
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


def test_rejected_merge_is_not_finalized_as_merged() -> None:
	"""The #5227 finding: the gate leaves the issue alone, and the lineage step
	must not write its lineage as `merged` either."""
	gate = _run_step(
		issues={4688: {"body": SECURITY_FOLLOW_UP_BODY, "labels": ["ai:security", "ai:claude"]}},
		pr_base_ref="claude/implement-plan-issue-4688-own-project",
		pr_body=INCIDENT_PR_BODY,
	)
	assert gate["labels"] == [], gate
	assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [4688], gate["env"]
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], gate["env"]

	lineage = _run_lineage_step(gate["env"])
	assert lineage["finalized"] == [], lineage
	assert "No linked issue was accepted by the target-branch gate; skipping lineage finalization." in lineage["stdout"]
	assert '"reason":"no_accepted_issues"' in lineage["stdout"], lineage["stdout"]


def test_accepted_merges_are_finalized_as_merged() -> None:
	cases = [
		({10: {"body": "Standalone issue.", "labels": ["ai:claude"]}}, "main", [10]),
		({10: {"body": "- Integration branch: `claude/implement-plan-parent`\n", "labels": ["ai:security"]}}, "claude/implement-plan-parent", [10]),
		({10: {"body": MANAGED_CHILD_BODY, "labels": ["ai:orchestrator-managed"]}}, "orchestrator/project-5", []),
	]
	for issues, base, closing_refs in cases:
		gate = _run_step(issues=issues, pr_base_ref=base, pr_body="Fixes #10\n", closing_refs=closing_refs, pr_head_ref="ai/issue-10")
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [10], (base, gate["env"])
		lineage = _run_lineage_step(gate["env"])
		assert lineage["finalized"] == [(10, "merged")], (base, lineage)


def test_mixed_link_finalizes_only_the_accepted_issue() -> None:
	"""One PR, two linked issues: #10 names the PR's base as its integration
	branch, #11 does not. Only #10 finishes here."""
	gate = _run_step(
		issues={
			10: {"body": "- Integration branch: `claude/implement-plan-parent`\n", "labels": ["ai:security"]},
			11: {"body": "Standalone issue.", "labels": ["ai:claude"]},
		},
		pr_base_ref="claude/implement-plan-parent",
		pr_body="Fixes #10\nFixes #11\n",
		closing_refs=[10, 11],
		pr_head_ref="ai/issue-10",
	)
	assert gate["labels"] == [("10", "ai:merged")], gate
	assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10, 11], gate["env"]
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [10], gate["env"]
	lineage = _run_lineage_step(gate["env"])
	assert lineage["finalized"] == [(10, "merged")], lineage


def test_unmerged_close_still_finalizes_as_closed() -> None:
	"""AD-1: the issue's own automation PR closed without merging keeps
	today's lineage finalization, whatever its base (issue #5617)."""
	gate = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": ["ai:claude"]}},
		pr_base_ref="feature/unrelated",
		pr_body="Fixes #10\n",
		closing_refs=[10],
		pr_merged=False,
		pr_head_ref="ai/issue-10",
	)
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [10], gate["env"]
	lineage = _run_lineage_step(gate["env"], pr_merged=False)
	assert lineage["finalized"] == [(10, "closed")], lineage


# Issue #5619: an orchestrator-tracking issue's lineage is finalized only by
# its completion PR, merged into the default branch from
# `orchestrator/project-<T>` in this repository.

TRACKING_BODY = "Project tracker."
# The body `ensure_eager_final_pr` (scripts/orchestrate_poll_process.sh) gives
# the completion PR: it names the tracker only as `Refs #<T>`, which the
# discovery never reads as a link (#1469, CLAUDE.md §19).
COMPLETION_PR_BODY = (
	"Squash merge of orchestrator project #5.\n\n"
	"This PR is created eagerly by the self-healing pipeline so that `main` <-> "
	"`orchestrator/project-5` drift can be resolved continuously rather than only at finalize time.\n\n"
	"Refs #5"
)
# A completion PR whose body also links the tracker (a repo-scoped URL).
LINKED_COMPLETION_PR_BODY = f"Squash merge of orchestrator project.\n\nhttps://github.com/{REPOSITORY}/issues/5\n"


def test_tracking_issue_completion_pr_finalizes_lineage() -> None:
	"""The completion PR finalizes the tracking issue's lineage as `merged`
	exactly once and never labels or closes it: with the production
	`Refs #<T>` body (the tracker is derived from the verified head and is not
	a linked issue), and with a body or closing reference that also links it
	(both payload sources)."""
	cases = [
		# (body, closing refs, tracker is a linked issue)
		(COMPLETION_PR_BODY, [], False),
		(LINKED_COMPLETION_PR_BODY, [], True),
		(LINKED_COMPLETION_PR_BODY, [5], True),
	]
	for body, closing_refs, linked in cases:
		gate = _run_step(
			issues={5: {"body": TRACKING_BODY, "labels": ["ai:orchestrator-tracking"]}},
			pr_base_ref="main",
			pr_body=body,
			closing_refs=closing_refs,
			pr_head_ref="orchestrator/project-5",
		)
		assert gate["labels"] == [] and gate["closed"] == [], (body, closing_refs, gate)
		assert (
			"PR #4748 is orchestrator project #5's completion PR (orchestrator/project-5 from "
			f"{REPOSITORY} merged into main); queuing tracking issue #5's lineage finalization."
		) in gate["stdout"], gate["stdout"]
		assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == ([5] if linked else []), (body, closing_refs, gate["env"])
		assert ("Skipping orchestrator-tracking issue #5" in gate["stdout"]) is linked, gate["stdout"]
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [5], (body, closing_refs, gate["env"])
		lineage = _run_lineage_step(gate["env"])
		assert lineage["finalized"] == [(5, "merged")], (body, closing_refs, lineage)


def test_completion_pr_head_alone_never_finalizes_unverified_prs() -> None:
	"""The head-derived path (production `Refs #<T>` body, no linked issue)
	applies the same completion-PR rule: a fork head, a non-default base, an
	unmerged close, or a head that is not exactly `orchestrator/project-<T>`
	queues nothing and never labels or closes anything."""
	cases = [
		# (base, head, head repo, merged, description)
		("main", "orchestrator/project-5", "attacker/widgets", True, "fork head named like the completion branch"),
		("main", "orchestrator/project-5", "", True, "empty head repository"),
		("claude/implement-plan-other", "orchestrator/project-5", REPOSITORY, True, "completion head into a non-default base"),
		("main", "orchestrator/project-5", REPOSITORY, False, "completion PR closed without merging"),
		("main", "orchestrator/project-5-extra", REPOSITORY, True, "suffixed head"),
		("main", "x/orchestrator/project-5", REPOSITORY, True, "prefixed head"),
		("main", "orchestrator/project-05", REPOSITORY, True, "zero-padded number"),
	]
	for base, head, head_repo, merged, description in cases:
		gate = _run_step(
			issues={5: {"body": TRACKING_BODY, "labels": ["ai:orchestrator-tracking"]}},
			pr_base_ref=base,
			pr_body=COMPLETION_PR_BODY,
			pr_merged=merged,
			pr_head_ref=head,
			pr_head_repo=head_repo,
		)
		assert gate["labels"] == [] and gate["closed"] == [], (description, gate)
		assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [], (description, gate["env"])
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], (description, gate["env"])
		assert "completion PR" not in gate["stdout"], (description, gate["stdout"])
		lineage = _run_lineage_step(gate["env"], pr_merged=merged)
		assert lineage["finalized"] == [], (description, lineage)
		assert "No linked issues found; skipping lineage finalization." in lineage["stdout"], (description, lineage["stdout"])


def test_tracking_issue_lineage_skipped_for_non_completion_prs() -> None:
	"""The #5619 finding: any other PR that links a tracking issue leaves its
	lineage alone, whatever it merged into, and so does an unmerged close."""
	cases = [
		# (base, head, head repo, merged, description, lineage queued)
		("orchestrator/project-5", "ai/issue-10", REPOSITORY, True, "child merged into the project branch", []),
		("main", "feature/unrelated", REPOSITORY, True, "unrelated default-branch merge", []),
		("claude/implement-plan-other", "feature/unrelated", REPOSITORY, True, "merge into an unrelated branch", []),
		("main", "orchestrator/project-5", "attacker/widgets", True, "fork head named like the completion branch", []),
		("main", "orchestrator/project-5", "", True, "empty head repository", []),
		("claude/implement-plan-other", "orchestrator/project-5", REPOSITORY, True, "completion head into a non-default base", []),
		# Project #6's completion PR finalizes #6 (from its head), never the #5 it links.
		("main", "orchestrator/project-6", REPOSITORY, True, "another project's completion PR", [6]),
		("main", "orchestrator/project-5", REPOSITORY, False, "completion PR closed without merging", []),
	]
	for base, head, head_repo, merged, description, queued in cases:
		gate = _run_step(
			# #10 is the child an `ai/issue-10` head links through its branch name.
			issues={5: {"body": TRACKING_BODY, "labels": ["ai:orchestrator-tracking"]}, 10: {"body": "Standalone issue.", "labels": []}},
			pr_base_ref=base,
			pr_body="Fixes #5\n",
			closing_refs=[5],
			pr_merged=merged,
			pr_head_ref=head,
			pr_head_repo=head_repo,
		)
		assert gate["labels"] == [] and gate["closed"] == [], (description, gate)
		assert 5 in _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]), (description, gate["env"])
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == queued, (description, gate["env"])
		assert (
			f"PR #4748 is not orchestrator-tracking issue #5's completion PR "
			f"(merged {'true' if merged else 'false'}, base {base}, head {head}, head repo {head_repo or 'none'}; "
			f"expected a merge of orchestrator/project-5 from {REPOSITORY} into main); skipping its lineage finalization."
		) in gate["stdout"], (description, gate["stdout"])
		lineage = _run_lineage_step(gate["env"], pr_merged=merged)
		assert lineage["finalized"] == [(num, "merged") for num in queued], (description, lineage)
		if not queued:
			assert '"reason":"no_accepted_issues"' in lineage["stdout"], (description, lineage["stdout"])


def test_tracking_issue_completion_pr_uses_the_payload_default_branch() -> None:
	"""The expected base is the default branch from the event payload, not a
	hardcoded `main`."""
	for base, expected in (("master", [5]), ("main", [])):
		gate = _run_step(
			issues={5: {"body": TRACKING_BODY, "labels": ["ai:orchestrator-tracking"]}},
			pr_base_ref=base,
			pr_body=COMPLETION_PR_BODY,
			closing_refs=[5],
			pr_head_ref="orchestrator/project-5",
			default_branch="master",
		)
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == expected, (base, gate["env"])


def test_tracking_issue_lineage_is_unchanged() -> None:
	"""Kept under its #5227 name as an alias (CLAUDE.md §6). Issue #5619
	changed the behaviour this name described: the gate still skips an
	orchestrator-tracking issue without mutating it, but its lineage is now
	finalized only by its completion PR."""
	test_tracking_issue_completion_pr_finalizes_lineage()
	test_tracking_issue_lineage_skipped_for_non_completion_prs()


def test_no_linked_issue_exports_an_empty_lineage_list() -> None:
	gate = _run_step(
		issues={},
		pr_base_ref="main",
		pr_body="No issue here.\n",
	)
	assert gate["env"].get("LINEAGE_FINALIZE_ISSUE_NUMBERS", None) is not None, gate["env"]
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], gate["env"]
	lineage = _run_lineage_step(gate["env"])
	assert lineage["finalized"] == [], lineage
	assert "No linked issues found; skipping lineage finalization." in lineage["stdout"], lineage["stdout"]


def test_unclassified_issue_on_non_default_merge_is_not_finalized() -> None:
	"""Conformance fix: when both classification reads fail, the gate treats
	the issue as tracking (skip) without reading its body. On a merge into a
	non-default branch its target branch is unknown, so its lineage is not
	finalized (fail closed)."""
	gate = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": ["ai:claude"]}},
		pr_base_ref="feature/unrelated",
		pr_body="Fixes #10\n",
		orch_graphql_fail=True,
		orch_rest_fail=True,
	)
	assert gate["labels"] == [] and gate["closed"] == [], gate
	assert gate["env"]["ORCHESTRATOR_CLASSIFICATION_COMPLETE"] == "false", gate["env"]
	assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10], gate["env"]
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], gate["env"]
	assert "Issue #10 could not be classified" in gate["stdout"], gate["stdout"]
	lineage = _run_lineage_step(gate["env"])
	assert lineage["finalized"] == [], lineage
	assert '"reason":"no_accepted_issues"' in lineage["stdout"], lineage["stdout"]


def test_unclassified_issue_lineage_needs_its_completion_pr() -> None:
	"""Issue #5619: an issue whose classification lookups both failed may be a
	tracking issue, so it follows the completion-PR rule. A default-branch
	merge from another head and an unmerged close (even of its own automation
	PR, #5617) no longer finalize it; a
	merge of `orchestrator/project-<n>` into the default branch does."""
	cases = [
		("main", "claude/some-feature", True, []),
		("feature/unrelated", "claude/some-feature", False, []),
		("feature/unrelated", "ai/issue-10", False, []),
		("main", "orchestrator/project-10", True, [(10, "merged")]),
	]
	for base, head, merged, expected in cases:
		gate = _run_step(
			issues={10: {"body": "Standalone issue.", "labels": ["ai:claude"]}},
			pr_base_ref=base,
			pr_body="Fixes #10\n",
			pr_merged=merged,
			pr_head_ref=head,
			orch_graphql_fail=True,
			orch_rest_fail=True,
		)
		assert gate["labels"] == [] and gate["closed"] == [], (base, head, gate)
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [num for num, _ in expected], (base, head, gate["env"])
		if not expected:
			assert "Issue #10 could not be classified and PR #4748 is not its completion PR" in gate["stdout"], gate["stdout"]
		lineage = _run_lineage_step(gate["env"], pr_merged=merged)
		assert lineage["finalized"] == expected, (base, head, lineage)


def test_unclassified_issue_keeps_default_merge_and_unmerged_lineage() -> None:
	"""Kept under its #5227 name as an alias (CLAUDE.md §6). Issue #5619
	reversed the behaviour this name described: an unclassified issue is no
	longer finalized on a default-branch merge or an unmerged close, only by
	its completion PR."""
	test_unclassified_issue_lineage_needs_its_completion_pr()


def test_classified_tracking_issue_keeps_lineage_when_rest_fallback_works() -> None:
	"""The completion-PR rule holds on the REST fallback path too: a tracking
	issue the fallback did classify is finalized by its completion PR, and
	not by a child merged into its project branch."""
	cases = [
		("main", "orchestrator/project-5", [5]),
		("orchestrator/project-5", "ai/issue-10", []),
	]
	for base, head, expected in cases:
		gate = _run_step(
			issues={5: {"body": TRACKING_BODY, "labels": ["ai:orchestrator-tracking"]}, 10: {"body": "Standalone issue.", "labels": []}},
			pr_base_ref=base,
			pr_body="Fixes #5\n",
			pr_head_ref=head,
			orch_graphql_fail=True,
		)
		assert "falling back to per-issue REST" in gate["stdout"], gate["stdout"]
		assert gate["labels"] == [] and gate["closed"] == [], (base, gate)
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == expected, (base, gate["env"])
		lineage = _run_lineage_step(gate["env"])
		assert lineage["finalized"] == [(num, "merged") for num in expected], (base, lineage)


def test_failed_lookup_keeps_payload_tracking_issue_skipped() -> None:
	"""Issue #5619 (found while fixing it): the REST fallback appended a failed
	lookup to TRACKING_ISSUES with `+=`, but the list from the
	closingIssuesReferences payload has no trailing newline, so "5" and "10"
	became "510". Tracking issue #5 then fell out of the skip list, and a
	default-branch merge labelled it `ai:merged` and closed it (the #2760
	incident class). Both issues must stay in the skip list and untouched."""
	gate = _run_step(
		issues={
			5: {"body": TRACKING_BODY, "labels": ["ai:orchestrator-tracking"]},
			10: {"body": "Standalone issue.", "labels": []},
		},
		pr_base_ref="main",
		pr_body="Fixes #5\n",
		closing_refs=[5],
		pr_head_ref="ai/issue-10",
		orch_graphql_fail=True,
		orch_rest_fail=True,
	)
	assert gate["labels"] == [] and gate["closed"] == [], gate
	assert _issue_list(gate["env"]["TRACKING_ISSUES"]) == [5, 10], gate["env"]
	assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [5, 10], gate["env"]
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], gate["env"]
	assert "Skipping orchestrator-tracking issue #5" in gate["stdout"], gate["stdout"]
	assert "Issue #10 could not be classified" in gate["stdout"], gate["stdout"]


def test_non_automation_head_merge_is_not_finalized() -> None:
	"""The #5226 head check is a target-branch gate rejection too: a merge into
	the issue's own integration branch from a head that is not the issue's
	automation branch (or from a fork) leaves the issue's lineage alone."""
	for head_ref, head_repo in (("feature/unrelated", REPOSITORY), ("ai/issue-10", "attacker/widgets")):
		gate = _run_step(
			issues={10: {"body": "- Integration branch: `claude/implement-plan-parent`\n", "labels": ["ai:security"]}},
			pr_base_ref="claude/implement-plan-parent",
			pr_body="Fixes #10\n",
			closing_refs=[10],
			pr_head_ref=head_ref,
			pr_head_repo=head_repo,
		)
		assert gate["labels"] == [], (head_ref, head_repo, gate)
		assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10], (head_ref, head_repo, gate["env"])
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], (head_ref, head_repo, gate["env"])
		lineage = _run_lineage_step(gate["env"])
		assert lineage["finalized"] == [], (head_ref, head_repo, lineage)
		assert '"reason":"no_accepted_issues"' in lineage["stdout"], lineage["stdout"]


# Issue #5617: a PR closed without merging changes an issue's labels, state,
# or lineage only when it is that issue's own automation PR (a same-repository
# `ai/issue-<n>…` or `fix/<n>-followup-<epoch>` head).

UNMERGED_REJECTED_HEADS = (
	("patch-1", REPOSITORY),
	("claude/some-feature", REPOSITORY),
	("ai/issue-10", "attacker/widgets"),
	("ai/issue-10", ""),
)


def test_unmerged_close_from_non_automation_head_leaves_issue_untouched() -> None:
	"""The #5617 finding: a PR whose author wrote `Fixes #10` and closed it
	without merging must not label, close, or finalize #10, whether its head is
	another branch in this repository or a fork (another issue's automation
	head is covered by the mixed-link test)."""
	for head_ref, head_repo in UNMERGED_REJECTED_HEADS:
		gate = _run_step(
			issues={10: {"body": "Standalone issue.", "labels": ["ai:claude"]}},
			pr_base_ref="main",
			pr_body="Fixes #10\n",
			closing_refs=[10],
			pr_merged=False,
			pr_head_ref=head_ref,
			pr_head_repo=head_repo,
		)
		assert gate["labels"] == [] and gate["closed"] == [], (head_ref, head_repo, gate)
		assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10], (head_ref, head_repo, gate["env"])
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], (head_ref, head_repo, gate["env"])
		assert (
			f"PR #4748 closed without merging is not an automation PR for issue #10 "
			f"(head {head_ref}, head repo {head_repo or 'none'}); leaving its labels, state, and lineage unchanged."
		) in gate["stdout"], gate["stdout"]
		lineage = _run_lineage_step(gate["env"], pr_merged=False)
		assert lineage["finalized"] == [], (head_ref, head_repo, lineage)
		assert '"reason":"no_accepted_issues"' in lineage["stdout"], lineage["stdout"]


def test_unmerged_close_from_non_automation_head_leaves_managed_child_untouched() -> None:
	"""A labelled orchestrator-managed child is gated the same way."""
	for head_ref, head_repo in UNMERGED_REJECTED_HEADS:
		gate = _run_step(
			issues={10: {"body": MANAGED_CHILD_BODY, "labels": ["ai:orchestrator-managed"]}},
			pr_base_ref="orchestrator/project-5",
			pr_body="Fixes #10\n",
			closing_refs=[10],
			pr_merged=False,
			pr_head_ref=head_ref,
			pr_head_repo=head_repo,
		)
		assert gate["labels"] == [] and gate["closed"] == [], (head_ref, head_repo, gate)
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], (head_ref, head_repo, gate["env"])


def test_unmerged_close_from_automation_heads_labels_and_closes() -> None:
	"""The implement pipeline's `ai/issue-<n>` shapes and the judge's
	`fix/<n>-followup-<epoch>` head keep today's `ai:closed` + close +
	lineage `closed`, for standalone and managed issues alike."""
	for body, labels, base in (
		("Standalone issue.", ["ai:claude"], "main"),
		(MANAGED_CHILD_BODY, ["ai:orchestrator-managed"], "orchestrator/project-5"),
	):
		for head_ref in ("ai/issue-10", "ai/issue-10-retry", "ai/issue-10/sub", "fix/10-followup-1790000000"):
			gate = _run_step(
				issues={10: {"body": body, "labels": labels}},
				pr_base_ref=base,
				pr_body="Fixes #10\n",
				closing_refs=[10],
				pr_merged=False,
				pr_head_ref=head_ref,
			)
			assert gate["labels"] == [("10", "ai:closed")], (head_ref, base, gate)
			assert gate["closed"] == [10], (head_ref, base, gate)
			assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [10], (head_ref, base, gate["env"])
			lineage = _run_lineage_step(gate["env"], pr_merged=False)
			assert lineage["finalized"] == [(10, "closed")], (head_ref, base, lineage)


def test_unmerged_close_mixed_link_changes_only_the_automation_issue() -> None:
	"""One PR closed without merging links #10 and #11, and its head is #10's
	automation branch: only #10 changes."""
	gate = _run_step(
		issues={
			10: {"body": "Standalone issue.", "labels": ["ai:claude"]},
			11: {"body": "Standalone issue.", "labels": ["ai:claude"]},
		},
		pr_base_ref="main",
		pr_body="Fixes #10\nFixes #11\n",
		closing_refs=[10, 11],
		pr_merged=False,
		pr_head_ref="ai/issue-10",
	)
	assert gate["labels"] == [("10", "ai:closed")], gate
	assert gate["closed"] == [10], gate
	assert _issue_list(gate["env"]["LINKED_ISSUE_NUMBERS"]) == [10, 11], gate["env"]
	assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [10], gate["env"]
	lineage = _run_lineage_step(gate["env"], pr_merged=False)
	assert lineage["finalized"] == [(10, "closed")], lineage


def test_unmerged_close_skips_tracking_and_unclassified_lineage() -> None:
	"""AD-2 of #5617: an orchestrator-tracking issue, and an issue whose
	classification lookups both failed, are not finalized by an unmerged close
	from a head that is not their own automation branch. Issue #5619's
	completion-PR rule covers both, so the skip is logged with its message."""
	cases = [
		(
			{5: {"body": "Project tracker.", "labels": ["ai:orchestrator-tracking"]}}, 5, [5], False,
			"PR #4748 is not orchestrator-tracking issue #5's completion PR (merged false, base main, head patch-1, head repo acme/widgets;",
		),
		(
			{10: {"body": "Standalone issue.", "labels": ["ai:claude"]}}, 10, [], True,
			"Issue #10 could not be classified and PR #4748 is not its completion PR (merged false, base main, head patch-1, head repo acme/widgets);",
		),
	]
	for issues, num, closing_refs, lookups_fail, expected_log in cases:
		gate = _run_step(
			issues=issues,
			pr_base_ref="main",
			pr_body=f"Fixes #{num}\n",
			closing_refs=closing_refs,
			pr_merged=False,
			pr_head_ref="patch-1",
			orch_graphql_fail=lookups_fail,
			orch_rest_fail=lookups_fail,
		)
		assert gate["labels"] == [] and gate["closed"] == [], (num, gate)
		assert _issue_list(gate["env"]["LINEAGE_FINALIZE_ISSUE_NUMBERS"]) == [], (num, gate["env"])
		assert expected_log in gate["stdout"], gate["stdout"]
		lineage = _run_lineage_step(gate["env"], pr_merged=False)
		assert lineage["finalized"] == [], (num, lineage)


# CI runs this file as a script (ci.yml "Phase label transition and fallback
# contract tests"), not under pytest, so every test must be called here. Keep
# this block last: a test defined after it never runs in CI.
if __name__ == "__main__":
	test_merge_into_another_project_branch_leaves_issue_untouched()
	test_default_branch_merge_labels_and_closes()
	test_declared_integration_branch_merge_labels_without_closing()
	test_target_branch_alias_counts()
	test_orchestrator_managed_child_closes_on_integration_branch_merge()
	test_body_marker_without_label_is_not_managed()
	test_body_marker_without_label_still_closes_on_default_branch()
	test_labelled_child_on_unrelated_base_is_left_untouched()
	test_rest_fallback_applies_the_same_managed_rule()
	test_non_main_default_branch_is_resolved_from_payload()
	test_empty_default_branch_falls_back_to_main()
	test_non_automation_head_on_integration_branch_is_left_untouched()
	test_fork_head_on_managed_child_project_branch_is_left_untouched()
	test_judge_followup_head_on_integration_branch_labels()
	test_default_branch_merge_from_fork_still_labels_and_closes()
	test_unmerged_close_behaviour_is_unchanged()
	test_rejected_merge_is_not_finalized_as_merged()
	test_accepted_merges_are_finalized_as_merged()
	test_mixed_link_finalizes_only_the_accepted_issue()
	test_unmerged_close_still_finalizes_as_closed()
	test_tracking_issue_completion_pr_finalizes_lineage()
	test_completion_pr_head_alone_never_finalizes_unverified_prs()
	test_tracking_issue_lineage_skipped_for_non_completion_prs()
	test_tracking_issue_completion_pr_uses_the_payload_default_branch()
	test_no_linked_issue_exports_an_empty_lineage_list()
	test_unclassified_issue_on_non_default_merge_is_not_finalized()
	test_unclassified_issue_lineage_needs_its_completion_pr()
	test_classified_tracking_issue_keeps_lineage_when_rest_fallback_works()
	test_non_automation_head_merge_is_not_finalized()
	test_failed_lookup_keeps_payload_tracking_issue_skipped()
	test_unmerged_close_from_non_automation_head_leaves_issue_untouched()
	test_unmerged_close_from_non_automation_head_leaves_managed_child_untouched()
	test_unmerged_close_from_automation_heads_labels_and_closes()
	test_unmerged_close_mixed_link_changes_only_the_automation_issue()
	test_unmerged_close_skips_tracking_and_unclassified_lineage()
	print("PASS")
