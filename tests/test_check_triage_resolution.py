"""Already-resolved gate for ai:check-triage issues (issue #7095).

Fixtures follow the incident: triage issue #6945 was filed for a failed `CI`
run on PR #6938 (job `orchestrate-poll (3)`); PR #6938 merged and PR #6941
merged the test correction, so the default branch passed again. The gate must
close such an issue with evidence instead of asking for /answer, and keep the
normal path (with the failing job's traceback) whenever that cannot be proven.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_triage_resolution.py"
CLARIFY_WF = ROOT / ".github" / "workflows" / "clarify.yml"
REPO = "shubhodeep1/coding-workflows"
PIPELINE = "pipeline-bot"
ISSUE = 6945
PR = 6938
RUN_ID = 37000000001
WORKFLOW_ID = 4242
MERGE_SHA = "a" * 40
EVIDENCE_SHA = "e" * 40
PR_HEAD_SHA = "b" * 40
FAILED_JOB_ID = 91000000003


def _load():
	sys.path.insert(0, str(ROOT / "scripts"))
	spec = importlib.util.spec_from_file_location("check_triage_resolution", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


mod = _load()


def _body(check_run_id: int | None = None, details: str | None = None, extra: str = "") -> str:
	details = details or f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
	lines = [
		"<!-- check-failure-triage:fp=" + "f" * 64 + " -->",
		"<!-- check-failure-triage:gen=1 -->",
		"<!-- check-failure-triage:root=" + "c" * 64 + " -->",
		f"<!-- check-failure-triage:pr={PR} -->",
		"",
		"## Automated CI failure triage (generation 1 of max 3)",
		"",
		f"- **Repository:** `{REPO}`",
		f"- **Pull request:** https://github.com/{REPO}/pull/{PR} (`ai/issue-6937`)",
		"- **Failing check:** `CI` (conclusion: `failure`)",
	]
	if check_run_id:
		lines.append(f"- **Check run id:** `{check_run_id}`")
	lines += [
		f"- **Check details:** {details}",
		f"- **Head SHA:** `{PR_HEAD_SHA}`",
		"",
		"---",
		"",
		"Diagnosis: the security-pass cap test expects the old rule.",
		extra,
	]
	return "\n".join(lines)


def _issue(labels=("ai:check-triage",), body=None, association="OWNER", user_type="User", login="maintainer", state="open"):
	return {
		"number": ISSUE,
		"state": state,
		"body": _body() if body is None else body,
		"labels": [{"name": name} for name in labels],
		"user": {"type": user_type, "login": login},
		"author_association": association,
		"repository_url": f"https://api.github.com/repos/{REPO}",
	}


def _pr(merged=True, merge_sha=MERGE_SHA, head_repo=REPO):
	return {
		"number": PR,
		"merged": merged,
		"merge_commit_sha": merge_sha if merged else None,
		"head": {"sha": PR_HEAD_SHA, "repo": {"full_name": head_repo}},
		"base": {"ref": "main", "repo": {"full_name": REPO}},
	}


def _run(**overrides):
	run = {
		"id": RUN_ID, "name": "CI", "event": "pull_request", "workflow_id": WORKFLOW_ID,
		"head_sha": PR_HEAD_SHA, "repository": {"full_name": REPO}, "pull_requests": [{"number": PR}],
	}
	run.update(overrides)
	return run


def _jobs(conclusions: dict[str, str], *, id_base=FAILED_JOB_ID):
	jobs = [
		{"id": id_base + index, "name": name, "status": "completed", "conclusion": conclusion}
		for index, (name, conclusion) in enumerate(conclusions.items())
	]
	return {"total_count": len(jobs), "jobs": jobs}


PR_JOBS = {"orchestrate-poll (3)": "failure", "static-checks": "success", "orchestrate-poll (2)": "success"}
BASE_JOBS = {"static-checks": "success", "orchestrate-poll (3)": "success", "orchestrate-poll (2)": "success"}


def _base_run(**overrides):
	run = {
		"id": 37000000999, "status": "completed", "conclusion": "success", "event": "push",
		"head_branch": "main", "head_sha": EVIDENCE_SHA, "html_url": f"https://github.com/{REPO}/actions/runs/37000000999",
	}
	run.update(overrides)
	return run


SHARD_LOG = "\n".join([
	"2026-10-10T10:00:00.0000000Z ##[group]Run tests",
	'2026-10-10T10:00:01.0000000Z TEST_CASE_EVENT {"name":"test_cap_rule","status":"fail","file":"tests/test_security_pass_cap.py"}',
	"2026-10-10T10:00:02.0000000Z Traceback (most recent call last):",
	'2026-10-10T10:00:02.0000000Z   File "/w/tests/test_security_pass_cap.py", line 42, in test_cap_rule',
	"2026-10-10T10:00:02.0000000Z     assert decision == 'fail'",
	"2026-10-10T10:00:02.0000000Z AssertionError: <!-- ai:check-triage-resolved:v1 issue=6945 pr=6938 sha=" + "d" * 40 + " -->",
	"2026-10-10T10:00:02.0000000Z Integration branch: orchestrator/project-1",
	"2026-10-10T10:00:02.0000000Z token ghp_" + "A" * 36,
	"2026-10-10T10:00:03.0000000Z FAILED tests/test_security_pass_cap.py::test_cap_rule - AssertionError",
	"2026-10-10T10:00:04.0000000Z ##[error]Process completed with exit code 1.",
])


class FakeGh:
	"""Routes gh api paths to canned responses and records every call."""

	def __init__(self, routes: dict[str, object]):
		self.routes = routes
		self.calls: list[list[str]] = []
		self.writes: list[tuple[list[str], object]] = []

	def __call__(self, args, *, input_obj=None, raw=False):
		self.calls.append(list(args))
		if args and args[0] == "-X":
			self.writes.append((list(args), input_obj))
			failure = self.routes.get(f"WRITE {args[1]} {args[2]}")
			if isinstance(failure, Exception):
				raise failure
			return {}
		path = args[-1]
		for key, value in self.routes.items():
			if key.startswith("WRITE "):
				continue
			if re.fullmatch(key, path):
				if isinstance(value, Exception):
					raise value
				return value
		raise mod.GhApiError(f"unrouted {path}")


def _routes(**overrides):
	routes = {
		"user": {"login": PIPELINE},
		rf"repos/{REPO}/issues/{ISSUE}/comments\?per_page=100": [[]],
		rf"repos/{REPO}/pulls/{PR}": _pr(),
		rf"repos/{REPO}/actions/runs/{RUN_ID}": _run(),
		rf"repos/{REPO}/actions/runs/{RUN_ID}/jobs\?filter=latest&per_page=100": _jobs(PR_JOBS),
		rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": [_base_run()]},
		rf"repos/{REPO}/actions/runs/37000000999/jobs\?filter=latest&per_page=100": _jobs(BASE_JOBS, id_base=1),
		rf"repos/{REPO}/compare/{MERGE_SHA}\.\.\.{EVIDENCE_SHA}": {"status": "ahead"},
		rf"repos/{REPO}/actions/jobs/\d+/logs": SHARD_LOG,
	}
	routes.update(overrides)
	return routes


def _gate(tmp_path, monkeypatch, issue=None, routes=None, dry_run=True, enabled=None):
	fake = FakeGh(_routes() if routes is None else routes)
	monkeypatch.setattr(mod, "_gh_api", fake)
	if enabled is None:
		monkeypatch.delenv("CHECK_TRIAGE_RESOLVED_GATE_ENABLED", raising=False)
	else:
		monkeypatch.setenv("CHECK_TRIAGE_RESOLVED_GATE_ENABLED", enabled)
	issue_path = tmp_path / "issue.json"
	issue_path.write_text(json.dumps(_issue() if issue is None else issue), encoding="utf-8")
	args = argparse.Namespace(issue_json=str(issue_path), repo=REPO, issue_number=ISSUE, default_branch="main", dry_run=dry_run)
	result, rc = mod.gate(args)
	return result, rc, fake


def _comment(body, login=PIPELINE, comment_id=1):
	return {"id": comment_id, "user": {"login": login}, "body": body}


# --- resolved ---------------------------------------------------------------


def test_incident_6945_is_closed_with_evidence_and_no_pr_write(tmp_path, monkeypatch):
	result, rc, fake = _gate(tmp_path, monkeypatch)
	assert rc == 0
	assert result["status"] == "resolved"
	assert result["evidence_sha"] == EVIDENCE_SHA
	assert result["merge_sha"] == MERGE_SHA
	writes = result["writes"]
	assert [(w["method"], w["path"]) for w in writes] == [
		("POST", f"repos/{REPO}/issues/{ISSUE}/comments"),
		("POST", f"repos/{REPO}/issues/{ISSUE}/labels"),
		("PATCH", f"repos/{REPO}/issues/{ISSUE}"),
	]
	body = writes[0]["payload"]["body"]
	assert body.endswith(f"<!-- ai:check-triage-resolved:v1 issue={ISSUE} pr={PR} sha={EVIDENCE_SHA} -->")
	assert "orchestrate-poll (3): success" in body
	assert writes[1]["payload"] == {"labels": ["ai:closed"]}
	assert writes[2]["payload"] == {"state": "closed", "state_reason": "completed"}
	assert not any("/answer" in json.dumps(w) for w in writes)
	assert not any(f"pulls/{PR}" in w["path"] for w in writes)
	assert fake.writes == []  # dry run


def test_resolved_writes_go_through_gh_when_not_dry_run(tmp_path, monkeypatch):
	result, rc, fake = _gate(tmp_path, monkeypatch, dry_run=False)
	assert rc == 0 and result["status"] == "resolved"
	assert [w[0][:3] for w in fake.writes] == [
		["-X", "POST", f"repos/{REPO}/issues/{ISSUE}/comments"],
		["-X", "POST", f"repos/{REPO}/issues/{ISSUE}/labels"],
		["-X", "PATCH", f"repos/{REPO}/issues/{ISSUE}"],
	]


def test_write_failure_after_verification_exits_3(tmp_path, monkeypatch):
	routes = _routes(**{f"WRITE PATCH repos/{REPO}/issues/{ISSUE}": mod.GhApiError("rc=1")})
	result, rc, _ = _gate(tmp_path, monkeypatch, routes=routes, dry_run=False)
	assert rc == 3
	assert result["status"] == "write_failed"


def test_check_run_triage_requires_tip_success_bound_to_tip_sha(tmp_path, monkeypatch):
	issue = _issue(body=_body(check_run_id=FAILED_JOB_ID, details="https://ci.example.com/check/1"))
	checks_path = rf"repos/{REPO}/commits/{EVIDENCE_SHA}/check-runs\?check_name=CI&filter=latest&per_page=100"
	ok = {"total_count": 1, "check_runs": [{"head_sha": EVIDENCE_SHA, "status": "completed", "conclusion": "success"}]}
	routes = _routes(**{rf"repos/{REPO}/commits/main": {"sha": EVIDENCE_SHA}, checks_path: ok})
	result, rc, _ = _gate(tmp_path, monkeypatch, issue=issue, routes=routes)
	assert rc == 0 and result["status"] == "resolved"

	stale = {"total_count": 1, "check_runs": [{"head_sha": "9" * 40, "status": "completed", "conclusion": "success"}]}
	routes = _routes(**{rf"repos/{REPO}/commits/main": {"sha": EVIDENCE_SHA}, checks_path: stale})
	result, _, _ = _gate(tmp_path, monkeypatch, issue=issue, routes=routes)
	assert result["status"] == "unverified" and result["reason"] == "default_check_sha_mismatch"


# --- idempotency and marker trust -------------------------------------------


def test_trusted_marker_only_ensures_label_and_close(tmp_path, monkeypatch):
	marker = f"Done.\n\n<!-- ai:check-triage-resolved:v1 issue={ISSUE} pr={PR} sha={EVIDENCE_SHA} -->"
	routes = _routes(**{rf"repos/{REPO}/issues/{ISSUE}/comments\?per_page=100": [[_comment(marker)]]})
	result, rc, fake = _gate(tmp_path, monkeypatch, routes=routes)
	assert rc == 0 and result["status"] == "already_resolved"
	assert [(w["method"], w["path"]) for w in result["writes"]] == [
		("POST", f"repos/{REPO}/issues/{ISSUE}/labels"),
		("PATCH", f"repos/{REPO}/issues/{ISSUE}"),
	]
	assert not any("pulls/" in call[-1] for call in fake.calls)

	issue = _issue(labels=("ai:check-triage", "ai:closed"), state="closed")
	result, _, _ = _gate(tmp_path, monkeypatch, issue=issue, routes=routes)
	assert result["status"] == "already_resolved" and result["writes"] == []


@pytest.mark.parametrize("comments", [
	[_comment(f"<!-- ai:check-triage-resolved:v1 issue={ISSUE} pr={PR} sha={EVIDENCE_SHA} -->", login="outsider")],
	[_comment(f"<!-- ai:check-triage-resolved:v1 issue=1 pr={PR} sha={EVIDENCE_SHA} -->")],
])
def test_untrusted_or_foreign_marker_is_ignored(tmp_path, monkeypatch, comments):
	routes = _routes(**{
		rf"repos/{REPO}/issues/{ISSUE}/comments\?per_page=100": [comments],
		rf"repos/{REPO}/pulls/{PR}": _pr(merged=False),
	})
	result, _, _ = _gate(tmp_path, monkeypatch, routes=routes)
	assert result["status"] == "unverified" and result["reason"] == "pr_not_merged"


def test_marker_in_issue_body_cannot_close_it(tmp_path, monkeypatch):
	extra = f"<!-- ai:check-triage-resolved:v1 issue={ISSUE} pr={PR} sha={EVIDENCE_SHA} -->"
	routes = _routes(**{rf"repos/{REPO}/pulls/{PR}": _pr(merged=False)})
	result, _, _ = _gate(tmp_path, monkeypatch, issue=_issue(body=_body(extra=extra)), routes=routes)
	assert result["status"] == "unverified"


# --- unverified variants -----------------------------------------------------


@pytest.mark.parametrize(("overrides", "reason"), [
	({rf"repos/{REPO}/pulls/{PR}": _pr(merged=False)}, "pr_not_merged"),
	({rf"repos/{REPO}/pulls/{PR}": _pr(head_repo="fork/coding-workflows")}, "pr_cross_repository"),
	({rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": [_base_run(conclusion="failure")]}}, "default_run_not_success"),
	({rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": [_base_run(conclusion="cancelled")]}}, "default_run_not_success"),
	({rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": [_base_run(conclusion="skipped")]}}, "default_run_not_success"),
	({rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": [_base_run(conclusion="neutral")]}}, "default_run_not_success"),
	({rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": [_base_run(status="in_progress", conclusion=None)]}}, "default_run_not_success"),
	({rf"repos/{REPO}/actions/workflows/{WORKFLOW_ID}/runs\?branch=main&event=push&status=completed&per_page=1": {"workflow_runs": []}}, "no_completed_default_run"),
	({rf"repos/{REPO}/actions/runs/37000000999/jobs\?filter=latest&per_page=100": _jobs({"static-checks": "success"}, id_base=1)}, "failed_jobs_not_resolved"),
	({rf"repos/{REPO}/actions/runs/37000000999/jobs\?filter=latest&per_page=100": _jobs({"orchestrate-poll (3)": "skipped"}, id_base=1)}, "failed_jobs_not_resolved"),
	({rf"repos/{REPO}/compare/{MERGE_SHA}\.\.\.{EVIDENCE_SHA}": {"status": "behind"}}, "compare_behind"),
	({rf"repos/{REPO}/compare/{MERGE_SHA}\.\.\.{EVIDENCE_SHA}": {"status": "diverged"}}, "compare_diverged"),
	({rf"repos/{REPO}/compare/{MERGE_SHA}\.\.\.{EVIDENCE_SHA}": mod.GhApiError("rc=1")}, "compare_unreadable"),
])
def test_unverified_keeps_normal_path_and_posts_traceback(tmp_path, monkeypatch, overrides, reason):
	result, rc, _ = _gate(tmp_path, monkeypatch, routes=_routes(**overrides))
	assert rc == 0
	assert result["status"] == "unverified" and result["reason"] == reason
	assert result["traceback_posted"] == [FAILED_JOB_ID]
	assert len(result["writes"]) == 1
	write = result["writes"][0]
	assert write["path"] == f"repos/{REPO}/issues/{ISSUE}/comments"
	assert not any(w["method"] == "PATCH" or w["path"].endswith("/labels") for w in result["writes"])


@pytest.mark.parametrize("run", [
	_run(repository={"full_name": "other/repo"}),
	_run(pull_requests=[{"number": 1}], head_sha="1" * 40),
	_run(name="Other workflow"),
	_run(event="push"),
])
def test_run_from_another_repository_or_pr_is_not_evidence(tmp_path, monkeypatch, run):
	result, _, _ = _gate(tmp_path, monkeypatch, routes=_routes(**{rf"repos/{REPO}/actions/runs/{RUN_ID}": run}))
	assert result["status"] == "unverified" and result["reason"] == "run_identity_mismatch"
	assert result["writes"] == []


def test_unreadable_pr_is_unverified_without_writes(tmp_path, monkeypatch):
	result, rc, _ = _gate(tmp_path, monkeypatch, routes=_routes(**{rf"repos/{REPO}/pulls/{PR}": mod.GhApiError("rc=1")}))
	assert rc == 0 and result["status"] == "unverified" and result["reason"] == "pr_unreadable"
	assert result["writes"] == []


def test_details_url_for_another_repository_is_ignored(tmp_path, monkeypatch):
	issue = _issue(body=_body(details=f"https://github.com/other/repo/actions/runs/{RUN_ID}"))
	result, _, _ = _gate(tmp_path, monkeypatch, issue=issue)
	assert result["status"] == "unverified" and result["reason"] == "run_id_unavailable"


def test_no_failed_jobs_never_resolves(tmp_path, monkeypatch):
	routes = _routes(**{rf"repos/{REPO}/actions/runs/{RUN_ID}/jobs\?filter=latest&per_page=100": _jobs(BASE_JOBS)})
	result, _, _ = _gate(tmp_path, monkeypatch, routes=routes)
	assert result["status"] == "unverified" and result["reason"] == "no_failed_jobs"


def test_traceback_comment_is_redacted_and_neutralized(tmp_path, monkeypatch):
	routes = _routes(**{rf"repos/{REPO}/pulls/{PR}": _pr(merged=False)})
	result, _, _ = _gate(tmp_path, monkeypatch, routes=routes)
	body = result["writes"][0]["payload"]["body"]
	assert "test_cap_rule" in body
	assert "tests/test_security_pass_cap.py" in body
	assert "Traceback (most recent call last)" in body
	assert "ghp_" + "A" * 36 not in body
	assert body.count("<!--") == 1
	assert body.endswith(f"<!-- ai:check-triage-traceback:v1 issue={ISSUE} job={FAILED_JOB_ID} -->")
	assert not re.search(r"(?m)^\s*Integration branch:", body)


def test_traceback_is_posted_once_per_job(tmp_path, monkeypatch):
	posted = _comment(f"x\n<!-- ai:check-triage-traceback:v1 issue={ISSUE} job={FAILED_JOB_ID} -->")
	routes = _routes(**{
		rf"repos/{REPO}/pulls/{PR}": _pr(merged=False),
		rf"repos/{REPO}/issues/{ISSUE}/comments\?per_page=100": [[posted]],
	})
	result, _, fake = _gate(tmp_path, monkeypatch, routes=routes)
	assert result["status"] == "unverified" and result["writes"] == []
	assert not any(call[-1].endswith("/logs") for call in fake.calls)


def test_unreadable_logs_do_not_fail_the_gate(tmp_path, monkeypatch):
	routes = _routes(**{
		rf"repos/{REPO}/pulls/{PR}": _pr(merged=False),
		rf"repos/{REPO}/actions/jobs/\d+/logs": mod.GhApiError("rc=1"),
	})
	result, rc, _ = _gate(tmp_path, monkeypatch, routes=routes)
	assert rc == 0 and result["status"] == "unverified" and result["writes"] == []


def test_identity_unavailable_is_unverified_without_writes(tmp_path, monkeypatch):
	result, rc, _ = _gate(tmp_path, monkeypatch, routes=_routes(user=mod.GhApiError("rc=1")))
	assert rc == 0 and result["status"] == "unverified" and result["writes"] == []


# --- no API calls -------------------------------------------------------------


@pytest.mark.parametrize("issue", [
	_issue(labels=()),
	_issue(association="NONE", login="outsider"),
	_issue(user_type="Bot", association="NONE", login="other[bot]"),
])
def test_non_triage_or_untrusted_issue_makes_no_api_call(tmp_path, monkeypatch, issue):
	result, rc, fake = _gate(tmp_path, monkeypatch, issue=issue)
	assert rc == 0 and result["status"] == "not_triage"
	assert fake.calls == []


def test_disabled_gate_makes_no_api_call(tmp_path, monkeypatch):
	result, rc, fake = _gate(tmp_path, monkeypatch, enabled="false")
	assert rc == 0 and result["status"] == "disabled" and fake.calls == []


@pytest.mark.parametrize("body", [
	"no marker at all",
	_body().replace("- **Failing check:** `CI` (conclusion: `failure`)\n", ""),
	_body(extra="- **Failing check:** `Other` (conclusion: `failure`)"),
	_body() + f"\n<!-- check-failure-triage:pr={PR + 1} -->",
])
def test_malformed_triage_body_is_unverified_without_calls(tmp_path, monkeypatch, body):
	result, rc, fake = _gate(tmp_path, monkeypatch, issue=_issue(body=body))
	assert rc == 0 and result["status"] == "unverified" and result["reason"].startswith("malformed_")
	assert fake.calls == []


# --- pure helpers ---------------------------------------------------------------


@pytest.mark.parametrize(("runs", "expected"), [
	([], "missing"),
	([{"head_sha": EVIDENCE_SHA, "status": "completed", "conclusion": "neutral"}], "neutral"),
	([{"head_sha": EVIDENCE_SHA, "status": "completed", "conclusion": "skipped"}], "skipped"),
	([{"head_sha": EVIDENCE_SHA, "status": "queued", "conclusion": None}], "pending"),
	([{"head_sha": EVIDENCE_SHA, "status": "completed", "conclusion": "success"}], "success"),
])
def test_classify_check_runs(runs, expected):
	assert mod.classify_check_runs({"total_count": len(runs), "check_runs": runs}, EVIDENCE_SHA) == expected


def test_classify_check_runs_rejects_incomplete_listing():
	runs = [{"head_sha": EVIDENCE_SHA, "status": "completed", "conclusion": "success"}]
	assert mod.classify_check_runs({"total_count": 2, "check_runs": runs}, EVIDENCE_SHA) == "incomplete"


def test_shard_jobs_resolved_requires_exact_unique_success():
	assert mod.shard_jobs_resolved(["a (1)"], [{"name": "a (1)", "status": "completed", "conclusion": "success"}])
	assert not mod.shard_jobs_resolved(["a (1)"], [{"name": "a (2)", "status": "completed", "conclusion": "success"}])
	assert not mod.shard_jobs_resolved([], [])
	duplicate = [{"name": "a (1)", "status": "completed", "conclusion": "success"}] * 2
	assert not mod.shard_jobs_resolved(["a (1)"], duplicate)


# --- workflow wiring --------------------------------------------------------------


def test_clarify_route_wires_the_gate():
	yaml = pytest.importorskip("yaml")
	workflow = yaml.safe_load(CLARIFY_WF.read_text(encoding="utf-8"))
	steps = workflow["jobs"]["clarify"]["steps"]
	stage = next(step for step in steps if step.get("name") == "Stage workflow support files")
	assert "check_triage_resolution.py" in stage["run"]
	route = next(step for step in steps if step.get("name") == "Decide clarify route")
	script = route["run"]
	assert route["env"]["CHECK_TRIAGE_RESOLVED_GATE_ENABLED"] == "${{ vars.CHECK_TRIAGE_RESOLVED_GATE_ENABLED || 'true' }}"
	call = 'python3 scripts/check_triage_resolution.py gate --issue-json "${ISSUE_META_FILE}"'
	assert call in script
	assert script.index("scripts/security_dependency.py") < script.index(call)
	assert script.index(call) < script.index("Only a verified orchestrator fast path")
	assert "AI_PHASE_GATE_V1 phase=clarify gate=check_triage_resolved reason=verified outcome=skip issue=${ISSUE_NUMBER}" in script
	gate_block = script[script.index(call):script.index("Only a verified orchestrator fast path")]
	assert 'if [ "${triage_rc}" -eq 3 ]; then' in gate_block and "exit 1" in gate_block
	assert 'SKIP_CODEX="true"' in gate_block
	assert "::warning::Check-triage resolution gate failed" in gate_block
	assert 'echo "check_triage_resolved=${CHECK_TRIAGE_RESOLVED}" >> "$GITHUB_OUTPUT"' in script
	assert '[ "${CHECK_TRIAGE_RESOLVED}" != "true" ]' in script[script.index("Only a verified orchestrator fast path"):]
