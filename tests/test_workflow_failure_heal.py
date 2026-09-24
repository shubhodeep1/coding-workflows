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
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_autofix_step_scripts import expanded_review_autofix_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_PATH = SCRIPTS_DIR / "workflow_failure_heal.py"
REPORT_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_report.sh"
INTAKE_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_intake.sh"
REUSABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow_failure_heal.yml"
INTAKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-failure-heal-intake.yml"
INTERNAL_WRAPPER = REPO_ROOT / ".github" / "workflows" / "internal-workflow-failure-heal.yml"
CONSUMER_TEMPLATE = REPO_ROOT / "workflow-templates" / "ai-workflow-failure-heal.yml"
AUTOFIX_REPORT_SCRIPT = SCRIPTS_DIR / "workflow_failure_heal_autofix_report.sh"
REVIEW_AUTOFIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
STAGE_SUPPORT_SCRIPT = SCRIPTS_DIR / "stage_workflow_support.sh"
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


def test_reporters_share_dispatch_envelope_failure_reason() -> None:
	assert "reason=dispatch_envelope_failed" in REPORT_SCRIPT.read_text(encoding="utf-8")
	assert "reason=dispatch_envelope_failed" in AUTOFIX_REPORT_SCRIPT.read_text(encoding="utf-8")


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
	for prefix in ("WORKFLOW_HEAL_REPORT", "WORKFLOW_HEAL_AUTOFIX_REPORT", "WORKFLOW_HEAL"):
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


def test_build_issue_payload_prioritizes_recent_diagnostics_and_compacts_state() -> None:
	state_comment = (
		f"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest={'a' * 64} -->\n"
		+ ("state-data" * 1000)
		+ "\nORCHESTRATOR_STATE_V2 -->"
	)
	run_url = f"https://github.com/{CONSUMER_REPO}/actions/runs/7001"
	payload = heal.build_issue_payload(
		repo=CONSUMER_REPO,
		kind="issue",
		label="ai:harness-broken",
		issue=_issue(),
		comments=[
			{"body": state_comment},
			{"body": f"Harness diagnosis: template renderer failed.\nRun: {run_url}"},
		],
		runs=[],
		wrapper_sha=None,
		reporter_run_url=None,
	)

	assert payload["comments_excerpt"].startswith("Harness diagnosis: template renderer failed.")
	assert "[ORCHESTRATOR_STATE_V2 state part 1/1 omitted]" in payload["comments_excerpt"]
	assert "state-datastate-data" not in payload["comments_excerpt"]
	assert len(payload["comments_excerpt"]) <= heal.COMMENTS_EXCERPT_LIMIT
	assert payload["run_refs"] == [{"repo": CONSUMER_REPO, "run_id": "7001", "url": run_url}]


def test_build_issue_payload_preserves_malformed_state_markers_and_full_body_run_refs() -> None:
	run_url = f"https://github.com/{CONSUMER_REPO}/actions/runs/7002"
	oversized_diagnosis = "Newest harness diagnosis\n" + ("x" * 7000) + f"\nRun: {run_url}"
	payload = heal.build_issue_payload(
		repo=CONSUMER_REPO,
		kind="issue",
		label="ai:harness-broken",
		issue=_issue(),
		comments=[
			{"body": "<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest=" + ("b" * 64) + " -->\nmalformed state"},
			{"body": oversized_diagnosis},
		],
		runs=[],
		wrapper_sha=None,
		reporter_run_url=None,
	)

	assert payload["comments_excerpt"].startswith("Newest harness diagnosis")
	assert "[... truncated ...]" in payload["comments_excerpt"]
	assert len(payload["comments_excerpt"]) <= heal.COMMENTS_EXCERPT_LIMIT
	assert payload["run_refs"] == [{"repo": CONSUMER_REPO, "run_id": "7002", "url": run_url}]

	malformed_excerpt = heal._build_recent_comments_excerpt([
		"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest=" + ("b" * 64) + " -->\nmalformed state",
		"newest comment",
	])
	assert malformed_excerpt.startswith("newest comment")
	assert "malformed state" in malformed_excerpt
	assert "state part 1/1 omitted" not in malformed_excerpt


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


def test_wrap_dispatch_stays_within_client_payload_limits() -> None:
	# GitHub rejects a client_payload with more than 10 top-level properties
	# (HTTP 422); every flat report kind exceeds that, the envelope never does.
	payloads = [
		heal.build_issue_payload(repo=CONSUMER_REPO, kind="issue", label="ai:needs-human", issue=_issue(), comments=[], runs=[], wrapper_sha=SHA_A, reporter_run_url=None),
		heal.build_workflow_run_payload(repo=SELF_REPO, workflow_run={"id": 9, "head_sha": SHA_A, "head_branch": "stable", "conclusion": "failure", "name": "Mark Stable Release", "html_url": "u"}),
	]
	for payload in payloads:
		assert len(payload) > heal.DISPATCH_CLIENT_PAYLOAD_MAX_KEYS
		body = heal.wrap_dispatch(payload)
		assert body["event_type"] == heal.DISPATCH_EVENT_TYPE
		assert body["client_payload"] == {"schema_version": heal.SCHEMA_VERSION, "report": payload}
		assert len(body["client_payload"]) <= heal.DISPATCH_CLIENT_PAYLOAD_MAX_KEYS
		assert heal.validate_payload(heal.unwrap_dispatch(body["client_payload"]))["source_repo"] == payload["source_repo"]
	# Every excerpt at its limit: the enveloped body stays under the 60 KB bound.
	huge = "x" * 20_000
	payload = heal.build_autofix_failure_payload(
		repo=CONSUMER_REPO,
		pr={"number": 7, "title": huge, "body": huge, "html_url": "u", "labels": [{"name": f"l{i}"} for i in range(80)], "head": {"sha": SHA_B, "ref": "ai/issue-7"}},
		comments=[{"body": huge} for _ in range(10)],
		workflow_name=huge,
		failure_reason="editor_empty_noop",
		failure_evidence=huge,
		failure_streak=3,
		run_id="500",
		run_url=huge,
		wrapper_sha=SHA_A,
		reporter_run_url=huge,
	)
	encoded = json.dumps(heal.wrap_dispatch(payload)).encode("utf-8")
	assert len(encoded) < heal.MAX_PAYLOAD_BYTES


def test_unwrap_dispatch_accepts_flat_and_enveloped() -> None:
	flat = _consumer_payload()
	assert heal.unwrap_dispatch(flat) is flat
	assert heal.unwrap_dispatch({"schema_version": heal.SCHEMA_VERSION, "report": flat}) == flat
	# Not an envelope: wrong schema, non-dict report, or not a dict at all.
	wrong_schema = {"schema_version": "other.v9", "report": flat}
	assert heal.unwrap_dispatch(wrong_schema) is wrong_schema
	bad_report = {"schema_version": heal.SCHEMA_VERSION, "report": "x"}
	assert heal.unwrap_dispatch(bad_report) is bad_report
	assert heal.unwrap_dispatch(["x"]) == ["x"]
	# CLI round trip: wrap-dispatch then unwrap-dispatch returns the report.
	with tempfile.TemporaryDirectory(prefix="heal-wrap-") as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "payload.json").write_text(json.dumps(flat), encoding="utf-8")
		wrapped = subprocess.run(["python3", str(LIB_PATH), "wrap-dispatch", "--payload-json", str(tmp / "payload.json")], capture_output=True, text=True, check=True)
		body = json.loads(wrapped.stdout)
		(tmp / "client_payload.json").write_text(json.dumps(body["client_payload"]), encoding="utf-8")
		unwrapped = subprocess.run(["python3", str(LIB_PATH), "unwrap-dispatch", "--payload-json", str(tmp / "client_payload.json")], capture_output=True, text=True, check=True)
		assert json.loads(unwrapped.stdout) == flat
		# A non-object report is refused by wrap-dispatch.
		(tmp / "list.json").write_text("[]", encoding="utf-8")
		refused = subprocess.run(["python3", str(LIB_PATH), "wrap-dispatch", "--payload-json", str(tmp / "list.json")], capture_output=True, text=True, check=False)
		assert refused.returncode == 2 and "payload must be a JSON object" in refused.stderr


def test_intake_materialize_step_unwraps_enveloped_repository_dispatch() -> None:
	# Execute the "Materialize the report payload" step body as the runner would
	# for both repository_dispatch shapes: enveloped (current reporters) and
	# flat (reporters staged from an older release).
	intake = _yaml(INTAKE_WORKFLOW)
	step = [s for s in intake["jobs"]["intake"]["steps"] if s.get("id") == "payload"][0]
	flat = _consumer_payload()
	for client_payload in (heal.wrap_dispatch(flat)["client_payload"], flat):
		with tempfile.TemporaryDirectory(prefix="heal-materialize-") as tmp_name:
			tmp = Path(tmp_name)
			output_file = tmp / "github_output"
			payload_file = tmp / "payload_raw.json"
			result = subprocess.run(
				["bash", "-c", step["run"]],
				cwd=REPO_ROOT,
				env={
					**os.environ,
					"EVENT_NAME": "repository_dispatch",
					"CLIENT_PAYLOAD_JSON": json.dumps(client_payload),
					"RUNTIME_DIR": str(tmp),
					"WORKFLOW_HEAL_PAYLOAD_FILE": str(payload_file),
					"GITHUB_OUTPUT": str(output_file),
					"PYTHONDONTWRITEBYTECODE": "1",
				},
				capture_output=True,
				text=True,
				check=False,
			)
			assert result.returncode == 0, result.stderr + result.stdout
			assert json.loads(payload_file.read_text(encoding="utf-8")) == flat
			assert heal.validate_payload(json.loads(payload_file.read_text(encoding="utf-8")))["source_kind"] == "issue"
			assert f"source_repo={CONSUMER_REPO}" in output_file.read_text(encoding="utf-8")


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


def test_fingerprint_ignores_the_promote_cycle_run_name_suffix() -> None:
	# Issue #4368 vs #4350: the same Phase 4b failure, once from a promote
	# cycle (run name `... [cycle:<id>]`) and once from a direct dispatch.
	step = "Phase 4b: Verify editor restored canary (pytest + retry)"
	signature = "##[error]retry review run did not complete within <n> minutes"
	plain = heal.fingerprint("Test & Mark Stable Release", step, signature)
	assert heal.fingerprint("Test & Mark Stable Release [cycle:35939056453]", step, signature) == plain
	assert heal.fingerprint("Test & Mark Stable Release [cycle:35802575310]", step, signature) == plain
	assert heal.fingerprint("Test & Mark Stable Release [CYCLE:1] ", step, signature) == plain
	# Only a trailing numeric cycle tag is volatile; other names stay distinct.
	assert heal.fingerprint("Test & Mark Stable Release [cycle:abc]", step, signature) != plain
	assert heal.fingerprint("Mark Stable Release [cycle:1]", step, signature) != plain


# Shape of a raw Actions job log (jobs/<id>/logs): the `run:` step header echoes
# every script line in ANSI cyan, including `echo "::error::..."` lines that
# never executed; the runner renders an executed `::error::` as `##[error]`.
RAW_STEP_LOG = (
	"2026-09-24T00:57:56.9937438Z ##[group]Run set -euo pipefail\n"
	"2026-09-24T00:57:56.9938000Z \x1b[36;1mset -euo pipefail\x1b[0m\n"
	"2026-09-24T00:57:56.9938100Z \x1b[36;1mif [ -z \"${sha}\" ]; then\x1b[0m\n"
	"2026-09-24T00:57:56.9938200Z \x1b[36;1m  echo \"::error::could not resolve ${branch} head sha before retry dispatch\"\x1b[0m\n"
	"2026-09-24T00:57:56.9938300Z \x1b[36;1mfi\x1b[0m\n"
	"2026-09-24T00:57:56.9938400Z shell: /usr/bin/bash -e {0}\n"
	"2026-09-24T00:57:56.9938500Z env:\n"
	"2026-09-24T00:57:56.9938600Z   EDITOR_RETRY_BUDGET_MINUTES: 25\n"
	"2026-09-24T00:57:56.9938700Z ##[endgroup]\n"
	"2026-09-24T00:58:15.4522834Z   retry run #35940786276: status=pending conclusion=null\n"
	"2026-09-24T01:23:12.7531832Z ##[error]Retry review run did not complete within 25 minutes\n"
	"2026-09-24T01:23:12.7534607Z ##[error]Process completed with exit code 1.\n"
)


def test_filter_log_drops_the_echoed_step_script_but_keeps_step_output() -> None:
	filtered = heal.filter_log(RAW_STEP_LOG)
	assert "could not resolve ${branch} head sha" not in filtered
	assert "set -euo pipefail\n" not in filtered.split("##[group]Run set -euo pipefail", 1)[1]
	# The header line, the env block, and every line the step printed survive.
	assert "##[group]Run set -euo pipefail" in filtered
	assert "EDITOR_RETRY_BUDGET_MINUTES: 25" in filtered
	assert "retry run #35940786276: status=pending" in filtered
	assert "##[error]Retry review run did not complete within 25 minutes" in filtered
	# Cyan output printed by the step itself (outside a Run header) is kept.
	assert "cyan" in heal.filter_log("2026-09-24T00:00:00Z \x1b[36;1mcyan\x1b[0m\n")
	# Text without a Run header (reporter evidence) is unchanged by the drop.
	evidence = "::error::PR diff unavailable\nstderr tail\n"
	assert heal._drop_step_script_lines(evidence) == evidence


def test_error_signature_uses_executed_errors_not_the_echoed_script() -> None:
	signature = heal.error_signature(heal.filter_log(RAW_STEP_LOG))
	assert signature == "##[error]retry review run did not complete within <n> minutes | ##[error]process completed with exit code <n>."
	assert "could not resolve" not in signature
	# A step that fails a different way in the same script gets a different signature.
	other = RAW_STEP_LOG.replace("Retry review run did not complete within 25 minutes", "Retry review run concluded failure")
	assert heal.error_signature(heal.filter_log(other)) != signature


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


SHA_C = "c" * 40
SHA_FIX = "4d2c9dcb5d12d89e462facf2712d27d66162d74d"


def _compare(ahead_by: int, commits: list[tuple[str, str]], files: list[str] | None = None, status: str = "ahead") -> dict:
	return {
		"status": status,
		"ahead_by": ahead_by,
		"behind_by": 0,
		"total_commits": ahead_by,
		"commits": [{"sha": sha, "commit": {"message": message}} for sha, message in commits],
		"files": [{"filename": name, "patch": "@@ -1 +1 @@"} for name in (files or [])],
	}


def test_summarize_branch_progress_validates_and_bounds_the_compare_response() -> None:
	compare = _compare(2, [(SHA_FIX, "AI implementation for issue #4350 (#4351)\n\nbody"), (SHA_C, "Merge pull request #4352")], [".github/workflows/test-and-mark-stable.yml"])
	summary = heal.summarize_branch_progress(compare, failed_sha=SHA_A, branch="main")
	assert summary["available"] is True
	assert summary["ahead_by"] == 2 and summary["tip_sha"] == SHA_C and summary["status"] == "ahead"
	assert summary["commits"][0] == {"sha": SHA_FIX, "subject": "AI implementation for issue #4350 (#4351)"}
	assert summary["files"] == [".github/workflows/test-and-mark-stable.yml"]
	assert summary["commits_truncated"] is False
	identical = heal.summarize_branch_progress(_compare(0, [], status="identical"), failed_sha=SHA_A, branch="main")
	assert identical["available"] and identical["tip_sha"] == SHA_A
	# Untrusted shapes are rejected, never trusted.
	assert heal.summarize_branch_progress(None, failed_sha=SHA_A, branch="main")["reason"] == "invalid_compare_response"
	assert heal.summarize_branch_progress({"status": "ahead", "ahead_by": "2"}, failed_sha=SHA_A, branch="main")["available"] is False
	assert heal.summarize_branch_progress({"status": "weird", "ahead_by": 1}, failed_sha=SHA_A, branch="main")["available"] is False
	assert heal.summarize_branch_progress(compare, failed_sha="nope", branch="main")["reason"] == "invalid_reference"
	assert heal.summarize_branch_progress(compare, failed_sha=SHA_A, branch="bad..branch")["reason"] == "invalid_reference"
	junk = _compare(3, [(SHA_FIX, "ok")])
	junk["commits"] += [{"sha": "zz"}, "text", {"sha": SHA_C.upper()}]
	cleaned = heal.summarize_branch_progress(junk, failed_sha=SHA_A, branch="main")
	assert [c["sha"] for c in cleaned["commits"]] == [SHA_FIX, SHA_C]
	assert heal.summarize_branch_progress(_compare(400, [(SHA_FIX, "x")]), failed_sha=SHA_A, branch="main")["commits_truncated"] is True


def test_render_branch_progress_states_whether_the_failing_code_is_current() -> None:
	ahead = heal.summarize_branch_progress(_compare(1, [(SHA_FIX, "Adopt the active review run")], ["scripts/x.sh"]), failed_sha=SHA_A, branch="main")
	text = heal.render_branch_progress(ahead)
	assert "`main` has 1 commit(s) after the failing SHA" in text
	assert f"- {SHA_FIX[:12]} Adopt the active review run" in text
	assert "- scripts/x.sh" in text and "HEAL_BRANCH_TIP_DIR" in text
	current = heal.render_branch_progress(heal.summarize_branch_progress(_compare(0, [], status="identical"), failed_sha=SHA_A, branch="main"))
	assert "still the current code" in current
	unavailable = heal.render_branch_progress({"available": False, "reason": "compare_fetch_failed"})
	assert "compare_fetch_failed" in unavailable and "`already-fixed`" in unavailable


def test_render_heal_lineage_context_lists_same_fingerprint_and_lineage_issues() -> None:
	fp = "1" * 64
	root = "2" * 64
	body = (
		f"<!-- {heal.MARKER_PREFIX}fp={fp} -->\n<!-- {heal.MARKER_PREFIX}gen=1 -->\n<!-- {heal.MARKER_PREFIX}root={root} -->\n"
		"## Root cause\nPhase 4b redispatches behind queued work.\n\n## Suggested fix\nAdopt the oldest active run.\n\n## Affected files\n- x\n"
	)
	issues = [
		{"number": 4350, "state": "closed", "state_reason": "completed", "closed_at": "2026-09-23T22:32:16Z", "title": "Workflow heal: Test & Mark Stable Release failed on stable", "body": body, "created_at": "2026-09-23T19:28:02Z", "html_url": "u4350"},
		{"number": 4360, "state": "open", "title": "same lineage", "body": f"<!-- {heal.MARKER_PREFIX}fp={'3' * 64} -->\n<!-- {heal.MARKER_PREFIX}root={root} -->", "created_at": "x", "html_url": "u4360"},
		{"number": 4361, "state": "open", "title": "unrelated", "body": f"<!-- {heal.MARKER_PREFIX}fp={'4' * 64} -->", "created_at": "x", "html_url": "u4361"},
		{"number": 4362, "state": "open", "title": "a pull request", "body": body, "pull_request": True},
	]
	text = heal.render_heal_lineage_context(issues, fp=fp, root=root)
	assert text.index("#4360") < text.index("#4350")  # newest first
	assert "[closed (completed), closed 2026-09-23T22:32:16Z]" in text
	assert "Phase 4b redispatches behind queued work." in text and "Adopt the oldest active run." in text
	assert "Affected files" not in text
	assert "#4361" not in text and "#4362" not in text
	assert "no earlier heal issue" in heal.render_heal_lineage_context([], fp=fp, root=root)


def test_check_heal_already_fixed_claim_requires_a_cited_commit_after_the_failing_sha() -> None:
	summary = heal.summarize_branch_progress(_compare(2, [(SHA_FIX, "fix"), (SHA_C, "merge")]), failed_sha=SHA_A, branch="main")
	good = f"## Classification\nalready-fixed\n\n## Fixed by\n- `{SHA_FIX[:12]}` adopts the active run (test-and-mark-stable.yml:2341)\n"
	verdict = heal.check_heal_already_fixed_claim(good, summary)
	assert verdict == {"ok": True, "reason": "fixed_by_commit_verified", "commits": [SHA_FIX]}
	assert heal.check_heal_already_fixed_claim(good.replace("## Fixed by", "## Evidence"), summary)["reason"] == "fixed_by_section_missing"
	assert heal.check_heal_already_fixed_claim(good.replace(SHA_FIX[:12], "deadbeef1234"), summary)["reason"] == "fixed_by_commit_not_after_failing_sha"
	# A SHA cited outside the Fixed by section does not count.
	elsewhere = f"## Evidence\n{SHA_FIX[:12]}\n\n## Fixed by\nsomething vague\n"
	assert heal.check_heal_already_fixed_claim(elsewhere, summary)["ok"] is False
	# Six hex chars are too short to identify a commit.
	assert heal.check_heal_already_fixed_claim(good.replace(SHA_FIX[:12], SHA_FIX[:6]), summary)["ok"] is False
	identical = heal.summarize_branch_progress(_compare(0, [], status="identical"), failed_sha=SHA_A, branch="main")
	assert heal.check_heal_already_fixed_claim(good, identical)["reason"] == "no_commits_after_failing_sha"
	assert heal.check_heal_already_fixed_claim(good, {"available": False, "reason": "x"})["reason"] == "branch_progress_unavailable"
	assert heal.parse_classification("## Classification\n`already-fixed`\n") == "already-fixed"


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
	if "/pulls/" in path and path.split("/")[-1].isdigit():
		if state.get("pull_fetch_fail"):
			fail(state.get("pull_fetch_error", "HTTP 403"))
		out(json.dumps(state.get("pull_request", {})))
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
		# Real gh refuses a log body with ANSI escape sequences unless the
		# caller opts in; job logs always contain them.
		if "--allow-escape-sequences" not in rest:
			fail("the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway")
		job_id = path.split("/")[-2]
		text = state.get("job_logs", {}).get(job_id)
		if text is None:
			fail("HTTP 404")
		out(text)
	if "/compare/" in path:
		if method != "GET":
			fail("HTTP method must be GET for compare")
		compare = state.get("compare")
		if compare is None:
			fail("HTTP 404")
		out(json.dumps(compare))
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
		elif key == "--label":
			record.setdefault("label", args[i + 1])
			record.setdefault("labels", []).append(args[i + 1])
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
		client_payload = dispatch["body"]["client_payload"]
		assert set(client_payload) == {"schema_version", "report"}
		assert len(client_payload) <= heal.DISPATCH_CLIENT_PAYLOAD_MAX_KEYS
		payload = heal.validate_payload(client_payload["report"])
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
		# The rejection is visible: the POST's stderr is logged, bounded.
		assert "detail=HTTP 422" in result.stdout


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


def _run_intake(payload: dict, state: dict, *, diagnosis: str, extra_env: dict[str, str] | None = None, setup_git=None) -> tuple[subprocess.CompletedProcess[str], dict, str]:
	with tempfile.TemporaryDirectory(prefix="heal-intake-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage(tmp, with_codex=True)
		if setup_git is not None:
			setup_git(tmp, work)
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


def _gate_run_payload() -> dict:
	return heal.build_workflow_run_payload(
		repo=SELF_REPO,
		workflow_run={"id": 500, "name": "Test & Mark Stable Release [cycle:35939056453]", "conclusion": "failure", "head_sha": SHA_A, "head_branch": "main", "html_url": f"https://github.com/{SELF_REPO}/actions/runs/500", "display_title": "Test & Mark Stable Release [cycle:35939056453]"},
	)


def _gate_state(**overrides) -> dict:
	gate_log = "2026-09-24T01:23:12.7531832Z ##[error]Retry review run did not complete within 25 minutes\n"
	state = _intake_state(
		jobs={"500": [{"id": 9001, "name": "e2e-smoke-test", "workflow_name": "Test & Mark Stable Release [cycle:35939056453]", "conclusion": "failure", "steps": [{"name": "Phase 4b: Verify editor restored canary (pytest + retry)", "conclusion": "failure"}]}]},
		job_logs={"9001": gate_log},
		compare=_compare(2, [(SHA_FIX, "AI implementation for issue #4350 (#4351)"), (SHA_C, "Merge pull request #4352")], [".github/workflows/test-and-mark-stable.yml"]),
	)
	state.update(overrides)
	return state


DIAG_ALREADY_FIXED = (
	"## Classification\nalready-fixed\n\n## Summary\nPhase 4b redispatched behind queued review work; #4351 adopts the active run.\n\n"
	f"## Fixed by\n- {SHA_FIX[:12]} adopts the oldest eligible active review run (.github/workflows/test-and-mark-stable.yml:2341)\n"
)


def test_intake_gives_the_model_branch_progress_and_earlier_heals() -> None:
	signature = heal.error_signature(heal.filter_log(_gate_state()["job_logs"]["9001"]))
	fp = heal.fingerprint("Test & Mark Stable Release", "Phase 4b: Verify editor restored canary (pytest + retry)", signature)
	prior = _heal_issue(4350, state="closed", fp=fp)
	prior.update({"title": "Workflow heal: Test & Mark Stable Release failed on stable", "state_reason": "completed", "closed_at": "2026-09-23T22:32:16Z"})
	prior["body"] += "\n## Suggested fix\nAdopt the oldest active review run.\n"
	result, state_after, prompt = _run_intake(_gate_run_payload(), _gate_state(heal_issues=[prior]), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL branch_progress available=true branch=main" in result.stdout and "ahead_by=2" in result.stdout
	compare_calls = [call for call in state_after["calls"] if any("/compare/" in part for part in call)]
	assert len(compare_calls) == 1
	assert f"repos/{SELF_REPO}/compare/{SHA_A}...main" in compare_calls[0]
	assert "=== BRANCH PROGRESS SINCE THE FAILING SHA ===" in prompt
	assert f"- {SHA_FIX[:12]} AI implementation for issue #4350 (#4351)" in prompt
	assert "- .github/workflows/test-and-mark-stable.yml" in prompt
	assert "=== EARLIER HEAL ISSUES FOR THIS FAILURE (UNTRUSTED) ===" in prompt
	assert "#4350 [closed (completed)" in prompt and "Adopt the oldest active review run." in prompt
	assert "HEAL_BRANCH_TIP_DIR: unavailable" in prompt  # source checkout is off in tests
	# The cycle-tagged run joined #4350's lineage instead of starting its own.
	created = state_after["issues_created"][0]
	markers = heal.parse_heal_markers(created["body"])
	assert markers["gen"] == "2" and markers["fp"] == fp


def test_intake_already_fixed_with_a_verified_commit_opens_no_issue() -> None:
	result, state_after, _ = _run_intake(_gate_run_payload(), _gate_state(), diagnosis=DIAG_ALREADY_FIXED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert f"WORKFLOW_HEAL already_fixed_verified commits={SHA_FIX[:12]} branch=main" in result.stdout
	assert "WORKFLOW_HEAL no_issue classification=already-fixed" in result.stdout
	assert "issues_created" not in state_after


def test_intake_already_fixed_consumer_report_comments_with_the_sync_hint() -> None:
	compare = _compare(1, [(SHA_FIX, "AI implementation for issue #4350 (#4351)")])
	result, state_after, prompt = _run_intake(_consumer_payload(), _intake_state(compare=compare), diagnosis=DIAG_ALREADY_FIXED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "issues_created" not in state_after
	assert any(f"repos/{SELF_REPO}/compare/{SHA_A}...stable" in part for call in state_after["calls"] for part in call)
	comment = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{CONSUMER_REPO}/issues/42/comments"][0]
	assert "`already-fixed`" in comment["body"] and SHA_FIX[:12] in comment["body"]
	assert "next wrapper sync to `stable`" in comment["body"]


def test_intake_downgrades_an_unverifiable_already_fixed_claim() -> None:
	cases = [
		(_gate_state(), DIAG_ALREADY_FIXED.replace(SHA_FIX[:12], "deadbeef1234"), "fixed_by_commit_not_after_failing_sha"),
		(_gate_state(), DIAG_ALREADY_FIXED.replace("## Fixed by", "## Evidence"), "fixed_by_section_missing"),
		(_gate_state(compare=None), DIAG_ALREADY_FIXED, "branch_progress_unavailable"),
		(_gate_state(compare=_compare(0, [], status="identical")), DIAG_ALREADY_FIXED, "no_commits_after_failing_sha"),
	]
	for state, diagnosis, reason in cases:
		result, state_after, prompt = _run_intake(_gate_run_payload(), state, diagnosis=diagnosis)
		assert result.returncode == 0, result.stderr + result.stdout
		assert f"WORKFLOW_HEAL warn already_fixed_unverified reason={reason}" in result.stdout, (reason, result.stdout)
		created = state_after["issues_created"][0]
		assert heal.parse_heal_markers(created["body"])["classification"] == "inconclusive"
		assert "**Heal intake note:**" in created["body"] and f"`{reason}`" in created["body"]
		if state.get("compare") is None:
			assert "branch progress unavailable: compare_fetch_failed" in prompt


def test_intake_without_linked_runs_still_files_from_label_context() -> None:
	payload = _consumer_payload(run_refs=[])
	result, state, prompt = _run_intake(payload, _intake_state(), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "no failed run could be linked" in prompt
	created = state["issues_created"][0]
	assert created["title"] == f"Workflow heal: ai:needs-human on {CONSUMER_REPO}#42"


# ---------------------------------------------------------------------------
# Autofix-failure reports (review_autofix.yml failure path)
# ---------------------------------------------------------------------------

AUTOFIX_NOOP_COMMENT = "**AI review/autofix produced no output — will retry**\n\nThe editor stage completed without a structured summary."
AUTOFIX_FAILED_COMMENT = "**AI review/autofix failed — needs human intervention**"
AUTOFIX_POST_EDITOR_FAILED_COMMENT = "**AI review/autofix encountered a post-editor failure — needs human intervention**"
AUTOFIX_SUMMARY_COMMENT = "AI autofix editor summary\n\nChanges made:\n- none"
RUN_SUMMARY_LINE = (
	'REVIEW_AUTOFIX_RUN_SUMMARY_V1 {"budget_elapsed_secs":129,"completed_phases":["editor","finalize"],'
	'"edits_pushed":false,"finalize_reason":"editor_empty_noop","skipped_phases":["reviewers"],'
	'"slot_results":{"editor":{"attempt_count":1,"failure_class":"empty_noop","status":"failed"}}}'
)


def _pr(number: int = 4174, *, title: str = "AI implementation for issue #4173", labels: list[str] | None = None) -> dict:
	return {
		"number": number,
		"title": title,
		"body": "Automated implementation. Refs #4139",
		"html_url": f"https://github.com/{CONSUMER_REPO}/pull/{number}",
		"labels": [{"name": name} for name in (labels or [])],
		"head": {"ref": "ai/issue-4173", "sha": SHA_B, "repo": {"full_name": CONSUMER_REPO}},
		"base": {"ref": "orchestrator/project-4139"},
	}


def test_count_autofix_failure_streak_reads_trailing_failure_comments() -> None:
	comments = [
		{"body": AUTOFIX_FAILED_COMMENT},
		{"body": AUTOFIX_SUMMARY_COMMENT},
		{"body": "unrelated reviewer note"},
		{"body": AUTOFIX_SUMMARY_COMMENT},
		{"body": AUTOFIX_POST_EDITOR_FAILED_COMMENT},
		{"body": AUTOFIX_SUMMARY_COMMENT},
		{"body": AUTOFIX_POST_EDITOR_FAILED_COMMENT},
	]
	assert heal.count_autofix_failure_streak(comments) == 2
	assert heal.count_autofix_failure_streak([]) == 0
	assert heal.count_autofix_failure_streak([{"body": AUTOFIX_SUMMARY_COMMENT}]) == 0
	assert heal.count_autofix_failure_streak([{"body": AUTOFIX_SUMMARY_COMMENT}, {"body": AUTOFIX_NOOP_COMMENT}]) == 1
	assert heal.count_autofix_failure_streak(
		[
			{"body": AUTOFIX_SUMMARY_COMMENT},
			{"body": "⚠️ **Editor changes lost** — no commit was produced."},
			{"body": AUTOFIX_SUMMARY_COMMENT},
			{"body": "⚠️ **Editor no-op suspicious** — disposition could not be verified."},
		]
	) == 2


def _autofix_payload(**overrides) -> dict:
	payload = heal.build_autofix_failure_payload(
		repo=CONSUMER_REPO,
		pr=_pr(),
		comments=[{"body": AUTOFIX_NOOP_COMMENT}],
		workflow_name="AI Review",
		failure_reason="editor_empty_noop",
		failure_evidence="failure_reason=editor_empty_noop\nfinalize_reason=editor_empty_noop\n" + RUN_SUMMARY_LINE + "\n::error::Process completed with exit code 1.",
		failure_streak=2,
		run_id="500",
		run_url=f"https://github.com/{CONSUMER_REPO}/actions/runs/500",
		wrapper_sha=SHA_A,
		reporter_run_url=f"https://github.com/{CONSUMER_REPO}/actions/runs/500",
	)
	payload.update(overrides)
	return payload


def test_autofix_payload_validates_and_fingerprints_by_reason() -> None:
	payload = heal.validate_payload(_autofix_payload())
	assert payload["source_kind"] == "autofix_failure"
	assert payload["issue_number"] == 4174 and payload["label"] is None
	assert payload["failure_reason"] == "editor_empty_noop" and payload["failure_streak"] == 2
	assert payload["run_refs"] == [{"repo": CONSUMER_REPO, "run_id": "500", "url": f"https://github.com/{CONSUMER_REPO}/actions/runs/500"}]
	assert payload["head_branch"] == "ai/issue-4173" and payload["head_sha"] == SHA_B
	assert payload["wrapper_sha"] == SHA_A
	assert "finalize_reason=editor_empty_noop" in payload["failure_evidence"]
	# Non-autofix kinds never carry the autofix fields.
	other = heal.validate_payload(_consumer_payload(failure_reason="x", failure_streak=9))
	assert other["failure_reason"] is None and other["failure_streak"] is None
	for mutate in (lambda p: p.update(failure_reason="Bad Reason!"), lambda p: p.update(failure_reason=None), lambda p: p.update(issue_number=None)):
		bad = json.loads(json.dumps(_autofix_payload()))
		mutate(bad)
		try:
			heal.validate_payload(bad)
		except ValueError:
			pass
		else:
			raise AssertionError(f"payload was accepted: {bad}")
	# The evidence-based signature is stable across run ids and SHAs.
	sig_a = heal.error_signature(_autofix_payload()["failure_evidence"])
	sig_b = heal.error_signature(_autofix_payload(run_refs=[])["failure_evidence"].replace("129", "777"))
	assert sig_a == sig_b
	assert heal.fingerprint("AI Review", "autofix:editor_empty_noop", sig_a) != heal.fingerprint("AI Review", "autofix:editor_changes_lost", sig_a)


def test_autofix_payload_prioritizes_recent_diagnostics_and_compacts_state() -> None:
	state_comment = (
		f"<!-- ORCHESTRATOR_STATE_V2 part=1/1 manifest={'a' * 64} -->\n"
		+ ("state-data" * 1000)
		+ "\nORCHESTRATOR_STATE_V2 -->"
	)
	run_url = f"https://github.com/{CONSUMER_REPO}/actions/runs/500"
	payload = heal.build_autofix_failure_payload(
		repo=CONSUMER_REPO,
		pr=_pr(),
		comments=[
			{"body": state_comment},
			{"body": f"Harness diagnosis: template renderer failed.\nRun: {run_url}"},
		],
		workflow_name="AI Review",
		failure_reason="editor_empty_noop",
		failure_evidence="failure_reason=editor_empty_noop",
		failure_streak=2,
		run_id="500",
		run_url=run_url,
		wrapper_sha=SHA_A,
		reporter_run_url=run_url,
	)

	assert payload["comments_excerpt"].startswith("Harness diagnosis: template renderer failed.")
	assert "[ORCHESTRATOR_STATE_V2 state part 1/1 omitted]" in payload["comments_excerpt"]
	assert "state-datastate-data" not in payload["comments_excerpt"]
	assert len(payload["comments_excerpt"]) <= heal.COMMENTS_EXCERPT_LIMIT
	assert payload["run_refs"] == [{"repo": CONSUMER_REPO, "run_id": "500", "url": run_url}]


def test_compose_autofix_issue_title_and_body() -> None:
	payload = heal.validate_payload(_autofix_payload())
	title = heal.compose_issue_title(payload, workflow_name=None)
	assert title == f"Workflow heal: AI Review failed 2x for {CONSUMER_REPO}#4174 (editor_empty_noop)"
	body = heal.compose_issue_body(payload=payload, diagnosis="## Classification\nworkflow-defect\n\n## Summary\nDiff fetch fails.", fp=FP_HEX, gen=1, root=FP_HEX, classification="workflow-defect", target_branch="stable", max_depth=3, intake_run_url="u", run_summaries=[])
	assert "failed repeatedly on one pull request" in body
	assert "**Source pull request:**" in body and "**Failure reason:** `editor_empty_noop`" in body
	assert "**Consecutive failed review runs on this PR:** 2" in body
	assert "Failure evidence from the reporting run (UNTRUSTED, verbatim)" in body and "finalize_reason=editor_empty_noop" in body
	assert "Escalation label" not in body
	match = TARGET_BRANCH_RE.search(body)
	assert match and (match.group(1) or match.group(2)) == "stable"
	assert not AUTO_CLOSE_RE.search(body)


def test_intake_autofix_failure_opens_upstream_issue_with_reason_fingerprint() -> None:
	state = _intake_state(jobs={"500": [{"id": 9001, "name": "review / codex-agent", "workflow_name": "AI Review", "conclusion": "failure", "steps": [{"name": "Run editor", "conclusion": "failure"}]}]}, job_logs={"9001": "2026-09-21T01:49:26.000Z ##[error]Process completed with exit code 1.\n"})
	result, state_after, prompt = _run_intake(_autofix_payload(), state, diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "step=autofix:editor_empty_noop" in result.stdout
	created = state_after["issues_created"][0]
	assert created["repo"] == SELF_REPO
	assert created["title"] == f"Workflow heal: AI Review failed 2x for {CONSUMER_REPO}#4174 (editor_empty_noop)"
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "stable"
	assert "Failed review/autofix run on pull request #4174" in prompt
	assert "Failure reason: editor_empty_noop" in prompt and "Consecutive failed review runs on this PR: 2" in prompt
	assert "Failure evidence from the reporting run (UNTRUSTED)" in prompt and RUN_SUMMARY_LINE in prompt
	# The escalated PR gets the outcome comment.
	assert any(c["path"] == f"repos/{CONSUMER_REPO}/issues/4174/comments" for c in state_after["comments_posted"])
	# A consumer PR ran the released workflows, so its fix stays a stable hotfix.
	assert "target_branch_source=default" in result.stdout


def _self_repo_autofix_payload() -> dict:
	return _autofix_payload(
		source_repo=SELF_REPO,
		issue_url=f"https://github.com/{SELF_REPO}/pull/4174",
		run_refs=[{"repo": SELF_REPO, "run_id": "500", "url": f"https://github.com/{SELF_REPO}/actions/runs/500"}],
	)


def _self_repo_autofix_state(branches: list[str]) -> dict:
	return _intake_state(
		jobs={"500": [{"id": 9001, "name": "review / codex-agent", "workflow_name": "AI Review", "conclusion": "failure", "steps": [{"name": "Run editor", "conclusion": "failure"}]}]},
		job_logs={"9001": "2026-09-23T13:51:28.000Z ##[error]Process completed with exit code 226.\n"},
		branches=branches,
	)


def test_intake_self_repo_autofix_failure_targets_source_pr_branch() -> None:
	# A review/autofix run on a PR in coding-workflows itself executes the PR's
	# own scripts (SCRIPT_REF = github.sha), so a fix aimed at stable can
	# neither unblock the PR nor merge without dragging the PR's unreleased
	# changes into stable (issue #4329 / PR #4332 against PR #4323).
	result, state_after, _prompt = _run_intake(_self_repo_autofix_payload(), _self_repo_autofix_state(["stable", "main", "ai/issue-4173"]), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	created = state_after["issues_created"][0]
	assert created["repo"] == SELF_REPO
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "ai/issue-4173"
	assert "target_branch=ai/issue-4173" in result.stdout and "target_branch_source=source_pr_head" in result.stdout
	outcome = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{SELF_REPO}/issues/4174/comments"]
	assert outcome and "on this pull request's own branch `ai/issue-4173`" in outcome[0]["body"]
	assert "hotfix on `stable`" not in outcome[0]["body"]
	# One branch lookup: the PR branch exists, so the default is never probed.
	branch_calls = [call for call in state_after["calls"] if any("/branches/" in part for part in call)]
	assert len(branch_calls) == 1


def test_intake_self_repo_autofix_failure_falls_back_to_stable_when_pr_branch_is_gone() -> None:
	result, state_after, _prompt = _run_intake(_self_repo_autofix_payload(), _self_repo_autofix_state(["stable", "main"]), diagnosis=DIAG_WORKFLOW_DEFECT)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "warn source_pr_branch_missing branch=ai/issue-4173; falling back to stable" in result.stdout
	created = state_after["issues_created"][0]
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "stable"
	assert "target_branch=stable" in result.stdout and "target_branch_source=default" in result.stdout
	outcome = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{SELF_REPO}/issues/4174/comments"]
	assert outcome and "as a hotfix on `stable`" in outcome[0]["body"]


def _stage_autofix_report(tmp: Path, *, comments: list[dict], flags: dict[str, str], summary_line: str | None = RUN_SUMMARY_LINE) -> tuple[Path, Path, dict[str, str]]:
	work, state_file, env = _stage(tmp, with_codex=False)
	shutil.copy(AUTOFIX_REPORT_SCRIPT, work / "scripts" / AUTOFIX_REPORT_SCRIPT.name)
	runtime = tmp / "runtime"
	runtime.mkdir(exist_ok=True)
	(runtime / "pr_payload.json").write_text(json.dumps(_pr()), encoding="utf-8")
	(runtime / "pr_issue_comments.json").write_text(json.dumps(comments), encoding="utf-8")
	(runtime / "codex_editor_log.txt").write_text("editor started\n::error::PR diff unavailable\n", encoding="utf-8")
	if summary_line is not None:
		(runtime / "review_autofix_run_summary_line.txt").write_text(summary_line + "\n", encoding="utf-8")
	state_file.write_text(json.dumps({}), encoding="utf-8")
	env.update(
		{
			"GITHUB_REPOSITORY": CONSUMER_REPO,
			"PR_NUMBER": "4174",
			"GITHUB_RUN_ID": "500",
			"RUNTIME_DIR": str(runtime),
			"PR_PAYLOAD_FILE": str(runtime / "pr_payload.json"),
			"PR_ISSUE_COMMENTS_FILE": str(runtime / "pr_issue_comments.json"),
			"REPORT_WORKFLOW_NAME": "AI Review",
			"REPORT_RUN_URL": f"https://github.com/{CONSUMER_REPO}/actions/runs/500",
			"REPORT_WRAPPER_SHA": SHA_A.upper(),
			"WORKFLOW_HEAL_PY": str(work / "scripts" / "workflow_failure_heal.py"),
		}
	)
	env.update(flags)
	return work, state_file, env


def test_autofix_report_dispatches_past_streak_threshold() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-autofix-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[{"body": AUTOFIX_SUMMARY_COMMENT}, {"body": AUTOFIX_NOOP_COMMENT}], flags={"AUTOFIX_EDITOR_EMPTY_NOOP": "true", "EDITOR_NOOP_SUSPICIOUS": "true"})
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "WORKFLOW_HEAL_AUTOFIX_REPORT dispatched pr=4174 failure=editor_empty_noop streak=2 workflow=AI Review" in result.stdout
		state = _state(state_file)
		assert len(state["dispatches"]) == 1
		dispatch = state["dispatches"][0]
		assert dispatch["path"] == f"repos/{SELF_REPO}/dispatches"
		assert dispatch["body"]["event_type"] == "workflow-failure-heal"
		client_payload = dispatch["body"]["client_payload"]
		assert set(client_payload) == {"schema_version", "report"}
		assert len(client_payload) <= heal.DISPATCH_CLIENT_PAYLOAD_MAX_KEYS
		payload = heal.validate_payload(client_payload["report"])
		assert payload["source_kind"] == "autofix_failure" and payload["failure_reason"] == "editor_empty_noop"
		assert payload["failure_streak"] == 2 and payload["issue_number"] == 4174
		assert payload["wrapper_sha"] == SHA_A and payload["workflow_name"] == "AI Review"
		assert RUN_SUMMARY_LINE in payload["failure_evidence"] and "PR diff unavailable" in payload["failure_evidence"]
		assert payload["run_refs"][0]["run_id"] == "500"
		# No GitHub read was needed: the PR payload and comments came from the run.
		assert all(call[:1] != ["api"] or "/dispatches" in " ".join(call) for call in state["calls"])


def test_autofix_report_counts_interleaved_post_editor_failures() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-autofix-interleaved-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(
			tmp,
			comments=[
				{"body": AUTOFIX_SUMMARY_COMMENT},
				{"body": AUTOFIX_POST_EDITOR_FAILED_COMMENT},
				{"body": AUTOFIX_SUMMARY_COMMENT},
				{"body": AUTOFIX_POST_EDITOR_FAILED_COMMENT},
			],
			flags={"EDITOR_CHANGES_LOST": "true", "WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK": "3"},
		)
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "dispatched pr=4174 failure=editor_changes_lost streak=3" in result.stdout
		assert len(_state(state_file)["dispatches"]) == 1


def test_autofix_report_skip_paths() -> None:
	cases = [
		("below_streak", [], {"AUTOFIX_EDITOR_EMPTY_NOOP": "true"}, "skip reason=below_streak pr=4174 reason=editor_empty_noop streak=1 threshold=2"),
		("disabled", [{"body": AUTOFIX_NOOP_COMMENT}], {"WORKFLOW_HEAL_ENABLED": "false"}, "skip reason=disabled"),
		("resolver", [{"body": AUTOFIX_NOOP_COMMENT}], {"RESOLVER_ESCALATED": "true"}, "skip reason=resolver_escalated"),
		("dispatch_denied", [{"body": AUTOFIX_NOOP_COMMENT}], {"MOCK_DISPATCH_FAIL": "1"}, "skip reason=dispatch_denied pr=4174 failure=editor_empty_noop streak=2 upstream=shubhodeep1/coding-workflows detail=HTTP 422"),
	]
	for name, comments, flags, expected in cases:
		with tempfile.TemporaryDirectory(prefix=f"heal-autofix-{name}-") as tmp_name:
			tmp = Path(tmp_name)
			work, state_file, env = _stage_autofix_report(tmp, comments=comments, flags=flags)
			if flags.get("MOCK_DISPATCH_FAIL"):
				state_file.write_text(json.dumps({"dispatch_fail": True}), encoding="utf-8")
			result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
			assert result.returncode == 0, (name, result.stderr, result.stdout)
			assert expected in result.stdout, (name, result.stdout)
			if name != "dispatch_denied":
				assert "dispatches" not in _state(state_file), name
	# Threshold 1 reports the first failure; the finalize reason is used when no editor flag fired.
	with tempfile.TemporaryDirectory(prefix="heal-autofix-threshold1-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[], flags={"WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK": "1"}, summary_line=RUN_SUMMARY_LINE.replace("editor_empty_noop", "reviewers_unavailable"))
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "dispatched pr=4174 failure=reviewers_unavailable streak=1" in result.stdout
	# Smoke-test PRs are never reported.
	with tempfile.TemporaryDirectory(prefix="heal-autofix-smoke-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[{"body": AUTOFIX_NOOP_COMMENT}], flags={"AUTOFIX_EDITOR_EMPTY_NOOP": "true"})
		(tmp / "runtime" / "pr_payload.json").write_text(json.dumps(_pr(labels=["e2e-smoke-test"])), encoding="utf-8")
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0 and "skip reason=smoke_test_fixture" in result.stdout
		assert "dispatches" not in _state(state_file)


def test_autofix_report_pr_fetch_failure_logs_bounded_detail() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-autofix-pr-fetch-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[{"body": AUTOFIX_NOOP_COMMENT}], flags={"AUTOFIX_EDITOR_EMPTY_NOOP": "true"})
		Path(env["PR_PAYLOAD_FILE"]).write_text("{}", encoding="utf-8")
		state_file.write_text(json.dumps({"pull_fetch_fail": True, "pull_fetch_error": "HTTP 403 missing pulls:read"}), encoding="utf-8")
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "skip reason=pr_fetch_failed pr=4174 detail=HTTP 403 missing pulls:read" in result.stdout
		assert "dispatches" not in _state(state_file)


def test_review_autofix_workflow_wires_the_heal_reporter() -> None:
	workflow = _yaml(REVIEW_AUTOFIX_WORKFLOW)
	steps = workflow["jobs"]["codex-agent"]["steps"]
	names = [step.get("name") for step in steps]
	report_index = names.index("Report autofix failure to workflow failure heal")
	assert names.index("Append review pipeline iteration summary") < report_index < names.index("Cleanup temporary artifacts")
	step = steps[report_index]
	assert step["continue-on-error"] is True
	assert step["if"].startswith(
		"(failure() || env.EDITOR_CHANGES_LOST == 'true' || env.EDITOR_NOOP_SUSPICIOUS == 'true') &&"
	)
	assert "WORKFLOW_HEAL_ENABLED" in step["if"] and "RESOLVER_ESCALATED" in step["if"]
	assert "${{" not in step["run"]
	assert step["env"]["WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK"] == "${{ vars.WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK || '2' }}"
	assert step["env"]["REPORT_WORKFLOW_NAME"] == "${{ github.workflow }}"
	assert "workflow_failure_heal_autofix_report.sh" in step["run"]
	# The summary step body lives in scripts/review_autofix_step_iteration_summary.sh.
	expanded_steps = yaml.safe_load(expanded_review_autofix_text())["jobs"]["codex-agent"]["steps"]
	summary_step = expanded_steps[names.index("Append review pipeline iteration summary")]
	assert "review_autofix_run_summary_line.txt" in summary_step["run"]
	staging = STAGE_SUPPORT_SCRIPT.read_text(encoding="utf-8")
	optional = re.search(r'^OPTIONAL_BOOTSTRAP_SCRIPTS="([^"]*)"', staging, re.MULTILINE)
	assert optional and {"workflow_failure_heal.py", "workflow_failure_heal_autofix_report.sh"} <= set(optional.group(1).split())


def test_review_autofix_workflow_backfills_the_heal_reporter_from_main_snapshot() -> None:
	# Regression: stage_workflow_support.sh is sourced from the PR branch
	# (SCRIPT_REF). A PR branch forked before #4208 added the reporter pair to
	# OPTIONAL_BOOTSTRAP_SCRIPTS never stages them, and the report step skipped
	# with reason=reporter_missing (run 35685250882 on PR #4259). The staging
	# step must backfill the pair from the main snapshot like the preflight set.
	workflow = _yaml(REVIEW_AUTOFIX_WORKFLOW)
	assert workflow["env"]["REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS"].split() == [
		"workflow_failure_heal.py",
		"workflow_failure_heal_autofix_report.sh",
	]
	steps = workflow["jobs"]["codex-agent"]["steps"]
	names = [step.get("name") for step in steps]
	stage_run = steps[names.index("Stage workflow support files")]["run"]
	assert 'for f in ${REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS}; do' in stage_run
	assert "reason=reporter_missing" in stage_run
	# Same source order as the preflight backfill: branch copy first, main
	# snapshot second, warning (never a failure) when both are absent.
	loop = stage_run.split('for f in ${REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS}; do', 1)[1].split("done", 1)[0]
	assert 'backfill_src=".codex-workflow-src/scripts/${f}"' in loop
	assert '[ -f ".codex-workflow-src-main/scripts/${f}" ]' in loop
	assert 'install -m 0755 "${backfill_src}" "${SUPPORT_SCRIPTS_DIR}/${f}"' in loop
	assert "exit 1" not in loop


def test_review_autofix_heal_reporter_backfill_loop_stages_pair_from_main_snapshot() -> None:
	# Execute the backfill loop against a stale branch checkout that lacks the
	# pair and a main snapshot that carries it: both land in
	# SUPPORT_SCRIPTS_DIR, executable, and the loop leaves an already-staged
	# branch copy alone.
	workflow = _yaml(REVIEW_AUTOFIX_WORKFLOW)
	steps = workflow["jobs"]["codex-agent"]["steps"]
	names = [step.get("name") for step in steps]
	stage_run = steps[names.index("Stage workflow support files")]["run"]
	loop = 'for f in ${REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS}; do' + stage_run.split(
		'for f in ${REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS}; do', 1
	)[1].split("done\n", 1)[0] + "done\n"
	with tempfile.TemporaryDirectory() as tmp:
		work = Path(tmp)
		(work / ".codex-workflow-src" / "scripts").mkdir(parents=True)
		(work / ".codex-workflow-src-main" / "scripts").mkdir(parents=True)
		support = work / "support" / "scripts"
		support.mkdir(parents=True)
		(work / ".codex-workflow-src-main" / "scripts" / "workflow_failure_heal.py").write_text("main-heal\n", encoding="utf-8")
		(work / ".codex-workflow-src-main" / "scripts" / "workflow_failure_heal_autofix_report.sh").write_text("main-reporter\n", encoding="utf-8")
		# Branch copy of an unrelated already-staged file must not be touched.
		(support / "workflow_failure_heal.py").write_text("branch-heal\n", encoding="utf-8")
		reporter_backfill_env = {key: value for key, value in os.environ.items() if key not in {"BASH_ENV", "ENV", "WORKSPACE_PATH"}}
		result = subprocess.run(
			["bash", "-euo", "pipefail", "-c", loop],
			cwd=work,
			env={
				**reporter_backfill_env,
				"REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS": workflow["env"]["REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS"],
				"SUPPORT_SCRIPTS_DIR": str(support),
				"SCRIPT_REF": SHA_A,
			},
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr + result.stdout
		assert (support / "workflow_failure_heal.py").read_text(encoding="utf-8") == "branch-heal\n"
		reporter = support / "workflow_failure_heal_autofix_report.sh"
		assert reporter.read_text(encoding="utf-8") == "main-reporter\n"
		assert os.access(reporter, os.X_OK)
		assert (
			"::notice::Backfilled workflow_failure_heal_autofix_report.sh into the runtime support bundle from "
			".codex-workflow-src-main/scripts/workflow_failure_heal_autofix_report.sh"
		) in result.stdout
		# Both absent: warning only, exit 0, nothing staged.
		reporter.unlink()
		(work / ".codex-workflow-src-main" / "scripts" / "workflow_failure_heal_autofix_report.sh").unlink()
		result = subprocess.run(
			["bash", "-euo", "pipefail", "-c", loop],
			cwd=work,
			env={
				**reporter_backfill_env,
				"REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS": workflow["env"]["REVIEW_HEAL_REPORTER_SUPPORT_SCRIPTS"],
				"SUPPORT_SCRIPTS_DIR": str(support),
				"SCRIPT_REF": SHA_A,
			},
			capture_output=True,
			text=True,
		)
		assert result.returncode == 0, result.stderr + result.stdout
		assert not reporter.exists()
		assert "::warning::Workflow-failure-heal reporter script workflow_failure_heal_autofix_report.sh was not staged" in result.stdout
		assert "reason=reporter_missing" in result.stdout


# ---------------------------------------------------------------------------
# Identical-failure fingerprint cap (review_autofix.yml gate + helpers)
# ---------------------------------------------------------------------------

CAP_AUTHOR = "workflow-pat-user"
EDITOR_STDERR_RUN_1 = (
	"2026-09-22T08:00:01.123Z scripts/review_apply_fixes.sh: line 168: OPENROUTER_API_KEY: OPENROUTER_API_KEY is required\n"
	"::error::editor exited in run 35713627310 at /tmp/codex-pr-35713627310-1-4242/editor.log on f9d7196c0ffee0000000000000000000000000ab\n"
)
EDITOR_STDERR_RUN_2 = (
	"2026-09-22T09:10:44.901Z scripts/review_apply_fixes.sh: line 168: OPENROUTER_API_KEY: OPENROUTER_API_KEY is required\n"
	"::error::editor exited in run 35719487197 at /tmp/codex-pr-35719487197-2-17/editor.log on 0123456789abcdef0123456789abcdef01234567\n"
)


def _cap_fp(reason: str = "editor_empty_noop", evidence: str = EDITOR_STDERR_RUN_1) -> str:
	return heal.autofix_failure_fingerprint(failure_reason=reason, evidence_text=evidence)["fp"]


def _failure_marker_comment(text: str, *, head: str = SHA_A, fp: str | None = None, run: str = "1", author: str = CAP_AUTHOR, reason: str = "editor_empty_noop") -> dict:
	marker = heal.render_failure_marker(head, reason, fp or _cap_fp(reason), False, run)
	assert marker, "marker must render for a valid head and fingerprint"
	return {"id": int(run) if run.isdigit() else 0, "author_login": author, "body": f"{text}\n\n{marker}"}


def test_autofix_failure_fingerprint_is_stable_across_volatile_tokens() -> None:
	first = heal.autofix_failure_fingerprint(failure_reason="editor_empty_noop", evidence_text=EDITOR_STDERR_RUN_1)
	second = heal.autofix_failure_fingerprint(failure_reason="editor_empty_noop", evidence_text=EDITOR_STDERR_RUN_2)
	assert first == second and first["degraded"] is False
	assert re.fullmatch(r"[0-9a-f]{64}", first["fp"])
	other_reason = heal.autofix_failure_fingerprint(failure_reason="workflow_failure", evidence_text=EDITOR_STDERR_RUN_1)
	assert other_reason["fp"] != first["fp"]
	other_message = heal.autofix_failure_fingerprint(failure_reason="editor_empty_noop", evidence_text="::error::PR diff unavailable\n")
	assert other_message["fp"] != first["fp"]
	# No evidence: degraded, and the fingerprint collapses to head + reason.
	degraded = heal.autofix_failure_fingerprint(failure_reason="editor_empty_noop", evidence_text="  \n")
	assert degraded["degraded"] is True
	assert degraded["fp"] == heal.autofix_failure_fingerprint(failure_reason="editor_empty_noop", evidence_text="")["fp"]


def test_render_failure_marker_is_log_safe_and_rejects_bad_input() -> None:
	fp = _cap_fp()
	marker = heal.render_failure_marker(SHA_A.upper(), "editor_empty_noop", fp, False, "35713627310")
	assert marker == f"<!-- review-autofix-failure:v1 head={SHA_A} reason=editor_empty_noop fp={fp} degraded=0 run=35713627310 -->"
	assert heal.render_failure_marker(SHA_A, "bad reason; rm -rf", fp, True, "x") == f"<!-- review-autofix-failure:v1 head={SHA_A} reason=badreasonrm-rf fp={fp} degraded=1 -->"
	assert heal.render_failure_marker("abc", "editor_empty_noop", fp, False) == ""
	assert heal.render_failure_marker(SHA_A, "editor_empty_noop", "not-a-fingerprint", False) == ""
	parsed = heal.parse_failure_markers([{"id": 9, "author_login": CAP_AUTHOR, "body": "x\n" + marker}], head_sha=SHA_A, author_login=CAP_AUTHOR)
	assert parsed == [{"fp": fp, "reason": "editor_empty_noop", "degraded": False, "run": "35713627310", "comment_id": "9"}]
	assert heal.parse_failure_markers([{"author_login": "someone-else", "body": marker}], head_sha=SHA_A, author_login=CAP_AUTHOR) == []
	assert heal.parse_failure_markers([{"user": {"login": CAP_AUTHOR}, "body": marker}], head_sha=SHA_B, author_login=CAP_AUTHOR) == []


def test_count_identical_failures_rules() -> None:
	fp = _cap_fp()
	three = [_failure_marker_comment(AUTOFIX_NOOP_COMMENT, run=str(run)) for run in (1, 2, 3)]
	result = heal.count_identical_failures(three, head_sha=SHA_A, author_login=CAP_AUTHOR)
	assert result == {"count": 3, "fp": fp, "reason": "editor_empty_noop", "cap_applied": False}
	# Unrelated comments, markers for another head and forged markers are skipped.
	mixed = [
		three[0],
		{"author_login": "reviewer-bot", "body": "unrelated reviewer note"},
		_failure_marker_comment(AUTOFIX_NOOP_COMMENT, head=SHA_B, run="7"),
		three[1],
		_failure_marker_comment(AUTOFIX_NOOP_COMMENT, run="8", author="attacker", fp="0" * 64),
		three[2],
	]
	assert heal.count_identical_failures(mixed, head_sha=SHA_A, author_login=CAP_AUTHOR)["count"] == 3
	# An editor summary (a productive-looking run) ends the scan.
	assert heal.count_identical_failures([three[0], three[1], {"author_login": CAP_AUTHOR, "body": AUTOFIX_SUMMARY_COMMENT}, three[2]], head_sha=SHA_A, author_login=CAP_AUTHOR)["count"] == 1
	# ...unless it is the summary a post-editor failure of the same run posted first.
	paired = []
	for run in ("4", "5", "6"):
		paired.append({"author_login": CAP_AUTHOR, "body": AUTOFIX_SUMMARY_COMMENT})
		paired.append(_failure_marker_comment(AUTOFIX_POST_EDITOR_FAILED_COMMENT, run=run, reason="workflow_failure"))
	assert heal.count_identical_failures(paired, head_sha=SHA_A, author_login=CAP_AUTHOR)["count"] == 3
	# Two failure comments of one run count once.
	same_run = [three[0], _failure_marker_comment("⚠️ **Editor no-op suspicious** — x", run="2"), three[1]]
	assert heal.count_identical_failures(same_run, head_sha=SHA_A, author_login=CAP_AUTHOR)["count"] == 2
	# A different fingerprint or a legacy failure comment without a marker ends the scan.
	changed = [three[0], three[1], _failure_marker_comment(AUTOFIX_FAILED_COMMENT, run="9", reason="workflow_failure")]
	assert heal.count_identical_failures(changed, head_sha=SHA_A, author_login=CAP_AUTHOR)["count"] == 1
	legacy = [three[0], {"author_login": CAP_AUTHOR, "body": AUTOFIX_FAILED_COMMENT}, three[1], three[2]]
	assert heal.count_identical_failures(legacy, head_sha=SHA_A, author_login=CAP_AUTHOR)["count"] == 2
	# The cap marker is detected per head and per trusted author.
	cap = {"author_login": CAP_AUTHOR, "body": f"**AI review/autofix stopped: identical failure repeated**\n\n<!-- review-autofix-failure-cap:v1 head={SHA_A} fp={fp} reason=editor_empty_noop count=3 -->"}
	assert heal.count_identical_failures([*three, cap], head_sha=SHA_A, author_login=CAP_AUTHOR) == {"count": 3, "fp": fp, "reason": "editor_empty_noop", "cap_applied": True}
	assert heal.count_identical_failures([*three, cap], head_sha=SHA_B, author_login=CAP_AUTHOR)["cap_applied"] is False
	assert heal.count_identical_failures([*three, {**cap, "author_login": "attacker"}], head_sha=SHA_A, author_login=CAP_AUTHOR)["cap_applied"] is False
	# No authenticated author: nothing is trusted.
	assert heal.count_identical_failures(three, head_sha=SHA_A, author_login="")["count"] == 0


def test_fingerprint_cli_subcommands_print_log_safe_key_values() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-fp-cli-") as tmp_name:
		tmp = Path(tmp_name)
		(tmp / "editor_stage_stderr.txt").write_text(EDITOR_STDERR_RUN_1, encoding="utf-8")
		env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AUTOFIX_EDITOR_EMPTY_NOOP": "true"}
		env.pop("AUTOFIX_FAILURE_REASON", None)
		result = subprocess.run(
			[
				"python3", str(LIB_PATH), "autofix-failure-fingerprint",
				"--evidence-file", str(tmp / "editor_stage_stderr.txt"),
				"--evidence-file", str(tmp / "missing.txt"),
				"--evidence-out", str(tmp / "failure_evidence_tail.txt"),
				"--head-sha", SHA_A, "--run-id", "42",
			],
			capture_output=True, text=True, check=True, env=env,
		)
		lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
		fp = _cap_fp()
		assert lines == {"fp": fp, "degraded": "0", "reason": "editor_empty_noop", "marker": heal.render_failure_marker(SHA_A, "editor_empty_noop", fp, False, "42")}
		assert "OPENROUTER_API_KEY is required" in (tmp / "failure_evidence_tail.txt").read_text(encoding="utf-8")
		comments = [_failure_marker_comment(AUTOFIX_NOOP_COMMENT, run=str(run)) for run in (1, 2, 3)]
		(tmp / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
		result = subprocess.run(
			["python3", str(LIB_PATH), "autofix-identical-failure-count", "--comments-json", str(tmp / "comments.json"), "--head-sha", SHA_A, "--author-login", CAP_AUTHOR],
			capture_output=True, text=True, check=True, env=env,
		)
		assert result.stdout.splitlines() == ["count=3", f"fp={fp}", "reason=editor_empty_noop", "cap_applied=false"]
		(tmp / "comments.json").write_text("{}", encoding="utf-8")
		bad = subprocess.run(
			["python3", str(LIB_PATH), "autofix-identical-failure-count", "--comments-json", str(tmp / "comments.json"), "--head-sha", SHA_A, "--author-login", CAP_AUTHOR],
			capture_output=True, text=True, check=False, env=env,
		)
		assert bad.returncode == 2 and bad.stdout == ""


def test_derived_failure_reason_matches_the_reporter_precedence() -> None:
	cases = [
		({"AUTOFIX_FAILURE_REASON": "identical_failure_cap", "AUTOFIX_EDITOR_EMPTY_NOOP": "true"}, RUN_SUMMARY_LINE),
		({"AUTOFIX_FAILURE_REASON": "Not A Reason", "EDITOR_CHANGES_LOST": "true"}, RUN_SUMMARY_LINE),
		({"AUTOFIX_FAILURE_REASON": "identical_failure_cap", "EDITOR_PREFLIGHT_FAILED": "true"}, None),
		({"EDITOR_PREFLIGHT_FAILED": "true", "AUTOFIX_EDITOR_EMPTY_NOOP": "true"}, RUN_SUMMARY_LINE),
		({"EDITOR_PREFLIGHT_FAILED": "false"}, None),
		({"AUTOFIX_EDITOR_EMPTY_NOOP": "true", "EDITOR_CHANGES_LOST": "true"}, None),
		({"EDITOR_CHANGES_LOST": "true", "EDITOR_NOOP_REFUSAL": "true"}, None),
		({"EDITOR_NOOP_REFUSAL": "true"}, RUN_SUMMARY_LINE),
		({}, RUN_SUMMARY_LINE.replace("editor_empty_noop", "reviewers_unavailable")),
		({}, None),
	]
	for flags, summary_line in cases:
		finalize = ""
		if summary_line is not None:
			finalize = json.loads(summary_line.split(" ", 1)[1])["finalize_reason"]
		expected = heal.derive_autofix_failure_reason(flags, finalize)
		with tempfile.TemporaryDirectory(prefix="heal-reason-parity-") as tmp_name:
			tmp = Path(tmp_name)
			work, _state_file, env = _stage_autofix_report(tmp, comments=[], flags={**flags, "WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK": "99"}, summary_line=summary_line)
			result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
			assert f"skip reason=below_streak pr=4174 reason={expected} " in result.stdout, (flags, expected, result.stdout)


def test_editor_preflight_failed_precedence() -> None:
	# A failed editor preflight means the editor never ran: it names the
	# failure ahead of the editor flags and the run summary, and only an
	# explicit AUTOFIX_FAILURE_REASON (the fingerprint cap) wins over it.
	assert heal.derive_autofix_failure_reason({"EDITOR_PREFLIGHT_FAILED": "true"}) == "editor_preflight_failed"
	assert heal.derive_autofix_failure_reason({"EDITOR_PREFLIGHT_FAILED": "true", "AUTOFIX_EDITOR_EMPTY_NOOP": "true", "EDITOR_CHANGES_LOST": "true"}, "reviewers_unavailable") == "editor_preflight_failed"
	assert heal.derive_autofix_failure_reason({"EDITOR_PREFLIGHT_FAILED": "true", "AUTOFIX_FAILURE_REASON": "identical_failure_cap"}) == "identical_failure_cap"
	assert heal.derive_autofix_failure_reason({"EDITOR_PREFLIGHT_FAILED": "false"}) == "workflow_failure"
	with tempfile.TemporaryDirectory(prefix="heal-autofix-preflight-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[], flags={"EDITOR_PREFLIGHT_FAILED": "true", "WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK": "1"})
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert "dispatched pr=4174 failure=editor_preflight_failed streak=1" in result.stdout, result.stdout + result.stderr
		payload = heal.validate_payload(_state(state_file)["dispatches"][0]["body"]["client_payload"]["report"])
		assert payload["failure_reason"] == "editor_preflight_failed"


def test_autofix_report_honours_cap_reason_and_carries_fingerprint() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-autofix-cap-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(
			tmp,
			comments=[],
			flags={"AUTOFIX_FAILURE_REASON": "identical_failure_cap", "AUTOFIX_FAILURE_FP": FP_HEX, "WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK": "1", "AUTOFIX_EDITOR_EMPTY_NOOP": "true"},
		)
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert "dispatched pr=4174 failure=identical_failure_cap streak=1" in result.stdout, result.stdout + result.stderr
		payload = heal.validate_payload(_state(state_file)["dispatches"][0]["body"]["client_payload"]["report"])
		assert payload["failure_reason"] == "identical_failure_cap" and payload["failure_fingerprint"] == FP_HEX
	with tempfile.TemporaryDirectory(prefix="heal-autofix-cap-badfp-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[], flags={"AUTOFIX_FAILURE_FP": "nothex", "WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK": "1"})
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		report = _state(state_file)["dispatches"][0]["body"]["client_payload"]["report"]
		assert "failure_fingerprint" not in report
		assert heal.validate_payload(report)["failure_fingerprint"] is None


def test_validate_payload_failure_fingerprint_is_optional_and_strict() -> None:
	assert heal.validate_payload(_autofix_payload())["failure_fingerprint"] is None
	assert heal.validate_payload(_autofix_payload(failure_fingerprint=FP_HEX.upper()))["failure_fingerprint"] == FP_HEX
	assert heal.validate_payload(_autofix_payload(failure_fingerprint="f" * 63))["failure_fingerprint"] is None
	built = heal.build_autofix_failure_payload(
		repo=CONSUMER_REPO, pr=_pr(), comments=[], workflow_name="AI Review", failure_reason="identical_failure_cap",
		failure_evidence="", failure_streak=1, run_id="500", run_url=None, wrapper_sha=None, reporter_run_url=None, failure_fingerprint=FP_HEX,
	)
	assert built["failure_fingerprint"] == FP_HEX
	assert len(heal.wrap_dispatch(built)["client_payload"]) <= heal.DISPATCH_CLIENT_PAYLOAD_MAX_KEYS
	issue_payload = heal.build_issue_payload(repo=CONSUMER_REPO, kind="issue", label="ai:needs-human", issue=_issue(), comments=[], runs=[], wrapper_sha=None, reporter_run_url=None)
	assert heal.validate_payload({**issue_payload, "failure_fingerprint": FP_HEX})["failure_fingerprint"] is None


def test_fingerprint_cap_log_prefixes_are_registered() -> None:
	agents_text = (REPO_ROOT / "agents.md").read_text(encoding="utf-8")
	for prefix in ("AUTOFIX_FINGERPRINT", "AUTOFIX_FINGERPRINT_CAP_TRIPPED", "AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED", "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED"):
		assert f"- `{prefix}`" in agents_text, prefix
		assert f"LOG_PREFIX.name={prefix}" in agents_text, prefix
	workflow_text = REVIEW_AUTOFIX_WORKFLOW.read_text(encoding="utf-8")
	for prefix in ("AUTOFIX_FINGERPRINT_CAP_TRIPPED pr=", "AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=", "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=", "AUTOFIX_FINGERPRINT pr="):
		assert prefix in workflow_text, prefix


GATE_MOCK_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

state = Path(os.environ["MOCK_GATE_STATE"])
data = json.loads(state.read_text())
args = sys.argv[1:]
data.setdefault("calls", []).append(args)
state.write_text(json.dumps(data))
jq = None
if "--jq" in args:
	jq = args[args.index("--jq") + 1]
path = next((a for a in args[1:] if a.startswith("repos/") or a == "user"), "")

def emit(value):
	text = json.dumps(value)
	if jq is not None:
		text = subprocess.run(["jq", "-rc", jq], input=text, capture_output=True, text=True, check=True).stdout
	sys.stdout.write(text if text.endswith("\n") else text + "\n")

if args[:1] != ["api"]:
	sys.exit(1)
if path == "user":
	if data.get("user_fail"):
		sys.exit(1)
	emit({"login": data["login"]})
elif path.endswith("/comments"):
	if data.get("comments_fail"):
		sys.exit(1)
	emit(data["comments"])
elif path.endswith("/files"):
	emit([{"filename": "scripts/big_change.sh"}])
elif "/pulls/" in path:
	emit(data["pr"])
else:
	sys.exit(1)
'''


def _gate_run_script() -> str:
	workflow = _yaml(REVIEW_AUTOFIX_WORKFLOW)
	steps = workflow["jobs"]["gate"]["steps"]
	return next(step for step in steps if step.get("name") == "Evaluate review gate")["run"]


def _run_gate(tmp: Path, *, comments: list[dict], event_name: str = "workflow_dispatch", extra_env: dict[str, str] | None = None, state_overrides: dict | None = None, with_helper: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, str], dict]:
	work = tmp / "work"
	(work / ".codex-workflow-src-main" / "scripts").mkdir(parents=True)
	if with_helper:
		shutil.copy(LIB_PATH, work / ".codex-workflow-src-main" / "scripts" / LIB_PATH.name)
	# The branch-pinned copy predates the cap: the gate must fall back to main's.
	(work / ".codex-workflow-src" / "scripts").mkdir(parents=True)
	(work / ".codex-workflow-src" / "scripts" / LIB_PATH.name).write_text("# old helper without the cap subcommand\n", encoding="utf-8")
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(GATE_MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp / "gate_state.json"
	# The API shape: the gate's jq projection reads the author from user.login.
	api_comments = [{"id": comment.get("id", 0), "user": {"login": comment.get("author_login", "")}, "created_at": "2026-09-22T08:00:00Z", "body": comment["body"]} for comment in comments]
	state = {
		"login": CAP_AUTHOR,
		"comments": api_comments,
		"pr": {"state": "open", "merged": False, "head": {"ref": "ai/issue-4255", "sha": SHA_A}, "labels": [], "additions": 400, "deletions": 50, "mergeable": True, "mergeable_state": "clean", "title": "AI implementation for issue #4255", "body": "Fixes #4255"},
	}
	state.update(state_overrides or {})
	state_file.write_text(json.dumps(state), encoding="utf-8")
	output_file = tmp / "github_output.txt"
	output_file.write_text("", encoding="utf-8")
	(tmp / "runner_temp").mkdir()
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"MOCK_GATE_STATE": str(state_file),
		"GITHUB_OUTPUT": str(output_file),
		"RUNNER_TEMP": str(tmp / "runner_temp"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"GH_TOKEN": "x",
		"REPOSITORY": SELF_REPO,
		"EVENT_NAME": event_name,
		"EVENT_ACTION": "",
		"PR_NUMBER": "4259",
		"PR_HEAD_SHA": "",
		"PR_IS_DRAFT": "false",
		"PR_SKIP_AI": "false",
		"PR_TITLE": "AI implementation for issue #4255",
		"PR_BODY": "Fixes #4255",
		"AUTOFIX_SKIP_SELF_TRIGGERED": "",
		"AUTOFIX_BOT_LOGIN": "",
		"AUTOFIX_SKIP_TERMINAL_SAME_HEAD": "",
		"FORCE_RB_JUDGE": "false",
		"AUTOFIX_SKIP_DOC_ONLY": "",
		"AUTOFIX_SKIP_SUPPRESS_ON_CONFLICT": "",
		"AUTOFIX_SKIP_MAX_ADDITIONS": "",
		"AUTOFIX_SKIP_MAX_DELETIONS": "",
		"FORCE_CLAUDE_BRANCH_REVIEW": "",
		"HEAD_REF_OVERRIDE": "",
		"REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED": "true",
		"REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL": "3",
	}
	env.update(extra_env or {})
	script = tmp / "gate.sh"
	script.write_text(_gate_run_script(), encoding="utf-8")
	result = subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	return result, outputs, json.loads(state_file.read_text(encoding="utf-8"))


def _comment_calls(state: dict) -> int:
	return sum(1 for call in state["calls"] if any(str(arg).endswith("/comments") for arg in call))


def test_gate_stops_a_head_with_three_identical_failure_markers() -> None:
	fp = _cap_fp()
	comments = [_failure_marker_comment(AUTOFIX_NOOP_COMMENT, run=str(run)) for run in (101, 102, 103)]
	with tempfile.TemporaryDirectory(prefix="heal-gate-cap-") as tmp_name:
		result, outputs, state = _run_gate(Path(tmp_name), comments=comments)
		assert result.returncode == 0, result.stderr + result.stdout
		assert f"AUTOFIX_FINGERPRINT_CAP_TRIPPED pr=4259 head={SHA_A} fp={fp} reason=editor_empty_noop count=3 max=3 already_applied=false" in result.stdout
		assert outputs["should_run"] == "false" and outputs["skip_reason"] == "fingerprint_cap"
		assert outputs["fingerprint_cap"] == "true" and outputs["fingerprint_cap_fp"] == fp
		assert outputs["fingerprint_cap_count"] == "3" and outputs["fingerprint_cap_max"] == "3"
		assert outputs["fingerprint_cap_reason"] == "editor_empty_noop" and outputs["fingerprint_cap_already_applied"] == "false"
		assert outputs["deterministic_skip"] == "false"
		# §15: the terminal same-head skip and the cap share one comments call.
		assert _comment_calls(state) == 1
		assert sum(1 for call in state["calls"] if "user" in call) == 1


def test_gate_cap_already_applied_threshold_bypass_and_fail_open() -> None:
	fp = _cap_fp()
	three = [_failure_marker_comment(AUTOFIX_NOOP_COMMENT, run=str(run)) for run in (101, 102, 103)]
	cap = {"id": 200, "author_login": CAP_AUTHOR, "body": f"**AI review/autofix stopped: identical failure repeated**\n\n<!-- review-autofix-failure-cap:v1 head={SHA_A} fp={fp} reason=editor_empty_noop count=3 -->"}
	with tempfile.TemporaryDirectory(prefix="heal-gate-cap-applied-") as tmp_name:
		result, outputs, _state = _run_gate(Path(tmp_name), comments=[*three, cap], event_name="pull_request")
		assert outputs["fingerprint_cap"] == "true" and outputs["fingerprint_cap_already_applied"] == "true", result.stdout
		assert f"AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=4259 head={SHA_A} fp={fp} count=3" in result.stdout
	# Two identical failures stay below the default threshold of 3.
	with tempfile.TemporaryDirectory(prefix="heal-gate-cap-below-") as tmp_name:
		result, outputs, _state = _run_gate(Path(tmp_name), comments=three[:2])
		assert outputs["fingerprint_cap"] == "false" and outputs["should_run"] == "true", result.stdout
		assert f"AUTOFIX_FINGERPRINT cap=not_tripped pr=4259 head={SHA_A} fp={fp} reason=editor_empty_noop count=2 max=3" in result.stdout
	# A malformed threshold falls back to 3; a lower threshold trips earlier.
	with tempfile.TemporaryDirectory(prefix="heal-gate-cap-max2-") as tmp_name:
		_result, outputs, _state = _run_gate(Path(tmp_name), comments=three[:2], extra_env={"REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL": "2"})
		assert outputs["fingerprint_cap"] == "true"
	with tempfile.TemporaryDirectory(prefix="heal-gate-cap-badmax-") as tmp_name:
		_result, outputs, _state = _run_gate(Path(tmp_name), comments=three[:2], extra_env={"REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL": "abc"})
		assert outputs["fingerprint_cap"] == "false" and outputs["fingerprint_cap_max"] == "3"
	# The kill switch, the force_rb_judge dispatch and the fail-open paths keep the run.
	cases = [
		("disabled", {"REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED": "false"}, {}, True, None),
		("force_rb_judge", {"FORCE_RB_JUDGE": "true"}, {}, True, "AUTOFIX_FINGERPRINT cap=bypassed pr=4259"),
		("helper_missing", {}, {}, False, "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=4259 head=" + SHA_A + " reason=helper_missing"),
		("comments_fail", {}, {"comments_fail": True}, True, "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=4259 head=" + SHA_A + " reason=api_error"),
		("user_fail", {}, {"user_fail": True}, True, "AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED pr=4259 head=" + SHA_A + " reason=marker_author_unavailable"),
	]
	for name, extra_env, overrides, with_helper, expected in cases:
		with tempfile.TemporaryDirectory(prefix=f"heal-gate-cap-{name}-") as tmp_name:
			result, outputs, _state = _run_gate(Path(tmp_name), comments=three, extra_env=extra_env, state_overrides=overrides, with_helper=with_helper)
			assert result.returncode == 0, (name, result.stderr)
			assert outputs["fingerprint_cap"] == "false" and outputs["should_run"] == "true", (name, result.stdout)
			if expected:
				assert expected in result.stdout, (name, result.stdout)
	# Markers for an older head do not count against a new head.
	with tempfile.TemporaryDirectory(prefix="heal-gate-cap-newhead-") as tmp_name:
		pr = {"state": "open", "merged": False, "head": {"ref": "ai/issue-4255", "sha": SHA_B}, "labels": [], "additions": 400, "deletions": 50, "mergeable": True, "mergeable_state": "clean", "title": "t", "body": "b"}
		_result, outputs, _state = _run_gate(Path(tmp_name), comments=three, state_overrides={"pr": pr})
		assert outputs["fingerprint_cap"] == "false" and outputs["fingerprint_cap_count"] == "0"


CAP_JOB_MOCK_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state = Path(os.environ["MOCK_CAP_STATE"])
data = json.loads(state.read_text())
args = sys.argv[1:]
data.setdefault("calls", []).append(args)

def save():
	state.write_text(json.dumps(data))

def field(name):
	for index, arg in enumerate(args):
		if arg == "-f" and args[index + 1].startswith(name + "="):
			return args[index + 1].split("=", 1)[1]
	return None

if args[:2] == ["label", "create"]:
	save()
	sys.exit(0)
if args[:1] != ["api"]:
	save()
	sys.exit(1)
method = "GET"
if "-X" in args:
	method = args[args.index("-X") + 1]
elif "--method" in args:
	method = args[args.index("--method") + 1]
elif any(arg == "-f" for arg in args):
	method = "POST"
path = next((a for a in args[1:] if a.startswith("repos/")), "")
if path.endswith("/dispatches"):
	body = json.loads(Path(args[args.index("--input") + 1]).read_text())
	data.setdefault("dispatches", []).append(body)
elif path.endswith("/comments") and method == "GET":
	print(json.dumps(data.get("comments", [])))
elif path.endswith("/comments") and method == "POST":
	data.setdefault("comments_posted", []).append(field("body"))
elif path.endswith("/labels") and method == "GET":
	print(json.dumps(["ai:done"]))
elif path.endswith("/labels"):
	number = path.split("/")[-2]
	if method == "PUT":
		body = json.loads(sys.stdin.read())
		data.setdefault("labels_set", []).append([number, body["labels"]])
	else:
		data.setdefault("labels_set", []).append([number, [field("labels[]")]])
elif "/pulls/" in path:
	print(json.dumps(data["pr"]))
else:
	save()
	sys.exit(1)
save()
'''


def _cap_job_script() -> str:
	workflow = _yaml(REVIEW_AUTOFIX_WORKFLOW)
	job = workflow["jobs"]["fingerprint-cap-block"]
	return next(step for step in job["steps"] if step.get("name") == "Apply identical-failure cap outcome")["run"]


def _run_cap_job(tmp: Path, *, pr_body: str, already_applied: str = "false", head: str = SHA_A, fresh_cap_marker: bool = False) -> tuple[subprocess.CompletedProcess[str], dict]:
	work = tmp / "work"
	scripts = work / ".codex-workflow-src" / "scripts"
	scripts.mkdir(parents=True)
	for name in ("gh_helpers.sh", "label_helpers.sh", "tg_helpers.sh", "workflow_failure_heal.py", "workflow_failure_heal_autofix_report.sh"):
		shutil.copy(SCRIPTS_DIR / name, scripts / name)
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(CAP_JOB_MOCK_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp / "cap_state.json"
	pr = {"number": 4259, "state": "open", "title": "AI implementation for issue #4255", "body": pr_body, "html_url": f"https://github.com/{SELF_REPO}/pull/4259", "labels": [], "head": {"ref": "ai/issue-4255", "sha": head}}
	comments = [{"author_login": CAP_AUTHOR, "body": f"<!-- review-autofix-failure-cap:v1 head={SHA_A} fp={FP_HEX} reason=editor_empty_noop count=3 -->"}] if fresh_cap_marker else []
	state_file.write_text(json.dumps({"pr": pr, "comments": comments}), encoding="utf-8")
	(tmp / "runner_temp").mkdir()
	summary = tmp / "summary.md"
	env = {
		"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		"HOME": str(tmp),
		"MOCK_CAP_STATE": str(state_file),
		"RUNNER_TEMP": str(tmp / "runner_temp"),
		"GITHUB_STEP_SUMMARY": str(summary),
		"GITHUB_REPOSITORY": SELF_REPO,
		"GITHUB_RUN_ID": "900",
		"GITHUB_SERVER_URL": "https://github.com",
		"PYTHONDONTWRITEBYTECODE": "1",
		"GH_TOKEN": "x",
		"REPOSITORY": SELF_REPO,
		"PR_NUMBER": "4259",
		"PR_HEAD_SHA": SHA_A,
		"FINGERPRINT_CAP_FP": FP_HEX,
		"FINGERPRINT_CAP_REASON": "editor_empty_noop",
		"FINGERPRINT_CAP_COUNT": "3",
		"FINGERPRINT_CAP_MAX": "3",
		"FINGERPRINT_CAP_ALREADY_APPLIED": already_applied,
		"FINGERPRINT_CAP_MARKER_AUTHOR_LOGIN": CAP_AUTHOR,
		"WORKFLOW_SUPPORT_REF": SHA_B,
		"WORKFLOW_HEAL_ENABLED": "true",
		"REPORT_WORKFLOW_NAME": "Internal Review",
		"REPORT_RUN_URL": f"https://github.com/{SELF_REPO}/actions/runs/900",
		"TG_BOT_SECRET": "",
		"TG_CHAT_ID": "",
	}
	script = tmp / "cap_job.sh"
	script.write_text(_cap_job_script(), encoding="utf-8")
	result = subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True, check=False)
	return result, json.loads(state_file.read_text(encoding="utf-8"))


def test_fingerprint_cap_block_labels_comments_and_reports() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-cap-job-") as tmp_name:
		result, state = _run_cap_job(Path(tmp_name), pr_body="Fixes #4255\n\nRefs #3965")
		assert result.returncode == 0, result.stderr + result.stdout
		assert state["labels_set"] == [["4255", ["ai:review-blocked"]]]
		assert len(state["comments_posted"]) == 1
		comment = state["comments_posted"][0]
		assert comment.startswith("**AI review/autofix stopped: identical failure repeated**")
		assert f"<!-- review-autofix-failure-cap:v1 head={SHA_A} fp={FP_HEX} reason=editor_empty_noop count=3 -->" in comment
		assert "linked issue(s) #4255" in comment
		# The cap marker the job posts is the one the gate's scan recognises.
		assert heal.count_identical_failures([{"author_login": CAP_AUTHOR, "body": comment}], head_sha=SHA_A, author_login=CAP_AUTHOR)["cap_applied"] is True
		report = heal.validate_payload(state["dispatches"][0]["client_payload"]["report"])
		assert report["failure_reason"] == "identical_failure_cap" and report["failure_fingerprint"] == FP_HEX
		assert report["issue_number"] == 4259 and report["failure_streak"] == 1
		assert "identical_failure_cap: 3 identical review/autofix failures" in report["failure_evidence"]
		assert f"AUTOFIX_FINGERPRINT cap=applied pr=4259 head={SHA_A} fp={FP_HEX} count=3" in result.stdout
		assert "WORKFLOW_HEAL_AUTOFIX_REPORT dispatched pr=4259 failure=identical_failure_cap streak=1" in result.stdout


def test_fingerprint_cap_block_pr_label_idempotency_and_head_moved() -> None:
	# No linked issue: the PR itself is labeled.
	with tempfile.TemporaryDirectory(prefix="heal-cap-job-pr-") as tmp_name:
		result, state = _run_cap_job(Path(tmp_name), pr_body="Manual change, no linked issue.")
		assert result.returncode == 0, result.stderr
		assert state["labels_set"] == [["4259", ["ai:review-blocked"]]]
		assert "this pull request (no linked issue found)" in state["comments_posted"][0]
	# A cap marker already on the head: log and write nothing.
	with tempfile.TemporaryDirectory(prefix="heal-cap-job-applied-") as tmp_name:
		result, state = _run_cap_job(Path(tmp_name), pr_body="Fixes #4255", already_applied="true")
		assert f"AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=4259 head={SHA_A} fp={FP_HEX} count=3" in result.stdout
		assert state.get("calls", []) == []
	# A queued cap job re-checks after acquiring the per-PR lock and writes nothing.
	with tempfile.TemporaryDirectory(prefix="heal-cap-job-fresh-marker-") as tmp_name:
		result, state = _run_cap_job(Path(tmp_name), pr_body="Fixes #4255", fresh_cap_marker=True)
		assert f"AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED pr=4259 head={SHA_A} fp={FP_HEX} count=3" in result.stdout
		assert "labels_set" not in state and "comments_posted" not in state and "dispatches" not in state
	# A push after the gate: the new head gets its own run.
	with tempfile.TemporaryDirectory(prefix="heal-cap-job-moved-") as tmp_name:
		result, state = _run_cap_job(Path(tmp_name), pr_body="Fixes #4255", head=SHA_B)
		assert "reason=head_moved" in result.stdout
		assert "labels_set" not in state and "comments_posted" not in state and "dispatches" not in state


# ---------------------------------------------------------------------------
# P3: self-inflicted classification and routing
# ---------------------------------------------------------------------------

EDITOR_GUARD_CRASH_LINE = "/home/runner/work/_temp/codex-support/scripts/review_apply_fixes.sh: line 168: OPENROUTER_API_KEY: OPENROUTER_API_KEY is required"
DIGEST_ERROR_LINE = "##[error]Could not publish the linked-issue metadata integrity digest."
INTEGRATION_BRANCH = "orchestrator/project-4139"
DIAG_PR_SELF_INFLICTED = "## Classification\npr-self-inflicted\n\n## Summary\nThe PR unsets OPENROUTER_API_KEY before the editor guard runs.\n"
DIAG_BASE_SELF_INFLICTED = "## Classification\nbase-self-inflicted\n\n## Summary\nThe integration branch pins a script to main that lacks the digest output.\n"


def test_extract_crash_file_on_observed_error_lines() -> None:
	assert heal.extract_crash_file(EDITOR_GUARD_CRASH_LINE) == "scripts/review_apply_fixes.sh"
	assert heal.extract_crash_file(DIGEST_ERROR_LINE) is None
	assert heal.extract_crash_file("2026-09-22T01:00:00Z ::error::Step failed in .github/workflows/review_autofix.yml.") == ".github/workflows/review_autofix.yml"
	assert heal.extract_crash_file("::error::bad call in /tmp/x/scripts/review_collect_pr_metadata.sh (exit 1)") == "scripts/review_collect_pr_metadata.sh"
	# A bare script name, a non-error line, and traversal never name a file.
	assert heal.extract_crash_file("::error::resolve_integration_ref.sh: Integration branch missing") is None
	assert heal.extract_crash_file("note: scripts/review_apply_fixes.sh was staged") is None
	assert heal.extract_crash_file("::error::see scripts/../etc/passwd") is None
	assert heal.extract_crash_file("") is None and heal.extract_crash_file(None) is None
	# The shell line wins over an earlier error line naming another path.
	assert heal.extract_crash_file("::error::from .github/workflows/review_autofix.yml\n" + EDITOR_GUARD_CRASH_LINE) == "scripts/review_apply_fixes.sh"


def test_classify_crash_ownership() -> None:
	crash = "scripts/review_apply_fixes.sh"
	assert heal.classify_crash_ownership(crash_file=crash, changed_files=[crash], base_changed_files=[]) == "pr"
	# In both diffs: the PR's own change owns it.
	assert heal.classify_crash_ownership(crash_file=crash, changed_files=[crash], base_changed_files=[crash]) == "pr"
	assert heal.classify_crash_ownership(crash_file=crash, changed_files=["scripts/opencode_helpers.sh"], base_changed_files=[crash]) == "base"
	assert heal.classify_crash_ownership(crash_file=crash, changed_files=["scripts/opencode_helpers.sh"], base_changed_files=[]) == "none"
	assert heal.classify_crash_ownership(crash_file=None, changed_files=[crash], base_changed_files=[crash]) == "none"


def test_parse_classification_accepts_self_inflicted_tokens() -> None:
	assert heal.parse_classification(DIAG_PR_SELF_INFLICTED) == "pr-self-inflicted"
	assert heal.parse_classification("## Classification\n`base-self-inflicted`\n") == "base-self-inflicted"
	assert set(heal.SELF_INFLICTED_CLASSIFICATIONS) <= set(heal.CLASSIFICATIONS)
	assert not set(heal.SELF_INFLICTED_CLASSIFICATIONS) & set(heal.UPSTREAM_ISSUE_CLASSIFICATIONS)


def _self_inflicted_payload(*, changed_files: list[str], crash_line: str = EDITOR_GUARD_CRASH_LINE, base_branch: str = INTEGRATION_BRANCH, source_repo: str = SELF_REPO, script_ref: str = SHA_A) -> dict:
	payload = heal.build_autofix_failure_payload(
		repo=source_repo,
		pr=_pr(),
		comments=[{"body": AUTOFIX_NOOP_COMMENT}],
		workflow_name="AI Review",
		failure_reason="editor_empty_noop",
		failure_evidence="failure_reason=editor_empty_noop\n--- failure_evidence_tail.txt (tail) ---\n" + crash_line + "\n",
		failure_streak=2,
		run_id="500",
		run_url=f"https://github.com/{source_repo}/actions/runs/500",
		wrapper_sha=SHA_A,
		reporter_run_url=f"https://github.com/{source_repo}/actions/runs/500",
		base_branch=base_branch,
		script_ref=script_ref,
		changed_files=changed_files,
	)
	payload["issue_url"] = f"https://github.com/{source_repo}/pull/4174"
	payload["run_refs"] = [{"repo": source_repo, "run_id": "500", "url": f"https://github.com/{source_repo}/actions/runs/500"}]
	return payload


def test_autofix_payload_carries_ownership_fields() -> None:
	payload = heal.validate_payload(_self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh", "scripts/opencode_helpers.sh", "../escape.sh", "x" * 121]))
	assert payload["base_branch"] == INTEGRATION_BRANCH
	assert payload["script_ref"] == SHA_A
	assert payload["changed_files"] == ["scripts/opencode_helpers.sh"]
	assert payload["crash_file"] == "scripts/review_apply_fixes.sh"
	# The dispatch envelope still has two keys, and the report round-trips.
	wrapped = heal.wrap_dispatch(payload)
	assert len(wrapped["client_payload"]) <= heal.DISPATCH_CLIENT_PAYLOAD_MAX_KEYS
	assert heal.validate_payload(heal.unwrap_dispatch(wrapped["client_payload"]))["crash_file"] == "scripts/review_apply_fixes.sh"
	# Caps: 200 entries.
	many = heal.build_autofix_failure_payload(repo=SELF_REPO, pr=_pr(), comments=[], workflow_name="AI Review", failure_reason="workflow_failure", failure_evidence="", failure_streak=1, run_id="1", run_url=None, wrapper_sha=None, reporter_run_url=None, changed_files=[f"scripts/f{i}.sh" for i in range(250)], script_ref="stable")
	assert len(many["changed_files"]) == heal.CHANGED_FILES_MAX_ENTRIES and many["script_ref"] == "stable"
	assert "crash_file" not in many and "base_branch" not in many
	# Invalid values from the wire are dropped, never fatal.
	raw = _self_inflicted_payload(changed_files=["scripts/a.sh"])
	raw.update({"base_branch": "bad branch", "script_ref": "main", "crash_file": "/etc/passwd", "changed_files": "not-a-list"})
	cleaned = heal.validate_payload(raw)
	assert cleaned["base_branch"] is None and cleaned["script_ref"] is None and cleaned["crash_file"] is None and cleaned["changed_files"] == []
	# Non-autofix payloads gain no ownership keys.
	assert "crash_file" not in heal.validate_payload(_consumer_payload(crash_file="scripts/a.sh"))


def test_payload_with_all_fields_at_limits_stays_under_dispatch_size() -> None:
	payload = _self_inflicted_payload(changed_files=[("scripts/" + "d" * 100 + f"{i:03d}.sh")[:120] for i in range(300)])
	payload["issue_excerpt"] = "x" * heal.ISSUE_EXCERPT_LIMIT
	payload["comments_excerpt"] = "y" * heal.COMMENTS_EXCERPT_LIMIT
	payload["failure_evidence"] = "z" * heal.FAILURE_EVIDENCE_LIMIT
	body = json.dumps(heal.wrap_dispatch(heal.validate_payload(payload)))
	assert len(body.encode("utf-8")) < heal.MAX_PAYLOAD_BYTES


def test_compose_base_self_inflicted_issue_body_carries_lineage() -> None:
	payload = heal.validate_payload(_self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"]))
	body = heal.compose_issue_body(payload=payload, diagnosis=DIAG_BASE_SELF_INFLICTED, fp=FP_HEX, gen=1, root=FP_HEX, classification="base-self-inflicted", target_branch=INTEGRATION_BRANCH, max_depth=3, intake_run_url="u", run_summaries=[], integration_branch=INTEGRATION_BRANCH)
	match = TARGET_BRANCH_RE.search(body)
	assert match and (match.group(1) or match.group(2)) == INTEGRATION_BRANCH
	assert re.search(r"^\s*(?:-\s*)?(?:\*\*Tracking issue:\*\*|Tracking issue:)\s*#(\d+)\s*$", body, re.MULTILINE).group(1) == "4139"
	assert re.search(r"^\s*(?:-\s*)?(?:\*\*Integration branch:\*\*|Integration branch:)\s*`?\s*([^`\n]+?)\s*`?\s*$", body, re.MULTILINE).group(1) == INTEGRATION_BRANCH
	assert "Refs #4139" in body
	assert not re.search(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+#4139", body)
	assert "**Crash file:** `scripts/review_apply_fixes.sh`" in body
	# A non-orchestrator base adds no lineage lines.
	plain = heal.compose_issue_body(payload=payload, diagnosis="x", fp=FP_HEX, gen=1, root=FP_HEX, classification="base-self-inflicted", target_branch="feature/x", max_depth=3, intake_run_url="u", run_summaries=[], integration_branch="feature/x")
	assert "Tracking issue" not in plain and "Refs #" not in plain


def test_classify_crash_ownership_cli() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-own-") as tmp_name:
		tmp = Path(tmp_name)
		payload_file = tmp / "p.json"
		payload_file.write_text(json.dumps(_self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"])), encoding="utf-8")
		base_file = tmp / "base.txt"
		base_file.write_text("README.md\nscripts/review_apply_fixes.sh\n", encoding="utf-8")
		out = subprocess.run(["python3", str(SCRIPTS_DIR / "workflow_failure_heal.py"), "classify-crash-ownership", "--payload-json", str(payload_file), "--base-changed-files", str(base_file)], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
		assert out.stdout.strip() == "base"
		out = subprocess.run(["python3", str(SCRIPTS_DIR / "workflow_failure_heal.py"), "classify-crash-ownership", "--payload-json", str(payload_file)], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
		assert out.stdout.strip() == "none"


def _git(cwd: Path, *args: str) -> None:
	subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _ownership_git_origin(tmp: Path, work: Path) -> None:
	"""A local origin whose integration branch changed review_apply_fixes.sh relative to main."""
	origin = tmp / "origin.git"
	_git(tmp, "init", "--bare", str(origin))
	seed = tmp / "seed"
	seed.mkdir()
	_git(seed, "init")
	(seed / "scripts").mkdir()
	(seed / "scripts" / "review_apply_fixes.sh").write_text("echo main\n", encoding="utf-8")
	(seed / "scripts" / "opencode_helpers.sh").write_text("echo helpers\n", encoding="utf-8")
	_git(seed, "add", ".")
	_git(seed, "commit", "-m", "main")
	_git(seed, "push", str(origin), "HEAD:refs/heads/main")
	(seed / "scripts" / "review_apply_fixes.sh").write_text("echo integration\n", encoding="utf-8")
	_git(seed, "commit", "-am", "integration change")
	_git(seed, "push", str(origin), f"HEAD:refs/heads/{INTEGRATION_BRANCH}")
	_git(work, "init")
	_git(work, "remote", "add", "origin", str(origin))


def _self_inflicted_state() -> dict:
	return _self_repo_autofix_state(["stable", "main", "ai/issue-4173", INTEGRATION_BRANCH])


def test_intake_pr_self_inflicted_comments_on_pr_without_issue() -> None:
	payload = _self_inflicted_payload(changed_files=["scripts/review_apply_fixes.sh", "scripts/opencode_helpers.sh"], base_branch="main")
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "WORKFLOW_HEAL crash_ownership=pr crash_file=scripts/review_apply_fixes.sh base=main" in result.stdout
	assert "## Ownership facts" in prompt and "- Ownership: pr" in prompt and "- Crash file in the pull request diff: yes" in prompt
	assert "issues_created" not in state_after
	assert "WORKFLOW_HEAL no_issue classification=pr-self-inflicted" in result.stdout
	comments = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{SELF_REPO}/issues/4174/comments"]
	assert len(comments) == 1
	body = comments[0]["body"]
	assert body.splitlines()[0] == "**Workflow failure heal: this failure is caused by this pull request's own changes**"
	assert "The PR unsets OPENROUTER_API_KEY" in body and "`scripts/review_apply_fixes.sh`" in body
	# Base is main: no git fetch and no branch lookup were needed.
	assert not [call for call in state_after["calls"] if any("/branches/" in part for part in call)]


def test_intake_base_self_inflicted_opens_issue_on_integration_branch() -> None:
	payload = _self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"])
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_BASE_SELF_INFLICTED, setup_git=_ownership_git_origin)
	assert result.returncode == 0, result.stderr + result.stdout
	assert f"WORKFLOW_HEAL crash_ownership=base crash_file=scripts/review_apply_fixes.sh base={INTEGRATION_BRANCH}" in result.stdout
	assert "- Ownership: base" in prompt and "- Crash file in the pull request diff: no" in prompt
	assert f"1 file(s) differ between main and {INTEGRATION_BRANCH}" in prompt
	created = state_after["issues_created"]
	assert len(created) == 1 and created[0]["repo"] == SELF_REPO
	assert created[0]["labels"] == [heal.HEAL_LABEL, "ai:orchestrator-managed"]
	body = created[0]["body"]
	match = TARGET_BRANCH_RE.search(body)
	assert match and (match.group(1) or match.group(2)) == INTEGRATION_BRANCH
	assert "- **Tracking issue:** #4139" in body and "Refs #4139" in body
	assert heal.parse_heal_markers(body)["classification"] == "base-self-inflicted"
	assert f"target_branch={INTEGRATION_BRANCH}" in result.stdout and "target_branch_source=base_branch" in result.stdout
	outcome = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{SELF_REPO}/issues/4174/comments"]
	assert outcome and f"on `{INTEGRATION_BRANCH}`" in outcome[0]["body"]
	assert "stable" not in outcome[0]["body"]
	# The ownership fetch proved the branch exists: no branch API lookup.
	assert not [call for call in state_after["calls"] if any("/branches/" in part for part in call)]


def test_intake_self_inflicted_token_without_backing_ownership_routes_as_workflow_defect() -> None:
	# The model claims base-self-inflicted, but the crash file is in the PR diff.
	payload = _self_inflicted_payload(changed_files=["scripts/review_apply_fixes.sh"], base_branch="main")
	result, state_after, _prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_BASE_SELF_INFLICTED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "classification_remapped from=base-self-inflicted to=workflow-defect reason=ownership_pr" in result.stdout
	created = state_after["issues_created"][0]
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "ai/issue-4173"
	assert heal.parse_heal_markers(created["body"])["classification"] == "workflow-defect"
	# Base diff unavailable (no git checkout in this harness): fail open to none.
	payload = _self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"])
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_BASE_SELF_INFLICTED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "warn crash_ownership_base_diff_failed reason=no_git_checkout" in result.stdout
	assert "- Ownership: none" in prompt
	assert "classification_remapped from=base-self-inflicted to=workflow-defect reason=ownership_none" in result.stdout


def test_intake_self_inflicted_routing_is_a_noop_for_consumer_reports() -> None:
	payload = _self_inflicted_payload(changed_files=["scripts/review_apply_fixes.sh"], base_branch="main", source_repo=CONSUMER_REPO)
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "crash_ownership=" not in result.stdout and "- Ownership:" not in prompt
	assert "classification_remapped from=pr-self-inflicted to=workflow-defect reason=ownership_none" in result.stdout
	created = state_after["issues_created"][0]
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "stable"


def test_intake_self_inflicted_routing_flag_off_restores_workflow_defect_route() -> None:
	payload = _self_inflicted_payload(changed_files=["scripts/review_apply_fixes.sh"], base_branch="main")
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED, extra_env={"WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED": "false"})
	assert result.returncode == 0, result.stderr + result.stdout
	assert "crash_ownership=" not in result.stdout and "- Ownership:" not in prompt
	assert "classification_remapped from=pr-self-inflicted to=workflow-defect reason=routing_disabled" in result.stdout
	created = state_after["issues_created"][0]
	match = TARGET_BRANCH_RE.search(created["body"])
	assert match and (match.group(1) or match.group(2)) == "ai/issue-4173"
	assert "target_branch_source=source_pr_head" in result.stdout


# Every model process exits before writing anything: no file is named.
SANDBOX_226_LINE = "Reviewer slot z-ai/glm-5.2 (z-ai/glm-5.2) execution failed on attempt 1 (exit=226)."


def test_classify_pipeline_ownership() -> None:
	pipeline = ["README.md", "scripts/opencode_helpers.sh", ".github/actions/install-opencode/action.yml", ".github/workflows/review_autofix.yml", ".github/workflows/ci.yml"]
	assert heal.classify_pipeline_ownership(crash_file=None, changed_files=pipeline, script_ref=SHA_B, head_sha=SHA_B) == (
		"pr",
		["scripts/opencode_helpers.sh", ".github/actions/install-opencode/action.yml", ".github/workflows/review_autofix.yml"],
	)
	# A crash file keeps the crash-file basis in charge.
	assert heal.classify_pipeline_ownership(crash_file="scripts/a.sh", changed_files=pipeline, script_ref=SHA_B, head_sha=SHA_B) == ("none", [])
	# The run did not execute the PR head's scripts.
	assert heal.classify_pipeline_ownership(crash_file=None, changed_files=pipeline, script_ref=SHA_A, head_sha=SHA_B) == ("none", [])
	assert heal.classify_pipeline_ownership(crash_file=None, changed_files=pipeline, script_ref="stable", head_sha=SHA_B) == ("none", [])
	assert heal.classify_pipeline_ownership(crash_file=None, changed_files=pipeline, script_ref=None, head_sha=None) == ("none", [])
	# No pipeline file in the PR diff.
	assert heal.classify_pipeline_ownership(crash_file=None, changed_files=["README.md", ".github/workflows/ci.yml"], script_ref=SHA_B, head_sha=SHA_B) == ("none", [])
	many = [f"scripts/f{i}.sh" for i in range(30)]
	assert heal.classify_pipeline_ownership(crash_file=None, changed_files=many, script_ref=SHA_B, head_sha=SHA_B)[1] == many[: heal.PIPELINE_OWNERSHIP_FILES_MAX]


def test_classify_pipeline_ownership_cli() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-pipe-own-") as tmp_name:
		payload_file = Path(tmp_name) / "p.json"
		payload_file.write_text(json.dumps(_self_inflicted_payload(changed_files=["README.md", "scripts/opencode_helpers.sh"], crash_line=SANDBOX_226_LINE, script_ref=SHA_B)), encoding="utf-8")
		out = subprocess.run(["python3", str(SCRIPTS_DIR / "workflow_failure_heal.py"), "classify-pipeline-ownership", "--payload-json", str(payload_file)], capture_output=True, text=True, check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
		assert out.stdout.splitlines() == ["pr", "scripts/opencode_helpers.sh"]


def test_intake_pipeline_ownership_routes_fileless_failure_to_pr() -> None:
	# PR #4376 shape: every slot exits 226, the evidence names no file, the run
	# staged the PR head's scripts, and the PR changes a pipeline script.
	payload = _self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh", "README.md"], crash_line=SANDBOX_226_LINE, script_ref=SHA_B)
	assert "crash_file" not in payload
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert f"WORKFLOW_HEAL crash_ownership=pr crash_file=none basis=pipeline_files files=1 base={INTEGRATION_BRANCH}" in result.stdout
	assert "## Ownership facts" in prompt and "- Ownership: pr" in prompt and "- Ownership basis: pipeline files" in prompt
	assert "  - scripts/opencode_helpers.sh" in prompt and "  - README.md" not in prompt
	assert "classification_remapped" not in result.stdout
	assert "issues_created" not in state_after
	comments = [c for c in state_after["comments_posted"] if c["path"] == f"repos/{SELF_REPO}/issues/4174/comments"]
	assert len(comments) == 1
	body = comments[0]["body"]
	assert "failed without naming a file" in body and "`scripts/opencode_helpers.sh`" in body
	# No base diff is needed for this basis: no git fetch warning.
	assert "crash_ownership_base_diff_failed" not in result.stdout


def test_intake_pipeline_ownership_needs_the_pr_head_scripts() -> None:
	# The run staged another ref: no ownership, so the token is remapped.
	payload = _self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"], crash_line=SANDBOX_226_LINE)
	result, state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED)
	assert result.returncode == 0, result.stderr + result.stdout
	assert "crash_ownership=" not in result.stdout and "- Ownership:" not in prompt
	assert "classification_remapped from=pr-self-inflicted to=workflow-defect reason=ownership_none" in result.stdout
	assert len(state_after["issues_created"]) == 1
	# Routing off: the pipeline basis is not computed either.
	payload = _self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"], crash_line=SANDBOX_226_LINE, script_ref=SHA_B)
	result, _state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED, extra_env={"WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED": "false"})
	assert "crash_ownership=" not in result.stdout and "- Ownership:" not in prompt
	# Consumer reports never get it.
	payload = _self_inflicted_payload(changed_files=["scripts/opencode_helpers.sh"], crash_line=SANDBOX_226_LINE, script_ref=SHA_B, source_repo=CONSUMER_REPO)
	result, _state_after, prompt = _run_intake(payload, _self_inflicted_state(), diagnosis=DIAG_PR_SELF_INFLICTED)
	assert "crash_ownership=" not in result.stdout and "- Ownership:" not in prompt


def test_autofix_report_sends_ownership_facts() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-autofix-own-") as tmp_name:
		tmp = Path(tmp_name)
		work, state_file, env = _stage_autofix_report(tmp, comments=[{"body": AUTOFIX_NOOP_COMMENT}], flags={"AUTOFIX_EDITOR_EMPTY_NOOP": "true"})
		runtime = Path(env["RUNTIME_DIR"])
		(runtime / "failure_evidence_tail.txt").write_text(EDITOR_GUARD_CRASH_LINE + "\n", encoding="utf-8")
		(runtime / "pr_changed_files.txt").write_text("scripts/opencode_helpers.sh\nREADME.md\n", encoding="utf-8")
		env["PR_CHANGED_FILES_FILE"] = str(runtime / "pr_changed_files.txt")
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "WORKFLOW_HEAL_AUTOFIX_REPORT dispatched" in result.stdout
		payload = heal.validate_payload(_state(state_file)["dispatches"][0]["body"]["client_payload"]["report"])
		assert payload["base_branch"] == INTEGRATION_BRANCH
		assert payload["script_ref"] == SHA_A
		assert payload["changed_files"] == ["scripts/opencode_helpers.sh", "README.md"]
		assert payload["crash_file"] == "scripts/review_apply_fixes.sh"
		assert "failure_evidence_tail.txt" in payload["failure_evidence"]


def test_autofix_report_omits_ownership_flags_for_an_older_helper() -> None:
	with tempfile.TemporaryDirectory(prefix="heal-autofix-old-") as tmp_name:
		tmp = Path(tmp_name)
		work, _state_file, env = _stage_autofix_report(tmp, comments=[{"body": AUTOFIX_NOOP_COMMENT}], flags={"AUTOFIX_EDITOR_EMPTY_NOOP": "true"})
		# A helper that predates the ownership flags: its build-autofix-payload
		# rejects unknown flags, so the reporter must not send them. Strip the
		# new flags and their uses from the current helper to get one.
		helper = work / "scripts" / "workflow_failure_heal.py"
		text = helper.read_text(encoding="utf-8").replace("classify-crash-ownership", "classify-crash-owner-x")
		for line in (
			'\tp.add_argument("--base-branch", default="")\n',
			'\tp.add_argument("--script-ref", default="")\n',
			'\tp.add_argument("--changed-files-file", default="")\n',
			"\t\tbase_branch=args.base_branch or None,\n",
			"\t\tscript_ref=args.script_ref or None,\n",
			"\t\tchanged_files=_read_path_list(args.changed_files_file),\n",
		):
			assert text.count(line) == 1, line
			text = text.replace(line, "")
		helper.write_text(text, encoding="utf-8")
		result = _run(work / "scripts" / AUTOFIX_REPORT_SCRIPT.name, work, env)
		assert result.returncode == 0, result.stderr + result.stdout
		assert "WORKFLOW_HEAL_AUTOFIX_REPORT dispatched" in result.stdout


def test_heal_workflows_wire_self_inflicted_routing() -> None:
	intake = _yaml(INTAKE_WORKFLOW)
	assert intake["jobs"]["intake"]["env"]["WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED"] == "${{ vars.WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED || 'true' }}"
	review = _yaml(REVIEW_AUTOFIX_WORKFLOW)
	steps = [step for job in review["jobs"].values() for step in job.get("steps", []) if step.get("name") == "Report autofix failure to workflow failure heal"]
	assert len(steps) == 1
	assert steps[0]["env"]["PR_CHANGED_FILES_FILE"] == "${{ env.PR_CHANGED_FILES_FILE }}"
