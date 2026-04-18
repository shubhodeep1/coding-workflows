#!/usr/bin/env python3
"""Tests for scripts/ai_labels.py contract and repair behavior."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ai_labels


CONTRACT_PATH = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
HELPER_PATH = REPO_ROOT / "scripts" / "label_helpers.sh"


FAILURE_LABELS = [
	"ai:clarify-failed",
	"ai:clarify-respond-failed",
	"ai:plan-failed",
	"ai:implement-diagnose-failed",
	"ai:review-autofix-failed",
	"ai:validate-failed",
	"ai:integration-judge-failed",
	"ai:log-analysis-failed",
	"ai:memory-maintenance-failed",
]


def _repair(issue_labels: str) -> dict[str, object]:
	args = argparse.Namespace(contract_file=str(CONTRACT_PATH), issue_labels=issue_labels)
	output_lines: list[str] = []
	original = ai_labels._print_json
	try:
		ai_labels._print_json = lambda payload: output_lines.append(json.dumps(payload, ensure_ascii=True, sort_keys=True))
		rc = ai_labels.cmd_repair_labels(args)
	finally:
		ai_labels._print_json = original
	assert rc == 0
	assert len(output_lines) == 1
	return json.loads(output_lines[0])


def _extract_helper_catalog() -> tuple[set[str], set[str]]:
	helper_body = HELPER_PATH.read_text(encoding="utf-8")
	colors_block_match = re.search(r"declare -A _AI_LABEL_COLORS=\((.*?)\n\)", helper_body, flags=re.S)
	descs_block_match = re.search(r"declare -A _AI_LABEL_DESCS=\((.*?)\n\)", helper_body, flags=re.S)
	assert colors_block_match, "Could not parse _AI_LABEL_COLORS from label_helpers.sh"
	assert descs_block_match, "Could not parse _AI_LABEL_DESCS from label_helpers.sh"
	colors_keys = set(re.findall(r'\["([^"]+)"\]=', colors_block_match.group(1)))
	descs_keys = set(re.findall(r'\["([^"]+)"\]=', descs_block_match.group(1)))
	return colors_keys, descs_keys


def test_contract_includes_failure_phase_group_and_members():
	contract = ai_labels.load_label_contract(CONTRACT_PATH)
	labels = set(contract["labels"].keys())
	assert set(FAILURE_LABELS).issubset(labels)

	groups = {group["name"]: group for group in contract["phase_groups"]}
	assert "failure_phase" in groups
	failure_group = groups["failure_phase"]
	assert "fallback" not in failure_group
	assert failure_group["members"] == FAILURE_LABELS


def test_repair_labels_enforces_failure_phase_exclusivity():
	payload = _repair("ai:planning,ai:clarify-failed,ai:plan-failed,ai:log-analysis-failed")
	assert payload["add"] == []
	assert payload["remove"] == ["ai:clarify-failed", "ai:plan-failed"]


def test_repair_labels_does_not_add_failure_phase_when_absent():
	payload = _repair("ai:planning")
	assert payload["add"] == []
	assert payload["remove"] == []


def test_contract_labels_match_helper_color_and_description_catalogs():
	contract = ai_labels.load_label_contract(CONTRACT_PATH)
	contract_labels = set(contract["labels"].keys())
	color_keys, desc_keys = _extract_helper_catalog()
	assert color_keys == contract_labels
	assert desc_keys == contract_labels


def test_contract_validate_command_succeeds_with_failure_phase():
	args = argparse.Namespace(contract_file=str(CONTRACT_PATH))
	output_lines: list[str] = []
	original = ai_labels._print_json
	try:
		ai_labels._print_json = lambda payload: output_lines.append(json.dumps(payload, ensure_ascii=True, sort_keys=True))
		rc = ai_labels.cmd_contract_validate(args)
	finally:
		ai_labels._print_json = original
	assert rc == 0
	payload = json.loads(output_lines[0])
	assert payload["ok"] is True
	assert payload["schema_version"] == "label_contract.v1"
	assert set(FAILURE_LABELS).issubset(set(payload["labels"]))
