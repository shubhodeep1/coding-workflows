#!/usr/bin/env python3
"""Regression guard: judge prompt reference assets are staged into the bundle.

Reported from consumer run 28009116022 (shubhodeep1/tele-funtoken-msg-scoring,
PR #3434): the review-blocked judge degraded with

  ERROR: Reference file for placeholder 'REFERENCE_OUTPUT_CONTRACT' not found:
  .../coding-workflows-runtime-.../prompts/references/output-contract.txt

`mode-judge-review-blocked.txt` (and `mode-judge-interim.txt`,
`review-consolidator.txt`, `review-reviewer-checklist.txt`) embed REFERENCE_*
placeholders that render_prompt.py hydrates from
`<prompt-root>/prompts/references/<name>.txt`. review_rb_judge.sh and
review_run_judge_interim.sh `cd "${SUPPORT_ROOT_DIR}"` before rendering, so the
prompt root is SUPPORT_ROOT_DIR and the only location render_prompt.py can
resolve is SUPPORT_PROMPTS_DIR/references/ — the cwd / .codex-workflow-src
fallbacks resolve under SUPPORT_ROOT_DIR, where the support checkout does not
exist. stage_review_runtime_support() never staged that subdirectory, so every
review-blocked / interim judge render hit the unhydrated-reference error.

Two tests pin the fix:

1. A static contract on scripts/stage_workflow_support.sh: it stages
   output-contract.txt and severity-classification.txt into
   ${SUPPORT_PROMPTS_DIR}/references/ with the .codex-workflow-src ->
   .codex-workflow-src-main fallback, and warns (does not exit 1) when a
   reference is unavailable so older support refs degrade gracefully.

2. A behavioural test that reproduces the bundle layout review_rb_judge.sh
   builds, renders the review-blocked prompt the same way (cd SUPPORT_ROOT_DIR
   then render_prompt.sh), and asserts the REFERENCE_OUTPUT_CONTRACT placeholder
   is hydrated when references/ is staged — with a negative control proving the
   exact consumer error fires when it is absent.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE_HELPER = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
REVIEW_BLOCKED_PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
REVIEW_BLOCKED_CONTRACT = REPO_ROOT / "prompts" / "contracts" / "mode-judge-review-blocked.yml"
REFERENCES_DIR = REPO_ROOT / "prompts" / "references"
JUDGE_REFERENCE_ASSETS = ("output-contract.txt", "severity-classification.txt")


def _stage_helper_text() -> str:
	return STAGE_HELPER.read_text(encoding="utf-8")


def test_stage_workflow_support_stages_judge_reference_assets() -> None:
	"""stage_review_runtime_support() must stage the judge REFERENCE_* assets."""
	stage_helper = _stage_helper_text()

	# The reference files the staged judge / reviewer prompts hydrate must exist.
	for reference_asset in JUDGE_REFERENCE_ASSETS:
		assert (REFERENCES_DIR / reference_asset).is_file(), (
			f"missing reference asset prompts/references/{reference_asset}"
		)

	# The staging loop pins both assets and the standard ref -> main fallback.
	assert (
		"for reference_asset in output-contract.txt severity-classification.txt; do"
		in stage_helper
	)
	assert (
		'if [ ! -f "${SUPPORT_PROMPTS_DIR}/references/${reference_asset}" ]; then'
		in stage_helper
	)
	assert (
		'src=".codex-workflow-src/prompts/references/${reference_asset}"' in stage_helper
	)
	assert (
		'src=".codex-workflow-src-main/prompts/references/${reference_asset}"'
		in stage_helper
	)
	assert (
		'install -m 0644 "${src}" "${SUPPORT_PROMPTS_DIR}/references/${reference_asset}"'
		in stage_helper
	)
	# Graceful degrade (warn, never exit 1) so older support refs still bootstrap.
	assert (
		"review-blocked/interim judge will degrade (REFERENCE_* placeholder left unhydrated)"
		in stage_helper
	)


def _render_review_blocked_prompt(*, stage_references: bool) -> subprocess.CompletedProcess[str]:
	"""Render the review-blocked prompt the way review_rb_judge.sh does.

	Builds the runtime bundle layout (scripts/ + prompts/, optionally
	prompts/references/), cds into the bundle root, and renders the staged
	prompt — mirroring review_rb_judge.sh:960-961.
	"""
	with tempfile.TemporaryDirectory(prefix="rb-judge-reference-staging-") as td:
		support_root = Path(td) / "coding-workflows-runtime-1-1"
		scripts_dir = support_root / "scripts"
		prompts_dir = support_root / "prompts"
		contracts_dir = prompts_dir / "contracts"
		scripts_dir.mkdir(parents=True)
		prompts_dir.mkdir(parents=True)
		contracts_dir.mkdir(parents=True)

		# Stage the renderer + prompt + contract exactly as the bundle does.
		for src in (RENDER_PROMPT_PY, RENDER_PROMPT_SH):
			(scripts_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
		(scripts_dir / "render_prompt.sh").chmod(0o755)
		(prompts_dir / REVIEW_BLOCKED_PROMPT.name).write_text(
			REVIEW_BLOCKED_PROMPT.read_text(encoding="utf-8"), encoding="utf-8"
		)
		(contracts_dir / REVIEW_BLOCKED_CONTRACT.name).write_text(
			REVIEW_BLOCKED_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
		)

		if stage_references:
			references_dir = prompts_dir / "references"
			references_dir.mkdir()
			for reference_asset in JUDGE_REFERENCE_ASSETS:
				(references_dir / reference_asset).write_text(
					(REFERENCES_DIR / reference_asset).read_text(encoding="utf-8"),
					encoding="utf-8",
				)

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		return subprocess.run(
			[
				"bash",
				str(scripts_dir / "render_prompt.sh"),
				str(prompts_dir / REVIEW_BLOCKED_PROMPT.name),
			],
			cwd=str(support_root),
			env=env,
			capture_output=True,
			text=True,
			timeout=120,
		)


def test_review_blocked_prompt_hydrates_references_from_bundle_layout() -> None:
	"""With references staged, the review-blocked render hydrates the placeholder."""
	result = _render_review_blocked_prompt(stage_references=True)
	assert result.returncode == 0, (
		f"render failed unexpectedly: rc={result.returncode}\nstderr={result.stderr}"
	)
	assert "Reference file for placeholder" not in result.stderr
	# Placeholder must be hydrated (no literal {{REFERENCE_OUTPUT_CONTRACT}} left).
	assert "{{REFERENCE_OUTPUT_CONTRACT}}" not in result.stdout


def test_review_blocked_prompt_fails_without_staged_references() -> None:
	"""Negative control: the exact consumer error fires when references are absent.

	This proves the behavioural test above is load-bearing — if a regression
	drops the references staging, the render fails with the reported error.
	"""
	result = _render_review_blocked_prompt(stage_references=False)
	assert result.returncode != 0
	assert (
		"Reference file for placeholder 'REFERENCE_OUTPUT_CONTRACT' not found"
		in result.stderr
	)
