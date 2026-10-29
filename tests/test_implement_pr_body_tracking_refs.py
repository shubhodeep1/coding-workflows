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
CREATE_PR_STEP_NAME = "Create Pull Request"


def _workflow_text() -> str:
	return IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")


def _step_text(step_name: str) -> str:
	return "\n".join(_step_block(step_name)) + "\n"


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


def _run_preflight(*, issue_meta: dict, issue_url: str, plan_text: str | None = None, extra_env: dict | None = None) -> str:
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

		plan_file = runtime_dir / "implementation_plan.txt"
		if plan_text is not None:
			plan_file.write_text(plan_text, encoding="utf-8")

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
				"PLAN_FILE": str(plan_file),
			}
		)
		env.pop("IMPLEMENT_STAGED_SUPPORT_RUN_DIR", None)
		env.pop("PARTIAL_DELIVERY_TRACKING_ENABLED", None)
		env.update(extra_env or {})

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


def test_create_pr_step_uses_the_linted_body_artifact() -> None:
	step = _step_text(CREATE_PR_STEP_NAME)
	assert 'PR_BODY_FILE="${RUNTIME_DIR}/pr-body-lint/body.txt"' in step
	assert '--body-file "${PR_BODY_FILE}"' in step


# Issue #7657: a plan that defers part of the issue gives a non-closing PR.
PARTIAL_PLAN = (
	"Implementation Plan\n\n## Files likely to change\n- `a.py`\n\n"
	"## Deferred scope\n- Implement the member homepage (Phase 2).\n\n"
	"## Functions/modules to implement\n1. thing\n"
)
FULL_PLAN = "Implementation Plan\n\n## Files likely to change\n- `a.py`\n"
PLAIN_ISSUE = {"body": "Implement both homepages.\n", "labels": [{"name": "bug"}]}
ISSUE_URL = "https://github.com/owner/repo/issues/123"


def test_partial_plan_uses_refs_and_embeds_the_deferred_items() -> None:
	body = _run_preflight(issue_meta=PLAIN_ISSUE, issue_url=ISSUE_URL, plan_text=PARTIAL_PLAN)
	# The issue URL without a closing keyword keeps the issue open while the
	# review pipeline's repo-scoped matcher still finds the linked issue.
	assert body.startswith(f"Automated implementation. Refs {ISSUE_URL}\n")
	assert "Closes" not in body
	assert "<!-- ai:partial-delivery:v1 issue=123 -->" in body
	assert "- Implement the member homepage (Phase 2)." in body


def test_plan_without_deferred_scope_still_closes_the_issue() -> None:
	body = _run_preflight(issue_meta=PLAIN_ISSUE, issue_url=ISSUE_URL, plan_text=FULL_PLAN)
	assert body == f"Automated implementation. Closes {ISSUE_URL}\n"
	malformed = FULL_PLAN + "\n## Deferred scope\n\n## Next\n"
	body = _run_preflight(issue_meta=PLAIN_ISSUE, issue_url=ISSUE_URL, plan_text=malformed)
	assert body == f"Automated implementation. Closes {ISSUE_URL}\n"


def test_managed_issue_and_disabled_switch_ignore_the_deferred_scope() -> None:
	managed = {"body": "x\n- Tracking issue: #829\n", "labels": [{"name": "ai:orchestrator-managed"}]}
	body = _run_preflight(issue_meta=managed, issue_url=ISSUE_URL, plan_text=PARTIAL_PLAN)
	assert body == f"Automated implementation. Closes {ISSUE_URL}\nRefs #829\n"
	marker_only = {"body": "Managed by: AI Orchestrator\n", "labels": []}
	body = _run_preflight(issue_meta=marker_only, issue_url=ISSUE_URL, plan_text=PARTIAL_PLAN)
	assert body == f"Automated implementation. Closes {ISSUE_URL}\n"
	for value in ("false", "maybe"):
		body = _run_preflight(
			issue_meta=PLAIN_ISSUE, issue_url=ISSUE_URL, plan_text=PARTIAL_PLAN,
			extra_env={"PARTIAL_DELIVERY_TRACKING_ENABLED": value},
		)
		assert body == f"Automated implementation. Closes {ISSUE_URL}\n", value


def test_preflight_reads_the_switch_from_a_repository_variable() -> None:
	step = _step_text(STEP_NAME)
	assert "PARTIAL_DELIVERY_TRACKING_ENABLED: ${{ vars.PARTIAL_DELIVERY_TRACKING_ENABLED || 'true' }}" in step
	assert " partial_delivery.py " in _workflow_text()


def main() -> int:
	test_orchestrator_managed_issue_appends_non_closing_tracking_ref()
	test_non_orchestrator_issue_keeps_close_only_body_even_with_tracking_footer()
	test_create_pr_step_uses_the_linted_body_artifact()
	test_partial_plan_uses_refs_and_embeds_the_deferred_items()
	test_plan_without_deferred_scope_still_closes_the_issue()
	test_managed_issue_and_disabled_switch_ignore_the_deferred_scope()
	test_preflight_reads_the_switch_from_a_repository_variable()
	print("OK: implement PR body tracking refs contract holds")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
