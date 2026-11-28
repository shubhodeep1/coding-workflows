#!/usr/bin/env python3
"""Wiring contract for the daily issue/PR audit (#10153).

The audit is steps of the existing collect-logs job of
workflow-log-analysis.yml (no new job, §26), gated to the 06:00 UTC schedule
or a dispatch with issue_pr_audit=true, with GH_PAT only in the collect and
deliver steps and no credential in the model step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"
GATE = "!cancelled() && (github.event.schedule == '0 6 * * *' || github.event.inputs.issue_pr_audit == 'true') && (vars.ISSUE_PR_AUDIT_ENABLED || 'true') == 'true'"
AUDIT_STEPS = (
	"Install Codex CLI for the issue/PR audit",
	"Create Codex config for the issue/PR audit",
	"Collect issue and PR audit data",
	"Remove persisted checkout credential",
	"Run issue and PR audit model pass",
	"Deliver issue and PR audit report",
	"Upload issue and PR audit report",
	"Look up today's issue/PR audit Telegram report",
	"Send issue and PR audit Telegram report",
	"Record today's issue/PR audit Telegram report",
)


def _workflow() -> dict:
	yaml = pytest.importorskip("yaml")
	return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> dict[str, dict]:
	return {step.get("name"): step for step in _workflow()["jobs"]["collect-logs"]["steps"]}


def test_jobs_are_unchanged_and_the_audit_lives_in_collect_logs() -> None:
	data = _workflow()
	assert list(data["jobs"]) == ["collect-logs", "weekly-retro", "analyze-commit-notify", "deep-audit", "api-redundancy", "deliver-report"]
	steps = _steps()
	for name in AUDIT_STEPS:
		assert name in steps, name
		assert steps[name]["if"].startswith("${{ " + GATE), name
	for job_name, job in data["jobs"].items():
		if job_name == "collect-logs":
			continue
		assert not any("issue_pr_audit" in json.dumps(step) for step in job["steps"]), job_name


def test_audit_steps_run_after_the_daily_reports() -> None:
	names = [step.get("name") for step in _workflow()["jobs"]["collect-logs"]["steps"]]
	assert names.index("Record today's stage switch alert") < names.index("Install Claude Code CLI")
	assert names.index("Remove persisted checkout credential") < names.index("Run issue and PR audit model pass")
	assert names.index("Collect issue and PR audit data") < names.index("Remove persisted checkout credential")
	assert names.index("Run issue and PR audit model pass") < names.index("Deliver issue and PR audit report") < names.index("Send issue and PR audit Telegram report")


def test_secrets_are_step_scoped() -> None:
	steps = _steps()
	model = json.dumps(steps["Run issue and PR audit model pass"]["env"])
	for secret in ("GH_PAT", "TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "AI_COMMENT_SIGNING_SECRET", "github.token"):
		assert secret not in model, secret
	assert steps["Collect issue and PR audit data"]["env"]["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert steps["Deliver issue and PR audit report"]["env"]["REPORT_PUSH_TOKEN"] == "${{ secrets.GH_PAT }}"
	telegram = steps["Send issue and PR audit Telegram report"]["env"]
	assert "GH_TOKEN" not in telegram and not any("GH_PAT" in str(value) for value in telegram.values())
	assert telegram["TG_BOT_SECRET"] == "${{ secrets.TG_BOT_SECRET }}"
	for name in ("Collect issue and PR audit data", "Deliver issue and PR audit report"):
		assert "TG_BOT_SECRET" not in steps[name]["env"], name


def test_job_limits_permissions_and_dispatch_input() -> None:
	data = _workflow()
	job = data["jobs"]["collect-logs"]
	assert job["timeout-minutes"] == 100 and job["timeout-minutes"] <= 360
	assert job["permissions"]["id-token"] == "write"
	assert job["permissions"]["contents"] == "read"
	assert job["env"]["AI_ENGINE_ISSUE_PR_AUDIT"] == "${{ vars.AI_ENGINE_ISSUE_PR_AUDIT || '' }}"
	inputs = (data.get("on") or data.get(True))["workflow_dispatch"]["inputs"]
	assert inputs["issue_pr_audit"]["default"] == "false"


def test_runs_are_one_line_script_calls_and_inputs_never_reach_run() -> None:
	steps = _steps()
	for name in AUDIT_STEPS:
		run = steps[name].get("run")
		if run is None:
			continue
		assert "github.event.inputs" not in run and "${{" not in run, name
	for name, sub in (
		("Collect issue and PR audit data", "collect"),
		("Remove persisted checkout credential", "scrub-credentials"),
		("Run issue and PR audit model pass", "analyze"),
		("Deliver issue and PR audit report", "deliver"),
		("Send issue and PR audit Telegram report", "telegram"),
	):
		assert steps[name]["run"].strip() == f"bash scripts/issue_pr_audit_run.sh {sub}", name


def test_telegram_marker_cache_keys_match() -> None:
	steps = _steps()
	lookup = steps["Look up today's issue/PR audit Telegram report"]
	save = steps["Record today's issue/PR audit Telegram report"]
	assert lookup["uses"].startswith("actions/cache/restore@") and lookup["with"]["lookup-only"] is True
	assert save["uses"].startswith("actions/cache/save@")
	assert lookup["with"]["key"] == save["with"]["key"] == "issue-pr-audit-tg-${{ steps.issue_pr_audit_collect.outputs.day }}"
	assert lookup["with"]["path"] == save["with"]["path"]
	assert "steps.issue_pr_audit_tg.outputs.sent == 'true'" in save["if"]


def test_engine_role_and_registry_entries() -> None:
	config = json.loads((REPO_ROOT / ".github" / "ai" / "claude_engine.json").read_text(encoding="utf-8"))
	assert config["role_defaults"]["ISSUE_PR_AUDIT"]["profile"] == "read"
	switches = json.loads((REPO_ROOT / "scripts" / "stage_switches.json").read_text(encoding="utf-8"))
	assert "ISSUE_PR_AUDIT_ENABLED" in {entry["name"] for entry in switches["switches"]}
	assert re.search(r"\bISSUE_PR_AUDIT_ENABLED\b", (REPO_ROOT / "README.md").read_text(encoding="utf-8"))
