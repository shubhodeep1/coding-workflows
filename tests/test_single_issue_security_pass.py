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
if args[:2] == ["api", "user"]:
	if "identity" in fail:
		sys.exit(1)
	print("owner")
	sys.exit(0)
if args[:2] == ["api", "repos/o/r/pulls/42"]:
	if "pr_lookup" in fail:
		sys.exit(1)
	print(open(os.environ["FAKE_GH_PR"]).read())
	sys.exit(0)
if args[:2] == ["api", "repos/o/r/issues/42/labels"] and "label" in fail:
	sys.exit(1)
if args[:2] == ["api", "repos/o/r/issues/42/comments"] and "comment_write" in fail:
	sys.exit(1)
if args[0] == "api" and "--paginate" in args:
	if "comments_once" in fail and sum('"--paginate"' in line for line in open(os.environ["FAKE_GH_LOG"])) == 1:
		sys.exit(1)
	print(open(os.environ["FAKE_GH_COMMENTS"]).read())
	sys.exit(0)
sys.exit(0)
'''


def _marker(status: str, head: str, cycle: int) -> str:
	return f"<!-- ai:single-issue-security-pass:v1 status={status} head={head} cycle={cycle} -->"


def _comment(body: str, association: str = "OWNER", login: str = "owner", age_hours: float = 0.1, comment_id: int = 1) -> dict:
	created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
	return {"id": comment_id, "body": body, "author_association": association, "user": {"login": login}, "created_at": created}


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
		FAKE_GH_PR=str(tmp_path / "pr.json"),
		GITHUB_OUTPUT=str(output),
		REPOSITORY="o/r",
		PR_NUMBER="42",
		PR_PAYLOAD_FILE=str(tmp_path / "pr.json"),
		PR_ISSUE_COMMENTS_FILE=str(tmp_path / "comments.json"),
		LINKED_ISSUES_JSON='[{"number": 7}]',
		DEFAULT_BRANCH="main",
		SECURITY_PASS_AUDIT_BRANCH="ai/issue-7",
		SUPPORT_SCRIPTS_DIR=str(support),
	)
	for name in ("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "MAX_SECURITY_PASS_CYCLES", "ORCH_INTEGRATION_BRANCH_PATTERN", "SECURITY_PASS_PENDING_STALE_HOURS", "SECURITY_PASS_AUTHOR_LOGIN_FALLBACK"):
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
	assert output == "hold=false\n" and "outcome=clean" in result.stdout
	assert calls == [["api", "user", "--jq", '.login // ""']]


@pytest.mark.parametrize("last_status, expected", [("failed", "dispatched"), ("findings", "hold")])
def test_only_pipeline_author_and_latest_head_result_can_authorize_merge(tmp_path: Path, last_status: str, expected: str) -> None:
	comments = [
		_comment(_marker("clean", HEAD, 1), login="other[bot]", comment_id=1),
		_comment(_marker("clean", HEAD, 1), comment_id=2),
		_comment(_marker(last_status, HEAD, 1), comment_id=3),
	]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\n" and f"outcome={expected}" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls) is (last_status == "failed")


def test_pipeline_identity_failure_holds_instead_of_authorizing(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("clean", HEAD, 1))], env={"FAKE_GH_FAIL": "identity"})
	assert output == "hold=true\n" and "reason=markers_unverifiable" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize("output_path", ["", "directory"])
def test_unwritable_hold_output_fails_the_gate(tmp_path: Path, output_path: str) -> None:
	output_destination = str(tmp_path) if output_path else ""
	result, calls, output = _run(tmp_path, "gate", env={"GITHUB_OUTPUT": output_destination, "FAKE_GH_FAIL": "identity"})
	assert result.returncode != 0 and output == ""
	assert "::error::" in result.stderr
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_installation_token_accepts_only_its_bot_marker(tmp_path: Path) -> None:
	comments = [_comment(_marker("clean", HEAD, 1), login="owner"), _comment(_marker("findings", HEAD, 1), login="github-actions[bot]", comment_id=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={
		"FAKE_GH_FAIL": "identity", "SECURITY_PASS_AUTHOR_LOGIN_FALLBACK": "github-actions[bot]",
	})
	assert output == "hold=true\n" and "reason=awaiting_followups" in result.stdout
	assert calls == [["api", "user", "--jq", '.login // ""']]


def test_mixed_linked_issues_cannot_skip_security_audit(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", skip=True, env={"LINKED_ISSUES_JSON": '[{"number":7},{"number":99}]'})
	assert output == "hold=true\n" and "outcome=dispatched" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


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
	assert output == "hold=true\n" and calls == [["api", "user", "--jq", '.login // ""']]


def test_a_stale_pending_audit_is_dispatched_again(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 1), age_hours=7)])
	assert "outcome=dispatched cycle=2" in result.stdout


def test_pending_timeout_uses_operator_override(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 1), age_hours=3)], env={"SECURITY_PASS_PENDING_STALE_HOURS": "2"})
	assert output == "hold=true\n" and "outcome=dispatched cycle=2" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_cycles_exhausted_labels_the_pr_for_the_unblock_judge(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", f"{n:040x}", n), comment_id=n) for n in range(1, 6)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\n" and "reason=cycles_exhausted" in result.stdout
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_last_cycle_findings_escalate_on_the_same_head(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 5))]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\n" and "reason=cycles_exhausted" in result.stdout
	assert any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_failed_exhaustion_label_fails_closed_for_workflow_recovery(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 5))], env={"FAKE_GH_FAIL": "label"})
	assert result.returncode == 1 and output == "hold=true\n"
	assert "reason=label_write_failed" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_two_digit_cycle_limit_is_counted_numerically(tmp_path: Path) -> None:
	comments = [_comment(_marker("failed", OLD, 9), comment_id=1), _comment(_marker("failed", OLD, 10), comment_id=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"MAX_SECURITY_PASS_CYCLES": "10"})
	assert output == "hold=true\n" and "reason=cycles_exhausted cycle=10" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_a_failed_dispatch_holds_and_records_a_failed_cycle(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "dispatch"})
	assert result.returncode == 0 and output == "hold=true\n"
	assert "outcome=hold reason=dispatch_failed cycle=1" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("failed", HEAD, 1))
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_a_failed_dispatch_without_a_marker_still_holds(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "dispatch,comment_write"})
	assert result.returncode == 0 and output == "hold=true\n"
	assert "Could not post the failed security-pass marker" in result.stdout
	assert any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_a_recorded_dispatch_failure_is_retried_on_the_next_review(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("failed", HEAD, 1))])
	assert result.returncode == 0 and output == "hold=true\n"
	assert "outcome=dispatched cycle=2" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_repeated_dispatch_failures_exhaust_into_the_label(tmp_path: Path) -> None:
	comments = [_comment(_marker("failed", HEAD, cycle), comment_id=cycle) for cycle in range(1, 5)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_FAIL": "dispatch"})
	assert result.returncode == 0 and output == "hold=true\n"
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("failed", HEAD, 5))
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)

	comments.append(_comment(_marker("failed", HEAD, 5), comment_id=5))
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert result.returncode == 0 and output == "hold=true\n"
	assert "reason=cycles_exhausted" in result.stdout
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def _report(tmp_path: Path, outcome: str, findings: str, cycle: int = 3):
	env = {
		"SECURITY_PASS_PR_NUMBER": "42",
		"SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": outcome,
		"SECURITY_PASS_FINDINGS": findings,
	}
	return _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, cycle))], env=env)


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


def test_report_does_not_rerun_review_without_a_persisted_result(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 3))], env={
		"FAKE_GH_FAIL": "comment_write", "SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0",
	})
	assert result.returncode == 0 and "reason=comment_write_failed" in result.stdout
	assert any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize("pr", [_pr(head_ref="other"), _pr(head_repo="fork/r"), _pr(base="other")])
def test_report_rejects_a_different_pr_head(tmp_path: Path, pr: dict) -> None:
	result, calls, _ = _run(tmp_path, "report", pr=pr, env={
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0",
	})
	assert "reason=pr_head_mismatch" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_report_lookup_failure_is_not_a_head_mismatch(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "report", env={
		"FAKE_GH_FAIL": "pr_lookup", "SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
	})
	assert "reason=pr_lookup_failed" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_report_with_installation_token_uses_its_pending_marker(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 1), login="github-actions[bot]")], env={
		"FAKE_GH_FAIL": "identity", "SECURITY_PASS_AUTHOR_LOGIN_FALLBACK": "github-actions[bot]",
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0",
	})
	assert "outcome=clean" in result.stdout
	assert any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_report_retries_transient_comment_listing(tmp_path: Path) -> None:
	(tmp_path / "scripts").mkdir()
	(tmp_path / "scripts" / "gh_helpers.sh").write_text('gh_retry() { "$@" || "$@"; }\n', encoding="utf-8")
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 1))], env={
		"GITHUB_WORKSPACE": str(tmp_path), "FAKE_GH_FAIL": "comments_once",
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0",
	})
	assert "outcome=clean" in result.stdout
	assert sum("--paginate" in call for call in calls) == 2


def test_last_cycle_findings_redispatch_for_exhaustion(tmp_path: Path) -> None:
	result, calls, _ = _report(tmp_path, "success", "2", cycle=5)
	assert "outcome=findings cycle=5" in result.stdout
	assert ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"] in calls


def test_summaryless_audit_never_reports_clean(tmp_path: Path) -> None:
	result, calls, _ = _report(tmp_path, "success", "")
	assert "outcome=failed" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker("failed", HEAD, 3))


def test_report_without_pipeline_pending_marker_does_not_authorize_merge(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 1), login="other[bot]")], env={
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0",
	})
	assert "reason=pending_unverifiable" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def _steps(path: Path, job: str) -> dict:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def test_review_wiring() -> None:
	steps = _steps(REVIEW, "codex-agent")
	names = list(steps)
	assert names.index("Single-issue security pass") + 1 == names.index("Enable auto-merge on PR")
	gate = steps["Single-issue security pass"]
	assert gate["id"] == "single_issue_security_pass"
	assert gate["env"]["SECURITY_PASS_PENDING_STALE_HOURS"] == "${{ vars.SECURITY_PASS_PENDING_STALE_HOURS || '6' }}"
	assert gate["env"]["SECURITY_PASS_AUTHOR_LOGIN_FALLBACK"] == "${{ secrets.GH_PAT == '' && 'github-actions[bot]' || '' }}"
	assert gate["if"] == steps["Enable auto-merge on PR"]["if"].replace(" && steps.single_issue_security_pass.outputs.hold != 'true'", "")
	for name in ("Enable auto-merge on PR", "Mark linked issues ready to merge"):
		assert steps[name]["if"].endswith("&& steps.single_issue_security_pass.outputs.hold != 'true'")
	assert "review_single_issue_security_pass.sh" in STAGE.read_text(encoding="utf-8")
	gate = _steps(REVIEW, "gate")["Evaluate review gate"]
	assert gate["env"]["SINGLE_ISSUE_SECURITY_PASS_ENABLED"] == "${{ vars.SINGLE_ISSUE_SECURITY_PASS_ENABLED || 'true' }}"
	assert "SECURITY_PASS_SKIP_SUPPRESSED=\"true\"" in gate["run"]
	assert '[ "${SECURITY_PASS_SKIP_SUPPRESSED}" != "true" ]' in gate["run"]
	assert '[ "${pr_base_ref}" = "${DEFAULT_BRANCH}" ]' in gate["run"]
	assert '[ "${pr_head_repo}" = "${REPOSITORY}" ]' in gate["run"]


def test_audit_wiring() -> None:
	workflow = yaml.safe_load(AUDIT.read_text(encoding="utf-8"))
	triggers = workflow.get("on", workflow.get(True))
	assert "pr_number" in triggers["workflow_dispatch"]["inputs"] and "pr_number" in triggers["workflow_call"]["inputs"]
	steps = _steps(AUDIT, "security-audit")
	resolve = steps["Resolve audit target"]
	report = steps["Report single-issue security pass"]
	assert report["if"] == "always() && inputs.pr_number != '' && env.AUDIT_DATA_SHA != ''"
	assert "review_single_issue_security_pass.sh\" report" in report["run"]
	assert "^security-audit: .*skipping" not in report["run"]
	assert 'echo "AUDIT_DEFAULT_BRANCH=${AUDIT_DEFAULT_BRANCH}"' in resolve["run"]
	assert report["env"]["DEFAULT_BRANCH"] == "${{ env.AUDIT_DEFAULT_BRANCH }}"
	assert report["env"]["SECURITY_PASS_AUDIT_BRANCH"] == "${{ env.AUDIT_BRANCH }}"
	assert report["env"]["SECURITY_PASS_AUTHOR_LOGIN_FALLBACK"] == "${{ secrets.GH_PAT == '' && 'github-actions[bot]' || '' }}"
	assert workflow["permissions"]["actions"] == "write"
	assert workflow["permissions"]["pull-requests"] == "read"
	template = AUDIT_TEMPLATE.read_text(encoding="utf-8")
	assert "pr_number: ${{ inputs.pr_number || '' }}" in template
	assert "actions: write" in template
	assert "pull-requests: read" in template
