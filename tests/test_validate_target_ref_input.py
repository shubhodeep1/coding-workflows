"""Contract for the optional `target_ref` validate input.

/implement-plan-claude validates its project branch before the branch merges
into the default branch, so the reusable workflow and both dispatch wrappers
accept an explicit branch. Empty keeps the tracking-issue / default-branch
resolution unchanged.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REUSABLE = ROOT / ".github" / "workflows" / "validate.yml"
WRAPPERS = (
	ROOT / ".github" / "workflows" / "internal-validate.yml",
	ROOT / "workflow-templates" / "ai-validate.yml",
)


def _load(path: Path) -> dict:
	workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
	# YAML 1.1 reads an unquoted `on:` key as boolean True.
	if True in workflow:
		workflow["on"] = workflow.pop(True)
	return workflow


def test_reusable_validate_declares_optional_target_ref():
	target_ref = _load(REUSABLE)["on"]["workflow_call"]["inputs"]["target_ref"]
	assert target_ref["required"] is False
	assert target_ref["default"] == ""
	assert target_ref["type"] == "string"


def test_checkout_prefers_target_ref_then_integration_ref_then_default():
	text = REUSABLE.read_text(encoding="utf-8")
	expression = "${{ inputs.target_ref || steps.refctx.outputs.ref || github.event.repository.default_branch }}"
	assert f"ref: {expression}" in text
	assert f"VALIDATE_RESOLVED_REF: {expression}" in text
	# No checkout path still ignores the explicit input.
	assert "ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}" not in text


def test_wrappers_forward_target_ref():
	for wrapper in WRAPPERS:
		workflow = _load(wrapper)
		dispatch_input = workflow["on"]["workflow_dispatch"]["inputs"]["target_ref"]
		assert dispatch_input["default"] == "", wrapper
		assert dispatch_input["required"] is False, wrapper
		assert workflow["jobs"]["validate"]["with"]["target_ref"] == "${{ inputs.target_ref || '' }}", wrapper


def test_consumer_wrapper_has_no_pr_number_input():
	# /implement-plan-claude passes -f pr_number=0 only to internal-validate.yml;
	# the consumer wrapper rejects unknown dispatch inputs.
	assert "pr_number" not in _load(WRAPPERS[1])["on"]["workflow_dispatch"]["inputs"]
