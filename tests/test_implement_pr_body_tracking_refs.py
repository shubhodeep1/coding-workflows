#!/usr/bin/env python3
"""Workflow-contract tests for implement.yml PR body composition.

These tests execute the extracted implement-step `run:` block so the
assertions stay pinned to the production workflow logic that builds the
child PR body and runs the tracking-issue auto-close lint.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
STEP_NAME = "Pre-flight — lint PR title/body for auto-close keywords against tracking issues"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _step_block(step_name: str) -> list[str]:
	lines = _workflow_text().splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return lines[idx:end]
	raise AssertionError(f"Step not found in workflow: {step_name}")


def _extract_run_script(step_name: str) -> str:
	block = _step_block(step_name)
	run_idx = -1
	run_indent = 0
	for i, line in enumerate(block):
		if line.strip() == "run: |":
			run_idx = i
			run_indent = len(line) - len(line.lstrip(" "))
			break
	if run_idx == -1:
		raise AssertionError(f"Step has no run block: {step_name}")

	script_lines: list[str] = []
	for line in block[run_idx + 1 :]:
		if line.strip() == "":
			script_lines.append("")
			continue
		indent = len(line) - len(line.lstrip(" "))
		if indent <= run_indent:
			break
		prefix = " " * (run_indent + 2)
		if line.startswith(prefix):
			script_lines.append(line[len(prefix) :])
		else:
			script_lines.append(line.lstrip())
	return "\n".join(script_lines).rstrip() + "\n"


def _install_mock_gh(bin_dir: Path) -> None:
	gh_path = bin_dir / "gh"
	gh_path.write_text(
		"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
labels_by_issue = json.loads(os.environ.get("MOCK_GH_ISSUE_LABELS_JSON", "{}"))

if args[:2] == ["issue", "view"] and len(args) >= 3:
    print(json.dumps(labels_by_issue.get(args[2], [])))
    raise SystemExit(0)

print(f"unexpected gh args: {args}", file=sys.stderr)
raise SystemExit(1)
""",
		encoding="utf-8",
	)
	gh_path.chmod(0o755)


def _run_preflight(*, issue_meta: dict, issue_url: str) -> str:
	script = _extract_run_script(STEP_NAME)
	with tempfile.TemporaryDirectory() as td:
		tmp = Path(td)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		_install_mock_gh(bin_dir)

		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		issue_meta_file = runtime_dir / "issue_meta.json"
		issue_meta_file.write_text(json.dumps(issue_meta), encoding="utf-8")

		workflow_script = tmp / "step.sh"
		workflow_script.write_text(script, encoding="utf-8")
		workflow_script.chmod(0o755)

		env = os.environ.copy()
		env.update(
			{
				"GH_TOKEN": "test-token",
				"GITHUB_REPOSITORY": "owner/repo",
				"ISSUE_META_FILE": str(issue_meta_file),
				"ISSUE_NUMBER": "123",
				"ISSUE_URL": issue_url,
				"MOCK_GH_ISSUE_LABELS_JSON": json.dumps({"123": []}),
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"PYTHONDONTWRITEBYTECODE": "1",
				"RUNTIME_DIR": str(runtime_dir),
			}
		)

		result = subprocess.run(
			["bash", str(workflow_script)],
			cwd=str(REPO_ROOT),
			env=env,
			text=True,
			capture_output=True,
			timeout=60,
		)
		assert result.returncode == 0, result.stderr or result.stdout

		return (runtime_dir / "pr-body-lint" / "body.txt").read_text(encoding="utf-8")


def test_orchestrator_managed_issue_appends_non_closing_tracking_ref() -> None:
	body = _run_preflight(
		issue_meta={
			"body": "Child issue context\n\n- Tracking issue: #829\n",
			"labels": [{"name": "ai:orchestrator-managed"}],
		},
		issue_url="https://github.com/owner/repo/issues/123",
	)

	assert body == (
		"Automated implementation. Closes https://github.com/owner/repo/issues/123\n"
		"Refs #829\n"
	)


def test_non_orchestrator_issue_keeps_close_only_body_even_with_tracking_footer() -> None:
	body = _run_preflight(
		issue_meta={
			"body": "Child issue context\n\n- Tracking issue: #829\n",
			"labels": [{"name": "bug"}],
		},
		issue_url="https://github.com/owner/repo/issues/123",
	)

	assert body == "Automated implementation. Closes https://github.com/owner/repo/issues/123\n"
