#!/usr/bin/env python3
"""Single-issue security pass (plan Phase 8a, port P1)."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review_single_issue_security_pass.sh"
REVIEW = ROOT / ".github" / "workflows" / "review_autofix.yml"
AUDIT = ROOT / ".github" / "workflows" / "security-audit.yml"
AUDIT_TEMPLATE = ROOT / "workflow-templates" / "ai-security-audit.yml"
STAGE = ROOT / "scripts" / "stage_workflow_support.sh"

HEAD = "c" * 40
OLD = "d" * 40

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
	log.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
fail = os.environ.get("FAKE_GH_FAIL", "")
if args[:2] == ["workflow", "run"]:
	sys.exit(1 if "dispatch" in fail else 0)
if args[0] == "api" and "--paginate" in args:
	print(open(os.environ["FAKE_GH_COMMENTS"]).read())
	sys.exit(0)
sys.exit(0)
'''


def _marker(status: str, head: str, cycle: int) -> str:
	return f"<!-- ai:single-issue-security-pass:v1 status={status} head={head} cycle={cycle} -->"


def _comment(body: str, association: str = "OWNER", login: str = "owner", age_hours: float = 0.1) -> dict:
	created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
	return {"body": body, "author_association": association, "user": {"login": login}, "created_at": created}


def _pr(base: str = "main", head_ref: str = "ai/issue-7", head_repo: str = "o/r", labels: tuple = ()) -> dict:
	return {
		"state": "open",
		"base": {"ref": base},
		"head": {"ref": head_ref, "sha": HEAD, "repo": {"full_name": head_repo}},
		"labels": [{"name": name} for name in labels],
	}


def _run(tmp_path: Path, mode: str, pr: dict | None = None, comments: list | None = None, env: dict | None = None, skip: bool = False):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	support = tmp_path / "support"
	support.mkdir(exist_ok=True)
	(support / "security_pass_skip.py").write_text(
		f"import json\nprint(json.dumps({{'skip': {skip}, 'label': None, 'reason': 'stub'}}))\n", encoding="utf-8"
	)
	(tmp_path / "pr.json").write_text(json.dumps(pr or _pr()), encoding="utf-8")
	(tmp_path / "comments.json").write_text(json.dumps(comments or []), encoding="utf-8")
	log = tmp_path / "calls.log"
	log.write_text("", encoding="utf-8")
	output = tmp_path / "output"
	output.write_text("", encoding="utf-8")
	run_env = dict(
		os.environ,
		PATH=f"{bin_dir}:{os.environ['PATH']}",
		FAKE_GH_LOG=str(log),
		FAKE_GH_COMMENTS=str(tmp_path / "comments.json"),
		GITHUB_OUTPUT=str(output),
		REPOSITORY="o/r",
		PR_NUMBER="42",
		PR_PAYLOAD_FILE=str(tmp_path / "pr.json"),
		PR_ISSUE_COMMENTS_FILE=str(tmp_path / "comments.json"),
		LINKED_ISSUES_JSON='[{"number": 7}]',
		DEFAULT_BRANCH="main",
		SUPPORT_SCRIPTS_DIR=str(support),
	)
	for name in ("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "MAX_SECURITY_PASS_CYCLES", "ORCH_INTEGRATION_BRANCH_PATTERN"):
		run_env.pop(name, None)
	run_env.update(env or {})
	result = subprocess.run(["bash", str(SCRIPT), mode], capture_output=True, text=True, env=run_env, check=False)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, calls, output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
	"pr, env, skip, reason",
	[
		(_pr(), {"SINGLE_ISSUE_SECURITY_PASS_ENABLED": "false"}, False, "disabled"),
		(_pr(base="orchestrator/project-9"), {}, False, "not_standalone"),
		(_pr(head_ref="orchestrator/project-9"), {}, False, "not_standalone"),
		(_pr(head_repo="fork/r"), {}, False, "not_standalone"),
		(_pr(labels=("e2e-smoke-test",)), {}, False, "not_standalone"),
		(_pr(), {}, True, "verified_followup"),
	],
)
def test_ineligible_prs_merge_as_before(tmp_path: Path, pr: dict, env: dict, skip: bool, reason: str) -> None:
	result, calls, output = _run(tmp_path, "gate", pr=pr, env=env, skip=skip)
	assert output == "hold=false\n"
	assert f"reason={reason}" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_clean_marker_for_this_head_lets_the_merge_run(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment("ok\n" + _marker("clean", HEAD, 1))])
	assert output == "hold=false\n" and "outcome=clean" in result.stdout and calls == []


def test_first_clean_review_dispatches_the_audit(tmp_path: Path) -> None:
	stale = [_comment(_marker("clean", OLD, 1)), _comment(_marker("clean", HEAD, 1), association="NONE", login="someone")]
	result, calls, output = _run(tmp_path, "gate", comments=stale)
	assert output == "hold=true\n" and "outcome=dispatched cycle=2" in result.stdout
	assert ["workflow", "run", "ai-security-audit.yml", "-R", "o/r", "--ref", "main", "-f", "ref=ai/issue-7", "-f", "pr_number=42"] in calls
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted and posted[0][-1].endswith(_marker("pending", HEAD, 2))


@pytest.mark.parametrize("status", ["pending", "findings"])
def test_a_running_audit_or_open_followups_hold_without_a_new_dispatch(tmp_path: Path, status: str) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker(status, HEAD, 1))])
	assert output == "hold=true\n" and calls == []


def test_a_stale_pending_audit_is_dispatched_again(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 1), age_hours=7)])
	assert "outcome=dispatched cycle=2" in result.stdout


def test_cycles_exhausted_labels_the_pr_for_the_unblock_judge(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", f"{n:040x}", n)) for n in range(1, 6)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\n" and "reason=cycles_exhausted" in result.stdout
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_a_failed_dispatch_falls_back_to_merging(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "dispatch"})
	assert output == "hold=false\n" and "reason=dispatch_failed" in result.stdout


def _report(tmp_path: Path, outcome: str, findings: str):
	env = {
		"SECURITY_PASS_PR_NUMBER": "42",
		"SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": outcome,
		"SECURITY_PASS_FINDINGS": findings,
	}
	return _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 3))], env=env)


@pytest.mark.parametrize(
	"outcome, findings, status, redispatch",
	[("success", "0", "clean", True), ("success", "2", "findings", False), ("failure", "", "failed", True)],
)
def test_report_posts_the_result_and_reruns_the_review(tmp_path: Path, outcome: str, findings: str, status: str, redispatch: bool) -> None:
	result, calls, _ = _report(tmp_path, outcome, findings)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker(status, HEAD, 3))
	review = ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"]
	assert (review in calls) is redispatch


def _steps(path: Path, job: str) -> dict:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def test_review_wiring() -> None:
	steps = _steps(REVIEW, "codex-agent")
	names = list(steps)
	assert names.index("Single-issue security pass") + 1 == names.index("Enable auto-merge on PR")
	gate = steps["Single-issue security pass"]
	assert gate["id"] == "single_issue_security_pass"
	assert gate["if"] == steps["Enable auto-merge on PR"]["if"].replace(" && steps.single_issue_security_pass.outputs.hold != 'true'", "")
	for name in ("Enable auto-merge on PR", "Mark linked issues ready to merge"):
		assert steps[name]["if"].endswith("&& steps.single_issue_security_pass.outputs.hold != 'true'")
	assert "review_single_issue_security_pass.sh" in STAGE.read_text(encoding="utf-8")


def test_audit_wiring() -> None:
	workflow = yaml.safe_load(AUDIT.read_text(encoding="utf-8"))
	triggers = workflow.get("on", workflow.get(True))
	assert "pr_number" in triggers["workflow_dispatch"]["inputs"] and "pr_number" in triggers["workflow_call"]["inputs"]
	steps = _steps(AUDIT, "security-audit")
	report = steps["Report single-issue security pass"]
	assert report["if"] == "always() && inputs.pr_number != '' && env.AUDIT_DATA_SHA != ''"
	assert "review_single_issue_security_pass.sh\" report" in report["run"]
	template = AUDIT_TEMPLATE.read_text(encoding="utf-8")
	assert "pr_number: ${{ inputs.pr_number || '' }}" in template
