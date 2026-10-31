#!/usr/bin/env python3
"""Tests for scripts/persist_postmerge_verify.py (issue #8427, phase P5 of
docs/plans/persistent-unattended-workflows-plan.md): check after merge."""

from __future__ import annotations

import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import acceptance_check as ac  # noqa: E402
import attempt_journal  # noqa: E402
import persist_postmerge_verify as ppv  # noqa: E402

REPO = "owner/repo"
LOGIN = "pipeline-bot"
FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "persist" / "postmerge_release_gate_runs.json").read_text(encoding="utf-8"))
MARKER = dict(FIXTURE["marker"])
MERGE_SHA = MARKER["merge_sha"]
MERGED_AT = ppv.parse_ts(MARKER["merged_at"])
ISSUE_PR_STATUS = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"
ORCHESTRATE_POLL = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"


class FakeGh:
	"""Routes ppv._gh calls; records every call's argument list."""

	def __init__(self) -> None:
		self.calls: list[list[str]] = []
		self.issues: list = []
		self.comments: dict[int, list] = {}
		self.workflow_runs: dict = {"workflow_runs": []}
		self.compare_status = "ahead"
		self.jobs: dict[str, dict] = {}
		self.issue: dict = {"labels": []}
		self.fail: tuple[str, ...] = ()

	def __call__(self, args: list[str]) -> bytes | None:
		self.calls.append(list(args))
		endpoint = next(a for a in args if not a.startswith("-"))
		if any(token in endpoint for token in self.fail):
			return None
		method = args[args.index("-X") + 1] if "-X" in args else "GET"
		if method != "GET":
			return b"{}"
		if "/issues?labels=" in endpoint:
			return json.dumps(self.issues).encode()
		if endpoint.endswith("/comments?per_page=100"):
			number = int(endpoint.split("/issues/")[1].split("/")[0])
			return json.dumps(self.comments.get(number, [])).encode()
		if "/actions/workflows/" in endpoint:
			return json.dumps(self.workflow_runs).encode()
		if "/compare/" in endpoint:
			return json.dumps({"status": self.compare_status}).encode()
		if "/jobs?" in endpoint:
			run_id = endpoint.split("/runs/")[1].split("/")[0]
			return json.dumps(self.jobs.get(run_id, {"jobs": []})).encode()
		if re.match(r"^repos/[^/]+/[^/]+/issues/\d+$", endpoint):
			return json.dumps(self.issue).encode()
		return None

	def writes(self) -> list[list[str]]:
		return [c for c in self.calls if "-X" in c]

	def endpoints(self) -> list[str]:
		return [next(a for a in c if not a.startswith("-")) for c in self.calls]


@pytest.fixture()
def fake(monkeypatch):
	for name in ("PERSIST_POSTMERGE_VERIFY_ENABLED", "PERSIST_POSTMERGE_WINDOW_HOURS", "PERSIST_POSTMERGE_MAX_PER_TICK",
			"PERSIST_MAX_APPROACHES", "PERSIST_BUDGET_ENABLED", "WORKFLOW_HEAL_VERIFY_ENABLED", "PERSIST_JOURNAL_ENABLED",
			"RUNTIME_DIR"):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.setenv("PERSIST_POSTMERGE_VERIFY_ENABLED", "true")
	gh = FakeGh()
	monkeypatch.setattr(ppv, "_gh", gh)
	monkeypatch.setattr(attempt_journal, "_gh", lambda args: pytest.fail(f"unexpected journal call {args}"))
	return gh


def _comment(body: str, login: str = LOGIN, cid: int = 1, created: str = "2026-10-19T00:00:00Z") -> dict:
	return {"id": cid, "body": body, "user": {"login": login}, "created_at": created}


def _plan_comment(post_merge: dict | None, login: str = LOGIN, none_reason: str = "") -> dict:
	done = ac._empty("valid", "plan")
	done["checks"] = [{"name": "CI"}]
	done["post_merge"] = post_merge
	done["post_merge_none"] = none_reason
	body = "Implementation Plan\n\nACCEPTANCE: check CI\n\n" + ac.marker_line(done) + "\n" + ac.PROPOSAL_TRAILER
	return _comment(body, login=login, cid=100)


def _arm(*extra: str, labels: str = "[]") -> int:
	return ppv.main(["arm", "--repo", REPO, "--issue", "12", "--pr", str(MARKER["pr"]), "--merge-sha", MERGE_SHA,
		"--merged-at", MARKER["merged_at"], "--base", "main", "--pr-labels-json", labels, "--trusted-login", LOGIN, *extra])


# --- arm ----------------------------------------------------------------------


def test_arm_posts_fixture_marker_and_label(fake):
	fake.comments[12] = [_plan_comment({"workflow": MARKER["workflow"], "job": MARKER["job"]})]
	assert _arm() == 0
	writes = fake.writes()
	assert len(writes) == 2
	comment = writes[0]
	assert comment[0] == f"repos/{REPO}/issues/12/comments" and comment[2] == "POST"
	body = comment[comment.index("-f") + 1][len("body="):]
	assert body.startswith(ppv.HEADER_PENDING + "\n")
	assert ppv.marker_line(MARKER) in body.split("\n")
	assert writes[1] == [f"repos/{REPO}/issues/12/labels", "-X", "POST", "-f", "labels[]=ai:verify-pending"]
	assert len(fake.calls) <= 5


def test_arm_writes_journal_entry_when_journal_on(fake, monkeypatch, tmp_path):
	monkeypatch.setenv("PERSIST_JOURNAL_ENABLED", "true")
	monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
	journal_calls: list[list[str]] = []
	monkeypatch.setattr(attempt_journal, "_gh", lambda args: journal_calls.append(list(args)) or b"{}")
	fake.comments[12] = [_plan_comment({"workflow": MARKER["workflow"], "job": MARKER["job"]})]
	assert _arm() == 0
	assert len(journal_calls) == 1 and journal_calls[0][0] == f"repos/{REPO}/issues/12/comments"
	assert len(fake.calls) + len(journal_calls) <= 5
	assert list(tmp_path.iterdir()) == []


def test_arm_post_merge_none_records_ok_without_label(fake, capsys):
	fake.comments[12] = [_plan_comment(None, none_reason="docs only")]
	assert _arm() == 0
	assert fake.writes() == []
	assert "outcome=ok reason=no_postmerge_workflow issue=12" in capsys.readouterr().err


def test_arm_no_done_check_marker(fake, capsys):
	fake.comments[12] = [_comment("Implementation Plan\n" + ac.PROPOSAL_TRAILER)]
	assert _arm() == 0
	assert fake.writes() == []
	assert "reason=no_postmerge_workflow" in capsys.readouterr().err


def test_arm_ignores_untrusted_plan_comment(fake):
	fake.comments[12] = [_plan_comment({"workflow": MARKER["workflow"], "job": None}, login="mallory")]
	assert _arm() == 0
	assert fake.writes() == []


def test_arm_skips_skip_label_and_disabled_and_invalid_without_calls(fake, monkeypatch, capsys):
	assert _arm(labels='["ai:skip-activation-verify"]') == 0
	assert ppv.main(["arm", "--repo", REPO, "--issue", "12", "--pr", "5", "--merge-sha", "zz", "--merged-at", "x",
		"--base", "main", "--trusted-login", LOGIN]) == 0
	monkeypatch.setenv("PERSIST_POSTMERGE_VERIFY_ENABLED", "false")
	assert _arm() == 0
	err = capsys.readouterr().err
	assert "reason=skip_activation_verify" in err and "reason=invalid_input" in err and "reason=disabled" in err
	assert fake.calls == []


def test_arm_heal_owned_reads_issue_only_with_heal_verify(fake, monkeypatch, capsys):
	monkeypatch.setenv("WORKFLOW_HEAL_VERIFY_ENABLED", "true")
	fake.issue = {"labels": [{"name": "ai:workflow-heal"}]}
	assert _arm() == 0
	assert fake.endpoints() == [f"repos/{REPO}/issues/12"]
	assert "reason=heal_owned" in capsys.readouterr().err


def test_arm_is_idempotent_for_the_same_merge(fake):
	fake.comments[12] = [_plan_comment({"workflow": MARKER["workflow"], "job": MARKER["job"]}),
		_comment(ppv.pending_comment(MARKER, 24), cid=200)]
	assert _arm() == 0
	assert [w[0] for w in fake.writes()] == [f"repos/{REPO}/issues/12/labels"]


# --- markers ------------------------------------------------------------------


def test_marker_validation_rejects_bad_fields():
	assert ppv.normalise_marker(MARKER) == MARKER
	for key, value in (("pr", 0), ("pr", True), ("merge_sha", "abc"), ("workflow", "ci.yml"), ("job", ""),
			("job", "x" * 101), ("branch", "a..b"), ("branch", "a b"), ("merged_at", "2026-10-20"), ("state", "held")):
		assert ppv.normalise_marker(dict(MARKER, **{key: value})) is None, key


def test_parse_markers_trust_and_header_rules():
	good = _comment(ppv.pending_comment(MARKER, 24))
	forged = _comment(ppv.pending_comment(MARKER, 24), login="mallory")
	wrong_header = _comment(ppv.HEADER_FAILED + "\n\n" + ppv.marker_line(MARKER))
	assert ppv.parse_markers([forged, wrong_header], LOGIN) == []
	assert ppv.parse_markers([good], "") == []
	assert ppv.parse_markers([good], LOGIN) == [MARKER]
	# No literal < or > survives inside the marker JSON.
	line = ppv.marker_line(dict(MARKER, job="a<b>c"))
	assert "<b>" not in line and line.endswith(" -->")


def test_active_marker_and_replan_count():
	failed = dict(MARKER, state="recurred")
	newer = dict(MARKER, merge_sha="4" * 40)
	assert ppv.active_marker([MARKER]) == MARKER
	assert ppv.active_marker([MARKER, failed]) is None
	assert ppv.active_marker([MARKER, failed, newer]) == newer
	assert ppv.replan_count([MARKER, failed, newer, dict(newer, state="recurred")]) == 2


def test_select_run_ignores_cancelled_and_prefers_merge_commit():
	runs = [dict(r) for r in FIXTURE["runs"]["workflow_runs"]]
	cancelled = dict(runs[1], id=40876853000, conclusion="cancelled", created_at="2026-10-20T10:01:00Z")
	assert ppv.select_run({"workflow_runs": [cancelled] + runs}, MARKER)["id"] == 40876853022
	only_later = [runs[3], runs[2]]
	assert ppv.select_run(only_later, MARKER)["id"] == 40932430968
	assert ppv.select_run([runs[0]], MARKER) is None
	assert ppv.select_run([dict(runs[1], head_branch="dev")], MARKER) is None


def test_job_verdict_matches_matrix_names():
	jobs = {"jobs": [{"name": "gate (ubuntu)", "conclusion": "success"}, {"name": "gate (mac)", "conclusion": "failure"}]}
	assert ppv.job_verdict(jobs, "gate") == ("recurred", "gate (mac)")
	assert ppv.job_verdict({"jobs": [{"name": "gate (ubuntu)", "conclusion": "success"}]}, "gate") == ("held", "")
	assert ppv.job_verdict(jobs, "other") == (None, "")


def test_replay_release_gate_fixture():
	for run in FIXTURE["runs"]["workflow_runs"]:
		key = str(run["id"])
		if key not in FIXTURE["expected"]:
			continue
		selected = ppv.select_run([run], MARKER)
		assert selected is not None
		verdict, _ = ppv.job_verdict(FIXTURE["jobs"][key], MARKER["job"])
		result = ppv.decide(MARKER, selected, True, verdict, MERGED_AT + timedelta(hours=1), 24)
		assert result == FIXTURE["expected"][key], key


def test_labels_after_reopen_drops_phase_and_pending_labels():
	current = [{"name": "ai:merged"}, {"name": "ai:verify-pending"}, {"name": "bug"}, {"name": "ai:engine-claude"}]
	assert ppv.labels_after_reopen(current, "ai:planning") == ["bug", "ai:engine-claude", "ai:planning"]


def test_phase_labels_mirror_label_helpers_and_label_is_registered():
	helpers = (REPO_ROOT / "scripts" / "label_helpers.sh").read_text(encoding="utf-8")
	phase = json.loads(re.search(r"^_AI_PHASE_LABELS='(\[.*\])'$", helpers, re.M).group(1))
	assert tuple(phase) == ppv.PHASE_LABELS
	assert ppv.LABEL not in phase
	assert '["ai:verify-pending"]="c5def5"' in helpers
	assert '["ai:verify-pending"]="Merged; waiting for the check after merge"' in helpers
	contract = json.loads((REPO_ROOT / ".github" / "ai" / "label_contract.v1.json").read_text(encoding="utf-8"))
	assert contract["labels"]["ai:verify-pending"] == {"color": "c5def5", "description": "Merged; waiting for the check after merge"}


# --- check --------------------------------------------------------------------


def _candidate(number: int = 12, labels: tuple[str, ...] = ("ai:merged", "ai:verify-pending", "bug"), body: str = "Fix it",
		closed_at: str = "2026-10-20T10:00:00Z", updated_at: str = "2026-10-20T10:00:00Z") -> dict:
	return {"number": number, "state": "closed", "closed_at": closed_at, "updated_at": updated_at,
		"labels": [{"name": n} for n in labels], "body": body}


def _run(run_id: int, conclusion: str, head_sha: str = MERGE_SHA, created: str = "2026-10-20T10:05:00Z") -> dict:
	return {"id": run_id, "path": MARKER["workflow"], "head_branch": "main", "head_sha": head_sha, "status": "completed",
		"conclusion": conclusion, "created_at": created, "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}


def _check(fake, monkeypatch, tmp_path, *, now_hours: float = 1.0, runs_cache: dict | None = None) -> dict:
	monkeypatch.setattr(ppv, "_now", lambda: MERGED_AT + timedelta(hours=now_hours))
	out = tmp_path / "out.json"
	argv = ["check", "--repo", REPO, "--out", str(out), "--trusted-login", LOGIN]
	if runs_cache is not None:
		runs_file = tmp_path / "runs.json"
		runs_file.write_text(json.dumps(runs_cache), encoding="utf-8")
		argv += ["--runs-file", str(runs_file)]
	assert ppv.main(argv) == 0
	return json.loads(out.read_text(encoding="utf-8"))


def _armed(fake, marker: dict = MARKER, number: int = 12, extra: list | None = None) -> None:
	fake.issues = [_candidate(number)]
	fake.comments[number] = (extra or []) + [_comment(ppv.pending_comment(marker, 24), cid=300)]


def test_check_held_from_cache_without_compare(fake, monkeypatch, tmp_path, capsys):
	_armed(fake)
	fake.jobs["501"] = {"jobs": [{"name": "release-gate", "conclusion": "success"}]}
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(501, "success")]})
	assert out == {"processed": 1, "replan": []}
	assert not any("/compare/" in e or "/actions/workflows/" in e for e in fake.endpoints())
	assert fake.writes() == [[f"repos/{REPO}/issues/12/labels/ai%3Averify-pending", "-X", "DELETE"]]
	assert "outcome=held" in capsys.readouterr().err


def test_check_recurred_from_failed_job_reopens_in_planning(fake, monkeypatch, tmp_path):
	_armed(fake)
	fake.workflow_runs = {"workflow_runs": [_run(502, "failure", head_sha="5" * 40)]}
	fake.jobs["502"] = {"jobs": [{"name": "release-gate", "conclusion": "failure"}]}
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": []})
	assert out == {"processed": 1, "replan": [12]}
	patch = fake.writes()[0]
	assert patch[:5] == [f"repos/{REPO}/issues/12", "-X", "PATCH", "-f", "state=open"]
	labels = [a[len("labels[]="):] for a in patch if a.startswith("labels[]=")]
	assert labels == ["bug", "ai:planning"]
	comment = fake.writes()[1]
	body = comment[comment.index("-f") + 1][len("body="):]
	assert body.startswith(ppv.HEADER_FAILED + "\n") and "actions/runs/502" in body and "release-gate" in body
	assert ppv.marker_line(dict(MARKER, state="recurred")) in body.split("\n")
	# list + comments + workflow runs + compare + jobs + 2 writes; within 1 + 8 x MAX_PER_TICK.
	assert len(fake.calls) == 7 and len(fake.calls) <= 1 + 8 * 3
	queries = [e for e in fake.endpoints() if "/actions/workflows/" in e]
	assert queries == [f"repos/{REPO}/actions/workflows/release-gate.yml/runs?branch=main&status=completed"
		f"&created=%3E%3D2026-10-20T10%3A00%3A00Z&per_page=20"]


def test_check_recurred_from_failed_run_without_job(fake, monkeypatch, tmp_path):
	_armed(fake, marker=dict(MARKER, job=None))
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(503, "failure")]})
	assert out["replan"] == [12]
	assert not any("/jobs?" in e for e in fake.endpoints())


def test_check_pending_inside_window_and_unverified_after(fake, monkeypatch, tmp_path, capsys):
	_armed(fake)
	assert _check(fake, monkeypatch, tmp_path, now_hours=1)["replan"] == []
	assert fake.writes() == []
	assert "outcome=pending reason=no_run" in capsys.readouterr().err
	fake.calls.clear()
	assert _check(fake, monkeypatch, tmp_path, now_hours=25)["replan"] == []
	writes = fake.writes()
	assert writes[0][2] == "DELETE"
	body = writes[1][writes[1].index("-f") + 1][len("body="):]
	assert body.startswith(ppv.HEADER_UNVERIFIED) and "within 24 h" in body
	assert "outcome=unverified" in capsys.readouterr().err


def test_check_not_contained_waits(fake, monkeypatch, tmp_path, capsys):
	_armed(fake)
	fake.compare_status = "diverged"
	_check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(504, "failure", head_sha="6" * 40)]})
	assert fake.writes() == []
	assert any("/compare/" in e for e in fake.endpoints())
	assert "reason=not_contained" in capsys.readouterr().err


def test_check_forged_marker_only_removes_label(fake, monkeypatch, tmp_path, capsys):
	fake.issues = [_candidate()]
	fake.comments[12] = [_comment(ppv.pending_comment(MARKER, 24), login="mallory")]
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(505, "failure")]})
	assert out["replan"] == []
	assert [w[2] for w in fake.writes()] == ["DELETE"]
	assert "reason=marker_absent" in capsys.readouterr().err


def test_check_heal_and_orchestrator_skips(fake, monkeypatch, tmp_path, capsys):
	monkeypatch.setenv("WORKFLOW_HEAL_VERIFY_ENABLED", "true")
	fake.issues = [_candidate(12, labels=("ai:verify-pending", "ai:workflow-heal")),
		_candidate(13, body="x\n**Orchestrator metadata** (do not edit)", updated_at="2026-10-20T11:00:00Z")]
	out = _check(fake, monkeypatch, tmp_path)
	assert out == {"processed": 2, "replan": []}
	assert [w[2] for w in fake.writes()] == ["DELETE", "DELETE"]
	assert not any("/comments" in e for e in fake.endpoints())
	err = capsys.readouterr().err
	assert "reason=heal_owned issue=12" in err and "reason=orchestrator_managed issue=13" in err


def test_check_cap_reached_escalates_without_replan(fake, monkeypatch, tmp_path, capsys):
	history = []
	for index, sha in enumerate(("7" * 40, "8" * 40, "9" * 40)):
		old = dict(MARKER, merge_sha=sha)
		history.append(_comment(ppv.pending_comment(old, 24), cid=10 + 2 * index))
		history.append(_comment(ppv.failed_comment(old, _run(600 + index, "failure"), "", "x"), cid=11 + 2 * index))
	_armed(fake, extra=history)
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(506, "failure")]})
	assert out["replan"] == []
	labels = [a[len("labels[]="):] for a in fake.writes()[0] if a.startswith("labels[]=")]
	assert labels == ["bug", "ai:needs-human"]
	assert "outcome=recurred reason=escalated issue=12" in capsys.readouterr().err


def test_check_budget_owner_with_stub_budget_script(fake, monkeypatch, tmp_path, capsys):
	monkeypatch.setenv("PERSIST_BUDGET_ENABLED", "true")
	(tmp_path / "persist_budget.py").write_text("# stub\n", encoding="utf-8")
	monkeypatch.setattr(ppv, "__file__", str(tmp_path / "persist_postmerge_verify.py"))
	_armed(fake, marker=dict(MARKER, job=None))
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(507, "failure")]})
	assert out["replan"] == []
	assert fake.writes()[0][2] == "PATCH"
	assert "reason=budget_owner" in capsys.readouterr().err


def test_check_disabled_cleans_only_expired_labels(fake, monkeypatch, tmp_path, capsys):
	monkeypatch.setenv("PERSIST_POSTMERGE_VERIFY_ENABLED", "false")
	fake.issues = [_candidate(12, closed_at="2026-10-19T00:00:00Z"), _candidate(13, closed_at="2026-10-21T09:00:00Z")]
	out = _check(fake, monkeypatch, tmp_path, now_hours=25)
	assert out == {"processed": 0, "replan": []}
	assert fake.writes() == [[f"repos/{REPO}/issues/12/labels/ai%3Averify-pending", "-X", "DELETE"]]
	assert len(fake.calls) == 2
	assert "outcome=cleaned reason=disabled_window_passed issue=12" in capsys.readouterr().err


def test_check_disabled_with_no_labels_makes_one_read(fake, monkeypatch, tmp_path):
	monkeypatch.setenv("PERSIST_POSTMERGE_VERIFY_ENABLED", "false")
	_check(fake, monkeypatch, tmp_path)
	assert len(fake.calls) == 1 and fake.writes() == []


def test_check_honours_max_per_tick(fake, monkeypatch, tmp_path):
	monkeypatch.setenv("PERSIST_POSTMERGE_MAX_PER_TICK", "2")
	fake.issues = [_candidate(n, updated_at=f"2026-10-20T1{n % 10}:00:00Z") for n in (21, 22, 23, 24)]
	for n in (21, 22, 23, 24):
		fake.comments[n] = [_comment(ppv.pending_comment(MARKER, 24))]
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": []})
	assert out["processed"] == 2
	reads = [e for e in fake.endpoints() if e.endswith("/comments?per_page=100")]
	assert reads == [f"repos/{REPO}/issues/21/comments?per_page=100", f"repos/{REPO}/issues/22/comments?per_page=100"]


def test_check_gh_failures_exit_zero(fake, monkeypatch, tmp_path, capsys):
	_armed(fake)
	fake.fail = ("/comments",)
	assert _check(fake, monkeypatch, tmp_path)["replan"] == []
	assert "outcome=error reason=gh_read issue=12" in capsys.readouterr().err
	fake.fail = ("repos/",)
	assert _check(fake, monkeypatch, tmp_path) == {"processed": 0, "replan": []}
	assert "outcome=error reason=gh_read\n" in capsys.readouterr().err


def test_check_failed_reopen_skips_later_writes(fake, monkeypatch, tmp_path, capsys):
	_armed(fake)

	def patched(args):
		if "PATCH" in args:
			fake.calls.append(list(args))
			return None
		return FakeGh.__call__(fake, args)

	monkeypatch.setattr(ppv, "_gh", patched)
	out = _check(fake, monkeypatch, tmp_path, runs_cache={"workflow_runs": [_run(508, "failure")]})
	assert out["replan"] == []
	assert [w[2] for w in fake.writes()] == ["PATCH"]
	assert "reason=reopen_write" in capsys.readouterr().err


def test_window_and_cap_env_are_clamped(monkeypatch):
	monkeypatch.setenv("PERSIST_POSTMERGE_WINDOW_HOURS", "9999")
	monkeypatch.setenv("PERSIST_POSTMERGE_MAX_PER_TICK", "0")
	monkeypatch.setenv("PERSIST_MAX_APPROACHES", "abc")
	assert ppv.window_hours() == 168 and ppv.max_per_tick() == 1 and ppv.max_approaches() == 3
	monkeypatch.delenv("PERSIST_POSTMERGE_VERIFY_ENABLED", raising=False)
	assert ppv.enabled() is False


# --- workflow contracts -------------------------------------------------------


def _step(text: str, name: str) -> str:
	start = text.index(f"      - name: {name}\n")
	end = text.index("\n      - name: ", start + 10)
	return text[start:end]


def test_issue_pr_status_arm_step_contract():
	text = ISSUE_PR_STATUS.read_text(encoding="utf-8")
	step = _step(text, "Arm check after merge")
	assert "        if: github.event.pull_request.merged == true && vars.PERSIST_POSTMERGE_VERIFY_ENABLED == 'true'\n" in step
	assert "        continue-on-error: true\n" in step
	run = step[step.index("        run: |\n"):]
	assert "${{" not in run
	assert "--merge-sha \"${PR_MERGE_SHA}\"" in run and "LINEAGE_FINALIZE_ISSUE_NUMBERS" in run and "MANAGED_ISSUES" in run
	assert text.index("      - name: Update linked issue labels when PR closes") < text.index("      - name: Arm check after merge")
	assert text.index("      - name: Arm check after merge") < text.index("      - name: Expose the first linked issue")
	for line in step.splitlines():
		if "vars." in line and line.strip().startswith(("PERSIST_", "WORKFLOW_HEAL_")):
			assert re.search(r"\|\| '[^']+' \}\}$", line), line
	fetch = _step(text, "Fetch memory helper scripts")
	assert "PERSIST_POSTMERGE_VERIFY_ENABLED: ${{ vars.PERSIST_POSTMERGE_VERIFY_ENABLED || 'false' }}" in fetch
	assert 'if [ "${PERSIST_POSTMERGE_VERIFY_ENABLED:-false}" = "true" ]; then' in fetch
	assert "persist_postmerge_verify.py acceptance_check.py attempt_journal.py gh_api_retry.py targeted_file_context.py" in fetch


def test_orchestrate_poll_env_keys_once_with_defaults():
	text = ORCHESTRATE_POLL.read_text(encoding="utf-8")
	for key, default in (("PERSIST_POSTMERGE_VERIFY_ENABLED", "false"), ("PERSIST_POSTMERGE_WINDOW_HOURS", "24"),
			("PERSIST_POSTMERGE_MAX_PER_TICK", "3"), ("PERSIST_MAX_APPROACHES", "3"), ("PERSIST_BUDGET_ENABLED", "false"),
			("WORKFLOW_HEAL_VERIFY_ENABLED", "false")):
		line = f"  {key}: ${{{{ vars.{key} || '{default}' }}}}\n"
		assert text.count(line) == 1, key
		assert text.count(f"{key}:") == 1, key
	assert " attempt_journal.py gh_api_retry.py persist_postmerge_verify.py acceptance_check.py " in text
