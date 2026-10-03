#!/usr/bin/env python3
"""Inline the step bodies that review_autofix.yml sources from scripts/.

GitHub refuses to start runs for a workflow file over 512,000 bytes: the
push that crosses the limit gets a zero-job "workflow file issue" run named
after the file path. To keep ``.github/workflows/review_autofix.yml`` under
that limit, the largest ``run:`` bodies live in
``scripts/review_autofix_step_*.sh``. Each of those steps keeps its
``name:``, ``if:``, ``env:`` and ``continue-on-error:`` in the workflow and
its ``run:`` block is a short wrapper that resolves the script (staged
support bundle, then the verified ``.codex-workflow-src/scripts``) and
``source``s it in the step shell.

Contract tests that grep or execute those step bodies read the workflow
through :func:`expanded_review_autofix_text`, which swaps each wrapper back
for the script body at the original indentation. The result is the workflow
as it read before the move, so the tests keep checking the exact code that
runs.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEW_AUTOFIX_STEP_SCRIPTS_DIR = REPO_ROOT / "scripts"

# Step name -> script under scripts/, and whether a missing script fails the
# step ("error") or skips it ("warning"). The two always() steps fail open
# because they also run after support staging itself failed.
REVIEW_AUTOFIX_STEP_SCRIPTS: dict[str, tuple[str, str]] = {
	"Pre-review deterministic merge-topology gate": ("review_autofix_step_merge_topology_gate.sh", "error"),
	"Detect editor-claimed-but-uncommitted changes": ("review_autofix_step_editor_uncommitted_changes.sh", "error"),
	"Detect merge conflicts": ("review_autofix_step_detect_merge_conflicts.sh", "error"),
	"Post partial finalize comment and persist runtime marker": ("review_autofix_step_partial_finalize.sh", "warning"),
	"Append review pipeline iteration summary": ("review_autofix_step_iteration_summary.sh", "warning"),
}

_WRAPPER_START_RE = re.compile(
	r'^(?P<indent> *)REVIEW_AUTOFIX_STEP_SCRIPT="\$\{SUPPORT_SCRIPTS_DIR:-\}/(?P<script>review_autofix_step_[a-z0-9_]+\.sh)"$'
)
_WRAPPER_END = 'source "${REVIEW_AUTOFIX_STEP_SCRIPT}"'


def review_autofix_step_script_body(script_name: str) -> str:
	"""Return a step script without its leading comment header.

	The header is the shebang plus the comment block before the first
	non-comment line; everything after it is the step body verbatim.
	"""
	lines = (REVIEW_AUTOFIX_STEP_SCRIPTS_DIR / script_name).read_text(encoding="utf-8").split("\n")
	idx = 0
	while idx < len(lines) and lines[idx].startswith("#"):
		idx += 1
	body = "\n".join(lines[idx:])
	return body[:-1] if body.endswith("\n") else body


def expand_review_autofix_step_scripts(workflow_text: str) -> str:
	"""Replace every step-script wrapper in *workflow_text* with its body."""
	lines = workflow_text.split("\n")
	out: list[str] = []
	idx = 0
	while idx < len(lines):
		match = _WRAPPER_START_RE.match(lines[idx])
		if not match:
			out.append(lines[idx])
			idx += 1
			continue
		indent = match.group("indent")
		end = idx
		while end < len(lines) and lines[end] != indent + _WRAPPER_END:
			end += 1
		if end >= len(lines):
			raise AssertionError(f"Unterminated step-script wrapper for {match.group('script')}")
		for body_line in review_autofix_step_script_body(match.group("script")).split("\n"):
			out.append(indent + body_line if body_line else "")
		idx = end + 1
	return "\n".join(out)


def expanded_review_autofix_text() -> str:
	"""review_autofix.yml with each moved step body inlined again."""
	return expand_review_autofix_step_scripts(REVIEW_AUTOFIX_WORKFLOW_PATH.read_text(encoding="utf-8"))
