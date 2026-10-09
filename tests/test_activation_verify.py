#!/usr/bin/env python3
"""Activation verification (plan Phase 8c, port P4) and the ai:operator-step writer."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "activation_verify.sh"
WRITER = ROOT / "scripts" / "operator_step_issue.py"
STATUS = ROOT / ".github" / "workflows" / "issue_pr_status.yml"
POLL = ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"

SPEC = importlib.util.spec_from_file_location("operator_step_issue", WRITER)
writer = importlib.util.module_from_spec(SPEC)
sys.modules["operator_step_issue"] = writer
SPEC.loader.exec_module(writer)

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state["calls"].append(args)
def done(out=""):
	json.dump(state, open(state_path, "w"))
	if out:
		print(out)
	sys.exit(0)
endpoint = next((a for a in args[1:] if a.startswith(("repos/", "search/"))), "")
fields = {}
for i, a in enumerate(args):
	if a == "-f":
		k, _, v = args[i + 1].partition("=")
		fields.setdefault(k, v)
if args[:3] == ["label", "create", "ai:operator-step"]:
	error = os.environ.get("FAKE_GH_LABEL_CREATE_ERROR", "")
	if not error and state.get("operator_label_exists"):
		error = "label already exists"
	if error:
		if "label already exists" in error or "already_exists" in error:
			state["operator_label_exists"] = True  # Concurrent creator won the race.
		json.dump(state, open(state_path, "w"))
		print(error, file=sys.stderr)
		sys.exit(1)
	state["operator_label_exists"] = True
	done()
method = args[args.index("-X") + 1] if "-X" in args else ("POST" if "-f" in args else "GET")
if "user" in args:
	done("pipeline-bot")
if endpoint.endswith("/labels/ai%3Aoperator-step"):
	if state.get("label_missing"):
		json.dump(state, open(state_path, "w"))
		print("HTTP 404: Not Found", file=sys.stderr)
		sys.exit(1)
	done(json.dumps({"name": "ai:operator-step"}))
if endpoint == "repos/o/r/labels" and fields.get("name") == "ai:operator-step":
	state["label_missing"] = False
	done(json.dumps({"name": "ai:operator-step"}))
if "issues?labels=ai:operator-step" in endpoint:
	if state.get("stale_operator_lists", 0) and state.get("created"):
		state["stale_operator_lists"] -= 1
		done("[]")
	if os.environ.get("FAKE_GH_CRLF_LIST") == "1":
		done(json.dumps([{**listed_issue, "body": listed_issue["body"].replace("\n", "\r\n")} for listed_issue in state.get("operator_issues", [])]))
	done(json.dumps(state.get("operator_issues", [])))
if endpoint.endswith("/comments?per_page=100") and method == "GET":
	comment_endpoint = endpoint.split("?", 1)[0]
	done(json.dumps([[comment for comment in state["comments"] if comment["endpoint"] == comment_endpoint]]))
if endpoint.endswith("/files?per_page=100"):
	if os.environ.get("FAKE_GH_FAIL_FILES") == "1":
		sys.exit(1)
	done(json.dumps([[{"filename": "README.md"}]]))
if endpoint.startswith("search/issues?"):
	if os.environ.get("FAKE_GH_FAIL_SEARCH") == "1":
		sys.exit(1)
	done(json.dumps(state.get("fix_search", {"total_count": 0, "items": []})))
if "-X" in args and "PATCH" in args:
	issue_number = int(endpoint.rsplit("/", 1)[-1])
	if fields.get("state") == "closed":
		state["closed"].append(issue_number)
		state["operator_issues"] = [issue for issue in state["operator_issues"] if issue["number"] != issue_number]
	else:
		state["patched"] = {"endpoint": endpoint, "body": fields.get("body", "")}
		for issue in state["operator_issues"]:
			if issue["number"] == issue_number:
				issue["body"] = fields.get("body", "")
	done("{}")
if endpoint.endswith("/comments"):
	comment = {"id": state.get("next_comment_id", 100), "endpoint": endpoint, "body": fields.get("body", ""), "user": {"login": "pipeline-bot"}}
	state["next_comment_id"] = comment["id"] + 1
	state["comments"].append(comment)
	done(json.dumps(comment))
if endpoint == "repos/o/r/issues" and "title" in fields:
	if os.environ.get("FAKE_GH_FAIL_CREATE") == "1":
		sys.exit(1)
	if fields.get("labels[]") == "ai:operator-step" and not state.get("operator_label_exists"):
		json.dump(state, open(state_path, "w"))
		print("label does not exist", file=sys.stderr)
		sys.exit(1)
	state["created"].append(fields)
	created = {"number": 900 + len(state["created"]), "html_url": "u"}
	if fields.get("title") == "Operator steps waiting":
		state["operator_issues"].append({**created, "body": fields.get("body", ""), "author_association": "OWNER", "user": {"login": "o"}})
	done(json.dumps(created))
if endpoint.startswith("repos/o/r/issues/"):
	issue_number = int(endpoint.rsplit("/", 1)[-1])
	for issue in state["operator_issues"]:
		if issue["number"] == issue_number:
			done(json.dumps(issue))
	done(json.dumps(state.get("linked", {})))
done("")
'''


def _setup(tmp_path: Path, **state) -> tuple[dict, Path]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	state_file = tmp_path / "state.json"
	base = {"calls": [], "comments": [], "created": [], "closed": [], "operator_issues": [], "linked": {}, "next_comment_id": 100}
	base.update(state)
	state_file.write_text(json.dumps(base), encoding="utf-8")
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_GH_STATE=str(state_file), PYTHONDONTWRITEBYTECODE="1")
	for name in ("ACTIVATION_VERIFY_ENABLED", "TG_BOT_SECRET"):
		env.pop(name, None)
	env["GH_RETRY_MAX_ATTEMPTS"] = "1"
	return env, state_file


def _verify(tmp_path: Path, verdict: dict | str, linked: dict | None = None, env_extra: dict | None = None, mode: str = "pr", fix_search: dict | None = None):
	target = tmp_path / "target"
	target.mkdir(exist_ok=True)
	env, state_file = _setup(tmp_path, linked=linked or {"number": 7, "title": "t", "body": "spec", "author_association": "OWNER"},
		fix_search=fix_search or {"total_count": 0, "items": []})
	env.update(
		REPOSITORY="o/r",
		PR_NUMBER="42",
		MERGE_SHA="",
		PR_TITLE="Add nightly report",
		PR_BODY="body",
		LINKED_ISSUE="7",
		TARGET_DIR=str(target),
		SUPPORT_DIR=str(ROOT),
		RUNTIME_DIR=str(tmp_path / "rt"),
		MOCK_ACTIVATION_VERIFY_JSON=verdict if isinstance(verdict, str) else json.dumps(verdict),
	)
	env.update(env_extra or {})
	result = subprocess.run(["bash", str(VERIFY), mode], capture_output=True, text=True, env=env, check=False)
	return result, json.loads(state_file.read_text(encoding="utf-8"))


DORMANT = {
	"verdict": "DORMANT",
	"trigger": "cron '0 6 * * *' from the default branch",
	"summary": "The report job is wired but its flag defaults off.",
	"gaps": [
		{"kind": "code", "title": "README lacks the new variable", "evidence": "README.md:12", "fix": "Add the NIGHTLY_REPORT_ENABLED row"},
		{"kind": "operator", "title": "Set NIGHTLY_REPORT_ENABLED", "evidence": "nightly.yml:9", "fix": "gh variable set NIGHTLY_REPORT_ENABLED --body true", "dormant_until": "NIGHTLY_REPORT_ENABLED"},
	],
}


def test_dormant_verdict_posts_marker_opens_fix_issue_and_records_operator_step(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT)
	assert result.returncode == 0
	assert state["operator_label_exists"] is True
	label_call = next(i for i, call in enumerate(state["calls"]) if call[:3] == ["label", "create", "ai:operator-step"])
	issue_call = next(i for i, call in enumerate(state["calls"]) if call[:2] == ["api", "repos/o/r/issues"] and "-f" in call and "title=Operator steps waiting" in call)
	assert label_call < issue_call
	assert "ACTIVATION_VERIFY mode=pr item=42 verdict=DORMANT code_gaps=1 operator_gaps=1 outcome=posted" in result.stdout
	verdict_comment = state["comments"][-1]
	assert verdict_comment["endpoint"] == "repos/o/r/issues/7/comments"
	assert verdict_comment["body"].endswith("<!-- ai:activation:v1 verdict=DORMANT source=pr-42 -->")
	fix_issue = state["created"][0]
	assert fix_issue["title"] == "Activation gaps after PR #42"
	assert fix_issue["body"].startswith("<!-- ai:activation-fix:v1 source=pr-42 -->")
	operator_issue = state["created"][1]
	assert operator_issue["labels[]"] == "ai:operator-step"
	operator_entry = next(comment for comment in state["comments"] if comment["body"].startswith("<!-- ai:operator-step:entry key=pr-42 -->"))
	assert "Stays off until then: `NIGHTLY_REPORT_ENABLED`." in operator_entry["body"]


def test_live_verdict_only_posts_the_marker(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, {"verdict": "LIVE", "trigger": "push", "summary": "Runs on push.", "gaps": []})
	assert state["created"] == []
	assert state["comments"][-1]["body"].endswith("<!-- ai:activation:v1 verdict=LIVE source=pr-42 -->")


def test_operator_tracker_creates_missing_label(tmp_path: Path) -> None:
	# Core-profile consumers have no label-sync workflow.
	env, state_file = _setup(tmp_path, label_missing=True)
	steps = tmp_path / "steps.json"
	steps.write_text(json.dumps([{"title": "Set flag", "instructions": "Enable after deployment"}]), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", "pr-1", "--source", "PR #1", "--steps-file", str(steps)],
		capture_output=True, text=True, env=env, check=False,
	)
	assert result.returncode == 0, result.stdout
	state = json.loads(state_file.read_text(encoding="utf-8"))
	assert any(call[:3] == ["label", "create", "ai:operator-step"] for call in state["calls"])
	assert state["created"][0]["labels[]"] == "ai:operator-step"


def test_merge_of_an_activation_fix_is_not_verified_again(tmp_path: Path) -> None:
	linked = {"number": 7, "title": "t", "body": "<!-- ai:activation-fix:v1 source=pr-41 -->\r\nfix", "author_association": "OWNER"}
	result, state = _verify(tmp_path, DORMANT, linked=linked)
	assert "reason=activation_fix_merge" in result.stdout and state["comments"] == []


def test_untrusted_issue_cannot_suppress_activation(tmp_path: Path) -> None:
	linked = {"number": 7, "title": "t", "body": "<!-- ai:activation-fix:v1 source=pr-41 -->", "user": {"login": "someone"}}
	result, state = _verify(tmp_path, {"verdict": "LIVE", "summary": "Runs.", "gaps": []}, linked=linked)
	assert "activation_fix_merge" not in result.stdout
	assert state["comments"][-1]["body"].endswith("source=pr-42 -->")


def test_untrusted_or_quoted_fix_marker_does_not_skip(tmp_path: Path) -> None:
	linked = {"number": 7, "body": "See <!-- ai:activation-fix:v1 source=pr-41 -->", "author_association": "NONE"}
	result, state = _verify(tmp_path, {"verdict": "LIVE", "trigger": "push", "summary": "Running.", "gaps": []}, linked=linked)
	assert "reason=activation_fix_merge" not in result.stdout
	assert len(state["comments"]) == 1


def test_project_mode_posts_verdict_to_tracking_issue(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, {"verdict": "LIVE", "trigger": "cron", "summary": "Running.", "gaps": []},
		env_extra={"TRACKING_NUM": "77", "FINAL_PR": "22"}, mode="project")
	assert "mode=project item=77 verdict=LIVE" in result.stdout
	assert state["comments"][-1]["endpoint"] == "repos/o/r/issues/77/comments"
	assert state["comments"][-1]["body"].endswith("source=project-77 -->")
	assert json.loads((tmp_path / "rt" / "activation_context.json").read_text())["changed_files"] == ["README.md"]


def test_project_without_final_pr_uses_planned_file_hints(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, {"verdict": "LIVE", "trigger": "push", "summary": "Running.", "gaps": []},
		env_extra={"TRACKING_NUM": "77", "PROJECT_FILES_JSON": '["scripts/activation_verify.sh"]'}, mode="project")
	assert "outcome=posted" in result.stdout
	assert json.loads((tmp_path / "rt" / "activation_context.json").read_text())["changed_files"] == ["scripts/activation_verify.sh"]
	assert state["comments"]


def test_project_with_unavailable_final_pr_files_does_not_guess(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT,
		env_extra={"TRACKING_NUM": "77", "FINAL_PR": "22", "FAKE_GH_FAIL_FILES": "1"}, mode="project")
	assert "reason=files_unavailable" in result.stdout
	assert state["comments"] == []


def test_failed_fix_issue_does_not_mark_verification_complete(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT, env_extra={"FAKE_GH_FAIL_CREATE": "1"})
	assert "reason=fix_issue_failed" in result.stdout
	assert state["comments"] == []


def test_existing_fix_issue_is_reused_before_posting_missing_verdict(tmp_path: Path) -> None:
	fix_search = {"total_count": 1, "items": [{"body": "<!-- ai:activation-fix:v1 source=pr-42 -->\r\nwork", "author_association": "OWNER"}]}
	result, state = _verify(tmp_path, DORMANT, fix_search=fix_search)
	assert "outcome=fix_issue_reused" in result.stdout
	assert len(state["created"]) == 1  # Only the operator-step issue.
	assert state["comments"][-1]["body"].endswith("source=pr-42 -->")


def test_failed_fix_lookup_still_reports_unfinalized_verdict_and_operator_steps(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT, env_extra={"FAKE_GH_FAIL_SEARCH": "1"})
	assert "outcome=partial reason=fix_lookup_failed" in result.stdout
	assert len(state["created"]) == 1
	assert state["created"][0]["title"] == "Operator steps waiting"
	assert "Code-gap follow-up is pending" in state["comments"][-1]["body"]
	assert state["comments"][-1]["body"].endswith("<!-- ai:activation:v1 partial=true source=pr-42 -->")
	assert "<!-- ai:activation:v1 verdict=" not in state["comments"][-1]["body"]


def test_pr_file_lookup_failure_cannot_misgrade_multi_commit_rebase(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT, env_extra={"FAKE_GH_FAIL_FILES": "1", "PR_COMMITS": "2"})
	assert "reason=multi_commit_rebase" in result.stdout
	assert state["comments"] == [] and state["created"] == []


def test_pr_file_lookup_failure_reports_failed_single_commit_diff(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT, env_extra={"FAKE_GH_FAIL_FILES": "1", "PR_COMMITS": "1"})
	assert "reason=fallback_diff_failed" in result.stdout
	assert state["comments"] == [] and state["created"] == []


def test_pr_file_lookup_failure_cannot_grade_an_empty_diff(tmp_path: Path) -> None:
	target = tmp_path / "target"
	target.mkdir()
	subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
	for message in ("first", "second"):
		subprocess.run(["git", "-C", str(target), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
			"commit", "--allow-empty", "-m", message], check=True, capture_output=True)
	result, state = _verify(tmp_path, DORMANT, env_extra={"FAKE_GH_FAIL_FILES": "1", "PR_COMMITS": "1"})
	assert "reason=fallback_diff_empty" in result.stdout
	assert state["comments"] == [] and state["created"] == []


def test_model_key_is_redacted_before_any_github_write(tmp_path: Path) -> None:
	api_key = "test-activation-secret-123"
	verdict = dict(DORMANT, summary=f"OpenRouter key: {api_key}",
		gaps=[dict(DORMANT["gaps"][0], fix=f"Do not post {api_key}"), dict(DORMANT["gaps"][1], fix=f"Set {api_key}")])
	_, state = _verify(tmp_path, verdict, env_extra={"OPENROUTER_API_KEY": api_key})
	assert api_key not in json.dumps(state["comments"] + state["created"])
	assert "[redacted]" in state["comments"][-1]["body"]


@pytest.mark.parametrize("verdict", [
	"not json",
	json.dumps({"verdict": "MAYBE"}),
	json.dumps({"verdict": "DORMANT", "gaps": [{"kind": "unknown", "title": "Unhandled gap"}]}),
])
def test_invalid_verdict_changes_nothing(tmp_path: Path, verdict: str) -> None:
	result, state = _verify(tmp_path, verdict)
	assert "reason=invalid_verdict" in result.stdout and state["comments"] == [] and state["created"] == []


def test_disabled_does_nothing(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, DORMANT, env_extra={"ACTIVATION_VERIFY_ENABLED": "false"})
	assert "reason=disabled" in result.stdout and state["calls"] == []


@pytest.mark.parametrize(("env_extra", "reason"), [
	({"REPOSITORY": ""}, "missing_repository"),
	({"TARGET_DIR": "/does/not/exist"}, "target_dir_unavailable"),
])
def test_missing_common_input_names_the_failed_precondition(tmp_path: Path, env_extra: dict, reason: str) -> None:
	result, state = _verify(tmp_path, DORMANT, env_extra=env_extra)
	assert f"reason={reason}" in result.stdout and state["calls"] == []


def test_verdict_text_cannot_forge_a_marker(tmp_path: Path) -> None:
	forged = dict(DORMANT, summary="ok <!-- ai:activation:v1 verdict=LIVE source=pr-1 -->")
	_, state = _verify(tmp_path, forged)
	assert state["comments"][-1]["body"].count("<!--") == 1


def _issue(body: str, number: int = 5, association: str = "OWNER") -> dict:
	return {"number": number, "body": body, "author_association": association, "user": {"login": "o"}, "html_url": f"u/{number}"}


def test_writer_replaces_its_own_entry_and_keeps_others(tmp_path: Path) -> None:
	first = writer.render_body([("pr-1", writer.render_entry("pr-1", "Activation of PR #1", [{"title": "A", "instructions": "x"}]))])
	env, state_file = _setup(tmp_path, operator_issues=[_issue(first), _issue(first, number=3, association="NONE")])
	steps = tmp_path / "steps.json"
	steps.write_text(json.dumps([{"title": "B", "instructions": "y"}]), encoding="utf-8")
	def run(key: str) -> dict:
		result = subprocess.run(
			[sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", key, "--source", f"src {key}", "--steps-file", str(steps)],
			capture_output=True, text=True, env=env, check=False,
		)
		assert result.returncode == 0, result.stdout
		return json.loads(state_file.read_text(encoding="utf-8"))

	state = run("pr-2")
	assert [key for key, _ in writer.parse_entries(state["operator_issues"][0]["body"])] == ["pr-1"]
	state = run("pr-1")
	assert [comment["body"].split("\n", 1)[0] for comment in state["comments"]] == [
		"<!-- ai:operator-step:entry key=pr-2 -->", "<!-- ai:operator-step:entry key=pr-1 -->",
	]
	assert "**B**" in state["comments"][-1]["body"]
	assert "**A**" in state["operator_issues"][0]["body"]  # Legacy entry is immutable.
	assert state["created"] == []
	assert not any(call[:2] == ["label", "create"] for call in state["calls"])


def test_writer_reconciles_duplicate_trackers_without_losing_entries(tmp_path: Path) -> None:
	first = writer.render_body([("pr-1", writer.render_entry("pr-1", "one", [{"title": "A", "instructions": "x"}]))])
	second = writer.render_body([("pr-2", writer.render_entry("pr-2", "two", [{"title": "B", "instructions": "y"}]))])
	env, state_file = _setup(tmp_path, operator_issues=[_issue(first, number=5), _issue(second, number=6)])
	steps = tmp_path / "steps.json"
	steps.write_text(json.dumps([{"title": "C", "instructions": "z"}]), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", "pr-3", "--source", "three", "--steps-file", str(steps)],
		capture_output=True, text=True, env=env, check=False,
	)
	assert result.returncode == 0, result.stdout
	state = json.loads(state_file.read_text(encoding="utf-8"))
	assert state["closed"] == []  # Do not close an issue whose legacy entries have not been migrated.
	assert [key for key, _ in writer.parse_entries(state["operator_issues"][0]["body"])] == ["pr-1"]
	assert [key for key, _ in writer.parse_entries(state["operator_issues"][1]["body"])] == ["pr-2"]
	assert state["comments"][-1]["body"].startswith("<!-- ai:operator-step:entry key=pr-3 -->")


def test_writer_upserts_against_crlf_tracker_body(tmp_path: Path) -> None:
	first_entry = writer.render_entry("pr-1", "one", [{"title": "A", "instructions": "x"}])
	first_body = writer.render_body([("pr-1", first_entry)])
	env, state_file = _setup(tmp_path, operator_issues=[_issue(first_body)])
	env["FAKE_GH_CRLF_LIST"] = "1"
	steps = tmp_path / "steps.json"
	steps.write_text(json.dumps([{"title": "B", "instructions": "y"}]), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", "pr-2", "--source", "two", "--steps-file", str(steps)],
		capture_output=True, text=True, env=env, check=False,
	)
	state = json.loads(state_file.read_text(encoding="utf-8"))
	assert result.returncode == 0, result.stdout
	assert [key for key, _ in writer.parse_entries(state["operator_issues"][0]["body"])] == ["pr-1"]
	assert state["comments"][-1]["body"].startswith("<!-- ai:operator-step:entry key=pr-2 -->")


def test_writer_does_not_recreate_when_label_listing_lags_create(tmp_path: Path) -> None:
	env, state_file = _setup(tmp_path, stale_operator_lists=2)
	steps = tmp_path / "steps.json"
	steps.write_text(json.dumps([{"title": "A", "instructions": "x"}]), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", "pr-7", "--source", "seven", "--steps-file", str(steps)],
		capture_output=True, text=True, env=env, check=False,
	)
	state = json.loads(state_file.read_text(encoding="utf-8"))
	assert result.returncode == 0, result.stdout
	assert len(state["created"]) == 1
	assert state["comments"][-1]["body"].startswith("<!-- ai:operator-step:entry key=pr-7 -->")


def test_writer_uses_registered_label_metadata(tmp_path: Path) -> None:
	registration = json.loads((ROOT / ".github/ai/label_contract.v1.json").read_text(encoding="utf-8"))["labels"]["ai:operator-step"]
	helpers = (ROOT / "scripts/label_helpers.sh").read_text(encoding="utf-8")
	assert registration["color"] == re.search(r'\["ai:operator-step"\]="([0-9a-f]{6})"', helpers).group(1)
	assert registration["description"] == re.search(r'\["ai:operator-step"\]="([^"]+)"', helpers[helpers.index("declare -A _AI_LABEL_DESCS="):]).group(1)
	assert len(registration["description"]) <= 100
	env, state_file = _setup(tmp_path)
	steps = tmp_path / "steps.json"
	steps.write_text('[{"title":"Set a variable"}]', encoding="utf-8")
	result = subprocess.run([sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", "pr-7",
		"--source", "seven", "--steps-file", str(steps)], capture_output=True, text=True, env=env, check=False)
	assert result.returncode == 0, result.stdout
	label_call = next(call for call in json.loads(state_file.read_text(encoding="utf-8"))["calls"] if call[:2] == ["label", "create"])
	assert label_call == ["label", "create", "ai:operator-step", "--repo", "o/r", "--color", registration["color"],
		"--description", registration["description"]]


@pytest.mark.parametrize(("error", "preexisting", "expected_code"), [
	("", True, 0),
	("label already exists", False, 0),
	("Validation Failed: already_exists", False, 0),
	("Validation Failed", False, 2),
	("description already exists", False, 2),
	("forbidden", False, 2),
	("rate limit exceeded", False, 2),
])
def test_writer_label_create_race_or_failure(tmp_path: Path, error: str, preexisting: bool, expected_code: int) -> None:
	env, state_file = _setup(tmp_path, operator_label_exists=preexisting)
	if error:
		env["FAKE_GH_LABEL_CREATE_ERROR"] = error
	steps = tmp_path / "steps.json"
	steps.write_text('[{"title":"Set a variable"}]', encoding="utf-8")
	result = subprocess.run([sys.executable, str(WRITER), "upsert", "--repo", "o/r", "--key", "pr-7",
		"--source", "seven", "--steps-file", str(steps)], capture_output=True, text=True, env=env, check=False)
	assert result.returncode == expected_code, result.stdout
	state = json.loads(state_file.read_text(encoding="utf-8"))
	assert len(state["created"]) == (1 if expected_code == 0 else 0)
	assert sum(call[:2] == ["label", "create"] for call in state["calls"]) == 1


@pytest.mark.parametrize("registration", [
	None,
	{"labels": {"ai:operator-step": {"color": "not-hex", "description": "short"}}},
	{"labels": {"ai:operator-step": {"color": "fbca04", "description": "x" * 101}}},
])
def test_writer_fails_closed_without_valid_label_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registration: dict | None) -> None:
	if registration is not None:
		contract_path = tmp_path / ".github/ai/label_contract.v1.json"
		contract_path.parent.mkdir(parents=True)
		contract_path.write_text(json.dumps(registration), encoding="utf-8")
	monkeypatch.setattr(writer, "__file__", str(tmp_path / "scripts" / "operator_step_issue.py"))
	monkeypatch.setattr(writer, "_gh", lambda *args, **kwargs: pytest.fail("unexpected GitHub call"))
	with pytest.raises(writer.ApiError, match="(invalid operator-step label registration|operator-step label registration unavailable)"):
		writer._ensure_operator_label("o/r")


def test_writer_trims_oldest_entries_to_fit() -> None:
	big = "x" * 30000
	entries = [(f"pr-{n}", writer.render_entry(f"pr-{n}", "s", [{"title": "t", "instructions": big}])) for n in range(4)]
	body = writer.render_body(entries)
	assert len(body) <= writer.MAX_BODY
	assert [key for key, _ in writer.parse_entries(body)][-1] == "pr-3"


def test_writer_refuses_to_split_last_entry() -> None:
	with pytest.raises(writer.UsageError, match="entry exceeds"):
		writer.render_body([("pr-1", "x" * writer.MAX_BODY)])


def test_writer_refuses_bad_keys_and_ignores_untrusted_issues() -> None:
	with pytest.raises(writer.UsageError):
		writer.upsert("o/r", "Bad Key", "s", [{"title": "t"}])
	assert writer.find_issue([_issue(writer.MARKER, association="NONE")]) is None
	assert writer.find_issue([dict(_issue(writer.MARKER, association="NONE"), user={"login": "external[bot]"})]) is None
	assert writer.find_issue([_issue("no marker")]) is None


def test_status_workflow_wiring() -> None:
	workflow = yaml.safe_load(STATUS.read_text(encoding="utf-8"))
	job = workflow["jobs"]["activation-verify"]
	assert job["needs"] == "sync-issue-status"
	assert "github.event.pull_request.merged == true" in job["if"]
	assert "github.event.pull_request.base.ref == github.event.repository.default_branch" in job["if"]
	assert "vars.ACTIVATION_VERIFY_ENABLED != 'false'" in job["if"]
	assert job["continue-on-error"] is True
	assert workflow["jobs"]["sync-issue-status"]["outputs"]["linked_issue"] == "${{ steps.linked_issue.outputs.number }}"
	steps = {step["name"]: step for step in job["steps"]}
	assert "activation_verify.sh\" pr" in steps["Verify activation"]["run"]


def test_poller_runs_it_on_every_completion_path() -> None:
	text = POLLER.read_text(encoding="utf-8")
	lines = text.splitlines()
	calls = [i for i, line in enumerate(lines) if line.strip() == "emit_orchestrator_completion_lessons"]
	assert len(calls) == 4
	for i in calls:
		assert lines[i + 1].strip() == "run_project_activation_verify"
	assert 'if [ "${PROJECT_STATUS}" = "complete" ] && [ "${COMMENTS_FETCH_OK:-false}" = "true" ] &&' in text
	assert 'endswith("<!-- ai:activation:v1 partial=true source=" + $src + " -->")' in text
	assert 'TRACKING_NUM="${TRACKING_NUM}" \\' in text
	assert 'PROJECT_FILES_JSON="$(jq -c' in text
	assert '(.body // "" | sub("[[:space:]]+$"; ""))' in text
	assert 'COMMENTS_FETCH_OK:-false' in text
	assert 'activation-verify-project-${TRACKING_NUM}-XXXXXX' in text
	poll = POLL.read_text(encoding="utf-8")
	assert "for activation_asset in scripts/activation_verify.sh scripts/operator_step_issue.py prompts/mode-activation-verify.txt prompts/_templates/mode-activation-verify.txt; do" in poll
	assert "ACTIVATION_VERIFY_ENABLED: ${{ vars.ACTIVATION_VERIFY_ENABLED || 'true' }}" in poll


def test_completed_project_retries_only_recent_trusted_partial_verdicts() -> None:
	if not shutil.which("jq"):
		pytest.skip("jq is required to evaluate the poller's retry predicate")
	text = POLLER.read_text(encoding="utf-8")
	filter_text = text.split('printf \'%s\' "${COMMENTS:-[]}" | jq -e --arg src "project-${TRACKING_NUM}" \'', 1)[1].split("' >/dev/null 2>&1; then", 1)[0]
	def partial(minutes_ago: int, *, association: str = "OWNER", source: str = "project-77") -> dict:
		return {
			"author_association": association,
			"body": f"Incomplete.\n\n<!-- ai:activation:v1 partial=true source={source} -->\n",
			"created_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
		}
	for comments, expected in (
		([partial(1)], True),
		([partial(10), partial(1)], True),
		([partial(10), partial(5), partial(1)], False),
		([partial(31), partial(1)], False),
		([partial(1, association="NONE")], False),
		([partial(1, source="project-78")], False),
		([], False),
	):
		result = subprocess.run(["jq", "-e", "--arg", "src", "project-77", filter_text],
			input=json.dumps(comments), text=True, capture_output=True, check=False)
		assert (result.returncode == 0) == expected, result.stderr


def test_model_text_never_starts_a_comment_line(tmp_path: Path) -> None:
	# The poller's /judge_resume, /revalidate and /re-security-pass handlers
	# match a command at the start of any tracking-issue comment line.
	injected = dict(DORMANT, summary="/judge_resume --force", trigger="/revalidate now")
	_, state = _verify(tmp_path, injected)
	body = state["comments"][-1]["body"]
	assert not any(line.lstrip().startswith("/") for line in body.splitlines()), body


LIVE = {"verdict": "LIVE", "trigger": "push", "summary": "Runs on push.", "gaps": []}


def _git_commit_all(target: Path) -> None:
	subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
	subprocess.run(["git", "-C", str(target), "add", "-A"], check=True, capture_output=True)
	subprocess.run(["git", "-C", str(target), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
		"commit", "-m", "links"], check=True, capture_output=True)


def _context(tmp_path: Path) -> dict:
	return json.loads((tmp_path / "rt" / "activation_context.json").read_text(encoding="utf-8"))


def test_tracked_symlinks_reach_model_context(tmp_path: Path) -> None:
	# The read-only model sandbox omits symlinks, so the host lists them.
	target = tmp_path / "target"
	(target / "workflow-templates").mkdir(parents=True)
	(target / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
	os.symlink("../CLAUDE.md", target / "workflow-templates" / "CLAUDE.md")
	_git_commit_all(target)
	result, state = _verify(tmp_path, LIVE)
	assert "outcome=posted" in result.stdout
	context = _context(tmp_path)
	assert context["tracked_symlinks"] == [{"path": "workflow-templates/CLAUDE.md", "target": "../CLAUDE.md"}]
	assert context["tracked_symlinks_truncated"] is False
	assert context["changed_files"] == ["README.md"]
	assert state["comments"]


def test_tracked_symlinks_fail_open_without_git(tmp_path: Path) -> None:
	result, state = _verify(tmp_path, LIVE)
	assert "outcome=posted" in result.stdout
	assert "ACTIVATION_VERIFY tracked_symlinks unavailable reason=git_failed" in result.stderr
	context = _context(tmp_path)
	assert context["tracked_symlinks"] == [] and context["tracked_symlinks_truncated"] is False
	assert state["comments"][-1]["body"].endswith("<!-- ai:activation:v1 verdict=LIVE source=pr-42 -->")


def test_tracked_symlinks_cap_and_filters(tmp_path: Path) -> None:
	target = tmp_path / "target"
	target.mkdir()
	(target / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
	os.symlink("bad\ntarget", target / "aa-bad")
	for n in range(201):
		os.symlink("CLAUDE.md", target / f"link-{n:03d}")
	_git_commit_all(target)
	_verify(tmp_path, LIVE)
	context = _context(tmp_path)
	links = context["tracked_symlinks"]
	assert len(links) == 200 and context["tracked_symlinks_truncated"] is True
	assert links[0] == {"path": "link-000", "target": "CLAUDE.md"}
	assert all("\n" not in link["target"] and link["path"] != "aa-bad" for link in links)


def test_prompt_tells_model_snapshot_omits_symlinks() -> None:
	runtime = (ROOT / "prompts" / "mode-activation-verify.txt").read_text(encoding="utf-8")
	template = (ROOT / "prompts" / "_templates" / "mode-activation-verify.txt").read_text(encoding="utf-8")
	for text in (runtime, template):
		assert "tracked_symlinks" in text and "Never report a listed path" in text
	assert runtime.split("</compaction-rules>\n", 1)[1] == template.split("\n", 1)[1]
