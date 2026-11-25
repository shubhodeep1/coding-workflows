#!/usr/bin/env python3
"""Single-issue security pass (plan Phase 8a, port P1)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402

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
	# Issue #10104: entry n is the stderr of the n-th dispatch (null succeeds);
	# calls past the end reuse the last entry.
	if os.environ.get("FAKE_GH_DISPATCH_ERRORS"):
		errors = json.loads(os.environ["FAKE_GH_DISPATCH_ERRORS"])
		n = sum(json.loads(line)[:2] == ["workflow", "run"] for line in open(os.environ["FAKE_GH_LOG"]))
		error = errors[min(n, len(errors)) - 1]
		if error is None:
			sys.exit(0)
		sys.stderr.write(error)
		sys.exit(1)
	if "reject_delta_inputs" in fail and any(arg.startswith(("audit_diff_since=", "prior_findings=")) for arg in args):
		sys.exit(1)
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
if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in args:
	if "followups" in fail:
		sys.exit(1)
	print(open(os.environ["FAKE_GH_SECURITY_ISSUES"]).read(), end="")
	sys.exit(0)
if args[0] == "api" and any(arg.startswith("repos/o/r/pulls?state=open&base=") for arg in args):
	if "branch_prs" in fail:
		sys.exit(1)
	print(open(os.environ["FAKE_GH_BRANCH_PRS"]).read(), end="")
	sys.exit(0)
if args[0] == "api" and any(arg.startswith("repos/o/r/pulls?state=open&head=") for arg in args):
	if "head_prs" in fail:
		sys.exit(1)
	print(open(os.environ["FAKE_GH_HEAD_PRS"]).read(), end="")
	sys.exit(0)
if "followup_comment" in fail and len(args) > 1 and args[1].startswith("repos/o/r/issues/") and args[1].endswith("/comments") and args[1] != "repos/o/r/issues/42/comments":
	sys.exit(1)
if args[:3] == ["api", "-X", "PATCH"]:
	sys.exit(1 if "close" in fail else 0)
if args[:2] == ["api", "repos/o/r/issues/42/comments"]:
	if "comment_write" in fail and (fail != "comment_write_once" or sum(
		json.loads(line)[:2] == args[:2] for line in open(os.environ["FAKE_GH_LOG"])
	) == 1):
		sys.exit(1)
	if "comment_bad_json" in fail:
		print("not json")
		sys.exit(0)
	if "comment_missing_id" in fail:
		print(json.dumps({"body": next((arg[5:] for arg in args if arg.startswith("body=")), "")}))
		sys.exit(0)
	if "comment_wrong_body" in fail:
		print(json.dumps({"id": 17, "body": "not the pending marker"}))
		sys.exit(0)
	print(json.dumps({"id": 17, "body": next((arg[5:] for arg in args if arg.startswith("body=")), "")}))
	sys.exit(0)
# Issue #9533 base-sync carry: the shared freshness verifier reads the head
# commit and the base compare (the fake prints what --jq would produce).
if len(args) > 1 and args[0] == "api" and args[1].startswith("repos/o/r/commits/") and os.environ.get("FAKE_GH_COMMIT"):
	print(os.environ["FAKE_GH_COMMIT"])
	sys.exit(0)
if len(args) > 1 and args[0] == "api" and args[1].startswith("repos/o/r/compare/") and os.environ.get("FAKE_GH_COMPARE"):
	print(os.environ["FAKE_GH_COMPARE"])
	sys.exit(0)
if "pr_comments" in fail and "repos/o/r/issues/77/comments?per_page=100" in args:
	sys.exit(1)
if "parent_comments" in fail and "repos/o/r/issues/42/comments?per_page=100" in args:
	sys.exit(1)
if args[0] == "api" and len(args) > 1 and args[1].startswith("repos/o/r/commits/") and os.environ.get("FAKE_GH_COMMITS"):
	commits = json.load(open(os.environ["FAKE_GH_COMMITS"]))
	sha = args[1].rsplit("/", 1)[1]
	if sha not in commits:
		sys.exit(1)
	print(json.dumps(commits[sha]))
	sys.exit(0)
if args[0] == "api" and len(args) > 1 and args[1].startswith("repos/o/r/compare/") and os.environ.get("FAKE_GH_COMMITS"):
	print(os.environ.get("FAKE_GH_COMPARE_STATUS", "ahead"))
	sys.exit(0)
if args[0] == "api" and any("/actions/workflows/" in arg for arg in args):
	if "runs" in fail or not os.environ.get("FAKE_GH_RUNS"):
		sys.exit(1)
	print(open(os.environ["FAKE_GH_RUNS"]).read())
	sys.exit(0)
if args[0] == "api" and "--paginate" in args:
	if "comments_once" in fail and sum('"--paginate"' in line for line in open(os.environ["FAKE_GH_LOG"])) == 1:
		sys.exit(1)
	print(open(os.environ["FAKE_GH_COMMENTS"]).read())
	sys.exit(0)
sys.exit(0)
'''

FAKE_GIT = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if len(args) >= 2 and args[0] == "-C":
	args = args[2:]
if args == ["rev-parse", "HEAD"]:
	print(os.environ.get("FAKE_GIT_HEAD", "c" * 40))
	sys.exit(0)
if args == ["rev-parse", "--is-shallow-repository"]:
	print("false" if os.path.exists(os.environ.get("FAKE_GIT_UNSHALLOW_MARKER", "")) else os.environ.get("FAKE_GIT_SHALLOW", "false"))
	sys.exit(0)
if args[:2] == ["cat-file", "-e"]:
	sys.exit(0 if args[2].removesuffix("^{commit}") in os.environ.get("FAKE_GIT_ANCESTORS", "").split(",") else 1)
if args[:2] == ["check-ref-format", "--branch"]:
	sys.exit(0)
if args[:3] == ["fetch", "--no-tags", "--unshallow"]:
	if os.environ.get("FAKE_GIT_FETCH_FAIL") == "true":
		sys.exit(1)
	if os.environ.get("FAKE_GIT_UNSHALLOW_MARKER"):
		open(os.environ["FAKE_GIT_UNSHALLOW_MARKER"], "w").close()
	sys.exit(0)
if args[:2] == ["merge-base", "--is-ancestor"]:
	if os.environ.get("FAKE_GIT_INCOMPLETE_ANCESTRY") == "true" and not os.path.exists(os.environ.get("FAKE_GIT_UNSHALLOW_MARKER", "")):
		sys.exit(1)
	sys.exit(0 if args[2] in os.environ.get("FAKE_GIT_ANCESTORS", "").split(",") and args[3] == os.environ.get("FAKE_GIT_HEAD", "c" * 40) else 1)
if args[:1] == ["diff"] and "--name-only" in args and "FAKE_GIT_DIFF_FILES" in os.environ:
	print("\n".join(name for name in os.environ["FAKE_GIT_DIFF_FILES"].split(",") if name))
	sys.exit(0)
print("FAKE_GIT unmatched: " + " ".join(args), file=sys.stderr)
sys.exit(1)
'''


def _marker(status: str, head: str, cycle: int, finding_ids: tuple[str, ...] = ("F-1",)) -> str:
	marker = f"<!-- ai:single-issue-security-pass:v1 status={status} head={head} cycle={cycle} -->"
	if status == "findings" and finding_ids:
		encoded_ids = base64.b64encode(json.dumps(list(finding_ids)).encode("ascii")).decode("ascii")
		return f"Finding IDs (base64 JSON): {encoded_ids}\n\n{marker}"
	return marker


def _followup_body(branch: str = "ai/issue-7", finding_id: str = "F-1") -> str:
	return f"<!-- ai:security-finding:{finding_id} -->\n- Integration branch: `{branch}`"


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


def _run(tmp_path: Path, mode: str, pr: dict | None = None, comments: list | None = None, env: dict | None = None, skip: bool = False, security_issues: list | str | None = None, branch_prs: list | str | None = None, head_prs: list | str | None = None, cwd: Path | None = None, real_git: bool = False):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	git = bin_dir / "git"
	if real_git:
		git.unlink(missing_ok=True)
	else:
		git.write_text(FAKE_GIT, encoding="utf-8")
		git.chmod(0o755)
	support = tmp_path / "support"
	support.mkdir(exist_ok=True)
	(support / "security_pass_skip.py").write_text(
		f"import json\nprint(json.dumps({{'skip': {skip}, 'label': None, 'reason': 'stub'}}))\n", encoding="utf-8"
	)
	(tmp_path / "pr.json").write_text(json.dumps(pr or _pr()), encoding="utf-8")
	(tmp_path / "comments.json").write_text(json.dumps(comments or []), encoding="utf-8")
	if security_issues is None:
		security_issues = [[{"number": number, "state": "open", "body": _followup_body(), "user": {"login": login}} for number, login in ((101, "owner"), (102, "github-actions[bot]"))]]
	(tmp_path / "security_issues.json").write_text(security_issues if isinstance(security_issues, str) else json.dumps(security_issues), encoding="utf-8")
	for name, payload in (("branch_prs.json", branch_prs), ("head_prs.json", head_prs)):
		payload = [] if payload is None else payload
		(tmp_path / name).write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
	log = tmp_path / "calls.log"
	log.write_text("", encoding="utf-8")
	output = tmp_path / "output"
	output.write_text("", encoding="utf-8")
	run_env = dict(
		os.environ,
		PATH=f"{bin_dir}:{os.environ['PATH']}",
		FAKE_GH_LOG=str(log),
		FAKE_GH_COMMENTS=str(tmp_path / "comments.json"),
		FAKE_GH_SECURITY_ISSUES=str(tmp_path / "security_issues.json"),
		FAKE_GH_PR=str(tmp_path / "pr.json"),
		FAKE_GH_BRANCH_PRS=str(tmp_path / "branch_prs.json"),
		FAKE_GH_HEAD_PRS=str(tmp_path / "head_prs.json"),
		FAKE_GIT_HEAD=HEAD,
		GITHUB_OUTPUT=str(output),
		REPOSITORY="o/r",
		PR_NUMBER="42",
		PR_PAYLOAD_FILE=str(tmp_path / "pr.json"),
		PR_ISSUE_COMMENTS_FILE=str(tmp_path / "comments.json"),
		LINKED_ISSUES_JSON='[{"number": 7}]',
		DEFAULT_BRANCH="main",
		SECURITY_PASS_AUDIT_BRANCH="ai/issue-7",
		SUPPORT_SCRIPTS_DIR=str(support),
		GITHUB_WORKSPACE=str(tmp_path),
	)
	for name in ("SINGLE_ISSUE_SECURITY_PASS_ENABLED", "MAX_SECURITY_PASS_CYCLES", "ORCH_INTEGRATION_BRANCH_PATTERN", "SECURITY_PASS_PENDING_STALE_HOURS", "SECURITY_PASS_FOLLOWUP_STALE_HOURS", "SECURITY_PASS_PENDING_MARKER_ATTEMPTS", "SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS", "SECURITY_PASS_AUTHOR_LOGIN_FALLBACK", "FAKE_GIT_ANCESTORS", "FAKE_GIT_SHALLOW", "FAKE_GIT_FETCH_FAIL", "FAKE_GIT_UNSHALLOW_MARKER", "FAKE_GIT_INCOMPLETE_ANCESTRY", "FAKE_GIT_DIFF_FILES", "GITHUB_ENV", "SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED", "SECURITY_PASS_DELTA_SINCE", "SECURITY_PASS_PRIOR_FINDINGS_FILE", "SECURITY_PASS_FINDINGS_FILE", "SECURITY_PASS_DELTA_SINCE_INPUT", "SECURITY_PASS_PRIOR_FINDINGS_INPUT", "SECURITY_PASS_AUDIT_CHECKOUT"):
		run_env.pop(name, None)
	for name in ("FAKE_GH_RUNS", "SECURITY_PASS_MAX_AUDIT_ATTEMPTS", "SECURITY_PASS_MAX_CONCURRENT_AUDITS", "SECURITY_PASS_PENDING_CANCEL_CHECK_MINUTES"):
		run_env.pop(name, None)
	for name in ("FAKE_GH_DISPATCH_ERRORS", "SECURITY_PASS_DISPATCH_RETRIES", "SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS"):
		run_env.pop(name, None)
	run_env["SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS"] = "0"
	run_env["SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS"] = "0"
	# The pass defaults to off; tests that exercise it opt in, and None unsets a name.
	run_env["SINGLE_ISSUE_SECURITY_PASS_ENABLED"] = "true"
	run_env.update(env or {})
	for name in [key for key, value in run_env.items() if value is None]:
		run_env.pop(name)
	result = subprocess.run(["bash", str(SCRIPT), mode], capture_output=True, text=True, env=run_env, check=False, cwd=str(cwd) if cwd else None)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, calls, output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
	"pr, env, skip, reason",
	[
		(_pr(), {"SINGLE_ISSUE_SECURITY_PASS_ENABLED": "false"}, False, "disabled"),
		(_pr(), {"SINGLE_ISSUE_SECURITY_PASS_ENABLED": None}, False, "disabled"),
		(_pr(), {"SINGLE_ISSUE_SECURITY_PASS_ENABLED": ""}, False, "disabled"),
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
	assert output == f"hold=true\nhold_reason={'audit_dispatched' if expected == 'dispatched' else 'awaiting_followups'}\n" and f"outcome={expected}" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls) is (last_status == "failed")


def test_pipeline_identity_failure_holds_instead_of_authorizing(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("clean", HEAD, 1))], env={"FAKE_GH_FAIL": "identity"})
	assert output == "hold=true\nhold_reason=markers_unverifiable\n" and "reason=markers_unverifiable" in result.stdout
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
	assert output == "hold=true\nhold_reason=awaiting_followups\n" and "reason=awaiting_followups" in result.stdout
	assert calls == [["api", "user", "--jq", '.login // ""'], ["api", "--paginate", "--slurp", "repos/o/r/issues?labels=ai:security&state=open&per_page=100"]]


def test_mixed_linked_issues_cannot_skip_security_audit(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", skip=True, env={"LINKED_ISSUES_JSON": '[{"number":7},{"number":99}]'})
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_first_clean_review_dispatches_the_audit(tmp_path: Path) -> None:
	stale = [_comment(_marker("clean", OLD, 1)), _comment(_marker("clean", HEAD, 1), association="NONE", login="someone")]
	result, calls, output = _run(tmp_path, "gate", comments=stale)
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=2" in result.stdout
	# A trusted clean result for an older head requests a delta re-audit (P5a).
	assert ["workflow", "run", "ai-security-audit.yml", "-R", "o/r", "--ref", "main", "-f", "ref=ai/issue-7", "-f", "pr_number=42", "-f", f"audit_diff_since={OLD}"] in calls
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted and posted[0][-1].endswith(_marker("pending", HEAD, 2))


def test_pending_marker_write_failure_fails_closed_with_distinct_reason(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "comment_write"})
	assert result.returncode == 1 and output == "hold=true\nhold_reason=pending_marker_failed\n"
	assert "reason=pending_marker_failed" in result.stdout and "::error::" in result.stdout
	assert "audit_dispatched" not in result.stdout and "audit_dispatched" not in output
	assert len([call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]) == 3
	assert calls.index(["workflow", "run", "ai-security-audit.yml", "-R", "o/r", "--ref", "main", "-f", "ref=ai/issue-7", "-f", "pr_number=42"]) < next(i for i, call in enumerate(calls) if call[:2] == ["api", "repos/o/r/issues/42/comments"])


def test_pending_marker_retry_recovers(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "comment_write_once"})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=audit_dispatched\n"
	assert len([call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]) == 2


@pytest.mark.parametrize("response_failure", ["comment_bad_json", "comment_missing_id", "comment_wrong_body"])
def test_unconfirmed_marker_response_is_not_audit_dispatched(tmp_path: Path, response_failure: str) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": response_failure})
	assert result.returncode == 1 and output == "hold=true\nhold_reason=pending_marker_failed\n"
	assert len([call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]) == 3


@pytest.mark.parametrize("attempts, expected", [("0", 3), ("abc", 3), ("2", 2), ("999999999999999999999", 3)])
def test_pending_marker_attempts_is_validated(tmp_path: Path, attempts: str, expected: int) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "comment_write", "SECURITY_PASS_PENDING_MARKER_ATTEMPTS": attempts})
	assert result.returncode == 1 and output == "hold=true\nhold_reason=pending_marker_failed\n"
	assert f"after {expected} attempt(s)" in result.stdout
	assert len([call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]) == expected


@pytest.mark.parametrize("status", ["pending", "findings"])
def test_a_running_audit_or_open_followups_hold_without_a_new_dispatch(tmp_path: Path, status: str) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker(status, HEAD, 1))])
	assert output == f"hold=true\nhold_reason={'audit_pending' if status == 'pending' else 'awaiting_followups'}\n"
	assert calls == [["api", "user", "--jq", '.login // ""']] + (
		[["api", "--paginate", "--slurp", "repos/o/r/issues?labels=ai:security&state=open&per_page=100"]] if status == "findings" else []
	)


@pytest.mark.parametrize("issues", [
	[[]],
	[[{"state": "closed", "body": "- Integration branch: `ai/issue-7`", "user": {"login": "owner"}}]],
	[[{"state": "open", "body": "- Integration branch: `other`", "user": {"login": "owner"}}]],
	[[{"state": "open", "body": "- Integration branch: `ai/issue-7`", "user": {"login": "owner"}, "pull_request": {"url": "pr"}}]],
	[[{"state": "open", "body": "- Integration branch: `ai/issue-7`", "user": {"login": "other"}}]],
	[[{"state": "open", "body": "- Integration branch: `ai/issue-7`", "user": {"login": "owner"}}]],
])
def test_missing_pipeline_followups_page_without_mutating(tmp_path: Path, issues: list) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 1))], security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"
	assert "outcome=hold reason=followups_missing" in result.stdout
	assert len([call for call in calls if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call]) == 1
	assert not any(call[:2] in (["workflow", "run"], ["api", "repos/o/r/issues/42/comments"], ["api", "repos/o/r/issues/42/labels"]) for call in calls)


@pytest.mark.parametrize("line", [
	"- Integration branch: ai/issue-7", "- Integration branch: `ai/issue-7`",
	"Integration branch: ai/issue-7", "- **Integration branch:** `ai/issue-7`",
])
def test_matching_open_pipeline_followups_keep_waiting(tmp_path: Path, line: str) -> None:
	issues = [[{"state": "open", "body": "<!-- ai:security-finding:F-1 -->\nOther text\n" + line, "user": {"login": "owner"}}]]
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 1))], security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=awaiting_followups\n"
	assert len([call for call in calls if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call]) == 1


def test_old_branch_followup_does_not_silence_new_findings(tmp_path: Path) -> None:
	issues = [[{"state": "open", "body": _followup_body(finding_id="F-old"), "user": {"login": "owner"}}]]
	result, _, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 2))], security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"


def test_latest_trusted_result_controls_followup_ids(tmp_path: Path) -> None:
	issues = [[{"state": "open", "body": _followup_body(finding_id="F-old"), "user": {"login": "owner"}}]]
	comments = [
		_comment(_marker("findings", HEAD, 1, ("F-old",)), comment_id=1, age_hours=1),
		_comment(_marker("findings", HEAD, 2), comment_id=2),
		_comment(_marker("findings", HEAD, 2, ("F-old",)), comment_id=3, login="other"),
	]
	result, _, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"


def test_every_current_finding_needs_a_matching_open_followup(tmp_path: Path) -> None:
	issues = [[{"state": "open", "body": _followup_body(finding_id="F-1"), "user": {"login": "owner"}}]]
	comments = [_comment(_marker("findings", HEAD, 2, ("F-1", "F-2")))]
	result, calls, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"
	issues.append([{"state": "open", "body": _followup_body(finding_id="F-2"), "user": {"login": "owner"}}])
	result, calls, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=awaiting_followups\n"
	assert len([call for call in calls if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call]) == 1


@pytest.mark.parametrize("body", [
	_marker("findings", HEAD, 1, ()),
	"Finding IDs (base64 JSON): invalid!\n\n" + _marker("findings", HEAD, 1, ()),
	"Finding IDs (base64 JSON): W10=\n\n" + _marker("findings", HEAD, 1, ()),
	_marker("findings", HEAD, 1, ("F-1\nspoof",)),
])
def test_legacy_or_invalid_findings_ids_page_instead_of_silencing(tmp_path: Path, body: str) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(body)])
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_unverifiable\n"
	assert not any("repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call for call in calls)


def test_followups_across_paginated_pages(tmp_path: Path) -> None:
	issues = [[], [{"state": "open", "body": _followup_body(), "user": {"login": "owner"}}]]
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 1))], security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=awaiting_followups\n"
	assert len([call for call in calls if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call]) == 1


@pytest.mark.parametrize("issues, fail", [("{}", ""), ("[{}]", ""), ("", ""), ("[[]]", "followups")])
def test_followup_listing_failure_or_bad_shape_pages(tmp_path: Path, issues: str, fail: str) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 1))], security_issues=issues, env={"FAKE_GH_FAIL": fail})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_unverifiable\n"
	assert "outcome=hold reason=followups_unverifiable" in result.stdout
	assert len([call for call in calls if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call]) == 1


@pytest.mark.parametrize("age_hours, override, reason", [
	(23, "", "awaiting_followups"), (25, "", "followups_stalled"),
	(3, "2", "followups_stalled"), (3, "0", "awaiting_followups"),
	(25, "abc", "followups_stalled"),
])
def test_followup_stale_limit(tmp_path: Path, age_hours: int, override: str, reason: str) -> None:
	result, _, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 1), age_hours=age_hours)], env={"SECURITY_PASS_FOLLOWUP_STALE_HOURS": override})
	assert result.returncode == 0 and output == f"hold=true\nhold_reason={reason}\n"


def test_bad_findings_timestamp_pages(tmp_path: Path) -> None:
	comment = _comment(_marker("findings", HEAD, 1))
	comment["created_at"] = "bad-time"
	result, _, output = _run(tmp_path, "gate", comments=[comment])
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_stalled\n"


def test_status_findings_never_lists_followups(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "status", comments=[_comment(_marker("findings", HEAD, 1))], env={"FAKE_GH_FAIL": "followups"})
	assert result.returncode == 0 and output == ""
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=findings" in result.stdout
	assert not any("repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call for call in calls)


def test_branch_name_is_matched_literally(tmp_path: Path) -> None:
	branch_ref = "ai/issue-7.x+"
	issues = [[{"state": "open", "body": _followup_body("ai/issue-7axxx"), "user": {"login": "owner"}}]]
	comments = [_comment(_marker("findings", HEAD, 1))]
	result, _, output = _run(tmp_path, "gate", pr=_pr(head_ref=branch_ref), comments=comments, security_issues=issues)
	assert output == "hold=true\nhold_reason=followups_missing\n"
	issues[0][0]["body"] = _followup_body(branch_ref)
	result, _, output = _run(tmp_path, "gate", pr=_pr(head_ref=branch_ref), comments=comments, security_issues=issues)
	assert output == "hold=true\nhold_reason=awaiting_followups\n"


def test_a_stale_pending_audit_is_dispatched_again(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 1), age_hours=7)])
	assert "outcome=dispatched cycle=2" in result.stdout


def test_pending_timeout_uses_operator_override(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 1), age_hours=3)], env={"SECURITY_PASS_PENDING_STALE_HOURS": "2"})
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=2" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_cycles_exhausted_labels_the_pr_for_the_unblock_judge(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD if n == 5 else f"{n:040x}", n), comment_id=n) for n in range(1, 6)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n" and "reason=cycles_exhausted" in result.stdout
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_last_cycle_findings_escalate_on_the_same_head(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 5))]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n" and "reason=cycles_exhausted" in result.stdout
	assert any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_failed_exhaustion_label_fails_closed_for_workflow_recovery(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", HEAD, 5))], env={"FAKE_GH_FAIL": "label"})
	assert result.returncode == 1 and output == "hold=true\nhold_reason=label_write_failed\n"
	assert "reason=label_write_failed" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_two_digit_cycle_limit_is_counted_numerically(tmp_path: Path) -> None:
	comments = [_comment(_marker("failed", OLD, 9), comment_id=1), _comment(_marker("failed", OLD, 10), comment_id=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"MAX_SECURITY_PASS_CYCLES": "10"})
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=11" in result.stdout
	assert "reason=exhausted_retry" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_failed_cycles_do_not_authorize_exhaustion_merge(tmp_path: Path) -> None:
	comments = [
		_comment(_marker(status, HEAD, n), comment_id=2 * n - (status == "pending"), age_hours=8 - n)
		for n in range(1, 6) for status in ("pending", "failed")
	]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "reason=exhausted_retry" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "retry audit" in posted[-1][-1] and "cycle 6 of 5" not in posted[-1][-1]
	assert posted[-1][-1].endswith(_marker("pending", HEAD, 6))


def test_two_failed_head_audits_stop_without_judge_exhaustion(tmp_path: Path) -> None:
	comments = [
		_comment(_marker("failed", HEAD, 5), comment_id=1, age_hours=4),
		_comment(_marker("pending", HEAD, 6), comment_id=2, age_hours=3),
		_comment(_marker("failed", HEAD, 6), comment_id=3, age_hours=2),
		_comment(_marker("pending", HEAD, 7), comment_id=4, age_hours=1),
		_comment(_marker("failed", HEAD, 7), comment_id=5),
	]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n" and "reason=exhausted_without_completed_audit cycle=7 head_attempts=2" in result.stdout
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "No completed audit exists" in posted[-1][-1]


def test_unaudited_exhaustion_label_already_present_does_not_recomment(tmp_path: Path) -> None:
	comments = [_comment(_marker("pending", HEAD, 6), age_hours=7)]
	result, calls, output = _run(
		tmp_path, "gate", pr=_pr(labels=("ai:security-pass-failed",)), comments=comments,
		env={"SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS": "1"},
	)
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n" and "reason=exhausted_without_completed_audit" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_stale_pending_at_cap_does_not_consume_post_cap_budget(tmp_path: Path) -> None:
	# An audit that never reported does not use a cycle: cycle 5 is still
	# available, and the marker numbers the attempt (6).
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 5), age_hours=7)])
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "reason=exhausted_retry" not in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "(cycle 5 of 5)" in posted[-1][-1] and posted[-1][-1].endswith(_marker("pending", HEAD, 6))
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_duplicate_pending_markers_count_one_exhausted_attempt(tmp_path: Path) -> None:
	comments = [_comment(_marker("failed", OLD, 5), comment_id=1)]
	comments.extend(_comment(_marker("pending", HEAD, 6), comment_id=n, age_hours=7) for n in (2, 3))
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=audit_dispatched\n"
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "head attempt 2 of 2" in posted[-1][-1]


@pytest.mark.parametrize("limit, should_retry", [("1", False), ("not-a-number", True), ("0", True), ("3", True)])
def test_exhausted_head_attempt_limit_is_validated(tmp_path: Path, limit: str, should_retry: bool) -> None:
	comments = [_comment(_marker("failed", OLD, 5)), _comment(_marker("pending", HEAD, 6), comment_id=2, age_hours=7)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS": limit})
	assert output == f"hold=true\nhold_reason={'audit_dispatched' if should_retry else 'exhausted_without_completed_audit'}\n" and ("reason=exhausted_retry" in result.stdout) is should_retry
	assert any(call[:2] == ["workflow", "run"] for call in calls) is should_retry


def test_findings_on_old_head_retry_after_head_moves(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("findings", OLD, 5))])
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "reason=exhausted_retry" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_later_failed_audit_preserves_completed_findings_on_head(tmp_path: Path) -> None:
	comments = [
		_comment(_marker("findings", HEAD, 5), comment_id=1, age_hours=1),
		_comment(_marker("failed", HEAD, 6), comment_id=2),
	]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n" and "reason=cycles_exhausted" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	status, _, _ = _run(tmp_path, "status", comments=comments)
	assert f"SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD={HEAD}" in status.stdout


def test_stale_pending_after_completed_findings_does_not_erase_audit(tmp_path: Path) -> None:
	comments = [
		_comment(_marker("findings", HEAD, 5), comment_id=1, age_hours=9),
		_comment(_marker("pending", HEAD, 6), comment_id=2, age_hours=7),
	]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n" and "reason=cycles_exhausted" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_a_failed_dispatch_holds_and_records_a_failed_cycle(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "dispatch"})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=dispatch_failed\n"
	assert "outcome=hold reason=dispatch_failed" in result.stdout and "cycle=1" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("failed", HEAD, 1))
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_a_failed_dispatch_without_a_marker_still_holds(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_FAIL": "dispatch,comment_write"})
	assert result.returncode == 1 and output == "hold=true\nhold_reason=dispatch_failed\n"
	assert "::error::Could not post the failed security-pass marker" in result.stdout
	assert any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_a_recorded_dispatch_failure_is_retried_on_the_next_review(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("failed", HEAD, 1))])
	assert result.returncode == 0 and output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "outcome=dispatched cycle=2" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_repeated_dispatch_failures_record_each_cycle(tmp_path: Path) -> None:
	comments = [_comment(_marker("failed", HEAD, cycle), comment_id=cycle) for cycle in range(1, 5)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_FAIL": "dispatch"})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=dispatch_failed\n"
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("failed", HEAD, 5))
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_exhausted_retry_dispatch_failure_holds(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("failed", HEAD, 5))], env={"FAKE_GH_FAIL": "dispatch"})
	assert output == "hold=true\nhold_reason=dispatch_failed\n" and "reason=dispatch_failed_exhausted" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("failed", HEAD, 6))


def test_exhausted_retry_dispatch_failure_without_a_marker_fails_closed(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("failed", HEAD, 5))], env={"FAKE_GH_FAIL": "dispatch,comment_write"})
	assert result.returncode == 1 and output == "hold=true\nhold_reason=dispatch_failed\n"
	assert "reason=dispatch_failed_exhausted" in result.stdout and "the head attempt is not recorded" in result.stdout


def test_repeated_exhausted_dispatch_failures_reach_the_label(tmp_path: Path) -> None:
	comments = [_comment(_marker("failed", HEAD, cycle), comment_id=cycle) for cycle in range(1, 8)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_FAIL": "dispatch"})
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n" and "head_attempts=2" in result.stdout
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def _extension(head: str) -> str:
	return f"<!-- ai:single-issue-security-pass-extension:v1 head={head} -->"


def test_judge_extension_marker_grants_one_more_audit_cycle(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", f"{n:040x}", n), comment_id=n) for n in range(1, 6)]
	comments.append(_comment("Judge pushed a fix.\n\n" + _extension(OLD), comment_id=9))
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GIT_ANCESTORS": OLD})
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=6" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "(cycle 6 of 6)" in posted[-1][-1]


def test_duplicate_extension_markers_grant_only_one_cycle(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", f"{n:040x}", n), comment_id=n) for n in range(1, 6)]
	comments.extend([_comment(_extension(OLD), comment_id=9), _comment(_extension(OLD), comment_id=10)])
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GIT_ANCESTORS": OLD})
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=6" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "(cycle 6 of 6)" in posted[-1][-1]


def test_unpushed_extension_does_not_hold_exhausted_pass(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 5)), _comment(_extension(OLD), comment_id=9)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n"
	assert "reason=cycles_exhausted" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_extension_checkout_mismatch_holds_without_granting_or_merging(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 5)), _comment(_extension(OLD), comment_id=9)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GIT_HEAD": OLD, "FAKE_GIT_ANCESTORS": OLD})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=extensions_unverifiable\n"
	assert "reason=extensions_unverifiable" in result.stdout
	assert not any(call[:2] in (["workflow", "run"], ["api", "repos/o/r/issues/42/labels"]) for call in calls)


@pytest.mark.parametrize("mode", ["gate", "status"])
def test_unpushed_commit_on_top_of_the_pr_head_holds_as_head_unpushed(tmp_path: Path, mode: str) -> None:
	# PR #9283 (run 43161924339): the PR head HEAD was audited clean, then the
	# conflict resolver committed [ai-merge-resolve] locally (OLD here) with
	# the push deferred until after the review-blocked judge. The merge must
	# hold (the new head is unaudited), but as head_unpushed, which the judge
	# alert treats as self-clearing, not extensions_unverifiable. Only an
	# extension marker needs the checkout at the PR head (no marker: #9011).
	comments = [_comment(_marker("clean", HEAD, 6)), _comment(_extension(OLD), comment_id=9)]
	result, calls, output = _run(tmp_path, mode, comments=comments, env={"FAKE_GIT_HEAD": OLD, "FAKE_GIT_ANCESTORS": HEAD})
	assert result.returncode == 0, result.stderr + result.stdout
	assert "extensions_unverifiable" not in result.stdout
	assert not any(call[:2] in (["workflow", "run"], ["api", "repos/o/r/issues/42/labels"], ["api", "repos/o/r/issues/42/comments"]) for call in calls)
	if mode == "gate":
		assert output == "hold=true\nhold_reason=head_unpushed\n"
		assert "outcome=hold reason=head_unpushed" in result.stdout and f"checkout={OLD}" in result.stdout
	else:
		assert output == "" and "SINGLE_ISSUE_SECURITY_PASS_STATE=unverifiable" in result.stdout


def test_head_unpushed_is_a_self_clearing_judge_hold() -> None:
	assert "security_hold_awaiting_followups|security_hold_head_unpushed)" in REVIEW.read_text(encoding="utf-8")


def test_unpushed_local_commit_without_extensions_does_not_hold(tmp_path: Path) -> None:
	# PRs #9011 / #9134: the review job's deferred [ai-merge-resolve] commit
	# moved the checkout past the PR head before the judge's merge gate ran.
	# With no extension marker there is nothing to verify against the checkout.
	# FAKE_GIT_ANCESTORS: the deferred commit really descends from the PR head,
	# so a head_unpushed hold placed ahead of the marker check would fail here.
	comments = [_comment("ok\n" + _marker("clean", HEAD, 1))]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GIT_HEAD": OLD, "FAKE_GIT_ANCESTORS": HEAD})
	assert output == "hold=false\n" and "outcome=clean" in result.stdout
	assert "extensions_unverifiable" not in result.stdout and "head_unpushed" not in result.stdout
	result, calls, output = _run(tmp_path, "status", comments=comments, env={"FAKE_GIT_HEAD": OLD, "FAKE_GIT_ANCESTORS": HEAD})
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=unverifiable" not in result.stdout


def test_missing_extension_in_shallow_checkout_holds_if_history_unavailable(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 5)), _comment(_extension(OLD), comment_id=9)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GIT_SHALLOW": "true", "FAKE_GIT_FETCH_FAIL": "true"})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=extensions_unverifiable\n"
	assert "reason=extensions_unverifiable" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_status_does_not_fetch_missing_shallow_extension(tmp_path: Path) -> None:
	# Keep the existing identifier; a failed history fetch still leaves status
	# unverifiable without publishing an audit decision or judge merge.
	comments = [_comment(_marker("findings", HEAD, 5)), _comment(_extension(OLD), comment_id=9)]
	result, calls, output = _run(tmp_path, "status", comments=comments, env={"FAKE_GIT_SHALLOW": "true", "FAKE_GIT_FETCH_FAIL": "true"})
	assert result.returncode == 0 and output == ""
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=unverifiable" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_status_unshallows_before_classifying_exhaustion(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 6)), _comment(_extension(OLD), comment_id=9)]
	marker_file = tmp_path / "unshallowed"
	result, calls, output = _run(tmp_path, "status", comments=comments, env={
		"FAKE_GIT_SHALLOW": "true", "FAKE_GIT_ANCESTORS": OLD,
		"FAKE_GIT_INCOMPLETE_ANCESTRY": "true", "FAKE_GIT_UNSHALLOW_MARKER": str(marker_file),
	})
	assert result.returncode == 0 and output == "" and marker_file.exists()
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=exhausted" in result.stdout
	assert not any(call[:2] in (["workflow", "run"], ["api", "repos/o/r/issues/42/labels"], ["api", "repos/o/r/issues/42/comments"]) for call in calls)


def test_shallow_existing_extension_unshallows_before_exhaustion(tmp_path: Path) -> None:
	# Cycle 5 audited the pre-fix head; the extension fix is on the new HEAD.
	comments = [_comment(_marker("findings", f"{5:040x}", 5)), _comment(_extension(OLD), comment_id=9)]
	marker_file = tmp_path / "unshallowed"
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={
		"FAKE_GIT_SHALLOW": "true", "FAKE_GIT_ANCESTORS": OLD,
		"FAKE_GIT_INCOMPLETE_ANCESTRY": "true", "FAKE_GIT_UNSHALLOW_MARKER": str(marker_file),
	})
	assert marker_file.exists() and output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "outcome=dispatched cycle=6" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_extension_does_not_reaudit_head_with_findings(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 5)), _comment(_extension(OLD), comment_id=9)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GIT_ANCESTORS": OLD})
	assert output == "hold=true\nhold_reason=awaiting_followups\n" and "reason=awaiting_followups" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize("mode", ["gate", "status", "report"])
def test_incomplete_shallow_extension_never_grants_or_denies_a_cycle(tmp_path: Path, mode: str) -> None:
	comments = [_comment(_marker("pending" if mode == "report" else "findings", HEAD, 5)), _comment(_extension(OLD), comment_id=9)]
	marker_file = tmp_path / "unshallowed"
	env = {
		"FAKE_GIT_SHALLOW": "true", "FAKE_GIT_ANCESTORS": OLD,
		"FAKE_GIT_INCOMPLETE_ANCESTRY": "true", "FAKE_GIT_UNSHALLOW_MARKER": str(marker_file),
		"FAKE_GIT_FETCH_FAIL": "true",
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0",
	}
	result, calls, output = _run(tmp_path, mode, comments=comments, env=env)
	assert result.returncode == 0 and "reason=extensions_unverifiable" in result.stdout
	assert not marker_file.exists() and "exhausted=true" not in output
	assert not any(call[:2] in (["workflow", "run"], ["api", "repos/o/r/issues/42/labels"], ["api", "repos/o/r/issues/42/comments"]) for call in calls)
	if mode == "status":
		assert "SINGLE_ISSUE_SECURITY_PASS_STATE=unverifiable" in result.stdout
		assert output == ""
	else:
		assert output == ("hold=true\nhold_reason=extensions_unverifiable\n" if mode == "gate" else "")


def test_extension_marker_from_another_author_is_ignored(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD if n == 5 else f"{n:040x}", n), comment_id=n) for n in range(1, 6)]
	comments.append(_comment(_extension(OLD), login="other[bot]", comment_id=9))
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n"


@pytest.mark.parametrize(
	"comments, env, state",
	[
		([], {"SINGLE_ISSUE_SECURITY_PASS_ENABLED": "false"}, "skip"),
		([_comment(_marker("clean", HEAD, 1))], {}, "clean"),
		([_comment(_marker("pending", HEAD, 1))], {}, "pending"),
		([_comment(_marker("findings", HEAD, 1))], {}, "findings"),
		([], {}, "needs_audit"),
		([_comment(_marker("findings", HEAD, 5))], {}, "exhausted"),
	],
)
def test_status_mode_reports_state_without_side_effects(tmp_path: Path, comments: list, env: dict, state: str) -> None:
	result, calls, output = _run(tmp_path, "status", comments=comments, env=env)
	assert result.returncode == 0
	assert f"SINGLE_ISSUE_SECURITY_PASS_STATE={state}" in result.stdout.splitlines()
	assert output == ""
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert not any(call[:2] in (["api", "repos/o/r/issues/42/labels"], ["api", "repos/o/r/issues/42/comments"]) for call in calls)


def test_status_only_cannot_be_forced_on_the_gate_from_the_environment(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("pending", HEAD, 1))], env={"SINGLE_PASS_STATUS_ONLY": "true"})
	assert output == "hold=true\nhold_reason=audit_pending\n"
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=" not in result.stdout


def test_status_attests_only_completed_current_head(tmp_path: Path) -> None:
	for comments, state, audited in (
		([_comment(_marker("findings", HEAD, 5))], "exhausted", True),
		([_comment(_marker("failed", HEAD, 5)), _comment(_marker("pending", HEAD, 4), comment_id=2, age_hours=7)], "needs_audit", False),
		([_comment(_marker("pending", HEAD, 6), comment_id=1, age_hours=9), _comment(_marker("failed", HEAD, 6), comment_id=2, age_hours=8), _comment(_marker("pending", HEAD, 7), comment_id=3, age_hours=7)], "exhausted_unaudited", False),
	):
		result, calls, output = _run(tmp_path, "status", comments=comments)
		assert result.returncode == 0 and output == ""
		assert f"SINGLE_ISSUE_SECURITY_PASS_STATE={state}" in result.stdout.splitlines()
		assert (f"SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD={HEAD}" in result.stdout.splitlines()) is audited
		assert not any(call[:2] in (["workflow", "run"], ["api", "repos/o/r/issues/42/labels"]) for call in calls)


@pytest.mark.parametrize("comments", [[], [_comment(_marker("failed", HEAD, 1))]])
def test_failed_dispatch_holds_unaudited_head(tmp_path: Path, comments: list) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_FAIL": "dispatch"})
	assert output == "hold=true\nhold_reason=dispatch_failed\n" and "outcome=hold reason=dispatch_failed" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def _report(tmp_path: Path, outcome: str, findings: str, cycle: int = 3, extra_comments: list | None = None):
	encoded_ids = base64.b64encode(json.dumps([f"F-{n}" for n in range(1, int(findings or 0) + 1)]).encode("ascii")).decode("ascii")
	env = {
		"SECURITY_PASS_PR_NUMBER": "42",
		"SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": outcome,
		"SECURITY_PASS_FINDINGS": findings,
		"SECURITY_PASS_FINDING_IDS_B64": encoded_ids,
	}
	comments = [_comment(_marker("pending", HEAD, cycle))] + list(extra_comments or [])
	return _run(tmp_path, "report", comments=comments, env=env)


@pytest.mark.parametrize(
	"outcome, findings, status, redispatch",
	[("success", "0", "clean", True), ("success", "2", "findings", False), ("failure", "", "failed", True)],
)
def test_report_posts_the_result_and_reruns_the_review(tmp_path: Path, outcome: str, findings: str, status: str, redispatch: bool) -> None:
	result, calls, _ = _report(tmp_path, outcome, findings)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker(status, HEAD, 3, tuple(f"F-{n}" for n in range(1, int(findings or 0) + 1))))
	review = ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"]
	assert (review in calls) is redispatch


def test_report_without_matching_ids_keeps_findings_alertable(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 3))], env={
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "2",
		"SECURITY_PASS_FINDING_IDS_B64": base64.b64encode(b'["F-1"]').decode("ascii"),
	})
	assert result.returncode == 0
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker("findings", HEAD, 3, ()))
	assert "Finding IDs (base64 JSON):" not in posted[-1][-1]
	assert ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"] in calls


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
	assert sum("--paginate" in call and "repos/o/r/issues/42/comments?per_page=100" in call for call in calls) == 2


def test_last_cycle_findings_redispatch_for_exhaustion(tmp_path: Path) -> None:
	result, calls, _ = _report(tmp_path, "success", "2", cycle=5)
	assert "outcome=findings cycle=5" in result.stdout
	assert ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"] in calls


def test_report_counts_judge_extensions_before_escalating(tmp_path: Path) -> None:
	# Valid finding IDs keep the report off the missing-IDs re-dispatch, so
	# only the extension-aware cycle cap decides.
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 5)), _comment(_extension(OLD), comment_id=99)], env={
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD, "SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "2", "FAKE_GIT_ANCESTORS": OLD,
		"SECURITY_PASS_FINDING_IDS_B64": base64.b64encode(b'["F-1", "F-2"]').decode("ascii"),
	})
	assert "outcome=findings cycle=5" in result.stdout
	assert "Audit finding IDs are missing or invalid" not in result.stdout
	assert ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"] not in calls


def test_report_ignores_unpushed_extension_when_escalating(tmp_path: Path) -> None:
	result, calls, _ = _report(tmp_path, "success", "2", cycle=5, extra_comments=[_comment(_extension(OLD), comment_id=99)])
	assert "outcome=findings cycle=5" in result.stdout
	assert ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"] in calls


def test_report_skips_publication_if_audit_checkout_does_not_match(tmp_path: Path) -> None:
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 5))], env={
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD, "SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": "0", "FAKE_GIT_HEAD": OLD,
	})
	assert result.returncode == 0 and "reason=extensions_unverifiable" in result.stdout
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


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
	# review_autofix.yml step bodies moved to scripts/ are read inlined again.
	text = expanded_review_autofix_text() if path == REVIEW else path.read_text(encoding="utf-8")
	workflow = yaml.safe_load(text)
	return {step.get("name", ""): step for step in workflow["jobs"][job]["steps"]}


def test_review_wiring() -> None:
	steps = _steps(REVIEW, "codex-agent")
	names = list(steps)
	assert names.index("Single-issue security pass") + 1 == names.index("Enable auto-merge on PR")
	gate = steps["Single-issue security pass"]
	assert gate["id"] == "single_issue_security_pass"
	assert gate["env"]["SECURITY_PASS_PENDING_STALE_HOURS"] == "${{ vars.SECURITY_PASS_PENDING_STALE_HOURS || '6' }}"
	assert gate["env"]["SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS"] == "${{ vars.SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS || '2' }}"
	assert gate["env"]["SECURITY_PASS_PENDING_MARKER_ATTEMPTS"] == "${{ vars.SECURITY_PASS_PENDING_MARKER_ATTEMPTS || '3' }}"
	assert gate["env"]["SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS"] == "${{ vars.SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS || '5' }}"
	assert gate["env"]["SECURITY_PASS_FOLLOWUP_STALE_HOURS"] == "${{ vars.SECURITY_PASS_FOLLOWUP_STALE_HOURS || '24' }}"
	assert _steps(REVIEW, "codex-agent")["Review-blocked judge decision"]["env"]["SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS"] == gate["env"]["SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS"]
	# Issue #9942: the lost-attempt budget reaches the gate and status mode.
	assert gate["env"]["SECURITY_PASS_LOST_AUDIT_RETRIES"] == "${{ vars.SECURITY_PASS_LOST_AUDIT_RETRIES || '2' }}"
	assert _steps(REVIEW, "codex-agent")["Review-blocked judge decision"]["env"]["SECURITY_PASS_LOST_AUDIT_RETRIES"] == gate["env"]["SECURITY_PASS_LOST_AUDIT_RETRIES"]
	assert _steps(REVIEW, "codex-agent")["Review-blocked judge decision"]["env"]["SECURITY_PASS_PENDING_MARKER_ATTEMPTS"] == gate["env"]["SECURITY_PASS_PENDING_MARKER_ATTEMPTS"]
	assert _steps(REVIEW, "codex-agent")["Review-blocked judge decision"]["env"]["SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS"] == gate["env"]["SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS"]
	assert _steps(REVIEW, "codex-agent")["Review-blocked judge decision"]["env"]["SECURITY_PASS_FOLLOWUP_STALE_HOURS"] == gate["env"]["SECURITY_PASS_FOLLOWUP_STALE_HOURS"]
	# Issue #10104: the in-run dispatch retry settings reach every gate step.
	assert gate["env"]["SECURITY_PASS_DISPATCH_RETRIES"] == "${{ vars.SECURITY_PASS_DISPATCH_RETRIES || '2' }}"
	assert gate["env"]["SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS"] == "${{ vars.SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS || '20' }}"
	for name in ("SECURITY_PASS_DISPATCH_RETRIES", "SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS"):
		assert _steps(REVIEW, "codex-agent")["Review-blocked judge decision"]["env"][name] == gate["env"][name]
		assert _steps(REVIEW, "deterministic-skip-merge")["Single-issue security pass (deterministic skip)"]["env"][name] == gate["env"][name]
	assert gate["env"]["SECURITY_PASS_AUTHOR_LOGIN_FALLBACK"] == "${{ secrets.GH_PAT == '' && 'github-actions[bot]' || '' }}"
	assert gate["if"] == steps["Enable auto-merge on PR"]["if"].replace(" && steps.single_issue_security_pass.outputs.hold != 'true'", "")
	for name in ("Enable auto-merge on PR", "Mark linked issues ready to merge"):
		assert steps[name]["if"].endswith("&& steps.single_issue_security_pass.outputs.hold != 'true'")
	assert "review_single_issue_security_pass.sh" in STAGE.read_text(encoding="utf-8")
	gate = _steps(REVIEW, "gate")["Evaluate review gate"]
	assert gate["env"]["SINGLE_ISSUE_SECURITY_PASS_ENABLED"] == "${{ vars.SINGLE_ISSUE_SECURITY_PASS_ENABLED || 'false' }}"
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
	# Issue #7370: a Claude capacity wait skips the report, so the pass stays pending.
	assert report["if"] == "always() && inputs.pr_number != '' && env.AUDIT_DATA_SHA != '' && env.CLAUDE_CAPACITY_WAIT != 'true'"
	assert "review_single_issue_security_pass.sh\" report" in report["run"]
	assert "^security-audit: .*skipping" not in report["run"]
	assert 'echo "AUDIT_DEFAULT_BRANCH=${AUDIT_DEFAULT_BRANCH}"' in resolve["run"]
	assert report["env"]["DEFAULT_BRANCH"] == "${{ env.AUDIT_DEFAULT_BRANCH }}"
	assert report["env"]["SECURITY_PASS_AUDIT_BRANCH"] == "${{ env.AUDIT_BRANCH }}"
	assert report["env"]["SECURITY_PASS_AUTHOR_LOGIN_FALLBACK"] == "${{ secrets.GH_PAT == '' && 'github-actions[bot]' || '' }}"
	assert "SECURITY_PASS_FINDING_IDS_B64=" in report["run"]
	assert "finding_ids_b64=" in report["run"]
	assert "export SECURITY_PASS_HEAD_SHA SECURITY_PASS_FINDINGS SECURITY_PASS_FINDING_IDS_B64" in report["run"]
	assert "finding_ids_b64=${SURVIVING_FINDING_IDS_B64}" in (ROOT / "scripts" / "security_audit.sh").read_text(encoding="utf-8")
	assert workflow["permissions"]["actions"] == "write"
	assert workflow["permissions"]["pull-requests"] == "read"
	template = AUDIT_TEMPLATE.read_text(encoding="utf-8")
	assert "pr_number: ${{ inputs.pr_number || '' }}" in template
	assert "actions: write" in template
	assert "pull-requests: read" in template


# ----- Skip-path security pass (#8333) -----
SKIP_STEP = ROOT / "scripts" / "review_autofix_step_deterministic_skip_security_pass.sh"
FRESHNESS = ROOT / "scripts" / "review_freshness_ci_failed.sh"
HEAD_GATE = ROOT / "scripts" / "review_head_gate.sh"
GH_HELPERS = ROOT / "scripts" / "gh_helpers.sh"
REVIEW_WORKFLOW_NAMES = {"ai-review.yml", "internal-review.yml", "review_autofix.yml"}


def _run_skip_step(tmp_path: Path, comments: list | None = None, pr: dict | None = None, env: dict | None = None):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	for name, body in (("gh", FAKE_GH), ("git", FAKE_GIT)):
		(bin_dir / name).write_text(body, encoding="utf-8")
		(bin_dir / name).chmod(0o755)
	support = tmp_path / "freshness-src" / "scripts"
	support.mkdir(parents=True, exist_ok=True)
	for src in (SCRIPT, FRESHNESS, HEAD_GATE, GH_HELPERS):
		(support / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
	(support / "security_pass_skip.py").write_text("import json\nprint(json.dumps({'skip': False}))\n", encoding="utf-8")
	checkout = tmp_path / "pr-head"
	checkout.mkdir(exist_ok=True)
	(tmp_path / "comments.json").write_text(json.dumps(comments or []), encoding="utf-8")
	(tmp_path / "security_issues.json").write_text("[[]]", encoding="utf-8")
	log = tmp_path / "calls.log"
	log.write_text("", encoding="utf-8")
	output = tmp_path / "output"
	output.write_text("", encoding="utf-8")
	run_env = {key: value for key, value in os.environ.items() if not key.startswith(("FAKE_", "SECURITY_PASS_", "GITHUB_"))}
	run_env.update(
		PATH=f"{bin_dir}:{os.environ['PATH']}",
		FAKE_GH_LOG=str(log),
		FAKE_GH_COMMENTS=str(tmp_path / "comments.json"),
		FAKE_GH_SECURITY_ISSUES=str(tmp_path / "security_issues.json"),
		FAKE_GIT_HEAD=HEAD,
		GITHUB_OUTPUT=str(output),
		RUNNER_TEMP=str(tmp_path),
		REPOSITORY="o/r",
		PR_NUMBER="42",
		PR_HEAD_SHA=HEAD,
		DEFAULT_BRANCH="main",
		SINGLE_ISSUE_SECURITY_PASS_ENABLED="true",
		SECURITY_PASS_PENDING_MARKER_RETRY_DELAY_SECS="0",
		SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS="0",
		DET_SKIP_SECURITY_PASS_RETRY_SLEEP_SECS="0",
		DET_SKIP_SECURITY_PASS_PR_JSON=json.dumps(pr or _pr()),
		DET_SKIP_SECURITY_PASS_SUPPORT_DIR=str(support),
		DET_SKIP_SECURITY_PASS_CHECKOUT=str(checkout),
		REVIEW_AUTOFIX_CALLER_WORKFLOW_REF="o/r/.github/workflows/internal-review.yml@refs/heads/main",
		ALLOW_WORKFLOW_EDITS="",
		RUN_URL="",
		REVIEW_HEAD_GATE_STATUS_ENABLED="true",
	)
	run_env.update(env or {})
	result = subprocess.run(["bash", str(SKIP_STEP)], capture_output=True, text=True, env=run_env, check=False)
	calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, calls, output.read_text(encoding="utf-8")


def _review_dispatches(calls: list) -> list:
	return [call for call in calls if call[:2] == ["workflow", "run"] and call[2] in REVIEW_WORKFLOW_NAMES]


def _exhausted_comments() -> list:
	return [_comment(_marker("findings", HEAD if n == 5 else f"{n:040x}", n), comment_id=n) for n in range(1, 6)]


def test_skip_step_clean_marker_lets_the_merge_run(tmp_path: Path) -> None:
	result, calls, output = _run_skip_step(tmp_path, comments=[_comment(_marker("clean", HEAD, 1))])
	assert result.returncode == 0, result.stderr
	assert output == "hold=false\nexhausted=false\n"
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert f"AUTOFIX_DET_SKIP_SECURITY_PASS pr=42 head={HEAD} outcome=clean hold_reason=none redispatch=none" in result.stdout
	# The gate withholds the head-gate success while the pass is required;
	# the skip step posts it once the pass lets the merge run.
	assert [call for call in calls if f"repos/o/r/statuses/{HEAD}" in call] == [[
		"api", "-X", "POST", f"repos/o/r/statuses/{HEAD}", "-f", "state=success",
		"-f", "context=ai-review/head-gate", "-f", "description=deterministic skip and security pass passed",
	]]


def test_skip_step_without_marker_dispatches_the_audit_and_holds(tmp_path: Path) -> None:
	result, calls, output = _run_skip_step(tmp_path)
	assert result.returncode == 0, result.stderr
	assert output == "hold=true\nhold_reason=audit_dispatched\nexhausted=false\n"
	assert any(call[:3] == ["workflow", "run", "ai-security-audit.yml"] for call in calls)
	assert _review_dispatches(calls) == []
	assert "outcome=hold hold_reason=audit_dispatched redispatch=none" in result.stdout
	assert not any(any("/statuses/" in arg for arg in call) for call in calls)
	# The comments were read fresh (paginated) and the pass ran in the PR-head checkout.
	assert ["api", "--paginate", "repos/o/r/issues/42/comments?per_page=100"] in calls


def test_skip_step_exhausted_redispatches_review_once(tmp_path: Path) -> None:
	result, calls, output = _run_skip_step(tmp_path, comments=_exhausted_comments())
	assert result.returncode == 0, result.stderr
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n"
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls
	assert _review_dispatches(calls) == [["workflow", "run", "internal-review.yml", "--repo", "o/r", "-f", "pr_number=42", "-f", "allow_workflow_edits=false"]]
	assert "outcome=exhausted hold_reason=cycles_exhausted redispatch=dispatched workflow=internal-review.yml" in result.stdout
	# The label is the routing signal; the skip step posts no marker of its
	# own, so the only comment is the pass's usual exhaustion comment.
	comment_posts = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert len(comment_posts) == 1, comment_posts
	assert any(arg.startswith("body=## Single-issue security pass exhausted") for arg in comment_posts[0])
	assert not any("<!--" in arg for arg in comment_posts[0])
	assert not any(any("/statuses/" in arg for arg in call) for call in calls)


def test_skip_step_exhausted_dispatch_failure_warns_and_holds(tmp_path: Path) -> None:
	result, calls, output = _run_skip_step(tmp_path, comments=_exhausted_comments(), env={"FAKE_GH_FAIL": "dispatch"})
	assert result.returncode == 0, result.stderr
	assert output == "hold=true\nhold_reason=cycles_exhausted\nexhausted=true\n"
	assert [call[2] for call in _review_dispatches(calls)] == ["internal-review.yml", "ai-review.yml", "review_autofix.yml"]
	assert "::warning::Could not dispatch a review workflow" in result.stdout
	assert "redispatch=dispatch_failed" in result.stdout


@pytest.mark.parametrize(
	"overrides",
	[
		{"FAKE_GIT_HEAD": "d" * 40},
		{"DET_SKIP_SECURITY_PASS_PR_JSON": json.dumps({**_pr(), "head": {"ref": "ai/issue-7", "sha": OLD, "repo": {"full_name": "o/r"}}})},
		{"DET_SKIP_SECURITY_PASS_PR_JSON": "not json"},
		{"PR_HEAD_SHA": "abc"},
		{"PR_NUMBER": "42;x"},
	],
)
def test_skip_step_unverifiable_input_fails_closed(tmp_path: Path, overrides: dict) -> None:
	result, calls, output = _run_skip_step(tmp_path, comments=[_comment(_marker("clean", HEAD, 1))], env=overrides)
	assert result.returncode != 0
	assert output == "hold=true\nhold_reason=skip_input_unverifiable\nexhausted=false\n"
	assert "outcome=skip_input_unverifiable" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_skip_step_pass_failure_fails_the_step_and_holds(tmp_path: Path) -> None:
	result, _calls, output = _run_skip_step(tmp_path, comments=_exhausted_comments(), env={"FAKE_GH_FAIL": "label"})
	assert result.returncode != 0
	assert output.startswith("hold=true\n")
	assert "hold=false" not in output


def test_skip_step_comment_read_failure_holds_markers_unverifiable(tmp_path: Path) -> None:
	# A missing comments file makes every paginated read in the fake gh fail.
	result, calls, output = _run_skip_step(tmp_path, env={"FAKE_GH_COMMENTS": str(tmp_path / "missing.json")})
	assert result.returncode == 0, result.stderr
	assert output == "hold=true\nhold_reason=markers_unverifiable\nexhausted=false\n"
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_skip_step_never_imports_pr_head_python_modules(tmp_path: Path) -> None:
	# The pass's `python3 -c` age checks run with the PR head as cwd and
	# GH_TOKEN set; PYTHONSAFEPATH=1 keeps a PR-supplied datetime.py out.
	checkout = tmp_path / "pr-head"
	checkout.mkdir()
	sentinel = tmp_path / "pr_module_imported"
	(checkout / "datetime.py").write_text(f"open({str(sentinel)!r}, 'w').close()\n", encoding="utf-8")
	result, _calls, output = _run_skip_step(tmp_path, comments=[_comment(_marker("pending", HEAD, 1))])
	assert result.returncode == 0, result.stderr
	assert output == "hold=true\nhold_reason=audit_pending\nexhausted=false\n"
	assert not sentinel.exists()
	text = SKIP_STEP.read_text(encoding="utf-8")
	assert "&& PYTHONSAFEPATH=1 \\\n" in text
	assert "python_safe_path_unavailable" in text


def test_deterministic_skip_merge_wiring() -> None:
	gate = _steps(REVIEW, "gate")["Evaluate review gate"]
	assert gate["env"]["AUTOFIX_SKIP_RUNS_SECURITY_PASS"] == "${{ vars.AUTOFIX_SKIP_RUNS_SECURITY_PASS || 'true' }}"
	workflow = yaml.safe_load(REVIEW.read_text(encoding="utf-8"))
	outputs = workflow["jobs"]["gate"]["outputs"]
	assert outputs["det_skip_security_pass"] == "${{ steps.evaluate.outputs.det_skip_security_pass }}"
	assert outputs["det_skip_security_pass_pr_json"] == "${{ steps.evaluate.outputs.det_skip_security_pass_pr_json }}"
	steps = _steps(REVIEW, "deterministic-skip-merge")
	names = list(steps)
	merge_name = "Mark PR review-skipped, mark linked issues ready-to-merge, enable auto-merge"
	checkout = steps["Checkout PR head for security pass"]
	pass_step = steps["Single-issue security pass (deterministic skip)"]
	assert names.index("Checkout merge-base freshness helper") < names.index("Checkout PR head for security pass")
	assert names.index("Checkout PR head for security pass") < names.index("Single-issue security pass (deterministic skip)") < names.index(merge_name)
	required = "${{ needs.gate.outputs.det_skip_security_pass == 'true' }}"
	assert checkout["if"] == required and pass_step["if"] == required
	assert pass_step["id"] == "det_skip_security_pass"
	assert steps[merge_name]["if"] == "${{ needs.gate.outputs.det_skip_security_pass != 'true' || steps.det_skip_security_pass.outputs.hold == 'false' }}"
	assert checkout["with"]["ref"] == "${{ needs.gate.outputs.head_sha }}"
	assert checkout["with"]["repository"] == "${{ github.repository }}"
	assert checkout["with"]["path"] == ".codex-skip-pr-head"
	codex_pass = _steps(REVIEW, "codex-agent")["Single-issue security pass"]
	for key, value in codex_pass["env"].items():
		assert pass_step["env"][key] == value, key
	assert pass_step["env"]["DET_SKIP_SECURITY_PASS_PR_JSON"] == "${{ needs.gate.outputs.det_skip_security_pass_pr_json }}"
	assert pass_step["env"]["REVIEW_HEAD_GATE_STATUS_ENABLED"] == "${{ vars.REVIEW_HEAD_GATE_STATUS_ENABLED || 'true' }}"
	# The gate posts no head-gate success while the skip-path pass is required.
	status_step = _steps(REVIEW, "gate")["Withdraw stale auto-merge and publish head-gate status"]
	assert status_step["env"]["DETERMINISTIC_SKIP"] == (
		"${{ steps.evaluate.outputs.det_skip_security_pass != 'true' && steps.evaluate.outputs.deterministic_skip || 'false' }}"
	)
	sparse = steps["Checkout merge-base freshness helper"]["with"]["sparse-checkout"].split()
	for script in ("scripts/review_single_issue_security_pass.sh", "scripts/security_pass_skip.py",
			"scripts/review_autofix_step_deterministic_skip_security_pass.sh", "scripts/review_freshness_ci_failed.sh",
			"scripts/review_head_gate.sh", "scripts/gh_helpers.sh"):
		assert script in sparse, script


def test_gate_switch_off_keeps_todays_suppression_and_outputs() -> None:
	text = (ROOT / "scripts" / "review_autofix_step_evaluate_gate.sh").read_text(encoding="utf-8")
	block = text[text.index('SECURITY_PASS_SKIP_SUPPRESSED="false"'):]
	block = block[: block.index("FRESHNESS_CI_FAILED_SKIP_SUPPRESSED=")]
	off = block.index('if [ "${AUTOFIX_SKIP_RUNS_SECURITY_PASS:-false}" != "true" ]; then')
	assert block.index('SECURITY_PASS_SKIP_SUPPRESSED="true"', off) < block.index("reason=single_issue_security_pass", off) < block.index("elif", off)
	assert 'elif [[ ",${pr_labels}," == *,ai:security-pass-failed,* ]]; then' in block
	assert "reason=security_pass_failed pr=${PR_NUMBER} candidate=${candidate_skip_reason}" in block
	assert 'DET_SKIP_SECURITY_PASS_REQUIRED="true"' in block
	tail = text[text.index('echo "force_rb_judge_conflict_skip='):]
	guard = tail.index('if [ "${AUTOFIX_SKIP_RUNS_SECURITY_PASS:-false}" = "true" ]; then')
	assert guard < tail.index('echo "det_skip_security_pass=') < tail.index("det_skip_security_pass_pr_json=")
	assert "det_skip_security_pass" not in text[: text.index('echo "force_rb_judge_conflict_skip=')].split("DET_SKIP_SECURITY_PASS_REQUIRED")[0]


def test_skip_step_holds_when_python_ignores_safe_path(tmp_path: Path) -> None:
	# A python3 that does not honour PYTHONSAFEPATH must stop the step before
	# the pass runs in the PR-head cwd with GH_TOKEN set. The stub fails only
	# the safe-path probe and delegates everything else (the fake gh) to the
	# real interpreter.
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	stub = bin_dir / "python3"
	stub.write_text(
		"#!/usr/bin/env bash\n"
		'case "$*" in *sys.flags.safe_path*) exit 1 ;; esac\n'
		f'exec "{sys.executable}" "$@"\n',
		encoding="utf-8",
	)
	stub.chmod(0o755)
	result, calls, output = _run_skip_step(tmp_path, comments=[_comment(_marker("clean", HEAD, 1))])
	assert result.returncode != 0
	assert output == "hold=true\nhold_reason=skip_input_unverifiable\nexhausted=false\n"
	assert "outcome=skip_input_unverifiable" in result.stdout
	assert "detail=python_safe_path_unavailable" in result.stdout
	# The pass never ran: no identity read, no dispatch, no status post.
	assert ["api", "user", "--jq", '.login // ""'] not in calls
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert not any(any("/statuses/" in arg for arg in call) for call in calls)


# ----- Delta re-audit with prior-finding carry-forward (stuck-states P5a) -----
LEGACY_DISPATCH = ["workflow", "run", "ai-security-audit.yml", "-R", "o/r", "--ref", "main", "-f", "ref=ai/issue-7", "-f", "pr_number=42"]
NEW_HEAD = "e" * 40


def _audit_dispatches(calls: list) -> list:
	return [call for call in calls if call[:3] == ["workflow", "run", "ai-security-audit.yml"]]


def _audited_lines(findings: list) -> str:
	raw = json.dumps(findings, separators=(",", ":")).encode("utf-8")
	digest = __import__("hashlib").sha256(raw).hexdigest()
	return f"Audited findings (base64 JSON): {base64.b64encode(raw).decode('ascii')}\nAudited findings sha256: {digest}"


def _prior_finding(finding_id: str = "F-9", file: str = "a.py", severity: str = "high") -> dict:
	return {
		"finding_id": finding_id, "owasp_or_stride_category": "A01", "severity": severity, "confidence": 9,
		"file": file, "line": 3, "exploit_scenario": "x", "recommendation": "y",
	}


def _findings_result(head: str, cycle: int, findings: list, payload: str | None = None) -> str:
	ids = base64.b64encode(json.dumps([f["finding_id"] for f in findings]).encode("ascii")).decode("ascii")
	lines = payload if payload is not None else _audited_lines(findings)
	return f"## result\n\nFinding IDs (base64 JSON): {ids}\n\n{lines}\n\n" + f"<!-- ai:single-issue-security-pass:v1 status=findings head={head} cycle={cycle} -->"


def test_delta_dispatch_from_a_findings_result_sends_since_and_comment_id(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, age_hours=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH + ["-f", f"audit_diff_since={OLD}", "-f", "prior_findings=5"]]
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert f"delta re-audit of the changes since `{OLD}`" in posted[-1][-1]
	assert posted[-1][-1].endswith("\n\n" + _marker("pending", HEAD, 2))
	assert f"delta_since={OLD}" in result.stdout


@pytest.mark.parametrize("comments", [
	[],
	[_comment(_marker("failed", HEAD, 1))],
	[_comment(_marker("clean", OLD, 1)), _comment(_marker("findings", HEAD, 2), comment_id=2), _comment(_marker("failed", HEAD, 3), comment_id=3)],
])
def test_no_older_completed_result_keeps_todays_dispatch(tmp_path: Path, comments: list) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH]
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "delta re-audit" not in posted[-1][-1]


def test_delta_switch_off_keeps_todays_dispatch_and_comment(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, age_hours=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED": "false"})
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH]
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1] == (
		"body=## Single-issue security pass\n\nAuto-merge waits for the security audit of `" + HEAD + "` (cycle 2 of 5). "
		"Findings become follow-up issues against this branch; a clean audit re-runs the review, which then merges.\n\n" + _marker("pending", HEAD, 2)
	)


def test_exhausted_head_retry_stays_a_full_audit(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_findings_result(OLD, 5, [_prior_finding()]), comment_id=5)])
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "reason=exhausted_retry" in result.stdout
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH]


def test_rejected_delta_dispatch_falls_back_to_legacy_arguments(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, age_hours=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_FAIL": "reject_delta_inputs"})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "outcome=delta_fallback reason=dispatch_rejected" in result.stdout
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH + ["-f", f"audit_diff_since={OLD}", "-f", "prior_findings=5"], LEGACY_DISPATCH]
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "delta re-audit" not in posted[-1][-1] and posted[-1][-1].endswith(_marker("pending", HEAD, 2))


def test_double_dispatch_failure_takes_the_dispatch_failed_path(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, age_hours=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_FAIL": "dispatch"})
	assert result.returncode == 0 and output == "hold=true\nhold_reason=dispatch_failed\n"
	assert "outcome=delta_fallback" in result.stdout and len(_audit_dispatches(calls)) == 2
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker("failed", HEAD, 2))


# Issue #10104: record why the audit dispatch failed and retry transient failures.
WRAPPER_HINT = "predates the `pr_number` input"
RATE_LIMIT_403 = "HTTP 403: API rate limit exceeded for installation ID 1. (https://api.github.com/repos/o/r/actions/workflows/security-audit.yml/dispatches)"


def _dispatch_errors(*errors: str | None) -> dict:
	return {"FAKE_GH_DISPATCH_ERRORS": json.dumps(list(errors))}


def _posted(calls: list) -> list:
	return [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]


def test_a_transient_dispatch_failure_is_retried_in_the_same_run(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env=_dispatch_errors("HTTP 502: Bad Gateway", None))
	assert result.returncode == 0 and output == "hold=true\nhold_reason=audit_dispatched\n"
	assert len(_audit_dispatches(calls)) == 2
	assert "outcome=dispatch_retry attempt=1 dispatch_error=server_error" in result.stdout
	assert "outcome=dispatched cycle=1" in result.stdout and "dispatch_attempts=2" in result.stdout
	assert "::warning::security-audit dispatch attempt 1 failed (dispatch_error=server_error): HTTP 502: Bad Gateway" in result.stdout
	posted = _posted(calls)
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("pending", HEAD, 1))
	assert not any(_marker("failed", HEAD, 1) in call[-1] for call in posted)


def test_a_first_attempt_dispatch_logs_no_attempt_count(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate")
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "outcome=dispatched" in result.stdout and "dispatch_attempts=" not in result.stdout


def test_a_rate_limited_dispatch_failure_records_the_class_and_the_cycle(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env=_dispatch_errors(RATE_LIMIT_403))
	assert result.returncode == 0 and output == "hold=true\nhold_reason=dispatch_failed\n"
	assert len(_audit_dispatches(calls)) == 3
	assert "outcome=hold reason=dispatch_failed workflow=ai-security-audit.yml cycle=1 dispatch_error=rate_limited" in result.stdout
	posted = _posted(calls)
	assert len(posted) == 1 and posted[0][-1].endswith(_marker("failed", HEAD, 1))
	assert "The error class is `rate_limited`: a GitHub API rate limit." in posted[0][-1]
	assert WRAPPER_HINT not in posted[0][-1] and "api.github.com" not in posted[0][-1]


def test_a_rejected_dispatch_is_not_retried_and_keeps_the_wrapper_hint(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env=_dispatch_errors("HTTP 422: Unexpected inputs provided: [\"pr_number\"]"))
	assert output == "hold=true\nhold_reason=dispatch_failed\n"
	assert len(_audit_dispatches(calls)) == 1 and "outcome=dispatch_retry" not in result.stdout
	assert "reason=dispatch_failed workflow=ai-security-audit.yml cycle=1 dispatch_error=rejected" in result.stdout
	body = _posted(calls)[0][-1]
	assert "The error class is `rejected`: GitHub rejected the workflow, ref or inputs." in body and WRAPPER_HINT in body


@pytest.mark.parametrize(
	"error, expected",
	[
		("HTTP 429: Too Many Requests", "rate_limited"),
		("HTTP 403: You have exceeded a secondary rate limit", "rate_limited"),
		("HTTP 403: Resource not accessible by integration", "auth"),
		("HTTP 401: Bad credentials", "auth"),
		("HTTP 404: Not Found", "rejected"),
		("could not find any workflows named security-audit.yml", "rejected"),
		("HTTP 409: Conflict", "other"),
		("", "other"),
		("dial tcp: lookup api.github.com: no such host", "network"),
	],
)
def test_dispatch_error_classes(tmp_path: Path, error: str, expected: str) -> None:
	result, calls, output = _run(tmp_path, "gate", env={**_dispatch_errors(error), "SECURITY_PASS_DISPATCH_RETRIES": "0"})
	assert output == "hold=true\nhold_reason=dispatch_failed\n"
	assert f"dispatch_error={expected}" in result.stdout and len(_audit_dispatches(calls)) == 1


def test_zero_dispatch_retries_make_one_attempt(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={**_dispatch_errors("HTTP 502: Bad Gateway", None), "SECURITY_PASS_DISPATCH_RETRIES": "0"})
	assert output == "hold=true\nhold_reason=dispatch_failed\n"
	assert len(_audit_dispatches(calls)) == 1 and "outcome=dispatch_retry" not in result.stdout
	assert "reason=dispatch_failed workflow=ai-security-audit.yml cycle=1 dispatch_error=server_error" in result.stdout


@pytest.mark.parametrize(
	"settings, warning",
	[
		({"SECURITY_PASS_DISPATCH_RETRIES": "abc"}, "::warning::SECURITY_PASS_DISPATCH_RETRIES is not a whole number from 0 to 5; using the default 2."),
		({"SECURITY_PASS_DISPATCH_RETRIES": "99"}, "::warning::SECURITY_PASS_DISPATCH_RETRIES is not a whole number from 0 to 5; using the default 2."),
	],
)
def test_invalid_dispatch_retry_settings_fall_back_to_the_default(tmp_path: Path, settings: dict, warning: str) -> None:
	result, calls, output = _run(tmp_path, "gate", env={**_dispatch_errors("HTTP 502: Bad Gateway"), **settings})
	assert warning in result.stdout and "abc" not in result.stdout.split(warning)[0][-40:]
	assert output == "hold=true\nhold_reason=dispatch_failed\n" and len(_audit_dispatches(calls)) == 3


def test_an_invalid_dispatch_retry_delay_falls_back_to_the_default(tmp_path: Path) -> None:
	# Zero retries, so the fallback delay of 20 seconds is never slept.
	result, calls, output = _run(tmp_path, "gate", env={**_dispatch_errors("HTTP 502: Bad Gateway"), "SECURITY_PASS_DISPATCH_RETRIES": "0", "SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS": "99"})
	assert "::warning::SECURITY_PASS_DISPATCH_RETRY_DELAY_SECS is not a whole number from 0 to 60; using the default 20." in result.stdout
	assert len(_audit_dispatches(calls)) == 1


def test_the_dispatch_error_warning_never_carries_a_token(tmp_path: Path) -> None:
	job_token = "s3cretJobTokenValue0123456789"
	classic = "ghp_" + "A1b2C3d4E5" * 4
	fine_grained = "github_pat_" + "11ABCDEFG0" * 6
	jwt = "eyJ" + "hbGciOiJSUzI1NiJ9" * 2 + ".eyJzdWIiOiIxIn0.sig"
	error = (
		f"HTTP 502: upstream said token={job_token} auth={classic}\n::error::injected {fine_grained} "
		f"https://user:{job_token}@example.com Authorization: Bearer abc.def {jwt} " + "x" * 300
	)
	result, calls, output = _run(tmp_path, "gate", env={**_dispatch_errors(error), "SECURITY_PASS_DISPATCH_RETRIES": "0", "GH_TOKEN": job_token})
	text = result.stdout + result.stderr
	for secret in (job_token, classic, fine_grained, classic[4:], fine_grained[11:], "abc.def", "hbGciOiJSUzI1NiJ9"):
		assert secret not in text
	assert not any(line.startswith("::error::injected") for line in text.splitlines())
	warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning::security-audit dispatch attempt 1 failed")]
	assert len(warnings) == 1
	excerpt = warnings[0].split("(dispatch_error=server_error): ", 1)[1]
	assert 0 < len(excerpt) <= 200 and "[redacted]" in excerpt
	assert job_token not in _posted(calls)[0][-1] and "upstream said" not in _posted(calls)[0][-1]


def test_an_exhausted_retry_dispatch_failure_logs_the_error_class(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", comments=[_comment(_marker("failed", HEAD, 5))], env=_dispatch_errors("HTTP 503: Service Unavailable"))
	assert output == "hold=true\nhold_reason=dispatch_failed\n"
	assert "reason=dispatch_failed_exhausted cycle=6 dispatch_error=server_error" in result.stdout
	assert len(_audit_dispatches(calls)) == 3
	body = _posted(calls)[0][-1]
	assert body.endswith(_marker("failed", HEAD, 6)) and "The error class is `server_error`: a GitHub server error." in body


def test_delta_fallback_shares_the_dispatch_retry_budget(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, age_hours=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env=_dispatch_errors("HTTP 422: Unexpected inputs", "HTTP 502: Bad Gateway", None))
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "outcome=delta_fallback reason=dispatch_rejected" in result.stdout and "dispatch_attempts=3" in result.stdout
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH + ["-f", f"audit_diff_since={OLD}", "-f", "prior_findings=5"], LEGACY_DISPATCH, LEGACY_DISPATCH]


def test_delta_fallback_stops_when_the_shared_budget_is_used(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, age_hours=2)]
	# Delta: 502, 502, 502 uses both retries; the plain fallback then gets one call.
	result, calls, output = _run(tmp_path, "gate", comments=comments, env=_dispatch_errors("HTTP 502: Bad Gateway"))
	assert output == "hold=true\nhold_reason=dispatch_failed\n"
	assert len(_audit_dispatches(calls)) == 4 and result.stdout.count("outcome=dispatch_retry") == 2
	assert "dispatch_error=server_error" in result.stdout


def _delta(tmp_path: Path, comments: list, env: dict | None = None):
	github_env = tmp_path / "github_env"
	github_env.write_text("", encoding="utf-8")
	run_env = {
		"GITHUB_ENV": str(github_env), "RUNNER_TEMP": str(tmp_path),
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_DELTA_SINCE_INPUT": OLD, "SECURITY_PASS_PRIOR_FINDINGS_INPUT": "5",
		"FAKE_GIT_ANCESTORS": OLD,
	}
	run_env.update(env or {})
	result, calls, _ = _run(tmp_path, "delta", comments=comments, env=run_env)
	return result, calls, github_env.read_text(encoding="utf-8")


def test_delta_mode_narrows_after_verifying_a_trusted_findings_result(tmp_path: Path) -> None:
	prior = [_prior_finding()]
	result, calls, written = _delta(tmp_path, [_comment(_findings_result(OLD, 1, prior), comment_id=5)])
	prior_file = tmp_path / "single-pass-prior-findings.json"
	assert written == (
		f"SECURITY_AUDIT_DIFF_SINCE={OLD}\nSECURITY_PASS_DELTA_SINCE={OLD}\n"
		f"SECURITY_AUDIT_PRIOR_FINDINGS={prior_file}\nSECURITY_PASS_PRIOR_FINDINGS_FILE={prior_file}\n"
	)
	assert json.loads(prior_file.read_text(encoding="utf-8")) == prior
	assert f"mode=delta pr=42 head={HEAD} outcome=delta since={OLD} prior_findings=1" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_delta_mode_clean_source_sends_since_only(tmp_path: Path) -> None:
	result, _, written = _delta(tmp_path, [_comment(_marker("clean", OLD, 1), comment_id=5)], env={"SECURITY_PASS_PRIOR_FINDINGS_INPUT": ""})
	assert written == f"SECURITY_AUDIT_DIFF_SINCE={OLD}\nSECURITY_PASS_DELTA_SINCE={OLD}\n"
	assert "outcome=delta" in result.stdout


@pytest.mark.parametrize("comments, env, reason", [
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED": "false"}, "disabled"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"SECURITY_PASS_DELTA_SINCE_INPUT": ""}, "no_delta_input"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"SECURITY_PASS_DELTA_SINCE_INPUT": HEAD}, "no_delta_input"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"SECURITY_PASS_DELTA_SINCE_INPUT": NEW_HEAD}, "input_mismatch"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"SECURITY_PASS_PRIOR_FINDINGS_INPUT": "6"}, "input_mismatch"),
	([_comment(_marker("clean", OLD, 1), comment_id=5)], {}, "input_mismatch"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5, login="someone")], {}, "input_mismatch"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()], payload=_audited_lines([_prior_finding()]).replace("sha256: ", "sha256: 0")[:-1]), comment_id=5)], {}, "prior_findings_unverifiable"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()], payload=_audited_lines([_prior_finding("F-10")]).split("\n")[0] + "\n" + _audited_lines([_prior_finding()]).split("\n")[1]), comment_id=5)], {}, "prior_findings_unverifiable"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()], payload=""), comment_id=5)], {}, "prior_findings_unverifiable"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"FAKE_GIT_ANCESTORS": ""}, "not_ancestor"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5)], {"FAKE_GIT_HEAD": NEW_HEAD}, "checkout_mismatch"),
	([_comment(_findings_result(OLD, 1, [_prior_finding()]), comment_id=5), _comment(_marker("findings", HEAD, 2), comment_id=6)], {}, "input_mismatch"),
])
def test_delta_mode_fails_open_to_a_full_audit(tmp_path: Path, comments: list, env: dict, reason: str) -> None:
	result, _, written = _delta(tmp_path, comments, env=env)
	assert result.returncode == 0 and written == ""
	assert f"outcome=full reason={reason}" in result.stdout


def _carry_report(tmp_path: Path, env: dict | None = None, prior: list | None = None, current: list | None = None, security_issues=None, findings: str | None = None):
	prior_file = tmp_path / "prior.json"
	prior_file.write_text(json.dumps(prior if prior is not None else [_prior_finding()]), encoding="utf-8")
	current_findings = current if current is not None else []
	findings_file = tmp_path / "current.json"
	findings_file.write_text(json.dumps(current_findings), encoding="utf-8")
	count = findings if findings is not None else str(len(current_findings))
	run_env = {
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": "success", "SECURITY_PASS_FINDINGS": count,
		"SECURITY_PASS_FINDING_IDS_B64": base64.b64encode(json.dumps([f["finding_id"] for f in current_findings]).encode("ascii")).decode("ascii"),
		"SECURITY_PASS_DELTA_SINCE": OLD, "SECURITY_PASS_PRIOR_FINDINGS_FILE": str(prior_file),
		"SECURITY_PASS_FINDINGS_FILE": str(findings_file), "FAKE_GIT_DIFF_FILES": "b.py",
	}
	run_env.update(env or {})
	if security_issues is None:
		security_issues = [[{"state": "open", "body": _followup_body(finding_id="F-9"), "user": {"login": "owner"}}]]
	result, calls, _ = _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 2))], env=run_env, security_issues=security_issues)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	return result, calls, posted[-1][-1][5:] if posted else ""


def _ids_in(body: str) -> list:
	line = next(line for line in body.splitlines() if line.startswith("Finding IDs (base64 JSON): "))
	return json.loads(base64.b64decode(line.split(": ", 1)[1]))


def test_untouched_open_prior_finding_is_carried_into_a_zero_finding_delta(tmp_path: Path) -> None:
	result, calls, body = _carry_report(tmp_path)
	assert body.endswith(_marker("findings", HEAD, 2, ()).split("\n")[-1])
	assert "## Single-issue security pass: 1 finding(s)" in body and _ids_in(body) == ["F-9"]
	assert f"Carried forward (file untouched since `{OLD}`, follow-up open): F-9" in body
	assert _audited_lines([_prior_finding()]) in body
	assert "outcome=findings cycle=2 findings=1" in result.stdout
	# Findings below the cycle cap wait for the follow-up merge, as today.
	assert not any(call[:3] == ["workflow", "run", "ai-review.yml"] for call in calls)


def test_carried_result_round_trips_through_delta_and_keeps_the_gate_holding(tmp_path: Path) -> None:
	_, _, body = _carry_report(tmp_path, current=[_prior_finding("F-1", file="b.py", severity="critical")])
	assert _ids_in(body) == ["F-1", "F-9"]
	stored = [_prior_finding("F-1", file="b.py", severity="critical"), _prior_finding()]
	assert _audited_lines(stored) in body
	delta_dir = tmp_path / "delta"
	delta_dir.mkdir()
	result, _, written = _delta(delta_dir, [_comment(body, comment_id=8)], env={
		"SECURITY_PASS_HEAD_SHA": NEW_HEAD, "FAKE_GIT_HEAD": NEW_HEAD, "SECURITY_PASS_DELTA_SINCE_INPUT": HEAD,
		"SECURITY_PASS_PRIOR_FINDINGS_INPUT": "8", "FAKE_GIT_ANCESTORS": HEAD,
	})
	assert f"SECURITY_AUDIT_DIFF_SINCE={HEAD}" in written and "prior_findings=2" in result.stdout
	assert json.loads((delta_dir / "single-pass-prior-findings.json").read_text(encoding="utf-8")) == stored
	gate_dir = tmp_path / "gate"
	gate_dir.mkdir()
	issues = [[{"state": "open", "body": _followup_body(finding_id=fid), "user": {"login": "owner"}} for fid in ("F-1", "F-9")]]
	_, _, output = _run(gate_dir, "gate", comments=[_comment(_marker("pending", HEAD, 2), comment_id=1), _comment(body, comment_id=8)], security_issues=issues)
	assert output == "hold=true\nhold_reason=awaiting_followups\n"


@pytest.mark.parametrize("env, issues", [
	({"FAKE_GIT_DIFF_FILES": "a.py"}, None),
	({"FAKE_GIT_DIFF_FILES": "c.py,a.py"}, None),
	({}, [[{"state": "open", "body": _followup_body(finding_id="F-8"), "user": {"login": "owner"}}]]),
	({}, [[{"state": "open", "body": _followup_body(finding_id="F-9"), "user": {"login": "someone"}}]]),
])
def test_touched_or_closed_prior_finding_is_resolved(tmp_path: Path, env: dict, issues) -> None:
	result, calls, body = _carry_report(tmp_path, env=env, security_issues=issues)
	assert body.startswith("## Single-issue security pass: clean") and "Carried forward" not in body
	assert ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"] in calls


def test_re_reported_prior_finding_is_not_carried_twice(tmp_path: Path) -> None:
	_, calls, body = _carry_report(tmp_path, current=[_prior_finding()])
	assert _ids_in(body) == ["F-9"] and "Carried forward" not in body
	assert not any("repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call for call in calls)


def test_listing_failure_carries_every_candidate(tmp_path: Path) -> None:
	_, _, body = _carry_report(tmp_path, env={"FAKE_GH_FAIL": "followups", "FAKE_GIT_DIFF_FILES": "c.py"}, prior=[_prior_finding(), _prior_finding("F-7", file="b.py")])
	assert sorted(_ids_in(body)) == ["F-7", "F-9"]
	assert "follow-up state unverifiable" in body


def test_git_failure_carries_every_prior_finding_with_an_open_followup(tmp_path: Path) -> None:
	issues = [[{"state": "open", "body": _followup_body(finding_id=fid), "user": {"login": "owner"}} for fid in ("F-7", "F-9")]]
	_, _, body = _carry_report(tmp_path, env={"FAKE_GIT_DIFF_FILES": None}, prior=[_prior_finding(), _prior_finding("F-7", file="b.py")], security_issues=issues)
	assert sorted(_ids_in(body)) == ["F-7", "F-9"]


def test_over_cap_audited_findings_are_omitted(tmp_path: Path) -> None:
	big = dict(_prior_finding("F-1", file="b.py"), exploit_scenario="z" * 60000)
	result, _, body = _carry_report(tmp_path, current=[big], prior=[_prior_finding("F-1", file="b.py")])
	assert _ids_in(body) == ["F-1"] and "Audited findings" not in body
	assert "reason=prior_findings_omitted" in result.stdout


def test_full_audit_findings_are_stored_for_the_next_delta(tmp_path: Path) -> None:
	current = [_prior_finding("F-1", file="b.py")]
	_, _, body = _carry_report(tmp_path, current=current, env={"SECURITY_PASS_DELTA_SINCE": None, "SECURITY_PASS_PRIOR_FINDINGS_FILE": None})
	assert _audited_lines(current) in body and "Carried forward" not in body


def test_delta_switch_off_report_body_is_unchanged(tmp_path: Path) -> None:
	_, calls, body = _carry_report(tmp_path, env={"SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED": "false"})
	assert body == (
		f"## Single-issue security pass: clean\n\nThe audit of `{HEAD}` found nothing to fix. The review re-runs and merges this head.\n\n"
		+ _marker("clean", HEAD, 2)
	)
	findings_dir = tmp_path / "findings"
	findings_dir.mkdir()
	_, _, findings_body = _carry_report(findings_dir, env={"SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED": "false"}, current=[_prior_finding("F-1", file="b.py")])
	assert "Audited findings" not in findings_body and "Carried forward" not in findings_body
	assert findings_body.endswith(_marker("findings", HEAD, 2, ("F-1",)))


def test_delta_audit_wiring() -> None:
	workflow = yaml.safe_load(AUDIT.read_text(encoding="utf-8"))
	triggers = workflow.get("on", workflow.get(True))
	for trigger in ("workflow_dispatch", "workflow_call"):
		for name in ("audit_diff_since", "prior_findings"):
			assert triggers[trigger]["inputs"][name]["default"] == "" and triggers[trigger]["inputs"][name]["type"] == "string"
	steps = _steps(AUDIT, "security-audit")
	names = list(steps)
	delta = steps["Resolve single-issue security pass delta"]
	assert names.index("Verify audit data and resolve scope") + 1 == names.index("Resolve single-issue security pass delta") < names.index("Run security audit")
	assert delta["if"] == "inputs.pr_number != '' && inputs.audit_diff_since != '' && env.SECURITY_AUDIT_TARGET_REF != ''"
	assert delta["continue-on-error"] is True
	assert "${{" not in delta["run"]
	assert 'bash "${GITHUB_WORKSPACE}/scripts/review_single_issue_security_pass.sh" delta' in delta["run"]
	assert "audit-data/scripts" not in delta["run"]
	assert delta["env"]["SECURITY_PASS_DELTA_SINCE_INPUT"] == "${{ inputs.audit_diff_since || '' }}"
	assert delta["env"]["SECURITY_PASS_PRIOR_FINDINGS_INPUT"] == "${{ inputs.prior_findings || '' }}"
	assert delta["env"]["SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED"] == "${{ vars.SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED || 'true' }}"
	assert delta["env"]["SECURITY_PASS_AUTHOR_LOGIN_FALLBACK"] == "${{ secrets.GH_PAT == '' && 'github-actions[bot]' || '' }}"
	run = steps["Run security audit"]
	assert run["env"]["SECURITY_AUDIT_PASS_FINDINGS_OUT"] == "${{ inputs.pr_number != '' && format('{0}/single-pass-findings.json', runner.temp) || '' }}"
	report = steps["Report single-issue security pass"]
	assert report["env"]["SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED"] == delta["env"]["SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED"]
	assert report["env"]["SECURITY_PASS_FINDINGS_FILE"] == "${{ format('{0}/single-pass-findings.json', runner.temp) }}"
	template = AUDIT_TEMPLATE.read_text(encoding="utf-8")
	assert "audit_diff_since: ${{ inputs.audit_diff_since || '' }}" in template
	assert "prior_findings: ${{ inputs.prior_findings || '' }}" in template
	template_inputs = yaml.safe_load(template)
	template_triggers = template_inputs.get("on", template_inputs.get(True))
	assert {"audit_diff_since", "prior_findings"} <= set(template_triggers["workflow_dispatch"]["inputs"])
	review_text = expanded_review_autofix_text()
	assert review_text.count("SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED: ${{ vars.SINGLE_ISSUE_SECURITY_PASS_DELTA_ENABLED || 'true' }}") >= 4


def test_findings_source_without_stored_payload_keeps_todays_dispatch(tmp_path: Path) -> None:
	comments = [_comment(_findings_result(OLD, 1, [_prior_finding()], payload=""), comment_id=5, age_hours=2)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert _audit_dispatches(calls) == [LEGACY_DISPATCH]
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "delta re-audit" not in posted[-1][-1] and "delta_since=" not in result.stdout


def test_carry_forward_diff_lists_rename_sources() -> None:
	text = (Path(__file__).resolve().parents[1] / "scripts" / "review_single_issue_security_pass.sh").read_text(encoding="utf-8")
	assert 'diff --no-renames --name-only "${carry_since}..${head_sha}"' in text


# Stuck-states P5b: one consolidated follow-up per cycle carries one leading
# `<!-- ai:security-finding:<id> -->` line per finding.
def _consolidated_body(finding_ids: tuple[str, ...], branch: str = "ai/issue-7", trailer: str = "") -> str:
	markers = "".join(f"<!-- ai:security-finding:{finding_id} -->\n" for finding_id in finding_ids)
	return f"{markers}Refs #9000\n\n- Pull request: #42\n- Integration branch: `{branch}`\n\n## Findings\n{trailer}"


def test_consolidated_followup_covers_every_leading_marker(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 2, ("F-1", "F-2")))]
	issues = [[{"state": "open", "body": _consolidated_body(("F-1", "F-2")), "user": {"login": "owner"}}]]
	result, calls, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=awaiting_followups\n"
	assert len([call for call in calls if "repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call]) == 1


def test_consolidated_followup_missing_an_id_still_pages(tmp_path: Path) -> None:
	comments = [_comment(_marker("findings", HEAD, 2, ("F-1", "F-2")))]
	issues = [[{"state": "open", "body": _consolidated_body(("F-1",)), "user": {"login": "owner"}}]]
	result, _, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"


@pytest.mark.parametrize("trailer", [
	"Quoted: <!-- ai:security-finding:F-2 -->\n",
	"<!-- ai:security-finding:F-2 -->\n",
])
def test_marker_outside_the_leading_block_does_not_count(tmp_path: Path, trailer: str) -> None:
	comments = [_comment(_marker("findings", HEAD, 2, ("F-1", "F-2")))]
	issues = [[{"state": "open", "body": _consolidated_body(("F-1",), trailer=trailer), "user": {"login": "owner"}}]]
	result, _, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"


@pytest.mark.parametrize("login, state", [("other", "open"), ("owner", "closed")])
def test_consolidated_followup_from_other_author_or_closed_does_not_count(tmp_path: Path, login: str, state: str) -> None:
	comments = [_comment(_marker("findings", HEAD, 2, ("F-1", "F-2")))]
	issues = [[{"state": state, "body": _consolidated_body(("F-1", "F-2")), "user": {"login": login}}]]
	result, _, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output == "hold=true\nhold_reason=followups_missing\n"


@pytest.mark.parametrize("issues", [
	[[{"state": "open", "body": _consolidated_body(("F-1", "F-2")), "user": {"login": "owner"}}]],
	[[]],
])
def test_findings_head_never_merges_with_or_without_consolidated_followup(tmp_path: Path, issues: list) -> None:
	# Severity gating is unchanged: a findings result for this head holds the merge.
	comments = [_comment(_marker("findings", HEAD, 2, ("F-1", "F-2")))]
	result, calls, output = _run(tmp_path, "gate", comments=comments, security_issues=issues)
	assert result.returncode == 0 and output.startswith("hold=true\n")
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


def test_prior_finding_covered_by_second_consolidated_marker_is_carried(tmp_path: Path) -> None:
	issues = [[{"state": "open", "body": _consolidated_body(("F-1", "F-9")), "user": {"login": "owner"}}]]
	_, _, body = _carry_report(tmp_path, security_issues=issues)
	assert _ids_in(body) == ["F-9"]
	assert f"Carried forward (file untouched since `{OLD}`, follow-up open): F-9" in body


def test_prior_finding_quoted_below_the_leading_block_is_not_carried(tmp_path: Path) -> None:
	issues = [[{"state": "open", "body": _consolidated_body(("F-1",), trailer="<!-- ai:security-finding:F-9 -->\n"), "user": {"login": "owner"}}]]
	_, _, body = _carry_report(tmp_path, security_issues=issues)
	assert "## Single-issue security pass: clean" in body


# --- Base-sync carry (issue #9533) ---------------------------------------
# The pipeline's own freshness update (merge of main into an audited head,
# recorded by an ai:merge-base-sync:v1 marker) carries the clean audit of
# from= when the merge is pure; anything else audits the head as before.

_CARRY_GIT_ENV = {
	"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
	"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
	"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def _carry_git(repo: Path, *args: str) -> str:
	env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
	env.update(_CARRY_GIT_ENV)
	return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()


def _carry_repo(tmp_path: Path, *, evil_merge: bool = False, whitespace_merge: bool = False) -> tuple[Path, str, str, str]:
	"""Build merge base -> PR head (from) and merge base -> main tip, then the update merge."""
	repo = tmp_path / "repo"
	repo.mkdir()
	_carry_git(repo, "init", "-q", "-b", "main")
	(repo / "a.txt").write_text("".join(f"line {n}\n" for n in range(1, 21)), encoding="utf-8")
	(repo / "b.txt").write_text("base\n", encoding="utf-8")
	_carry_git(repo, "add", "-A")
	_carry_git(repo, "commit", "-q", "-m", "base")
	_carry_git(repo, "checkout", "-q", "-b", "ai/issue-7")
	(repo / "a.txt").write_text((repo / "a.txt").read_text(encoding="utf-8").replace("line 3\n", "line 3 fixed\n"), encoding="utf-8")
	_carry_git(repo, "commit", "-q", "-am", "pr fix")
	from_sha = _carry_git(repo, "rev-parse", "HEAD")
	_carry_git(repo, "checkout", "-q", "main")
	(repo / "a.txt").write_text((repo / "a.txt").read_text(encoding="utf-8").replace("line 17\n", "line 17 on main\n"), encoding="utf-8")
	(repo / "b.txt").write_text("base moved\n", encoding="utf-8")
	_carry_git(repo, "commit", "-q", "-am", "main moves")
	base_parent = _carry_git(repo, "rev-parse", "HEAD")
	_carry_git(repo, "checkout", "-q", "ai/issue-7")
	_carry_git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
	if evil_merge:
		(repo / "a.txt").write_text((repo / "a.txt").read_text(encoding="utf-8").replace("line 10\n", "line 10 sneaked in\n"), encoding="utf-8")
		_carry_git(repo, "commit", "-q", "--amend", "--no-edit", "-a")
	if whitespace_merge:
		# Indentation-only change to the PR's own line, inside the merge.
		(repo / "a.txt").write_text((repo / "a.txt").read_text(encoding="utf-8").replace("line 3 fixed\n", "  line 3 fixed\n"), encoding="utf-8")
		_carry_git(repo, "commit", "-q", "--amend", "--no-edit", "-a")
	merge_sha = _carry_git(repo, "rev-parse", "HEAD")
	return repo, from_sha, base_parent, merge_sha


def _sync_comment(from_sha: str, base_parent: str, merge_sha: str, login: str = "owner", comment_id: int = 2) -> dict:
	body = (
		"**Branch updated from `main` before merging** (update 1; warning threshold 2).\n\n"
		f"<!-- ai:merge-base-sync:v1 pr=42 from={from_sha} sync=1 base={base_parent} merge={merge_sha} -->"
	)
	return _comment(body, login=login, comment_id=comment_id)


def _carry_stage_support(tmp_path: Path) -> None:
	# Production stages review_head_gate.sh and gh_helpers.sh in
	# SUPPORT_SCRIPTS_DIR, which also enables the freshness-sync inheritance
	# walk; the carry tests run with both so they cover the real order.
	support = tmp_path / "support"
	support.mkdir(exist_ok=True)
	(support / "review_head_gate.sh").write_text((ROOT / "scripts" / "review_head_gate.sh").read_text(encoding="utf-8"), encoding="utf-8")
	(support / "gh_helpers.sh").write_text('gh_retry() { "$@"; }\n', encoding="utf-8")


def _carry_run(tmp_path: Path, comments_for, *, evil_merge: bool = False, whitespace_merge: bool = False, env: dict | None = None):
	repo, from_sha, base_parent, merge_sha = _carry_repo(tmp_path, evil_merge=evil_merge, whitespace_merge=whitespace_merge)
	_carry_stage_support(tmp_path)
	pr = _pr()
	pr["head"]["sha"] = merge_sha
	commit = json.dumps({"parents": [from_sha, base_parent], "verified": True, "committer": "web-flow"})
	result, calls, output = _run(
		tmp_path, "gate", pr=pr, comments=comments_for(from_sha, base_parent, merge_sha), cwd=repo, real_git=True,
		env={"FAKE_GH_COMMIT": commit, "FAKE_GH_COMPARE": "ahead", "GH_RETRY_MAX_ATTEMPTS": "1", **(env or {})},
	)
	return result, calls, output, from_sha, merge_sha


def test_base_sync_carries_the_clean_audit_of_a_pure_merge(tmp_path: Path) -> None:
	result, calls, output, from_sha, merge_sha = _carry_run(
		tmp_path, lambda f, b, m: [_comment(_marker("clean", f, 1)), _sync_comment(f, b, m)],
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert output == "hold=false\n", result.stdout + result.stderr
	assert f"SINGLE_ISSUE_SECURITY_PASS mode=gate pr=42 head={merge_sha} outcome=clean reason=base_sync_carry from={from_sha}" in result.stdout
	# The carry decides before the inheritance walk, which never runs here.
	assert "reason=freshness_sync_inherited" not in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert any(len(call) > 1 and call[1] == f"repos/o/r/commits/{merge_sha}" for call in calls)


def test_base_sync_that_changes_a_pr_file_is_audited(tmp_path: Path) -> None:
	result, calls, output, _from_sha, merge_sha = _carry_run(
		tmp_path, lambda f, b, m: [_comment(_marker("clean", f, 1)), _sync_comment(f, b, m)], evil_merge=True,
	)
	assert f"head={merge_sha} outcome=carry_skipped reason=patch_mismatch" in result.stdout, result.stdout + result.stderr
	assert "reason=base_sync_carry" not in result.stdout
	# A merge proven impure is not cleared by the inheritance walk either.
	assert "reason=freshness_sync_inherited" not in result.stdout
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_base_sync_with_a_whitespace_only_change_to_a_pr_line_is_audited(tmp_path: Path) -> None:
	# `git patch-id --stable` ignores whitespace; the carry must not.
	result, calls, output, _from_sha, merge_sha = _carry_run(
		tmp_path, lambda f, b, m: [_comment(_marker("clean", f, 1)), _sync_comment(f, b, m)], whitespace_merge=True,
	)
	assert f"head={merge_sha} outcome=carry_skipped reason=patch_mismatch" in result.stdout, result.stdout + result.stderr
	assert "reason=freshness_sync_inherited" not in result.stdout
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize("from_status", ["findings", "pending", None])
def test_base_sync_of_a_head_that_was_not_clean_is_audited(tmp_path: Path, from_status: str | None) -> None:
	def comments(f: str, b: str, m: str) -> list:
		prior = [_comment(_marker(from_status, f, 1))] if from_status else []
		return prior + [_sync_comment(f, b, m)]
	result, calls, output, _from_sha, merge_sha = _carry_run(tmp_path, comments)
	assert f"head={merge_sha} outcome=carry_skipped reason=from_not_clean" in result.stdout, result.stdout + result.stderr
	assert "reason=base_sync_carry" not in result.stdout
	assert output.startswith("hold=true\n")
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_base_sync_marker_by_another_author_is_ignored(tmp_path: Path) -> None:
	result, calls, output, _from_sha, merge_sha = _carry_run(
		tmp_path, lambda f, b, m: [_comment(_marker("clean", f, 1)), _sync_comment(f, b, m, login="intruder")],
	)
	assert f"head={merge_sha} outcome=carry_skipped reason=freshness_unverified" in result.stdout, result.stdout + result.stderr
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert not any(len(call) > 1 and call[1].startswith("repos/o/r/commits/") for call in calls)


def test_base_sync_carry_never_overrides_findings_for_the_head(tmp_path: Path) -> None:
	result, calls, output, _from_sha, _merge_sha = _carry_run(
		tmp_path, lambda f, b, m: [_comment(_marker("clean", f, 1)), _sync_comment(f, b, m), _comment(_marker("findings", m, 2), comment_id=3)],
	)
	assert "reason=base_sync_carry" not in result.stdout
	assert "carry_skipped" not in result.stdout
	assert output.startswith("hold=true\n")


def test_base_sync_carry_kill_switch(tmp_path: Path) -> None:
	# The switch turns off the carry only; the existing freshness-sync
	# inheritance walk still decides a marker-less verified update.
	result, calls, output, from_sha, merge_sha = _carry_run(
		tmp_path, lambda f, b, m: [_comment(_marker("clean", f, 1)), _sync_comment(f, b, m)],
		env={"SECURITY_PASS_BASE_SYNC_CARRY_ENABLED": "false"},
	)
	assert f"head={merge_sha} outcome=carry_skipped reason=disabled" in result.stdout, result.stdout + result.stderr
	assert "reason=base_sync_carry" not in result.stdout
	assert f"head={merge_sha} outcome=clean reason=freshness_sync_inherited from={from_sha} hops=1" in result.stdout
	assert output == "hold=false\n"


def test_base_sync_carry_kill_switch_without_inheritance_helpers_audits(tmp_path: Path) -> None:
	repo, from_sha, base_parent, merge_sha = _carry_repo(tmp_path)
	pr = _pr()
	pr["head"]["sha"] = merge_sha
	result, calls, output = _run(
		tmp_path, "gate", pr=pr, comments=[_comment(_marker("clean", from_sha, 1)), _sync_comment(from_sha, base_parent, merge_sha)],
		cwd=repo, real_git=True, env={"SECURITY_PASS_BASE_SYNC_CARRY_ENABLED": "false"},
	)
	assert "outcome=carry_skipped reason=disabled" in result.stdout
	assert output == "hold=true\nhold_reason=audit_dispatched\n"


# --- #9479: retire superseded follow-ups after a clean audit -----------------

REVIEW_DISPATCH = ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"]
ISSUE_CLOSE = ["-f", "state=closed", "-f", "state_reason=not_planned"]


def _issue(number: int, body: str, login: str = "owner") -> dict:
	return {"number": number, "state": "open", "body": body, "user": {"login": login}}


def _branch_pr(number: int, head_ref: str, base: str = "ai/issue-7", head_repo: str = "o/r") -> dict:
	return {"number": number, "state": "open", "base": {"ref": base}, "head": {"ref": head_ref, "sha": OLD, "repo": {"full_name": head_repo}}}


def _patches(calls: list) -> list:
	return [call for call in calls if call[:3] == ["api", "-X", "PATCH"]]


def _audit_report(tmp_path: Path, outcome: str = "success", findings: str = "0", env: dict | None = None, **kwargs):
	report_env = {
		"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD,
		"SECURITY_PASS_AUDIT_OUTCOME": outcome, "SECURITY_PASS_FINDINGS": findings,
		"SECURITY_PASS_FINDING_IDS_B64": base64.b64encode(json.dumps([f"F-{n}" for n in range(1, int(findings or 0) + 1)]).encode("ascii")).decode("ascii"),
	}
	report_env.update(env or {})
	return _run(tmp_path, "report", comments=[_comment(_marker("pending", HEAD, 2))], env=report_env, **kwargs)


def test_clean_report_retires_superseded_followups(tmp_path: Path) -> None:
	issues = [[
		_issue(9399, _followup_body(finding_id="F-1")),
		_issue(9400, _followup_body(branch="ai/issue-8", finding_id="F-2")),
		_issue(9401, _followup_body(finding_id="F-3"), login="mallory"),
		_issue(9402, "- Integration branch: `ai/issue-7`"),
	]]
	prs = [[
		_branch_pr(9404, "ai/issue-9399"),
		_branch_pr(9405, "ai/issue-9399", head_repo="fork/r"),
		_branch_pr(9406, "ai/issue-93990"),
		_branch_pr(9407, "ai/issue-9400"),
		_branch_pr(9408, "ai/issue-9399-retry", base="main"),
	]]
	result, calls, _ = _audit_report(tmp_path, security_issues=issues, branch_prs=prs)
	assert result.returncode == 0
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker("clean", HEAD, 2))
	assert REVIEW_DISPATCH in calls
	assert ["api", "--paginate", "--slurp", "repos/o/r/pulls?state=open&base=ai%2Fissue-7&per_page=100"] in calls
	assert _patches(calls) == [
		["api", "-X", "PATCH", "repos/o/r/pulls/9404", "-f", "state=closed"],
		["api", "-X", "PATCH", "repos/o/r/issues/9399", *ISSUE_CLOSE],
	]
	for number in (9404, 9399):
		comment = next(call for call in calls if call[:2] == ["api", f"repos/o/r/issues/{number}/comments"])
		assert HEAD in comment[-1]
		assert comment[-1].endswith(f"<!-- ai:single-issue-security-pass-followup-retired:v1 head={HEAD} -->")
	# Retirement runs before the review re-dispatch.
	assert calls.index(["api", "-X", "PATCH", "repos/o/r/issues/9399", *ISSUE_CLOSE]) < calls.index(REVIEW_DISPATCH)
	assert "outcome=followups_retired issues=1 prs=1" in result.stdout
	assert result.stdout.strip().splitlines()[-1].startswith("SINGLE_ISSUE_SECURITY_PASS mode=report pr=42 head=" + HEAD + " outcome=clean")


@pytest.mark.parametrize("issue", [
	_issue(9400, _followup_body(branch="ai/issue-8")),
	_issue(9401, _followup_body(), login="mallory"),
	_issue(9402, "Some text\n- Integration branch: `ai/issue-7`"),
	{"number": 9403, "state": "open", "body": _followup_body(), "user": {"login": "owner"}, "pull_request": {"url": "x"}},
])
def test_clean_report_leaves_unrelated_issues_open(tmp_path: Path, issue: dict) -> None:
	prs = [[_branch_pr(9404, f"ai/issue-{issue['number']}")]]
	result, calls, _ = _audit_report(tmp_path, security_issues=[[issue]], branch_prs=prs)
	assert result.returncode == 0 and "outcome=clean" in result.stdout
	assert _patches(calls) == []
	assert not any(any(str(arg).startswith("repos/o/r/pulls?state=open&base=") for arg in call) for call in calls)
	assert REVIEW_DISPATCH in calls


def test_clean_report_followup_listing_failure_still_posts_marker_and_redispatches(tmp_path: Path) -> None:
	result, calls, _ = _audit_report(tmp_path, env={"FAKE_GH_FAIL": "followups"})
	assert result.returncode == 0
	assert "outcome=followup_retire_failed reason=listing_failed" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert posted[-1][-1].endswith(_marker("clean", HEAD, 2))
	assert REVIEW_DISPATCH in calls
	assert _patches(calls) == []
	assert "outcome=clean" in result.stdout


def test_clean_report_pr_listing_failure_still_closes_the_issue(tmp_path: Path) -> None:
	result, calls, _ = _audit_report(tmp_path, env={"FAKE_GH_FAIL": "branch_prs"}, security_issues=[[_issue(9399, _followup_body())]])
	assert result.returncode == 0 and "reason=pr_listing_failed" in result.stdout
	assert _patches(calls) == [["api", "-X", "PATCH", "repos/o/r/issues/9399", *ISSUE_CLOSE]]
	assert REVIEW_DISPATCH in calls


def test_clean_report_close_failures_fail_open(tmp_path: Path) -> None:
	result, calls, _ = _audit_report(tmp_path, env={"FAKE_GH_FAIL": "close"}, security_issues=[[_issue(9399, _followup_body())]], branch_prs=[[_branch_pr(9404, "ai/issue-9399")]])
	assert result.returncode == 0
	assert "reason=pr_close_failed item=9404" in result.stdout
	assert "reason=issue_close_failed item=9399" in result.stdout
	assert "outcome=followups_retired issues=0 prs=0" in result.stdout
	assert REVIEW_DISPATCH in calls and "outcome=clean" in result.stdout


def test_clean_report_comment_failure_still_closes_followups(tmp_path: Path) -> None:
	result, calls, _ = _audit_report(tmp_path, env={"FAKE_GH_FAIL": "followup_comment"}, security_issues=[[_issue(9399, _followup_body())]], branch_prs=[[_branch_pr(9404, "ai/issue-9399")]])
	assert result.returncode == 0
	assert "reason=comment_failed item=9404 kind=pr" in result.stdout
	assert "reason=comment_failed item=9399 kind=issue" in result.stdout
	assert _patches(calls) == [
		["api", "-X", "PATCH", "repos/o/r/pulls/9404", "-f", "state=closed"],
		["api", "-X", "PATCH", "repos/o/r/issues/9399", *ISSUE_CLOSE],
	]
	assert "outcome=followups_retired issues=1 prs=1" in result.stdout
	assert REVIEW_DISPATCH in calls and "outcome=clean" in result.stdout


@pytest.mark.parametrize("outcome, findings", [("success", "2"), ("failure", "")])
def test_findings_or_failed_report_never_retires_followups(tmp_path: Path, outcome: str, findings: str) -> None:
	result, calls, _ = _audit_report(tmp_path, outcome, findings, security_issues=[[_issue(9399, _followup_body())]], branch_prs=[[_branch_pr(9404, "ai/issue-9399")]])
	assert result.returncode == 0
	assert _patches(calls) == []
	assert not any("repos/o/r/issues?labels=ai:security&state=open&per_page=100" in call for call in calls)


FOLLOWUP_PR = _pr(base="ai/issue-7", head_ref="ai/issue-9399")
PARENT_PR = {"number": 42, "state": "open", "base": {"ref": "main"}, "head": {"ref": "ai/issue-7", "sha": HEAD, "repo": {"full_name": "o/r"}}}
HOLD_MARKER = f"<!-- ai:single-issue-security-pass-followup-hold:v1 parent=42 head={HEAD} -->"


def _guard(tmp_path: Path, parent_comments: list, followup_comments: list | None = None, pr: dict | None = None, head_prs: list | None = None, env: dict | None = None):
	followup_file = tmp_path / "followup_comments.json"
	followup_file.write_text(json.dumps(followup_comments or []), encoding="utf-8")
	guard_env = {"PR_NUMBER": "77", "PR_ISSUE_COMMENTS_FILE": str(followup_file), "GH_RETRY_MAX_ATTEMPTS": "1"}
	guard_env.update(env or {})
	return _run(tmp_path, "followup-guard", pr=pr or FOLLOWUP_PR, comments=parent_comments, head_prs=[PARENT_PR] if head_prs is None else head_prs, env=guard_env)


def _hold_posts(calls: list) -> list:
	return [call for call in calls if call[:2] == ["api", "repos/o/r/issues/77/comments"]]


def test_followup_merge_guard_refuses_into_clean_audited_head(tmp_path: Path) -> None:
	result, calls, _ = _guard(tmp_path, [_comment(_marker("pending", HEAD, 2)), _comment(_marker("clean", HEAD, 2), comment_id=2)])
	assert result.returncode == 0
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=refuse" in result.stdout.splitlines()
	assert "mode=followup_guard pr=77 parent=42 head=" + HEAD + " outcome=refuse reason=parent_head_clean comment=posted" in result.stdout
	assert ["api", "--paginate", "repos/o/r/pulls?state=open&head=o:ai%2Fissue-7&per_page=100"] in calls
	posts = _hold_posts(calls)
	assert len(posts) == 1 and posts[0][-1].endswith(HOLD_MARKER) and HEAD in posts[0][-1]


def test_followup_merge_guard_comments_once(tmp_path: Path) -> None:
	result, calls, _ = _guard(
		tmp_path,
		[_comment(_marker("clean", HEAD, 2))],
		followup_comments=[_comment("## Merge held\n\n" + HOLD_MARKER + "\n")],
	)
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=refuse" in result.stdout.splitlines()
	assert "comment=existing" in result.stdout
	assert _hold_posts(calls) == []


def test_followup_merge_guard_ignores_forged_clean_and_hold_markers(tmp_path: Path) -> None:
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2), login="mallory")])
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow" in result.stdout.splitlines()
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], followup_comments=[_comment(HOLD_MARKER, login="mallory")])
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=refuse" in result.stdout.splitlines()
	assert len(_hold_posts(calls)) == 1


def test_followup_merge_guard_allows_when_latest_marker_is_findings(tmp_path: Path) -> None:
	comments = [_comment(_marker("clean", HEAD, 2)), _comment(_marker("pending", HEAD, 3), comment_id=2), _comment(_marker("findings", HEAD, 3), comment_id=3)]
	result, calls, _ = _guard(tmp_path, comments)
	assert result.returncode == 0
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow" in result.stdout.splitlines()
	assert "reason=parent_head_findings" in result.stdout
	assert _hold_posts(calls) == []
	# A clean result for an older head does not cover the current one.
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", OLD, 1))])
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow" in result.stdout.splitlines()
	assert "reason=parent_head_unaudited" in result.stdout
	# A PR into the default branch makes no API call at all.
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], pr=_pr())
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow" in result.stdout.splitlines()
	assert "reason=default_base" in result.stdout and calls == []
	# So does a PR into an orchestrator integration branch.
	result, calls, _ = _guard(tmp_path, [], pr=_pr(base="orchestrator/project-9", head_ref="ai/issue-9399"))
	assert "reason=integration_base" in result.stdout and calls == []


def test_followup_merge_guard_allows_without_a_standalone_parent(tmp_path: Path) -> None:
	parent = dict(PARENT_PR, base={"ref": "release/v1"})
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], head_prs=[parent])
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow" in result.stdout.splitlines()
	assert "reason=no_standalone_parent" in result.stdout


@pytest.mark.parametrize("env, reason", [
	({"FAKE_GH_FAIL": "head_prs"}, "parent_lookup_failed"),
	({"FAKE_GH_FAIL": "identity"}, "author_unverifiable"),
	({"FAKE_GH_FAIL": "parent_comments"}, "markers_unverifiable"),
])
def test_followup_merge_guard_parent_lookup_failure_is_unverifiable(tmp_path: Path, env: dict, reason: str) -> None:
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], env=env)
	assert result.returncode == 0
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=unverifiable" in result.stdout.splitlines()
	assert f"reason={reason}" in result.stdout
	assert _hold_posts(calls) == []


def test_auto_merge_helper_runs_followup_guard_before_merge() -> None:
	text = (ROOT / "scripts" / "review_enable_auto_merge.sh").read_text(encoding="utf-8")
	head_bound = text.index('if [ -z "${_orch_pr_head_sha}" ] || [ "${_orch_pr_head_sha}" != "${INITIAL_HEAD_SHA}" ]; then')
	guard = text.index('review_single_issue_security_pass.sh" followup-guard')
	status = text.index('review_head_gate_post_status "${GITHUB_REPOSITORY}" "${INITIAL_HEAD_SHA}" success', head_bound)
	assert head_bound < guard < status
	assert guard < text.index("gh_retry gh pr merge")
	assert 'if [ "${_followup_guard_result}" != "allow" ]; then' in text[guard:status]
	assert "exit 0" in text[guard:status]
	# The guard runs again after the required-checks wait, before any --auto
	# merge, even when the wait returned without polling.
	wait = text.index("_pr_wait_for_required_checks", status)
	recheck = text.index("\nif ! run_security_followup_guard; then\n\texit 0\nfi\n", wait)
	assert "-gt 0 ] 2>/dev/null && ! run_security_followup_guard" not in text
	assert recheck < text.index("gh_retry gh pr merge")


def test_followup_merge_guard_honours_explicit_kill_switch(tmp_path: Path) -> None:
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], env={"SINGLE_ISSUE_SECURITY_PASS_ENABLED": "false"})
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=allow" in result.stdout.splitlines()
	assert "reason=disabled" in result.stdout and calls == []
	# Unset keeps the guard on: the auto-merge step does not pass the variable.
	result, _, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], env={"SINGLE_ISSUE_SECURITY_PASS_ENABLED": None})
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=refuse" in result.stdout.splitlines()


def test_followup_merge_guard_refuses_without_comment_when_own_comments_unreadable(tmp_path: Path) -> None:
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], env={"PR_ISSUE_COMMENTS_FILE": str(tmp_path / "missing.json"), "FAKE_GH_FAIL": "pr_comments"})
	assert result.returncode == 0
	assert "SINGLE_ISSUE_SECURITY_PASS_FOLLOWUP_MERGE=refuse" in result.stdout.splitlines()
	assert "reason=parent_head_clean comment=unverifiable" in result.stdout
	assert _hold_posts(calls) == []
	# A readable listing without the marker still posts the hold comment once.
	result, calls, _ = _guard(tmp_path, [_comment(_marker("clean", HEAD, 2))], env={"PR_ISSUE_COMMENTS_FILE": str(tmp_path / "missing.json")})
	assert "comment=posted" in result.stdout and len(_hold_posts(calls)) == 1


# --- Freshness-update heads reuse the approved head's clean audit ----------

SYNC_ONE = "e" * 40
BASE_ONE = "1" * 40
BASE_TWO = "2" * 40


def _sync_marker(from_head: str, merge: str, base: str, sync: int = 1) -> str:
	return (
		"**Branch updated from `main` before merging** (update 1; warning threshold 2).\n\n"
		f"<!-- ai:merge-base-sync:v1 pr=42 from={from_head} sync={sync} base={base} merge={merge} -->"
	)


def _merge_commit(first: str, second: str, committer: str = "web-flow", verified: bool = True) -> dict:
	return {"parents": [first, second], "verified": verified, "committer": committer}


def _inherit_run(tmp_path: Path, comments: list, commits: dict, mode: str = "gate", compare_status: str = "ahead"):
	support = tmp_path / "support"
	support.mkdir(exist_ok=True)
	(support / "review_head_gate.sh").write_text((ROOT / "scripts" / "review_head_gate.sh").read_text(encoding="utf-8"), encoding="utf-8")
	(support / "gh_helpers.sh").write_text('gh_retry() { "$@"; }\n', encoding="utf-8")
	(tmp_path / "commits.json").write_text(json.dumps(commits), encoding="utf-8")
	return _run(
		tmp_path,
		mode,
		comments=comments,
		env={"FAKE_GH_COMMITS": str(tmp_path / "commits.json"), "FAKE_GH_COMPARE_STATUS": compare_status},
	)


def test_verified_freshness_update_of_a_clean_head_needs_no_new_audit(tmp_path: Path) -> None:
	comments = [
		_comment("ok\n" + _marker("clean", OLD, 1), comment_id=1),
		_comment(_sync_marker(OLD, HEAD, BASE_ONE), comment_id=2),
	]
	result, calls, output = _inherit_run(tmp_path, comments, {HEAD: _merge_commit(OLD, BASE_ONE)})
	assert output == "hold=false\n", result.stdout + result.stderr
	assert f"outcome=clean reason=freshness_sync_inherited from={OLD} hops=1" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)


def test_chain_of_updates_walks_back_to_the_audited_head(tmp_path: Path) -> None:
	comments = [
		_comment("ok\n" + _marker("clean", OLD, 1), comment_id=1),
		_comment(_sync_marker(OLD, SYNC_ONE, BASE_ONE), comment_id=2),
		_comment(_sync_marker(SYNC_ONE, HEAD, BASE_TWO, sync=2), comment_id=3),
	]
	commits = {SYNC_ONE: _merge_commit(OLD, BASE_ONE), HEAD: _merge_commit(SYNC_ONE, BASE_TWO)}
	result, _calls, output = _inherit_run(tmp_path, comments, commits)
	assert output == "hold=false\n", result.stdout + result.stderr
	assert f"from={OLD} hops=2" in result.stdout


def test_status_mode_reports_an_inherited_clean_head(tmp_path: Path) -> None:
	comments = [
		_comment("ok\n" + _marker("clean", OLD, 1), comment_id=1),
		_comment(_sync_marker(OLD, HEAD, BASE_ONE), comment_id=2),
	]
	result, calls, _output = _inherit_run(tmp_path, comments, {HEAD: _merge_commit(OLD, BASE_ONE)}, mode="status")
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=clean" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize(
	"case",
	["from_findings", "not_github_merge", "unsigned", "wrong_first_parent", "base_parent_mismatch", "base_not_on_main", "foreign_marker", "unbound_marker", "commit_unreadable"],
)
def test_unverifiable_updates_are_audited_as_before(tmp_path: Path, case: str) -> None:
	prior = _marker("findings" if case == "from_findings" else "clean", OLD, 1)
	sync_body = _sync_marker(OLD, HEAD, BASE_ONE)
	if case == "unbound_marker":
		sync_body = "**Branch updated from `main` before merging** (update 1).\n\n" + f"<!-- ai:merge-base-sync:v1 pr=42 from={OLD} sync=1 -->"
	commit = _merge_commit(OLD, BASE_ONE)
	if case == "not_github_merge":
		commit = _merge_commit(OLD, BASE_ONE, committer="someone")
	elif case == "unsigned":
		commit = _merge_commit(OLD, BASE_ONE, verified=False)
	elif case == "wrong_first_parent":
		commit = _merge_commit(SYNC_ONE, BASE_ONE)
	elif case == "base_parent_mismatch":
		commit = _merge_commit(OLD, BASE_TWO)
	comments = [
		_comment("ok\n" + prior, comment_id=1),
		_comment(sync_body, login="intruder" if case == "foreign_marker" else "owner", comment_id=2),
	]
	commits = {} if case == "commit_unreadable" else {HEAD: commit}
	result, calls, output = _inherit_run(
		tmp_path, comments, commits, compare_status="diverged" if case == "base_not_on_main" else "ahead"
	)
	assert "freshness_sync_inherited" not in result.stdout
	assert output.startswith("hold=true\n"), result.stdout + result.stderr
	assert any(call[:2] == ["workflow", "run"] for call in calls)


def test_a_marker_on_the_update_head_itself_still_decides(tmp_path: Path) -> None:
	comments = [
		_comment("ok\n" + _marker("clean", OLD, 1), comment_id=1),
		_comment(_sync_marker(OLD, HEAD, BASE_ONE), comment_id=2),
		_comment(_marker("pending", HEAD, 2), comment_id=3),
	]
	result, calls, output = _inherit_run(tmp_path, comments, {HEAD: _merge_commit(OLD, BASE_ONE)})
	assert output == "hold=true\nhold_reason=audit_pending\n"
	assert "freshness_sync_inherited" not in result.stdout
	assert not any(call[:1] == ["api"] and "commits/" in call[1] for call in calls if len(call) > 1)


def test_missing_support_helpers_fall_back_to_an_audit(tmp_path: Path) -> None:
	comments = [
		_comment("ok\n" + _marker("clean", OLD, 1), comment_id=1),
		_comment(_sync_marker(OLD, HEAD, BASE_ONE), comment_id=2),
	]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output.startswith("hold=true\n") and "freshness_sync_inherited" not in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


# --- Convergence: per-PR audits, lost-audit retry, attempt-based budget ----


def _iso(age_minutes: float) -> str:
	return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _runs(tmp_path: Path, runs: list, total_count: int | None = None) -> str:
	path = tmp_path / "runs.json"
	payload = {"workflow_runs": runs}
	if total_count is not None:
		payload["total_count"] = total_count
	path.write_text(json.dumps(payload), encoding="utf-8")
	return str(path)


def _audit_run(status: str, conclusion: str | None, created_minutes: float, updated_minutes: float, pr: int = 42) -> dict:
	return {
		"display_title": f"AI Security Audit (PR #{pr})", "status": status, "conclusion": conclusion,
		"created_at": _iso(created_minutes), "updated_at": _iso(updated_minutes),
	}


def test_unreported_pending_cycles_do_not_use_the_cycle_budget(tmp_path: Path) -> None:
	# PR #9389's history: 3 completed clean audits on older heads, 5 audits
	# that were cancelled in the queue and never reported.
	heads = {n: f"{n:040x}" for n in range(1, 8)}
	comments = []
	for n, status in [(1, "clean"), (2, "clean"), (3, "pending"), (4, "pending"), (5, "pending"), (6, "clean"), (7, "pending")]:
		comments.append(_comment(_marker("pending", heads[n], n), comment_id=2 * n - 1, age_hours=20 - n))
		if status != "pending":
			comments.append(_comment(_marker(status, heads[n], n), comment_id=2 * n, age_hours=19.5 - n))
	comments.append(_comment(_marker("pending", HEAD, 8), comment_id=99, age_hours=7))
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "outcome=dispatched cycle=9" in result.stdout and "exhausted_retry" not in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "(cycle 4 of 5)" in posted[-1][-1] and posted[-1][-1].endswith(_marker("pending", HEAD, 9))
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)


def test_attempts_cap_bounds_audits_that_never_report(tmp_path: Path) -> None:
	comments = [_comment(_marker("pending", f"{n:040x}", n), comment_id=n, age_hours=30 - n) for n in range(1, 11)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	# Ten attempts (twice the 5-cycle cap) are spent: the exhausted path runs,
	# which still grants this head its bounded retries.
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "reason=exhausted_retry" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "head attempt 1 of 2" in posted[-1][-1] and posted[-1][-1].endswith(_marker("pending", HEAD, 11))
	comments += [_comment(_marker("pending", HEAD, n), comment_id=n, age_hours=7) for n in (11, 12)]
	result, calls, output = _run(tmp_path, "gate", comments=comments)
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n"
	assert not any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize("value, dispatched", [("12", True), ("10", False), ("3", False), ("abc", False)])
def test_attempts_cap_override_is_validated(tmp_path: Path, value: str, dispatched: bool) -> None:
	comments = [_comment(_marker("pending", f"{n:040x}", n), comment_id=n, age_hours=30 - n) for n in range(1, 11)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"SECURITY_PASS_MAX_AUDIT_ATTEMPTS": value})
	# Below the cycle cap (3) the cap is raised to it; invalid falls back to 10.
	assert ("reason=exhausted_retry" not in result.stdout) is dispatched
	assert "outcome=dispatched cycle=11" in result.stdout


def test_cancelled_audit_is_retried_before_the_stale_window(tmp_path: Path) -> None:
	runs = _runs(tmp_path, [
		_audit_run("completed", "cancelled", 39, 30),
		_audit_run("completed", "success", 300, 280),
		_audit_run("in_progress", None, 5, 1, pr=7),
	])
	comments = [_comment(_marker("pending", HEAD, 1), age_hours=40 / 60)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_RUNS": runs})
	assert output == "hold=true\nhold_reason=audit_dispatched\n"
	assert "reason=audit_run_lost" in result.stdout and "outcome=dispatched cycle=2" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	# The lost attempt used no cycle.
	assert "(cycle 1 of 5)" in posted[-1][-1]
	listings = [call for call in calls if any("/actions/workflows/ai-security-audit.yml/runs" in arg for arg in call)]
	assert len(listings) == 1


@pytest.mark.parametrize(
	"run_specs, marker_minutes, expected_state, listed",
	[
		([("queued", None, 39, 39, 42)], 40, "live", True),
		([("completed", "cancelled", 39, 5, 42)], 40, "live", True),
		([("completed", "cancelled", 39, 30, 7)], 40, "unknown", True),
		([("completed", "cancelled", 200, 190, 42)], 40, "unknown", True),
		(None, 40, "unknown", True),
		([("completed", "cancelled", 39, 30, 42)], 10, "", False),
	],
)
def test_pending_audit_holds_without_positive_evidence_of_loss(tmp_path: Path, run_specs, marker_minutes: int, expected_state: str, listed: bool) -> None:
	# Timestamps are built here, not at collection, so a slow run cannot age them.
	runs = None if run_specs is None else [_audit_run(*spec[:4], pr=spec[4]) for spec in run_specs]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, runs)} if runs is not None else {}
	comments = [_comment(_marker("pending", HEAD, 1), age_hours=marker_minutes / 60)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env=env)
	assert output == "hold=true\nhold_reason=audit_pending\n"
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert (f"audit_run={expected_state}" in result.stdout) if expected_state else ("audit_run=" not in result.stdout)
	assert any(any("/actions/workflows/" in arg for arg in call) for call in calls) is listed


def test_cancel_check_can_be_disabled(tmp_path: Path) -> None:
	runs = _runs(tmp_path, [_audit_run("completed", "cancelled", 39, 30)])
	comments = [_comment(_marker("pending", HEAD, 1), age_hours=40 / 60)]
	result, calls, output = _run(tmp_path, "gate", comments=comments, env={"FAKE_GH_RUNS": runs, "SECURITY_PASS_PENDING_CANCEL_CHECK_MINUTES": "0"})
	assert output == "hold=true\nhold_reason=audit_pending\n"
	assert not any(any("/actions/workflows/" in arg for arg in call) for call in calls)


@pytest.mark.parametrize(
	"active, env, throttled",
	[(3, {}, True), (2, {}, False), (5, {"SECURITY_PASS_MAX_CONCURRENT_AUDITS": "6"}, False), (9, {"SECURITY_PASS_MAX_CONCURRENT_AUDITS": "0"}, False)],
)
def test_concurrent_audits_are_throttled_without_using_a_cycle(tmp_path: Path, active: int, env: dict, throttled: bool) -> None:
	runs = [_audit_run("in_progress" if n % 2 else "queued", None, 5, 1, pr=100 + n) for n in range(active)]
	runs.append(_audit_run("completed", "success", 50, 30, pr=200))
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_RUNS": _runs(tmp_path, runs), **env})
	if throttled:
		assert output == "hold=true\nhold_reason=audit_throttled\n" and f"reason=audit_throttled active={active} max=3" in result.stdout
		assert not any(call[:2] == ["workflow", "run"] for call in calls)
		assert not any(call[:2] == ["api", "repos/o/r/issues/42/comments"] for call in calls)
	else:
		assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=1" in result.stdout


def test_throttle_fails_open_when_runs_cannot_be_listed(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "gate", env={"FAKE_GH_RUNS": _runs(tmp_path, []), "FAKE_GH_FAIL": "runs"})
	assert output == "hold=true\nhold_reason=audit_dispatched\n" and "outcome=dispatched cycle=1" in result.stdout


def test_status_mode_never_lists_audit_runs_for_a_fresh_head(tmp_path: Path) -> None:
	result, calls, output = _run(tmp_path, "status", env={"FAKE_GH_RUNS": _runs(tmp_path, [])})
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=needs_audit" in result.stdout
	assert not any(any("/actions/workflows/" in arg for arg in call) for call in calls)


@pytest.mark.parametrize("earlier, redispatch", [("pending", False), ("clean", True)])
def test_report_last_cycle_ignores_unreported_attempts(tmp_path: Path, earlier: str, redispatch: bool) -> None:
	# Cycle 5 with four earlier attempts: they used the budget only if they
	# reported. Findings on the last budget cycle re-run the review.
	extra = []
	for n in range(1, 5):
		extra.append(_comment(_marker("pending", f"{n:040x}", n), comment_id=10 + n))
		if earlier != "pending":
			extra.append(_comment(_marker(earlier, f"{n:040x}", n), comment_id=20 + n))
	result, calls, _ = _report(tmp_path, "success", "2", cycle=5, extra_comments=extra)
	assert "outcome=findings cycle=5" in result.stdout
	review = ["workflow", "run", "ai-review.yml", "-R", "o/r", "--ref", "main", "-f", "pr_number=42"]
	assert (review in calls) is redispatch


def _since(tmp_path: Path, comments: list, ancestors: str = "", env: dict | None = None):
	run_env = {"SECURITY_PASS_PR_NUMBER": "42", "SECURITY_PASS_HEAD_SHA": HEAD, "FAKE_GIT_ANCESTORS": ancestors}
	run_env.update(env or {})
	return _run(tmp_path, "since", comments=comments, env=run_env)


def test_since_names_the_newest_clean_ancestor(tmp_path: Path) -> None:
	first, second, foreign = "1" * 40, "2" * 40, "3" * 40
	comments = [
		_comment(_marker("clean", first, 1), comment_id=1),
		_comment(_marker("clean", second, 2), comment_id=2),
		_comment(_marker("clean", foreign, 3), comment_id=3),
		_comment(_marker("clean", "4" * 40, 4), login="other[bot]", comment_id=4),
	]
	result, calls, _ = _since(tmp_path, comments, ancestors=f"{first},{second},{'4' * 40}")
	assert result.stdout.strip() == second
	assert "outcome=delta" in result.stderr


@pytest.mark.parametrize(
	"comments, ancestors, env, reason",
	[
		([], "", {}, "no_clean_ancestor"),
		([_comment(_marker("findings", "1" * 40, 1))], "1" * 40, {}, "no_clean_ancestor"),
		([_comment(_marker("clean", "1" * 40, 1))], "", {}, "no_clean_ancestor"),
		([_comment(_marker("clean", HEAD, 1))], HEAD, {}, "no_clean_ancestor"),
		([_comment(_marker("clean", "1" * 40, 1))], "1" * 40, {"FAKE_GIT_HEAD": "e" * 40}, "checkout_mismatch"),
		([_comment(_marker("clean", "1" * 40, 1))], "1" * 40, {"FAKE_GH_FAIL": "identity"}, "author_unverifiable"),
		([_comment(_marker("clean", "1" * 40, 1))], "1" * 40, {"SECURITY_PASS_PR_NUMBER": ""}, "missing_input"),
	],
)
def test_since_falls_back_to_the_full_range(tmp_path: Path, comments: list, ancestors: str, env: dict, reason: str) -> None:
	result, _, _ = _since(tmp_path, comments, ancestors=ancestors, env=env)
	assert result.returncode == 0 and result.stdout.strip() == ""
	assert f"outcome=full reason={reason}" in result.stderr


def test_audit_concurrency_is_per_pr_in_both_workflows() -> None:
	audit = yaml.safe_load(AUDIT.read_text(encoding="utf-8"))
	wrapper = yaml.safe_load(AUDIT_TEMPLATE.read_text(encoding="utf-8"))
	reusable_group = audit["concurrency"]["group"]
	wrapper_group = wrapper["concurrency"]["group"]
	for group in (reusable_group, wrapper_group):
		assert "inputs.pr_number" in group and "format('pr-{0}', inputs.pr_number)" in group and "'default'" in group
	# Distinct prefixes keep the caller/callee deadlock away (see the comment
	# above the reusable workflow's concurrency block).
	assert reusable_group.startswith("security-audit-reusable-") and not wrapper_group.startswith("security-audit-reusable-")
	assert audit["concurrency"]["cancel-in-progress"] is False and wrapper["concurrency"]["cancel-in-progress"] is False
	# The gate finds the run behind a pending marker by this title suffix.
	assert "format('Security Audit (PR #{0})', inputs.pr_number)" in audit["run-name"]
	assert "format('AI Security Audit (PR #{0})', inputs.pr_number)" in wrapper["run-name"]


def test_audit_scope_step_wires_the_delta_helper() -> None:
	audit = yaml.safe_load(AUDIT.read_text(encoding="utf-8"))
	steps = {step.get("name"): step for step in audit["jobs"]["security-audit"]["steps"]}
	scope = steps["Verify audit data and resolve scope"]
	assert scope["env"]["SECURITY_PASS_PR_NUMBER"] == "${{ inputs.pr_number }}"
	assert "SECURITY_PASS_DELTA_AUDIT_ENABLED" in scope["env"]
	assert 'review_single_issue_security_pass.sh" since' in scope["run"]
	assert 'echo "SECURITY_AUDIT_DIFF_SINCE=${audit_since}" >> "$GITHUB_ENV"' in scope["run"]
	assert "SECURITY_PASS_MAX_AUDIT_ATTEMPTS" in steps["Report single-issue security pass"]["env"]


# --- Issue #9942: lost post-cap head attempts ---------------------------------


def _lost_attempt_comments(ages_hours: list, failed_cycles: tuple = ()) -> list:
	# A failed cycle 5 on HEAD fills the 5-cycle budget (cycles 1-4 have no
	# marker and still count); each age is a pending head attempt above it.
	comments = [_comment(_marker("failed", HEAD, 5), comment_id=1, age_hours=ages_hours[0] + 1)]
	for offset, age in enumerate(ages_hours):
		cycle = 6 + offset
		comments.append(_comment(_marker("pending", HEAD, cycle), comment_id=10 + cycle, age_hours=age))
		if cycle in failed_cycles:
			comments.append(_comment(_marker("failed", HEAD, cycle), comment_id=30 + cycle, age_hours=age - 0.01))
	return comments


def _lost_run(marker_age_hours: float, pr: int = 42) -> dict:
	# Dispatched a few seconds before its marker, cancelled long ago.
	minutes = marker_age_hours * 60
	return _audit_run("completed", "cancelled", minutes + 0.1, minutes - 5, pr=pr)


def _listing_calls(calls: list) -> list:
	return [call for call in calls if any("/actions/workflows/" in arg for arg in call)]


def test_lost_head_markers_do_not_count_toward_head_attempts(tmp_path: Path) -> None:
	runs = [_lost_run(9), _lost_run(7)]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, runs, total_count=2)}
	comments = _lost_attempt_comments([9, 7])
	result, calls, output = _run(tmp_path, "gate", comments=comments, env=env)
	assert output == "hold=true\nhold_reason=audit_dispatched\n", result.stdout
	assert "reason=exhausted_retry" in result.stdout
	assert "outcome=retry reason=lost_audit_not_counted head_attempts=0 lost_attempts=2" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)
	assert not any(call[:2] == ["api", "repos/o/r/issues/42/labels"] for call in calls)
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "head attempt 1 of 2" in posted[-1][-1] and posted[-1][-1].endswith(_marker("pending", HEAD, 8))
	# The lost classification and the throttle share one listing.
	assert len(_listing_calls(calls)) == 1
	result, calls, _ = _run(tmp_path, "status", comments=comments, env=env)
	assert "SINGLE_ISSUE_SECURITY_PASS_STATE=needs_audit" in result.stdout
	assert len(_listing_calls(calls)) == 1


def test_lost_markers_beyond_budget_count_again(tmp_path: Path) -> None:
	ages = [10, 9, 8, 7]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, [_lost_run(age) for age in ages], total_count=4)}
	result, calls, output = _run(tmp_path, "gate", comments=_lost_attempt_comments(ages), env=env)
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n", result.stdout
	assert "reason=exhausted_without_completed_audit cycle=5 head_attempts=2 lost_attempts=4" in result.stdout
	assert "lost_audit_not_counted" not in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	assert ["api", "repos/o/r/issues/42/labels", "-f", "labels[]=ai:security-pass-failed"] in calls


def test_later_marker_live_run_does_not_make_earlier_marker_live(tmp_path: Path) -> None:
	# The cycle-7 audit was dispatched 10 seconds before its marker and is
	# still running: it belongs to cycle 7, not to cycle 6's window.
	runs = [_lost_run(9), _audit_run("in_progress", None, 7 * 60 + 10 / 60, 1)]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, runs, total_count=2)}
	result, calls, output = _run(tmp_path, "gate", comments=_lost_attempt_comments([9, 7]), env=env)
	assert output == "hold=true\nhold_reason=audit_dispatched\n", result.stdout
	assert "reason=lost_audit_not_counted head_attempts=1 lost_attempts=1" in result.stdout
	posted = [call for call in calls if call[:2] == ["api", "repos/o/r/issues/42/comments"]]
	assert "head attempt 2 of 2" in posted[-1][-1]


def _run_old_lost_markers(tmp_path: Path):
	runs = [_lost_run(30), _lost_run(28)]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, runs, total_count=2)}
	return _run(tmp_path, "gate", comments=_lost_attempt_comments([30, 28]), env=env)


def test_listing_window_widens_to_oldest_pending_head_marker(tmp_path: Path) -> None:
	result, calls, _ = _run_old_lost_markers(tmp_path)
	listings = _listing_calls(calls)
	assert len(listings) == 1, result.stdout
	created = [arg for arg in listings[0] if arg.startswith("created=>=")]
	assert len(created) == 1
	start = dt.datetime.strptime(created[0][len("created=>="):], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
	expected = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30, minutes=2)
	assert abs((start - expected).total_seconds()) < 120
	assert start < dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)


def test_two_cancelled_runs_older_than_24h_dispatch_retry(tmp_path: Path) -> None:
	result, calls, output = _run_old_lost_markers(tmp_path)
	assert output == "hold=true\nhold_reason=audit_dispatched\n", result.stdout
	assert "reason=lost_audit_not_counted head_attempts=0 lost_attempts=2" in result.stdout
	assert any(call[:2] == ["workflow", "run"] for call in calls)


@pytest.mark.parametrize("total_count", [150, None])
def test_truncated_listing_counts_markers_outside_returned_window(tmp_path: Path, total_count) -> None:
	# More runs exist than were returned (or the count is missing): the oldest
	# returned run is 8 hours old, so cycle 6's window (from 9h2m ago) is not
	# covered and counts; cycle 7's window is covered and is lost.
	runs = [_lost_run(7), _audit_run("completed", "success", 8 * 60, 7 * 60, pr=300)]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, runs, total_count=total_count)}
	result, calls, output = _run(tmp_path, "gate", comments=_lost_attempt_comments([9, 7]), env=env)
	assert output == "hold=true\nhold_reason=audit_dispatched\n", result.stdout
	assert "reason=lost_audit_not_counted head_attempts=1 lost_attempts=1" in result.stdout


def test_lost_audit_retries_zero_restores_counting(tmp_path: Path) -> None:
	ages = [30, 28]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, [_lost_run(age) for age in ages], total_count=2), "SECURITY_PASS_LOST_AUDIT_RETRIES": "0"}
	result, calls, output = _run(tmp_path, "gate", comments=_lost_attempt_comments(ages), env=env)
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n", result.stdout
	assert "head_attempts=2 lost_attempts=0" in result.stdout
	assert not any(call[:2] == ["workflow", "run"] for call in calls)
	# No classification, so no listing (and never a widened one).
	assert _listing_calls(calls) == []


def test_unavailable_run_listing_counts_lost_markers(tmp_path: Path) -> None:
	comments = _lost_attempt_comments([9, 7])
	for env in ({}, {"FAKE_GH_RUNS": _runs(tmp_path, [_lost_run(9), _lost_run(7)], total_count=2), "FAKE_GH_FAIL": "runs"}, {"FAKE_GH_RUNS": _runs(tmp_path, [], total_count=0)}):
		result, calls, output = _run(tmp_path, "gate", comments=comments, env=env)
		assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n", (env, result.stdout)
		assert "head_attempts=2 lost_attempts=0" in result.stdout, env


def test_failed_head_attempt_still_counts_next_to_a_lost_one(tmp_path: Path) -> None:
	runs = [_lost_run(9), _lost_run(7)]
	env = {"FAKE_GH_RUNS": _runs(tmp_path, runs, total_count=2)}
	result, calls, output = _run(tmp_path, "gate", comments=_lost_attempt_comments([9, 7], failed_cycles=(6,)), env=env)
	assert output == "hold=true\nhold_reason=audit_dispatched\n", result.stdout
	assert "reason=lost_audit_not_counted head_attempts=1 lost_attempts=1" in result.stdout
	result, calls, output = _run(tmp_path, "gate", comments=_lost_attempt_comments([9, 7], failed_cycles=(6, 7)), env=env)
	assert output == "hold=true\nhold_reason=exhausted_without_completed_audit\n", result.stdout
	assert "head_attempts=2 lost_attempts=0" in result.stdout
