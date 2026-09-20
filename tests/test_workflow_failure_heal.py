#!/usr/bin/env python3
"""Contract and driver tests for the workflow failure heal path.

Covers scripts/workflow_failure_heal.py (pure logic), the three workflow files
plus the consumer template (trigger / label parity), and the two shell drivers
run end to end against a mock `gh` and a mock `codex`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_PATH = SCRIPTS_DIR / "workflow_failure_heal.py"
REPORT_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_report.sh"
INTAKE_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_intake.sh"
REUSABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow_failure_heal.yml"
INTAKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
INTERNAL_WRAPPER = REPO_ROOT / ".github" / "workflows" / "internal-workflow-failure-heal.yml"
CONSUMER_TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-workflow-failure-heal.yml"
PROMPT_FILE = REPO_ROOT / "prompts" / "mode-workflow-failure-heal.txt"
LABEL_CONTRACT = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
SELF_REPO = "shubhodeep1/coding-workflows"
CONSUMER_REPO = "shubhodeep1/example-consumer"
SHA_A = "a" * 40
SHA_B = "b" * 40
FP_HEX = "f" * 64

# Same regex as scripts/resolve_integration_ref.sh (Target branch alias).
TARGET_BRANCH_RE = re.compile(
	r"^\s*(?:-\s*)?(?:\*\*Target branch:\*\*|Target branch:)\s*(?:`\s*([^`\n]+?)\s*`(?:\s.*)?|([^`\s]+))\s*$",
	re.MULTILINE,
)
AUTO_CLOSE_RE = re.compile(r"\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\b\s*:?\s*#\d+", re.IGNORECASE)


def _load_lib():
	spec = importlib.util.spec_from_file_location("workflow_failure_heal", LIB_PATH)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


heal = _load_lib()


def _yaml(path: Path) -> dict:
	return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(doc: dict) -> dict:
	"""PyYAML reads an unquoted `on:` key as True and a quoted `"on":` as a string."""
	return doc[True] if True in doc else doc["on"]


def _labels_in_predicate(predicate: str) -> list[str]:
	# The first fromJson array in these predicates is the kill-switch `["false"]`;
	# the label allow-list is the array that contains `ai:` labels.
	arrays = [json.loads(m) for m in re.findall(r"contains\(fromJson\('(\[[^\]]*\])'\),", predicate)]
	labels = [arr for arr in arrays if any(str(item).startswith("ai:") for item in arr)]
	assert labels, predicate
	return labels[0]


# ---------------------------------------------------------------------------
# Workflow / template contracts
# ---------------------------------------------------------------------------


def test_human_needed_label_parity_across_workflows() -> None:
	expected = list(heal.HUMAN_NEEDED_LABELS)
	reusable = _yaml(REUSABLE_WORKFLOW)
	assert _labels_in_predicate(reusable["jobs"]["report"]["if"]) == expected
	for path in (INTERNAL_WRAPPER, CONSUMER_TEMPLATE):
		wrapper = _yaml(path)
		job = wrapper["jobs"]["report"]
		assert _labels_in_predicate(job["if"]) == expected
		assert set(_on(wrapper).keys()) == {"issues", "pull_request"}
		assert _on(wrapper)["issues"]["types"] == ["labeled"]
		assert _on(wrapper)["pull_request"]["types"] == ["labeled"]
		assert "WORKFLOW_HEAL_ENABLED" in job["if"]
		assert job["with"]["label"] == "${{ github.event.label.name }}"
	contract = json.loads(LABEL_CONTRACT.read_text(encoding="utf-8"))["labels"]
	for label in expected:
		assert label in contract, label
	assert heal.HEAL_LABEL in contract
	assert heal.ESCALATED_LABEL in contract
	# The validate step of the reusable workflow carries the same allow-list.
	validate_step = reusable["jobs"]["report"]["steps"][0]["run"]
	report_script = REPORT_SCRIPT.read_text(encoding="utf-8")
	for label in expected:
		assert label in validate_step
		assert label in report_script
	assert "importlib.util" not in report_script


def test_consumer_template_is_pinned_and_in_full_profile() -> None:
	text = CONSUMER_TEMPLATE.read_text(encoding="utf-8")
	assert "uses: shubhodeep1/coding-workflows/.github/workflows/workflow_failure_heal.yml@stable" in text
	assert "secrets: inherit" in text
	manifest = (REPO_ROOT / "workflow-templates" / "profiles" / "full.txt").read_text(encoding="utf-8").split()
	assert "ai-workflow-failure-heal.yml" in manifest
	internal = INTERNAL_WRAPPER.read_text(encoding="utf-8")
	assert "workflow_failure_heal.yml@main" in internal


def test_intake_workflow_triggers_and_release_names() -> None:
	intake = _yaml(INTAKE_WORKFLOW)
	on = _on(intake)
	assert on["repository_dispatch"]["types"] == [heal.DISPATCH_EVENT_TYPE]
	assert on["workflow_run"]["types"] == ["completed"]
	assert on["workflow_run"]["workflows"] == list(heal.RELEASE_WORKFLOW_NAMES)
	assert "payload_json" in on["workflow_dispatch"]["inputs"]
	actual_names = {
		_yaml(REPO_ROOT / ".github" / "workflows" / name)["name"]
		for name in (
			"test-and-mark-stable.yml",
			"mark-stable.yml",
			"promote-main-to-stable.yml",
			"auto-release-stable.yml",
			"forward-merge-stable-to-main.yml",
		)
	}
	assert set(heal.RELEASE_WORKFLOW_NAMES) == actual_names
	job = intake["jobs"]["intake"]
	assert "failure" in job["if"] and "timed_out" in job["if"]
	assert intake["concurrency"]["group"] == "workflow-failure-heal-intake"
	assert intake["concurrency"]["cancel-in-progress"] is False
	run_step = [s for s in job["steps"] if s.get("name") == "Run workflow failure heal intake"][0]
	assert "scripts/workflow_failure_heal_intake.sh" in run_step["run"]
	payload_step = [s for s in job["steps"] if s.get("id") == "payload"][0]
	# External inputs are env-bound, never interpolated into the script body.
	assert "${{" not in payload_step["run"]
	assert payload_step["env"]["CLIENT_PAYLOAD_JSON"] == "${{ toJson(github.event.client_payload) }}"
	checkout_step = [s for s in job["steps"] if s.get("name") == "Checkout repository"][0]
	assert checkout_step["with"]["persist-credentials"] is False
	intake_script = INTAKE_SCRIPT.read_text(encoding="utf-8")
	assert "--sandbox read-only" in intake_script
	assert "include_apply_patch_tool=false" in intake_script
	assert "shell_environment_policy.ignore_default_excludes=false" in intake_script
	assert 'shell_environment_policy.filters.OPENROUTER_API_KEY="exclude"' in intake_script
	assert "danger-full-access" not in intake_script


def test_reusable_report_workflow_binds_inputs_through_env() -> None:
	reusable = _yaml(REUSABLE_WORKFLOW)
	job = reusable["jobs"]["report"]
	assert job["env"]["WORKFLOW_HEAL_ISSUE_NUMBER"] == "${{ inputs.issue_number }}"
	assert job["env"]["WORKFLOW_HEAL_LABEL"] == "${{ inputs.label }}"
	for step in job["steps"]:
		if step.get("name") in ("Validate inputs", "Report the escalation to coding-workflows"):
			assert "${{" not in step["run"]
	assert _on(reusable)["workflow_call"]["inputs"]["is_pull_request"]["type"] == "boolean"
	stage = [s for s in job["steps"] if s.get("name") == "Stage workflow support files"][0]["run"]
	for name in ("gh_helpers.sh", "tg_helpers.sh", "workflow_failure_heal.py", "workflow_failure_heal_report.sh"):
		assert name in stage
	report_checkout_steps = [step for step in job["steps"] if step.get("uses") == "actions/checkout@v5"]
	assert len(report_checkout_steps) == 3
	assert all(step["with"]["persist-credentials"] is False for step in report_checkout_steps)


def test_prompt_declares_classification_tokens() -> None:
	text = PROMPT_FILE.read_text(encoding="utf-8")
	assert text.startswith("# tier:")
	assert "## Classification" in text
	for token in heal.CLASSIFICATIONS:
		assert f"`{token}`" in text
	assert "UNTRUSTED" in text


def test_stable_log_prefixes_are_registered() -> None:
	agents_text = (REPO_ROOT / "agents.md").read_text(encoding="utf-8")
	for prefix in ("WORKFLOW_HEAL_REPORT", "WORKFLOW_HEAL"):
		assert f"- `{prefix}`" in agents_text
		assert f"LOG_PREFIX.name={prefix}" in agents_text


# ---------------------------------------------------------------------------
# Library: run refs, payloads, validation, skip gates
# ---------------------------------------------------------------------------


def test_extract_run_refs_filters_repo_dedups_and_keeps_newest() -> None:
	texts = [
		f"AI clarification failed. Run: https://github.com/{CONSUMER_REPO}/actions/runs/11",
		f"other repo https://github.com/{SELF_REPO}/actions/runs/99",
		f"Run: https://github.com/{CONSUMER_REPO}/actions/runs/12 and again https://github.com/{CONSUMER_REPO}/actions/runs/11",
		f"Run: https://github.com/{CONSUMER_REPO}/actions/runs/13",
		f"Run: https://github.com/{CONSUMER_REPO}/actions/runs/14",
	]
	refs = heal.extract_run_refs(texts, CONSUMER_REPO)
	assert [ref["run_id"] for ref in refs] == ["11", "13", "14"]
	assert all(ref["repo"] == CONSUMER_REPO for ref in refs)


def test_select_failed_runs_matches_title_and_conclusion() -> None:
	runs = [
		{"id": 1, "conclusion": "success", "display_title": "Fix login", "created_at": "2026-09-01T00:00:00Z", "html_url": "u1", "name": "AI Plan"},
		{"id": 2, "conclusion": "failure", "display_title": "Fix login", "created_at": "2026-09-02T00:00:00Z", "html_url": "u2", "name": "AI Implement"},
		{"id": 3, "conclusion": "failure", "display_title": "Other", "created_at": "2026-09-03T00:00:00Z", "html_url": "u3", "name": "AI Implement"},
		{"id": 4, "conclusion": "timed_out", "display_title": " Fix   login ", "created_at": "2026-09-04T00:00:00Z", "html_url": "u4", "name": "AI Review"},
	]
	picked = heal.select_failed_runs(runs, title="Fix login")
	assert [run["run_id"] for run in picked] == ["2", "4"]


def _issue(number: int = 42, *, title: str = "Add retries to the poller", body: str = "body", labels: list[str] | None = None, state: str = "open") -> dict:
	return {
		"number": number,
		"title": title,
		"body": body,
		"state": state,
		"html_url": f"https://github.com/{CONSUMER_REPO}/issues/{number}",
		"labels": [{"name": name} for name in (labels or ["ai:implementing", "ai:needs-human"])],
	}


def test_build_issue_payload_validates_and_carries_lineage() -> None:
	body = f"<!-- {heal.MARKER_PREFIX}fp={FP_HEX} -->\n<!-- {heal.MARKER_PREFIX}gen=2 -->\n<!-- {heal.MARKER_PREFIX}root={FP_HEX} -->\nsome body"
	comments = [{"body": f"AI implementation workflow failed. Run: https://github.com/{CONSUMER_REPO}/actions/runs/500"}]
	runs = [{"id": 501, "conclusion": "failure", "display_title": "Add retries to the poller", "created_at": "2026-09-02T00:00:00Z", "html_url": "u", "name": "AI Implement"}]
	payload = heal.build_issue_payload(
		repo=CONSUMER_REPO,
		kind="issue",
		label="ai:needs-human",
		issue=_issue(body=body),
		comments=comments,
		runs=runs,
		wrapper_sha=SHA_A.upper(),
		reporter_run_url=f"https://github.com/{CONSUMER_REPO}/actions/runs/1",
	)
	normalized = heal.validate_payload(payload)
	assert normalized["source_gen"] == 2
	assert normalized["source_root"] == FP_HEX
	assert [ref["run_id"] for ref in normalized["run_refs"]] == ["500", "501"]
	assert normalized["wrapper_sha"] is None or normalized["wrapper_sha"] == SHA_A
	assert normalized["label"] == "ai:needs-human"
	assert normalized["issue_number"] == 42
	assert len(json.dumps(payload).encode("utf-8")) < heal.MAX_PAYLOAD_BYTES


def test_validate_payload_rejects_bad_inputs() -> None:
	good = heal.build_issue_payload(repo=CONSUMER_REPO, kind="issue", label="ai:needs-human", issue=_issue(), comments=[], runs=[], wrapper_sha=None, reporter_run_url=None)
	heal.validate_payload(good)
	for mutate in (
		lambda p: p.update(label="ai:planning"),
		lambda p: p.update(source_repo="../evil"),
		lambda p: p.update(source_kind="comment"),
		lambda p: p.update(schema_version="v0"),
		lambda p: p.update(issue_number=None),
	):
		bad = json.loads(json.dumps(good))
		mutate(bad)
		try:
			heal.validate_payload(bad)
		except ValueError:
			pass
		else:
			raise AssertionError(f"payload was accepted: {bad}")
	run_payload = heal.build_workflow_run_payload(repo=SELF_REPO, workflow_run={"id": 7, "name": "Promote main to stable", "conclusion": "failure", "head_sha": SHA_B, "head_branch": "main", "html_url": "u", "display_title": "Promote main to stable"})
	normalized = heal.validate_payload(run_payload)
	assert normalized["head_branch"] == "main" and normalized["head_sha"] == SHA_B
	bad_run = dict(run_payload, conclusion="success")
	try:
		heal.validate_payload(bad_run)
	except ValueError:
		pass
	else:
		raise AssertionError("successful workflow_run was accepted")
	# Foreign run refs are dropped, not trusted.
	tampered = dict(good, run_refs=[{"repo": SELF_REPO, "run_id": "1"}, {"repo": CONSUMER_REPO, "run_id": "abc"}])
	assert heal.validate_payload(tampered)["run_refs"] == []


def test_skip_reason_gates() -> None:
	base = heal.validate_payload(heal.build_issue_payload(repo=CONSUMER_REPO, kind="issue", label="ai:needs-human", issue=_issue(), comments=[], runs=[], wrapper_sha=None, reporter_run_url=None))
	assert heal.skip_reason(base, registered_repos=[CONSUMER_REPO], self_repo=SELF_REPO) == ""
	assert heal.skip_reason(base, registered_repos=[], self_repo=SELF_REPO) == "unregistered_source_repo"
	smoke = dict(base, issue_title="[E2E Smoke Test alt-model] canary")
	assert heal.skip_reason(smoke, registered_repos=[CONSUMER_REPO], self_repo=SELF_REPO) == "smoke_test_fixture"
	smoke_label = dict(base, labels=["e2e-smoke-test"])
	assert heal.skip_reason(smoke_label, registered_repos=[CONSUMER_REPO], self_repo=SELF_REPO) == "smoke_test_fixture"
	own = dict(base, source_repo=SELF_REPO, workflow_name="Internal: AI Workflow Failure Heal")
	assert heal.skip_reason(own, registered_repos=[], self_repo=SELF_REPO) == "self_workflow"


# ---------------------------------------------------------------------------
# Library: signature, fingerprint, budget, classification, composition
# ---------------------------------------------------------------------------


def test_error_signature_ignores_volatile_tokens_and_prefers_error_annotations() -> None:
	log_a = textwrap.dedent(f"""
		2026-09-20T10:00:01.123Z ::warning::something minor
		2026-09-20T10:00:02.123Z PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=123
		2026-09-20T10:00:03.123Z ::error::Unable to resolve integration ref for issue 4123 at {SHA_A} (https://github.com/x/y/actions/runs/1)
	""")
	log_b = log_a.replace("4123", "9999").replace(SHA_A, SHA_B).replace("runs/1", "runs/22").replace("10:00", "11:30")
	sig_a = heal.error_signature(log_a)
	assert sig_a == heal.error_signature(log_b)
	assert sig_a.startswith("::error::unable to resolve integration ref")
	assert "<sha>" in sig_a and "<url>" in sig_a
	assert heal.error_signature("all good\nnothing here") == "no-error-lines"
	assert heal.error_signature("Traceback (most recent call last):\n  File x\nKeyError: 'foo'").startswith("traceback")
	fp1 = heal.fingerprint("AI Implement", "Run codex", sig_a)
	assert fp1 == heal.fingerprint("ai implement", "run codex", sig_a)
	assert fp1 != heal.fingerprint("AI Plan", "Run codex", sig_a)
	assert re.fullmatch(r"[0-9a-f]{64}", fp1)


def _heal_issue(number: int, *, state: str, fp: str, gen: int = 1, root: str | None = None, created: datetime | None = None) -> dict:
	created = created or datetime(2026, 9, 1, tzinfo=timezone.utc)
	return {
		"number": number,
		"state": state,
		"body": f"<!-- {heal.MARKER_PREFIX}fp={fp} -->\n<!-- {heal.MARKER_PREFIX}gen={gen} -->\n<!-- {heal.MARKER_PREFIX}root={root or fp} -->",
		"created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
		"html_url": f"https://github.com/{SELF_REPO}/issues/{number}",
		"pull_request": False,
	}


def test_budget_decision_matrix() -> None:
	now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
	fp = "1" * 64
	other = "2" * 64
	assert heal.budget_decision([], fp=fp, now=now) == {"action": "open", "gen": 1, "root": fp, "open_count": 0, "today_count": 0}

	dup = heal.budget_decision([_heal_issue(10, state="open", fp=fp), _heal_issue(11, state="open", fp=fp, gen=2)], fp=fp, now=now)
	assert dup["action"] == "duplicate" and dup["existing_issue"] == 11
	cross_repo_dup = heal.budget_decision(
		[
			dict(_heal_issue(500, state="open", fp=fp), repository=SELF_REPO),
			dict(_heal_issue(5, state="open", fp=fp), repository=CONSUMER_REPO),
		],
		fp=fp,
		preferred_repo=CONSUMER_REPO,
		now=now,
	)
	assert cross_repo_dup["existing_issue"] == 5
	assert cross_repo_dup["existing_repo"] == CONSUMER_REPO

	lineage = heal.budget_decision([_heal_issue(5, state="closed", fp=fp, gen=1), _heal_issue(6, state="closed", fp=fp, gen=2, root=other)], fp=fp, now=now)
	assert lineage == {"action": "open", "gen": 3, "root": other, "open_count": 0, "today_count": 0}
	cross_repo_lineage = heal.budget_decision(
		[
			dict(_heal_issue(500, state="closed", fp=fp, gen=1), repository=SELF_REPO),
			dict(_heal_issue(5, state="closed", fp=fp, gen=3, root=other), repository=CONSUMER_REPO),
		],
		fp=fp,
		now=now,
	)
	assert cross_repo_lineage["action"] == "escalate"
	assert cross_repo_lineage["prior_issue"] == 5
	assert cross_repo_lineage["prior_repo"] == CONSUMER_REPO

	capped = heal.budget_decision([_heal_issue(6, state="closed", fp=fp, gen=3)], fp=fp, now=now)
	assert capped["action"] == "escalate" and capped["gen"] == 4 and capped["prior_issue"] == 6

	inherited = heal.budget_decision([], fp=fp, source_gen=3, source_root=other, now=now)
	assert inherited["action"] == "escalate" and inherited["root"] == other

	many_open = [_heal_issue(100 + i, state="open", fp=f"{i:064x}") for i in range(10)]
	assert heal.budget_decision(many_open, fp=fp, now=now)["action"] == "budget_exhausted"
	assert heal.budget_decision(many_open, fp=fp, max_open=11, now=now)["action"] == "open"

	today = [_heal_issue(200 + i, state="closed", fp=f"{i + 50:064x}", created=now - timedelta(hours=1)) for i in range(20)]
	per_day = heal.budget_decision(today, fp=fp, now=now)
	assert per_day["action"] == "budget_exhausted" and per_day["reason"] == "max_issues_per_day"
	yesterday = [dict(issue, created_at=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")) for issue in today]
	assert heal.budget_decision(yesterday, fp=fp, now=now)["action"] == "open"
	# Pull requests carrying the label never count.
	pr = dict(_heal_issue(300, state="open", fp=fp), pull_request=True)
	assert heal.budget_decision([pr], fp=fp, now=now)["action"] == "open"


def test_parse_classification_variants() -> None:
	assert heal.parse_classification("## Classification\n\nworkflow-defect\n\n## Summary\nx") == "workflow-defect"
	assert heal.parse_classification("## Classification\n`consumer-config`\n") == "consumer-config"
	assert heal.parse_classification("## classification\n- **Transient** (rate limit)\n") == "transient"
	assert heal.parse_classification("## Summary\nno classification section") == "inconclusive"
	assert heal.parse_classification("## Classification\nsomething-else\n") == "inconclusive"
	assert heal.parse_classification("") == "inconclusive"


def test_compose_issue_body_and_title() -> None:
	payload = heal.validate_payload(heal.build_issue_payload(repo=CONSUMER_REPO, kind="issue", label="ai:needs-human", issue=_issue(), comments=[], runs=[], wrapper_sha=SHA_A, reporter_run_url=None))
	body = heal.compose_issue_body(
		payload=payload,
		diagnosis="## Classification\nworkflow-defect\n\n## Summary\nThe poller crashes.",
		fp=FP_HEX,
		gen=2,
		root=FP_HEX,
		classification="workflow-defect",
		target_branch="stable",
		max_depth=3,
		intake_run_url="https://github.com/x/y/actions/runs/9",
		run_summaries=[{"url": "https://github.com/x/y/actions/runs/5", "workflow_name": "AI Implement", "failing_step": "Run codex"}],
	)
	match = TARGET_BRANCH_RE.search(body)
	assert match and (match.group(1) or match.group(2)) == "stable"
	markers = heal.parse_heal_markers(body)
	assert markers == {"fp": FP_HEX, "gen": "2", "root": FP_HEX, "source": f"{CONSUMER_REPO}#42", "classification": "workflow-defect"}
	assert not AUTO_CLOSE_RE.search(body)
	assert "The poller crashes." in body
	assert "Failed run" in body and "Run codex" in body
	assert "Fix constraints" in body
	title = heal.compose_issue_title(payload, workflow_name="AI Implement")
	assert title == f"Workflow heal: AI Implement failed for {CONSUMER_REPO}#42 (ai:needs-human)"
	no_target = heal.compose_issue_body(payload=payload, diagnosis="x", fp=FP_HEX, gen=1, root=FP_HEX, classification="consumer-app-defect", target_branch=None, max_depth=3, intake_run_url="u", run_summaries=[])
	assert not TARGET_BRANCH_RE.search(no_target)
	assert "this repository's own code" in no_target
	occurrence = heal.compose_occurrence_comment(payload, intake_run_url="u")
	assert f"{heal.MARKER_PREFIX}occurrence" in occurrence


def test_filter_log_keeps_signal_lines_and_bounds_size() -> None:
	lines = [f"line {i}" for i in range(1000)]
	lines[10] = "::error::early failure"
	filtered = heal.filter_log("\n".join(lines), max_lines=50, max_bytes=100_000)
	assert "::error::early failure" in filtered
	assert "line 999" in filtered and "line 500" not in filtered
	bounded = heal.filter_log("x" * 10_000, max_lines=10, max_bytes=1_000)
	assert len(bounded.encode("utf-8")) <= 1_100


# ---------------------------------------------------------------------------
# Shell drivers against a mock gh / mock codex
# ---------------------------------------------------------------------------

MOCK_GH = r'''#!/usr/bin/env python3
import json, os, sys, urllib.parse
from pathlib import Path
state_path = Path(os.environ["MOCK_GH_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)

def save():
	state_path.write_text(json.dumps(state))

def out(text):
	save()
	sys.stdout.write(text)
	sys.exit(0)

def fail(msg):
	save()
	sys.stderr.write(msg + "\n")
	sys.exit(1)

def inferred_method(rest):
	method = ""
	has_field = False
	index = 0
	while index < len(rest):
		arg = rest[index]
		if arg in ("-X", "--method"):
			method = rest[index + 1]
			index += 2
			continue
		if arg.startswith("-X") and len(arg) > 2:
			method = arg[2:]
		if arg in ("-f", "-F", "--field", "--raw-field"):
			has_field = True
		index += 1
	return method or ("POST" if has_field else "GET")

if args[:1] == ["api"]:
	rest = [a for a in args[1:]]
	method = inferred_method(rest)
	path = next((a for a in rest if a.startswith("repos/")), "")
	if "--input" in rest:
		body = json.loads(Path(rest[rest.index("--input") + 1]).read_text())
		state.setdefault("dispatches", []).append({"path": path, "body": body})
		if state.get("dispatch_fail"):
			fail("HTTP 422")
		out("")
	if path.endswith("/dispatches"):
		fail("unexpected dispatch call")
	if path.startswith("repos/") and "/issues/" in path and path.endswith("/comments") and "-F" in rest:
		body_arg = rest[rest.index("-F") + 1]
		if body_arg.startswith("body=@"):
			text = Path(body_arg[len("body=@"):]).read_text()
			state.setdefault("comments_posted", []).append({"path": path, "body": text})
			out("{}")
	if "/issues/" in path and path.endswith("/comments"):
		if method != "GET":
			fail("HTTP method must be GET for comments list")
		items = state.get("comments", {}).get(path, [])
		out("".join(json.dumps(item) + "\n" for item in items))
	if path.endswith("/issues") and "--paginate" in rest:
		if method != "GET":
			fail("HTTP method must be GET for issues list")
		repo_slug = path[len("repos/"):-len("/issues")]
		issues_by_repo = state.get("heal_issues_by_repo", {})
		items = issues_by_repo.get(repo_slug, state.get("heal_issues", []) if repo_slug == "shubhodeep1/coding-workflows" else [])
		out("".join(json.dumps(item) + "\n" for item in items))
	if "/issues/" in path and path.split("/")[-1].isdigit():
		number = path.split("/")[-1]
		issue = state.get("issues", {}).get(number)
		if issue is None:
			fail("HTTP 404")
		out(json.dumps(issue))
	if path.endswith("/actions/runs"):
		if method != "GET":
			fail("HTTP method must be GET for runs list")
		out(json.dumps({"workflow_runs": state.get("runs", [])}))
	if "/actions/runs/" in path and path.endswith("/jobs"):
		if method != "GET":
			fail("HTTP method must be GET for jobs list")
		run_id = path.split("/")[-2]
		jobs = state.get("jobs", {}).get(run_id)
		if jobs is None:
			fail("HTTP 404")
		out(json.dumps({"jobs": jobs}))
	if "/actions/jobs/" in path and path.endswith("/logs"):
		job_id = path.split("/")[-2]
		text = state.get("job_logs", {}).get(job_id)
		if text is None:
			fail("HTTP 404")
		out(text)
	if "/branches/" in path:
		branch = urllib.parse.unquote(path.split("/branches/")[-1])
		if branch in state.get("branches", ["stable", "main"]):
			out(json.dumps({"name": branch}))
		fail("HTTP 404")
	fail("unexpected api path " + path + " args=" + " ".join(rest))
if args[:2] == ["label", "create"]:
	state.setdefault("labels_created", []).append(args[2:])
	out("")
if args[:2] == ["issue", "create"]:
	record = {}
	i = 2
	while i < len(args):
		key = args[i]
		if key == "--body-file":
			record["body"] = Path(args[i + 1]).read_text()
		elif key.startswith("--"):
			record[key[2:]] = args[i + 1]
		i += 2
	state.setdefault("issues_created", []).append(record)
	if state.get("issue_create_fail"):
		fail("HTTP 500")
	out("https://github.com/%s/issues/%d\n" % (record.get("repo"), 900 + len(state["issues_created"])))
if args[:2] == ["issue", "edit"]:
	state.setdefault("issue_edits", []).append(args[2:])
	out("")
fail("unexpected gh call " + " ".join(args))
'''

MOCK_CODEX = r'''#!/usr/bin/env bash
set -euo pipefail
cat > "${MOCK_PROMPT_OUT}"
if [ "${MOCK_CODEX_FAIL:-}" = "true" ]; then
	exit 1
fi
cat "${MOCK_DIAGNOSIS_FILE}"
'''


def _stage(tmp: Path, *, with_codex: bool, wrapper_pin: str | None = None) -> tuple[Path, Path, dict[str, str]]:
	work = tmp / "work"
	(work / "scripts").mkdir(parents=True)
	(work / "prompts").mkdir()
	for name in ("workflow_failure_heal.py", "workflow_failure_heal_report.sh", "workflow_failure_heal_intake.sh", "label_helpers.sh", "render_prompt.sh"):
		src = SCRIPTS_DIR / name
		if src.exists():
			shutil.copy(src, work / "scripts" / name)
	shutil.copy(PROMPT_FILE, work / "prompts" / PROMPT_FILE.name)
	(work / "unattended_system_instructions.md").write_text("system instructions\n", encoding="utf-8")
	(work / "agents.md").write_text("architecture\n", encoding="utf-8")
	(work / ".github" / "ai").mkdir(parents=True)
	(work / ".github" / "ai" / "consumer_repos.json").write_text(json.dumps([CONSUMER_REPO]), encoding="utf-8")
	(work / ".github" / "workflows").mkdir()
	if wrapper_pin:
		(work / ".github" / "workflows" / "ai-implement.yml").write_text(
			f"jobs:\n  implement:\n    uses: shubhodeep1/coding-workflows/.github/workflows/implement.yml@{wrapper_pin} # stable\n",
			encoding="utf-8",
		)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	if with_codex:
		(bin_dir / "codex").write_text(MOCK_CODEX, encoding="utf-8")
		(bin_dir / "codex").chmod(0o755)
	state_file = tmp / "state.json"
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"MOCK_GH_STATE": str(state_file),
		"GH_TOKEN": "x",
		"RUNTIME_DIR": str(tmp / "runtime"),
		"GITHUB_RUN_ID": "777",
		"GITHUB_SERVER_URL": "https://github.com",
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	return work, state_file, env


def _run(script: Path, work: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True, check=False)


def _state(state_file: Path) -> dict:
	return json.loads(state_file.read_text(encoding="utf-8"))


def _report_state(**overrides) -> dict:
	issue = _issue()
	state = {
		"issues": {"42": issue},
		"comments": {
			f"repos/{CONSUMER_REPO}/issues/42/comments": [
				{"body": f"AI implementation workflow failed for issue #42. Run: https://github.com/{CONSUMER_REPO}/actions/runs/500", "created_at": "2026-09-19T00:00:00Z"},
			]
		},
		"runs": [
			{"id": 501, "conclusion": "failure", "display_title": issue["title"], "created_at": "2026-09-19T01:00:00Z", "html_url": f"https://github.com/{CONSUMER_REPO}/actions/runs/501", "name": "AI Review & Autofix"},
			{"id": 502, "conclusion": "success", "display_title": issue["title"], "created_at": "2026-09-19T02:00:00Z", "html_url": "x", "name": "AI Plan"},
		],
	}
	state.update(overrides)
	return state


def test_report_script_dispatches_thin_payload() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-report-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage(tmp, with_codex=False, wrapper_pin=SHA_A)
		state_file.write_text(json.dumps(_report_state()), encoding="utf-8")
		env.update({"GITHUB_REPOSITORY": CONSUMER_REPO, "WORKFLOW_HEAL_ISSUE_NUMBER": "42", "WORKFLOW_HEAL_LABEL": "ai:needs-human"})
		result = _run(REPORT_SCRIPT, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "WORKFLOW_HEAL_REPORT dispatched issue=42 kind=issue label=ai:needs-human runs=2" in result.stdout
		state = _state(state_file)
		assert len(state["dispatches"]) == 1
		dispatch = state["dispatches"][0]
		assert dispatch["path"] == f"repos/{SELF_REPO}/dispatches"
		assert dispatch["body"]["event_type"] == "workflow-failure-heal"
		payload = heal.validate_payload(dispatch["body"]["client_payload"])
		assert payload["source_repo"] == CONSUMER_REPO
		assert payload["wrapper_sha"] == SHA_A
		assert [ref["run_id"] for ref in payload["run_refs"]] == ["500", "501"]
		assert payload["source_kind"] == "issue"
		assert "AI implementation workflow failed" in payload["comments_excerpt"]
		for call in state["calls"]:
			if any(part.endswith("/comments") or part.endswith("/actions/runs") for part in call):
				assert "--method" in call and call[call.index("--method") + 1] == "GET"


def test_report_script_skip_paths() -> None:
	cases = [
		({"WORKFLOW_HEAL_ENABLED": "false"}, _report_state(), "skip reason=disabled"),
		({"WORKFLOW_HEAL_LABEL": "ai:planning"}, _report_state(), "skip reason=label_not_human_needed"),
		({"WORKFLOW_HEAL_ISSUE_NUMBER": "abc"}, _report_state(), "skip reason=invalid_issue_number"),
		({}, _report_state(issues={"42": _issue(state="closed")}), "skip reason=issue_not_open"),
		({}, _report_state(issues={"42": _issue(title="[E2E Smoke Test] canary")}), "skip reason=smoke_test_fixture"),
		({}, _report_state(issues={}), "skip reason=issue_fetch_failed"),
	]
	for extra_env, state, expected in cases:
		with tempfile.TemporaryDirectory(prefix="heal-report-skip-") as tmp_name:
			tmp = Path(tmp_name)
			work, state_file, env = _stage(tmp, with_codex=False)
			state_file.write_text(json.dumps(state), encoding="utf-8")
			env.update({"GITHUB_REPOSITORY": CONSUMER_REPO, "WORKFLOW_HEAL_ISSUE_NUMBER": "42", "WORKFLOW_HEAL_LABEL": "ai:needs-human"})
			env.update(extra_env)
			result = _run(REPORT_SCRIPT, work, env)
			assert result.returncode == 0, (expected, result.stderr, result.stdout)
			assert expected in result.stdout, (expected, result.stdout)
			assert "dispatches" not in _state(state_file)


def test_report_script_dispatch_failure_is_red() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-report-fail-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage(tmp, with_codex=False)
		state_file.write_text(json.dumps(_report_state(dispatch_fail=True)), encoding="utf-8")
		env.update({"GITHUB_REPOSITORY": CONSUMER_REPO, "WORKFLOW_HEAL_ISSUE_NUMBER": "42", "WORKFLOW_HEAL_LABEL": "ai:needs-human"})
		result = _run(REPORT_SCRIPT, work, env)
		assert result.returncode == 1
		assert "error dispatch_failed" in result.stdout


def _consumer_payload(**overrides) -> dict:
	payload = heal.build_issue_payload(
		repo=CONSUMER_REPO,
		kind="issue",
		label="ai:needs-human",
		issue=_issue(),
		comments=[{"body": f"Run: https://github.com/{CONSUMER_REPO}/actions/runs/500"}],
		runs=[],
		wrapper_sha=SHA_A,
		reporter_run_url=None,
	)
	payload.update(overrides)
	return payload


JOB_LOG = (
	"2026-09-20T10:00:00.000Z ##[group]Run codex\n"
	"2026-09-20T10:00:01.000Z codex: applying plan\n"
	"2026-09-20T10:00:02.000Z ::error::resolve_integration_ref.sh: Integration branch 'orchestrator/project-4123' declared in child issue #4200 does not exist.\n"
	"2026-09-20T10:00:03.000Z ##[error]Process completed with exit code 1.\n"
)


def _intake_state(**overrides) -> dict:
	state = {
		"jobs": {
			"500": [
				{
					"id": 9001,
					"name": "implement",
					"workflow_name": "AI Implement",
					"conclusion": "failure",
					"steps": [{"name": "Checkout", "conclusion": "success"}, {"name": "Run codex", "conclusion": "failure"}],
				},
				{"id": 9002, "name": "notify", "workflow_name": "AI Implement", "conclusion": "success", "steps": []},
			]
		},
		"job_logs": {"9001": JOB_LOG},
		"heal_issues": [],
		"branches": ["stable", "main"],
	}
	state.update(overrides)
	return state


def _run_intake(payload: dict, state: dict, *, diagnosis: str, extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict, str]:
	with tempfile.TemporaryDirectory(prefix="heal-intake-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage(tmp, with_codex=True)
		state_file.write_text(json.dumps(state), encoding="utf-8")
		payload_file = tmp / "payload_raw.json"
		payload_file.write_text(json.dumps(payload), encoding="utf-8")
		diagnosis_file = tmp / "diagnosis.md"
		diagnosis_file.write_text(diagnosis, encoding="utf-8")
		prompt_out = tmp / "prompt.txt"
		env.update(
			{
				"GITHUB_REPOSITORY": SELF_REPO,
				"WORKFLOW_HEAL_PAYLOAD_FILE": str(payload_file),
				"WORKFLOW_HEAL_SOURCE_CHECKOUT": "false",
				"MOCK_DIAGNOSIS_FILE": str(diagnosis_file),
				"MOCK_PROMPT_OUT": str(prompt_out),
			}
		)
		env.update(extra_env or {})
		result = _run(INTAKE_SCRIPT, work, env)
		prompt = prompt_out.read_text(encoding="utf-8") if prompt_out.exists() else ""
		return result, _state(state_file), prompt


DIAG_WORKFLOW_DEFECT = "## Classification\nworkflow-defect\n\n## Summary\nThe resolver rejects a missing integration branch instead of falling back.\n\n## Evidence\n```\n::error::resolve_integration_ref.sh\n```\n"


def test_intake_opens_upstream_hotfix_issue_for_workflow_defect() -> None:
	result, state, prompt = _run_intake(_consumer_payload(), _intake_state(), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL classification=workflow-defect" in result.stdout
	assert len(state["issues_created"]) == 1
	created = state["issues_created"][0]
	assert created["repo"] == SELF_REPO
	assert created["label"] == heal.HEAL_LABEL
	assert created["title"] == f"Workflow heal: AI Implement failed for {CONSUMER_REPO}#42 (ai:needs-human)"
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "stable"
	markers = heal.parse_heal_markers(created["body"])
	assert markers["gen"] == "1" and markers["classification"] == "workflow-defect"
	assert "The resolver rejects a missing integration branch" in created["body"]
	# The prompt carried the log evidence, the release pin, and the trust notice.
	assert "resolve_integration_ref.sh: Integration branch" in prompt
	assert SHA_A in prompt and "UNTRUSTED" in prompt and "failing step: Run codex" in prompt
	# The escalated consumer issue got an outcome comment.
	assert any(c["path"] == f"repos/{CONSUMER_REPO}/issues/42/comments" and "issues/901" in c["body"] for c in state["comments_posted"])
	# Heal labels were ensured through label_helpers (no raw gh label create in the script).
	assert any(args[0] == heal.HEAL_LABEL for args in state["labels_created"])
	assert "gh label create" not in INTAKE_SCRIPT.read_text(encoding="utf-8")
	for call in state["calls"]:
		if any(part.endswith("/jobs") or part.endswith("/issues") for part in call):
			assert "--method" in call and call[call.index("--method") + 1] == "GET"


def test_intake_routes_consumer_defect_to_consumer_repo() -> None:
	diag = "## Classification\nconsumer-app-defect\n\n## Summary\nThe app's tests import a removed module.\n"
	result, state, _ = _run_intake(_consumer_payload(), _intake_state(), diagnosis=diag)
	assert result.returncode == 0, result.stderr + result.stdout
	created = state["issues_created"][0]
	assert created["repo"] == CONSUMER_REPO
	assert not TARGET_BRANCH_RE.search(created["body"])
	assert any(args[0] == heal.HEAL_LABEL and args[2] == CONSUMER_REPO for args in state["labels_created"])


def test_intake_deduplicates_and_escalates_consumer_owned_heal_issues() -> None:
	signature = heal.error_signature(heal.filter_log(JOB_LOG))
	fp = heal.fingerprint("AI Implement", "Run codex", signature)
	consumer_duplicate = _heal_issue(31, state="open", fp=fp)
	state = _intake_state(heal_issues_by_repo={SELF_REPO: [], CONSUMER_REPO: [consumer_duplicate]})
	result, state_after, _ = _run_intake(_consumer_payload(), state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert f"existing_repo={CONSUMER_REPO}" in result.stdout
	assert "issues_created" not in state_after
	assert any(comment["path"] == f"repos/{CONSUMER_REPO}/issues/31/comments" for comment in state_after["comments_posted"])

	consumer_capped = _heal_issue(32, state="closed", fp=fp, gen=3)
	state = _intake_state(heal_issues_by_repo={SELF_REPO: [], CONSUMER_REPO: [consumer_capped]})
	result, state_after, _ = _run_intake(_consumer_payload(), state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert f"prior_repo={CONSUMER_REPO}" in result.stdout
	assert ["32", "--repo", CONSUMER_REPO, "--add-label", heal.ESCALATED_LABEL] in state_after["issue_edits"]


def test_intake_no_issue_for_config_and_transient() -> None:
	for classification in ("consumer-config", "transient"):
		diag = f"## Classification\n{classification}\n\n## Summary\nOperator action needed.\n"
		result, state, _ = _run_intake(_consumer_payload(), _intake_state(), diagnosis=diag)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "issues_created" not in state
		assert f"WORKFLOW_HEAL no_issue classification={classification}" in result.stdout
		comment = [c for c in state["comments_posted"] if c["path"].startswith(f"repos/{CONSUMER_REPO}/")][0]
		assert f"`{classification}`" in comment["body"] and "Operator action needed." in comment["body"]


def test_intake_duplicate_records_occurrence() -> None:
	# First compute the fingerprint the intake will derive for this failure.
	signature = heal.error_signature(heal.filter_log(JOB_LOG))
	fp = heal.fingerprint("AI Implement", "Run codex", signature)
	state = _intake_state(heal_issues=[_heal_issue(31, state="open", fp=fp)])
	result, state_after, _ = _run_intake(_consumer_payload(), state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL duplicate existing_issue=31" in result.stdout
	assert "issues_created" not in state_after
	occurrence = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{SELF_REPO}/issues/31/comments"]
	assert occurrence and f"{heal.MARKER_PREFIX}occurrence" in occurrence[0]["body"]
	# No model call happens for a duplicate.
	assert not any("codex" in " ".join(call) for call in state_after["calls"])


def test_intake_lineage_cap_escalates_prior_issue() -> None:
	signature = heal.error_signature(heal.filter_log(JOB_LOG))
	fp = heal.fingerprint("AI Implement", "Run codex", signature)
	state = _intake_state(heal_issues=[_heal_issue(31, state="closed", fp=fp, gen=3)])
	result, state_after, _ = _run_intake(_consumer_payload(), state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL escalate reason=lineage_cap gen=4 max=3" in result.stdout
	assert "issues_created" not in state_after
	assert ["31", "--repo", SELF_REPO, "--add-label", heal.ESCALATED_LABEL] in state_after["issue_edits"]


def test_intake_budget_and_registry_gates() -> None:
	many_open = [_heal_issue(100 + i, state="open", fp=f"{i:064x}") for i in range(10)]
	result, state, _ = _run_intake(_consumer_payload(), _intake_state(heal_issues=many_open), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0
	assert "skip reason=budget_exhausted detail=max_open_issues" in result.stdout
	assert "issues_created" not in state

	result, state, _ = _run_intake(_consumer_payload(source_repo="shubhodeep1/not-registered"), _intake_state(), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0
	assert "skip reason=unregistered_source_repo" in result.stdout
	assert "calls" not in state or not any("issue" in call for call in state["calls"])

	result, state, _ = _run_intake({"schema_version": "nope"}, _intake_state(), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0
	assert "skip reason=invalid_payload" in result.stdout

	result, state, _ = _run_intake(_consumer_payload(), _intake_state(), diagnosis=DIAG_WORKFLOW_DEFECT, extra_env={"WORKFLOW_HEAL_ENABLED": "false"})
	assert result.returncode == 0 and "skip reason=disabled" in result.stdout


def test_intake_falls_back_to_inconclusive_when_model_fails() -> None:
	result, state, _ = _run_intake(_consumer_payload(), _intake_state(), diagnosis="", extra_env={"MOCK_CODEX_FAIL": "true"})
	assert result.returncode == 0, result.stderr + result.stdout
	assert "warn codex_exec_nonzero" in result.stdout
	created = state["issues_created"][0]
	assert created["repo"] == SELF_REPO
	assert heal.parse_heal_markers(created["body"])["classification"] == "inconclusive"
	assert "Automated diagnosis failed (codex exited non-zero)" in created["body"]
	assert "inconclusive" in created["body"]


def test_intake_workflow_run_targets_failed_branch_and_skips_downstream_gate() -> None:
	run_payload = heal.build_workflow_run_payload(
		repo=SELF_REPO,
		workflow_run={"id": 500, "name": "Promote main to stable", "conclusion": "failure", "head_sha": SHA_B, "head_branch": "main", "html_url": f"https://github.com/{SELF_REPO}/actions/runs/500", "display_title": "Promote main to stable"},
	)
	promote_log = "2026-09-20T00:00:00.000Z ::error::Validate stable is fast-forwardable to main: stable has diverged\n"
	state = _intake_state(jobs={"500": [{"id": 9001, "name": "promote", "workflow_name": "Promote main to stable", "conclusion": "failure", "steps": [{"name": "Validate stable is fast-forwardable to main", "conclusion": "failure"}]}]}, job_logs={"9001": promote_log})
	result, state_after, prompt = _run_intake(run_payload, state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	created = state_after["issues_created"][0]
	assert created["repo"] == SELF_REPO
	assert created["title"] == "Workflow heal: Promote main to stable failed on main"
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "main"
	assert "Failed workflow: Promote main to stable" in prompt
	assert "comments_posted" not in state_after  # no source issue to comment on

	gate_log = "2026-09-20T00:00:00.000Z PROMOTE_CYCLE_FAILED reason=smoke_gate_failed run=123\n2026-09-20T00:00:01.000Z ##[error]Process completed with exit code 1.\n"
	state = _intake_state(jobs={"500": [{"id": 9001, "name": "cycle", "workflow_name": "Promote main to stable", "conclusion": "failure", "steps": [{"name": "Run the promote cycle", "conclusion": "failure"}]}]}, job_logs={"9001": gate_log})
	result, state_after, _ = _run_intake(run_payload, state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0
	assert "skip reason=downstream_gate_failure" in result.stdout
	assert "issues_created" not in state_after


def test_intake_workflow_run_preserves_slash_bearing_target_branch() -> None:
	branch_name = "release/2026-09"
	run_payload = heal.build_workflow_run_payload(
		repo=SELF_REPO,
		workflow_run={"id": 500, "name": "Mark Stable Release", "conclusion": "failure", "head_sha": SHA_B, "head_branch": branch_name, "html_url": f"https://github.com/{SELF_REPO}/actions/runs/500", "display_title": "Mark Stable Release"},
	)
	state = _intake_state(branches=["stable", "main", branch_name])
	result, state_after, _ = _run_intake(run_payload, state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	created = state_after["issues_created"][0]
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == branch_name
	branch_calls = [call for call in state_after["calls"] if any("/branches/" in part for part in call)]
	assert branch_calls and "%2F" in next(part for part in branch_calls[0] if "/branches/" in part)


def test_intake_without_linked_runs_still_files_from_label_context() -> None:
	payload = _consumer_payload(run_refs=[])
	result, state, prompt = _run_intake(payload, _intake_state(), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "no failed run could be linked" in prompt
	created = state["issues_created"][0]
	assert created["title"] == f"Workflow heal: ai:needs-human on {CONSUMER_REPO}#42"
