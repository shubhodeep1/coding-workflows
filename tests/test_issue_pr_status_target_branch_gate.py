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
label; the "Managed by: AI Orchestrator" body text alone never counts. These
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
	orch_graphql_fail: bool = False,
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
		return {"stdout": result.stdout, "closed": state["closed"], "labels": labels}
	finally:
		shutil.rmtree(tmp, ignore_errors=True)


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


def test_unmerged_close_behaviour_is_unchanged() -> None:
	"""Out of scope for #4813: a PR closed without merging keeps labelling the
	linked issue `ai:closed` and closing it, whatever its base."""
	result = _run_step(
		issues={10: {"body": "Standalone issue.", "labels": []}},
		pr_base_ref="claude/implement-plan-issue-10-own-project",
		pr_body="Fixes #10\n",
		pr_merged=False,
	)
	assert result["labels"] == [("10", "ai:closed")], result
	assert result["closed"] == [10], result


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
	test_unmerged_close_behaviour_is_unchanged()
	print("PASS")
