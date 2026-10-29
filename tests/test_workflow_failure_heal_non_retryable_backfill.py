"""Non-retryable review/autofix failure backfill (issue #7291).

Covers the selector in scripts/workflow_failure_heal.py, the driver
scripts/workflow_failure_heal_non_retryable_backfill.sh (run against a fake
`gh` with the real reporter and helper) and the workflow wiring in
review_autofix_sweep.yml and the consumer ai-review.yml template.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
HEAL_PY = SCRIPTS_DIR / "workflow_failure_heal.py"
REPORT_SH = SCRIPTS_DIR / "workflow_failure_heal_autofix_report.sh"
BACKFILL_SH = SCRIPTS_DIR / "workflow_failure_heal_non_retryable_backfill.sh"
SWEEP_YML = ROOT / ".github" / "workflows" / "review_autofix_sweep.yml"
AI_REVIEW_TEMPLATE = ROOT / "workflow-templates" / "ai-review.yml"

_spec = importlib.util.spec_from_file_location("workflow_failure_heal_backfill_under_test", HEAL_PY)
heal = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = heal
_spec.loader.exec_module(heal)

REPO = "acme/widgets"
LOGIN = "pipeline-bot"
HEAD = "b" * 40
OTHER_HEAD = "c" * 40
SUPPORT = "d" * 40
FP = "e" * 64
REASON = "conflict_resolver_sandbox_path_unsupported"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
RECENT = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure_comment(*, head: str = HEAD, reason: str = REASON, run: str | None = "777", author: str = LOGIN, created: str = OLD, fp: str = FP) -> dict:
	marker = heal.render_failure_marker(head, reason, fp, False, run_id=run, support_sha=SUPPORT)
	return {"body": f"AI review/autofix failed\n\n{marker}", "user": {"login": author}, "created_at": created}


def _comment(body: str, *, author: str = LOGIN, created: str = OLD) -> dict:
	return {"body": body, "user": {"login": author}, "created_at": created}


def _select(comments: list[dict], **overrides) -> dict:
	kwargs = {"pr_number": "12", "head_sha": HEAD, "author_login": LOGIN, "now": NOW, "min_age_minutes": 60}
	kwargs.update(overrides)
	return heal.select_non_retryable_backfill_candidate(comments, **kwargs)


# --- Selector ------------------------------------------------------------------


def test_eligible_trusted_marker_yields_run_fp_and_support() -> None:
	result = _select([_failure_comment()])
	assert result["eligible"] is True
	assert (result["run"], result["fp"], result["support"], result["failure_reason"]) == ("777", FP, SUPPORT, REASON)


def test_untrusted_author_marker_is_ignored() -> None:
	assert _select([_failure_comment(author="mallory")])["reason"] == "no_current_head_marker"


def test_untrusted_diagnosis_or_backfill_marker_does_not_suppress() -> None:
	diag = _comment(heal.render_non_retryable_marker("12", HEAD, REASON), author="mallory")
	sent = _comment(heal.render_non_retryable_backfill_marker("12", HEAD, REASON, "777"), author="mallory")
	assert _select([_failure_comment(), diag, sent])["eligible"] is True


def test_trusted_diagnosis_marks_already_diagnosed() -> None:
	diag = _comment(heal.render_non_retryable_marker("12", HEAD, REASON))
	assert _select([_failure_comment(), diag])["reason"] == "already_diagnosed"


def test_trusted_backfill_marker_marks_already_backfilled() -> None:
	sent = _comment(heal.render_non_retryable_backfill_comment("12", HEAD, REASON, "777"))
	assert _select([_failure_comment(), sent])["reason"] == "already_backfilled"


def test_other_head_marker_gives_no_candidate() -> None:
	assert _select([_failure_comment(head=OTHER_HEAD)])["reason"] == "no_current_head_marker"


def test_provider_outage_markers_and_comments_are_skipped() -> None:
	outage_marker = _failure_comment(reason="provider_unavailable", run="900")
	outage_comment = _comment(heal.PROVIDER_UNAVAILABLE_COMMENT_MARKER)
	assert _select([_failure_comment(), outage_comment, outage_marker])["eligible"] is True


def test_editor_summary_first_is_superseded() -> None:
	assert _select([_failure_comment(), _comment("AI autofix editor summary\n\nall good")])["reason"] == "superseded"


def test_retryable_reason_is_not_backfilled() -> None:
	assert _select([_failure_comment(reason="editor_empty_noop")])["reason"] == "retryable"


def test_marker_without_run_is_skipped() -> None:
	assert _select([_failure_comment(run=None)])["reason"] == "marker_run_missing"


def test_recent_or_undated_marker_is_too_recent() -> None:
	assert _select([_failure_comment(created=RECENT)])["reason"] == "too_recent"
	undated = _failure_comment()
	undated.pop("created_at")
	assert _select([undated])["reason"] == "too_recent"


def test_backfill_comment_is_not_counted_as_failure_or_success() -> None:
	body = heal.render_non_retryable_backfill_comment("12", HEAD, REASON, "777")
	assert not any(marker in body for marker in heal.AUTOFIX_FAILURE_COMMENT_MARKERS + heal.AUTOFIX_SUCCESS_COMMENT_MARKERS)
	assert heal.FAILURE_MARKER_TAG not in body and heal.FAILURE_CAP_MARKER_TAG not in body
	assert heal._NON_RETRYABLE_MARKER_RE.search(body) is None


def test_inline_quoted_marker_does_not_pick_the_run() -> None:
	# A marker quoted inline (e.g. a "First error" code span) before the real
	# trailing marker must not decide which run is backfilled.
	quoted = heal.render_failure_marker(HEAD, REASON, FP, False, run_id="666", support_sha=SUPPORT)
	real = heal.render_failure_marker(HEAD, REASON, FP, False, run_id="777", support_sha=SUPPORT)
	comment = _comment(f"AI review/autofix failed\n\n**First error:** `{quoted}`\n\n{real}")
	result = _select([comment])
	assert result["eligible"] is True and result["run"] == "777"


def _pr_node(number: int, comments: list[dict], *, cross: bool = False, head: str = HEAD) -> dict:
	nodes = [
		{"databaseId": index + 1, "body": item["body"], "createdAt": item.get("created_at", ""), "author": {"login": item["user"]["login"]}}
		for index, item in enumerate(comments)
	]
	return {"number": number, "isCrossRepository": cross, "headRefOid": head, "comments": {"nodes": nodes}}


def test_candidates_skip_cross_repository_and_convert_comments() -> None:
	nodes = [_pr_node(12, [_failure_comment()]), _pr_node(13, [_failure_comment()], cross=True), _pr_node(14, [_failure_comment()], head="nothex")]
	result = heal.non_retryable_backfill_candidates(nodes, author_login=LOGIN, now=NOW)
	assert [c["pr"] for c in result["candidates"]] == ["12"]
	assert result["candidates"][0]["comments"][0]["user"]["login"] == LOGIN
	assert {(s["pr"], s["reason"]) for s in result["skipped"]} == {("13", "cross_repository"), ("14", "invalid_head")}


# --- Driver --------------------------------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state["calls"].append(args)
def save():
	json.dump(state, open(state_path, "w"))
def fail():
	save()
	sys.exit(1)
if args[:2] == ["api", "user"]:
	if not state.get("login"):
		fail()
	save()
	print(state["login"])
	sys.exit(0)
if args[:2] == ["api", "graphql"]:
	if state.get("graphql_fail"):
		fail()
	save()
	query = next((a[len("query="):] for a in args if a.startswith("query=")), "")
	if "totalCount" in query:
		print(json.dumps({"data": {"repository": {"pullRequests": {"totalCount": state.get("total_count", 0)}}}}))
	elif "comments(" not in query:
		print(json.dumps({"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": True, "endCursor": "skipcursor"}}}}}))
	else:
		print(json.dumps({"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": state["nodes"]}}}}))
	sys.exit(0)
if "dispatches" in " ".join(args):
	if state.get("dispatch_fail"):
		fail()
	path = args[args.index("--input") + 1]
	state["dispatches"].append(json.load(open(path)))
	save()
	sys.exit(0)
for arg in args:
	if arg.startswith("repos/") and "/pulls/" in arg:
		number = int(arg.rsplit("/", 1)[1])
		save()
		if state.get("pr_payload_bad"):
			print("not json")
			sys.exit(0)
		print(json.dumps({"number": number, "title": "Fix things", "body": "", "html_url": "https://github.com/x/y/pull/%d" % number, "labels": [], "head": {"ref": "ai/issue-1", "sha": state["head"], "repo": {"full_name": state["repo"]}}, "base": {"ref": "main"}}))
		sys.exit(0)
	if arg.startswith("repos/") and arg.endswith("/comments"):
		body_arg = [a for a in args if a.startswith("body=@")][0]
		state["comments_posted"].append({"path": arg, "body": open(body_arg[len("body=@"):]).read()})
		save()
		sys.exit(0)
fail()
'''


def _stage(tmp: Path, nodes: list[dict], **state_overrides) -> tuple[dict[str, str], Path]:
	scripts = tmp / "scripts"
	scripts.mkdir()
	for source in (HEAL_PY, REPORT_SH, BACKFILL_SH):
		shutil.copy(source, scripts / source.name)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	state = {"calls": [], "dispatches": [], "comments_posted": [], "login": LOGIN, "nodes": nodes, "head": HEAD, "repo": REPO}
	state.update(state_overrides)
	state_file = tmp / "state.json"
	state_file.write_text(json.dumps(state), encoding="utf-8")
	env = {
		"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"TMPDIR": str(tmp),
		"FAKE_GH_STATE": str(state_file),
		"GH_TOKEN": "fake-token",
		"REPOSITORY": REPO,
		"WORKFLOW_HEAL_UPSTREAM_REPO": "shubhodeep1/coding-workflows",
		"WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES": "60",
	}
	return env, state_file


def _run_driver(tmp: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["bash", str(tmp / "scripts" / BACKFILL_SH.name)], env=env, capture_output=True, text=True, check=False, timeout=120)


def _old_nodes(count: int) -> list[dict]:
	# The real driver compares against the current time, so markers must be old.
	old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
	return [_pr_node(20 + index, [_failure_comment(created=old, run=str(5000 + index))]) for index in range(count)]


@pytest.mark.parametrize(("switch", "expected"), [("false", "reason=disabled"), ("maybe", "reason=invalid_switch")])
def test_driver_off_switch(switch: str, expected: str) -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1))
		env["WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED"] = switch
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert expected in result.stdout
		if switch == "maybe":
			assert "::warning::" in result.stdout
		assert json.loads(state_file.read_text())["calls"] == []


def test_driver_skips_when_workflow_heal_disabled() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1))
		env["WORKFLOW_HEAL_ENABLED"] = "false"
		result = _run_driver(tmp, env)
		assert result.returncode == 0 and "reason=workflow_heal_disabled" in result.stdout
		assert json.loads(state_file.read_text())["calls"] == []


def test_driver_identity_unavailable_skips_before_listing() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), login="")
		result = _run_driver(tmp, env)
		assert result.returncode == 0 and "reason=identity_unavailable" in result.stdout
		calls = json.loads(state_file.read_text())["calls"]
		assert not any(call[:2] == ["api", "graphql"] for call in calls)


def test_driver_listing_failure_fails_open() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), graphql_fail=True)
		result = _run_driver(tmp, env)
		assert result.returncode == 0 and "reason=pr_listing_unavailable" in result.stdout
		assert json.loads(state_file.read_text())["dispatches"] == []


def test_driver_dispatches_marker_run_once_and_posts_marker() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1))
		result = _run_driver(tmp, env)
		assert result.returncode == 0, result.stdout + result.stderr
		state = json.loads(state_file.read_text())
		assert len(state["dispatches"]) == 1, result.stdout
		report = state["dispatches"][0]["client_payload"]["report"]
		assert report["failure_reason"] == REASON
		assert str(report["run_refs"][0]["run_id"]) == "5000"
		assert "client_payload" in state["dispatches"][0] and "report_identity" not in state["dispatches"][0]["client_payload"]
		assert len(state["comments_posted"]) == 1
		assert heal.render_non_retryable_backfill_marker("20", HEAD, REASON, "5000") in state["comments_posted"][0]["body"]
		assert "pr=20 head=" in result.stdout and "outcome=dispatched marker=posted" in result.stdout


def test_driver_skips_untrusted_and_diagnosed_prs() -> None:
	old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
	untrusted = _pr_node(30, [_failure_comment(author="mallory", created=old)])
	diagnosed = _pr_node(31, [_failure_comment(created=old), _comment(heal.render_non_retryable_marker("31", HEAD, REASON))])
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, [untrusted, diagnosed])
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "pr=30 outcome=skip reason=no_current_head_marker" in result.stdout
		assert "pr=31 outcome=skip reason=already_diagnosed" in result.stdout
		assert json.loads(state_file.read_text())["dispatches"] == []


def test_driver_per_tick_cap() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(5))
		env["WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK"] = "2"
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert len(json.loads(state_file.read_text())["dispatches"]) == 2
		assert result.stdout.count("reason=tick_cap") == 3


def test_driver_report_failure_posts_no_marker() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), dispatch_fail=True)
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "outcome=failed reason=report_dispatch_denied" in result.stdout
		assert json.loads(state_file.read_text())["comments_posted"] == []


def test_driver_skips_when_head_moved_since_listing() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), head=OTHER_HEAD)
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "outcome=skip reason=head_moved" in result.stdout
		state = json.loads(state_file.read_text())
		assert state["dispatches"] == [] and state["comments_posted"] == []


def test_driver_lists_least_recently_updated_with_string_repo_vars() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1))
		_run_driver(tmp, env)
		call = next(c for c in json.loads(state_file.read_text())["calls"] if c[:2] == ["api", "graphql"] and "comments(" in " ".join(c))
		assert call[call.index("owner=acme") - 1] == "-f"
		assert call[call.index("name=widgets") - 1] == "-f"
		assert "direction: ASC" in " ".join(call)


def _graphql_calls(state_file: Path) -> list[list[str]]:
	return [c for c in json.loads(state_file.read_text())["calls"] if c[:2] == ["api", "graphql"]]


def test_driver_rotates_window_when_more_prs_than_max() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), total_count=120)
		env["WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS"] = "50"
		env["GITHUB_RUN_NUMBER"] = "7"  # 3 windows; 7 % 3 = 1, so 50 PRs are skipped
		env["DRY_RUN"] = "true"
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "reason=window_rotated window=1 windows=3 total_open=120" in result.stdout
		calls = _graphql_calls(state_file)
		skip_calls = [c for c in calls if "totalCount" not in " ".join(c) and "comments(" not in " ".join(c)]
		assert len(skip_calls) == 1 and "first=50" in skip_calls[0]
		listing = next(c for c in calls if "comments(" in " ".join(c))
		assert "cursor=skipcursor" in listing


def test_driver_no_rotation_within_max() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), total_count=20)
		env["DRY_RUN"] = "true"
		result = _run_driver(tmp, env)
		assert result.returncode == 0 and "window_rotated" not in result.stdout
		listing = next(c for c in _graphql_calls(state_file) if "comments(" in " ".join(c))
		assert not any(a.startswith("cursor=") for a in listing)


def test_driver_head_ref_filter_limits_prs() -> None:
	nodes = _old_nodes(2)
	nodes[0]["headRefName"] = "claude/x"
	nodes[1]["headRefName"] = "ai/issue-9"
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, nodes)
		env["HEAD_REF_FILTER"] = "claude/"
		env["DRY_RUN"] = "true"
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "pr=20 head=" in result.stdout and "pr=21 " not in result.stdout
		assert "scanned=1 " in result.stdout


def test_driver_unreadable_pr_payload_is_not_head_moved() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1), pr_payload_bad=True)
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "reason=head_unreadable" in result.stdout and "head_moved" not in result.stdout
		assert json.loads(state_file.read_text())["dispatches"] == []


def test_driver_leading_zero_setting_is_decimal() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1))
		env["WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS"] = "08"
		env["DRY_RUN"] = "true"
		result = _run_driver(tmp, env)
		assert result.returncode == 0
		assert "outcome=complete scanned=1 " in result.stdout
		listing = next(c for c in _graphql_calls(state_file) if "comments(" in " ".join(c))
		assert "first=8" in listing


def test_driver_dry_run_sends_nothing() -> None:
	with tempfile.TemporaryDirectory() as name:
		tmp = Path(name)
		env, state_file = _stage(tmp, _old_nodes(1))
		env["DRY_RUN"] = "true"
		result = _run_driver(tmp, env)
		assert result.returncode == 0 and "reason=dry_run" in result.stdout
		state = json.loads(state_file.read_text())
		assert state["dispatches"] == [] and state["comments_posted"] == []


# --- Workflow wiring -------------------------------------------------------------


def _expected_env() -> dict[str, str]:
	return {
		"WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED": "${{ vars.WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED || 'true' }}",
		"WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK": "${{ vars.WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PER_TICK || '3' }}",
		"WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS": "${{ vars.WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MAX_PRS || '50' }}",
		"WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES": "${{ vars.WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_MIN_AGE_MINUTES || '60' }}",
		"GH_TOKEN": "${{ secrets.GH_PAT }}",
	}


def test_sweep_step_wiring() -> None:
	yaml = pytest.importorskip("yaml")
	job = yaml.safe_load(SWEEP_YML.read_text(encoding="utf-8"))["jobs"]["sweep"]
	names = [step.get("name", "") for step in job["steps"]]
	index = names.index("Backfill non-retryable autofix failure reports")
	assert names[index - 1] == "Enumerate open PRs and dispatch internal-review.yml"
	assert names[index + 1] == "Record GH_PAT budget at sweep end"
	step = job["steps"][index]
	assert step["continue-on-error"] is True
	# !cancelled() drops the implicit success() check, so a failed enumerate
	# step does not also skip the backfill; both kill switches stay in place.
	assert "!cancelled()" in step["if"]
	assert "vars.WORKFLOW_HEAL_NON_RETRYABLE_BACKFILL_ENABLED != 'false'" in step["if"]
	assert "vars.WORKFLOW_HEAL_ENABLED != 'false'" in step["if"]
	assert step["run"].strip() == "bash .ci-rerun-support/scripts/workflow_failure_heal_non_retryable_backfill.sh"
	for key, value in _expected_env().items():
		assert step["env"][key] == value
	assert "id-token" not in job["permissions"]


def test_consumer_job_wiring() -> None:
	yaml = pytest.importorskip("yaml")
	job = yaml.safe_load(AI_REVIEW_TEMPLATE.read_text(encoding="utf-8"))["jobs"]["non-retryable-backfill"]
	assert job["permissions"] == {"contents": "read"}
	assert "github.event_name == 'schedule'" in job["if"]
	checkout = job["steps"][0]["with"]
	assert checkout["ref"] == "stable" and checkout["persist-credentials"] is False
	assert checkout["repository"] == "shubhodeep1/coding-workflows"
	step = job["steps"][1]
	assert step["run"].strip() == "bash .non-retryable-backfill-support/scripts/workflow_failure_heal_non_retryable_backfill.sh"
	for key, value in _expected_env().items():
		assert step["env"][key] == value


def test_wiring_text_without_yaml() -> None:
	sweep = SWEEP_YML.read_text(encoding="utf-8")
	template = AI_REVIEW_TEMPLATE.read_text(encoding="utf-8")
	assert "scripts/workflow_failure_heal_non_retryable_backfill.sh" in sweep
	assert "  non-retryable-backfill:\n" in template
	assert os.access(BACKFILL_SH, os.X_OK)
