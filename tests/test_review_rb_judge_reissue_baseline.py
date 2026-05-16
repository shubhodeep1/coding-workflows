#!/usr/bin/env python3
"""Regression tests for trusted review-blocked baseline checkout gating."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
VALID_PR_NUMBER = "41"
VALID_SHA_PREFIX = "acdeff123456"
VALID_HEAD_OID = VALID_SHA_PREFIX + "7890abcdef1234567890abcdef12"
VALID_BRANCH = f"ai/reissue-pr-{VALID_PR_NUMBER}-baseline-{VALID_SHA_PREFIX}"


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


def _step_block_text(step_name: str) -> str:
	return "\n".join(_step_block(step_name))


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


def _run_shell_script(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	script_path = cwd / "__workflow_step_under_test.sh"
	script_path.write_text(script, encoding="utf-8")
	script_path.chmod(0o755)
	return subprocess.run(
		["bash", str(script_path)],
		cwd=str(cwd),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def _install_mock_gh(bin_dir: Path) -> None:
	gh_script = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
state_path.write_text(json.dumps(state), encoding="utf-8")

if args[:2] == ["pr", "view"]:
	exit_code = int(state.get("pr_view_exit_code", 0))
	if exit_code:
		print(state.get("pr_view_stderr", "gh pr view failed"), file=sys.stderr)
		sys.exit(exit_code)
	stdout_override = state.get("pr_view_stdout")
	if stdout_override is not None:
		sys.stdout.write(str(stdout_override))
		if stdout_override and not str(stdout_override).endswith("\n"):
			sys.stdout.write("\n")
		sys.exit(0)
	payload = {
		"state": state.get("pr_state", "CLOSED"),
		"headRefOid": state.get("pr_head_oid", ""),
	}
	print(json.dumps(payload))
	sys.exit(0)

print(json.dumps({}))
'''
	gh_path = bin_dir / "gh"
	gh_path.write_text(gh_script, encoding="utf-8")
	gh_path.chmod(0o755)


def _parse_github_output(path: Path) -> dict[str, str]:
	outputs: dict[str, str] = {}
	if not path.exists():
		return outputs
	for line in path.read_text(encoding="utf-8").splitlines():
		if "=" not in line:
			continue
		key, value = line.split("=", 1)
		outputs[key] = value
	return outputs


def _valid_issue_body(*, branch: str = VALID_BRANCH, pr_number: str = VALID_PR_NUMBER) -> str:
	return textwrap.dedent(
		f"""\
		Fix the remaining trust-boundary gap.

		prior_pr_baseline_branch: {branch}
		files_touched:
		  - .github/workflows/implement.yml
		  - tests/test_review_rb_judge_reissue_baseline.py

		---
		**Review-blocked reissue metadata**
		- Replaces: #2629 (PR #{pr_number} closed \u2014 approach rework)
		- Type: review-blocked-reissue
		"""
	)


def _run_baseline_resolver(
	issue_body: str,
	*,
	feature_enabled: str,
	issue_author_association: str = "OWNER",
	pr_state: str = "CLOSED",
	pr_head_oid: str = VALID_HEAD_OID,
	pr_view_exit_code: int = 0,
	pr_view_stdout: str | None = None,
	repo: str = "owner/repo",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], dict[str, object]]:
	script = _extract_run_script("Resolve trusted prior PR baseline branch")
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp = Path(tmpdir)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		_install_mock_gh(bin_dir)

		state_file = tmp / "gh_state.json"
		state_file.write_text(
			json.dumps(
				{
					"calls": [],
					"pr_state": pr_state,
					"pr_head_oid": pr_head_oid,
					"pr_view_exit_code": pr_view_exit_code,
					"pr_view_stdout": pr_view_stdout,
				}
			),
			encoding="utf-8",
		)
		output_file = tmp / "github_output.txt"

		env = os.environ.copy()
		env.update(
			{
				"PYTHONDONTWRITEBYTECODE": "1",
				"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
				"MOCK_GH_STATE_FILE": str(state_file),
				"GITHUB_OUTPUT": str(output_file),
				"GITHUB_REPOSITORY": repo,
				"GH_TOKEN": "test-token",
				"ISSUE_BODY": issue_body,
				"ISSUE_AUTHOR_ASSOCIATION": issue_author_association,
				"REISSUE_PRESERVE_BASELINE_ENABLED": feature_enabled,
			}
		)

		result = _run_shell_script(script, cwd=tmp, env=env)
		outputs = _parse_github_output(output_file)
		state = json.loads(state_file.read_text(encoding="utf-8"))
		return result, outputs, state


def test_workflow_contains_guarded_baseline_override_checkout_path() -> None:
	workflow = _workflow_text()
	resolver_step = _step_block_text("Resolve trusted prior PR baseline branch")
	resolver_script = _extract_run_script("Resolve trusted prior PR baseline branch")
	baseline_checkout_step = _step_block_text("Checkout prior PR baseline branch")
	fallback_checkout_step = _step_block_text("Checkout repository")
	log_step = _step_block_text("Log checkout ref")

	assert "REISSUE_PRESERVE_BASELINE_ENABLED: ${{ vars.REISSUE_PRESERVE_BASELINE_ENABLED || 'false' }}" in workflow
	assert "ISSUE_BODY: ${{ github.event.issue.body || '' }}" in resolver_step
	assert "ISSUE_AUTHOR_ASSOCIATION: ${{ github.event.issue.author_association || '' }}" in resolver_step
	assert "REISSUE_PRESERVE_BASELINE_ENABLED: ${{ vars.REISSUE_PRESERVE_BASELINE_ENABLED || 'false' }}" in resolver_step
	assert 're.fullmatch(r"ai/reissue-pr-(\\d+)-baseline-([0-9a-f]{12})", branch)' in resolver_script
	assert "issue author association is not trusted for review-blocked baseline reuse" in resolver_script
	assert "files_touched metadata is missing or empty" in resolver_script
	assert "timeout=30" in resolver_script
	assert "subprocess.SubprocessError" in resolver_script
	assert '"gh",' in resolver_script and '"pr",' in resolver_script and '"view",' in resolver_script
	assert '"state,headRefOid"' in resolver_script
	assert "continue-on-error: true" in baseline_checkout_step
	assert "ref: ${{ steps.baseline_refctx.outputs.sha || steps.baseline_refctx.outputs.branch }}" in baseline_checkout_step
	assert "steps.baseline_refctx.outputs.branch == '' || steps.checkout_baseline.outcome != 'success'" in fallback_checkout_step
	assert "ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}" in fallback_checkout_step
	assert 'baseline_status="${{ steps.baseline_refctx.outputs.status }}"' in log_step
	assert "Baseline override: ignored (${baseline_status})" in log_step
	assert "Baseline override: fallback to resolved ref after checkout failure for" in log_step
	assert "Resolved fallback ref:" in log_step


def test_resolver_accepts_valid_machine_generated_reissue_branch() -> None:
	for feature_enabled in ("true", "TRUE", "1"):
		result, outputs, state = _run_baseline_resolver(_valid_issue_body(), feature_enabled=feature_enabled)

		assert result.returncode == 0, result.stderr
		assert outputs == {"branch": VALID_BRANCH, "sha": VALID_HEAD_OID, "status": "accepted"}
		assert state["calls"] == [["pr", "view", VALID_PR_NUMBER, "--repo", "owner/repo", "--json", "state,headRefOid"]]
		assert f"Baseline override accepted: {VALID_BRANCH}" in result.stdout


def test_resolver_ignores_valid_body_when_feature_flag_is_disabled() -> None:
	result, outputs, state = _run_baseline_resolver(_valid_issue_body(), feature_enabled="false")

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "disabled"}
	assert state["calls"] == []
	assert "REISSUE_PRESERVE_BASELINE_ENABLED is disabled" in result.stdout


def test_resolver_rejects_untrusted_issue_authorship_without_github_lookup() -> None:
	result, outputs, state = _run_baseline_resolver(
		_valid_issue_body(),
		feature_enabled="true",
		issue_author_association="NONE",
	)

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "issue-author-untrusted"}
	assert state["calls"] == []
	assert "issue author association is not trusted" in result.stdout


def test_resolver_rejects_missing_or_duplicate_baseline_entries_without_github_lookup() -> None:
	for issue_body in (
		_valid_issue_body().replace(f"prior_pr_baseline_branch: {VALID_BRANCH}\n", ""),
		_valid_issue_body() + f"\nprior_pr_baseline_branch: {VALID_BRANCH}\n",
	):
		result, outputs, state = _run_baseline_resolver(issue_body, feature_enabled="true")

		assert result.returncode == 0, result.stderr
		assert outputs == {"branch": "", "status": "absent"}
		assert state["calls"] == []
		assert "does not contain exactly one prior_pr_baseline_branch entry" in result.stdout


def test_resolver_rejects_empty_files_touched_metadata_without_github_lookup() -> None:
	issue_body = _valid_issue_body().replace(
		"files_touched:\n  - .github/workflows/implement.yml\n  - tests/test_review_rb_judge_reissue_baseline.py\n",
		"files_touched:\n",
	)
	result, outputs, state = _run_baseline_resolver(issue_body, feature_enabled="true")

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "files-missing"}
	assert state["calls"] == []
	assert "files_touched metadata is missing or empty" in result.stdout


def test_resolver_rejects_missing_reissue_metadata_header_without_github_lookup() -> None:
	issue_body = _valid_issue_body().replace(
		"\n---\n**Review-blocked reissue metadata**\n- Replaces: #2629 (PR #41 closed — approach rework)\n- Type: review-blocked-reissue\n",
		"\n---\n",
	)
	result, outputs, state = _run_baseline_resolver(issue_body, feature_enabled="true")

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "metadata-missing"}
	assert state["calls"] == []
	assert "review-blocked reissue metadata header is missing" in result.stdout


def test_resolver_rejects_metadata_injection_outside_footer_without_github_lookup() -> None:
	issue_body = textwrap.dedent(
		f"""\
		Fix the remaining trust-boundary gap.

		prior_pr_baseline_branch: {VALID_BRANCH}
		files_touched:
		  - .github/workflows/implement.yml

		---
		**Review-blocked reissue metadata**
		- Type: review-blocked-reissue

		This prose is not part of the machine-generated metadata footer.
		- Replaces: #2629 (PR #{VALID_PR_NUMBER} closed \u2014 approach rework)
		"""
	)
	result, outputs, state = _run_baseline_resolver(issue_body, feature_enabled="true")

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "metadata-missing"}
	assert state["calls"] == []
	assert "review-blocked reissue metadata footer is incomplete" in result.stdout


def test_resolver_rejects_manual_injected_refs_without_github_lookup() -> None:
	for manual_ref in (
		"main",
		"feature/not-a-review-blocked-baseline",
		VALID_HEAD_OID,
	):
		result, outputs, state = _run_baseline_resolver(
			_valid_issue_body(branch=manual_ref),
			feature_enabled="true",
		)

		assert result.returncode == 0, result.stderr
		assert outputs == {"branch": "", "status": "untrusted"}
		assert state["calls"] == []
		assert "not in the trusted review-blocked format" in result.stdout


def test_resolver_rejects_mismatched_reissue_footer_without_github_lookup() -> None:
	result, outputs, state = _run_baseline_resolver(
		_valid_issue_body(pr_number="99"),
		feature_enabled="true",
	)

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "pr-mismatch"}
	assert state["calls"] == []
	assert "PR number does not match reissue metadata" in result.stdout


def test_resolver_rejects_missing_repository_context_without_github_lookup() -> None:
	result, outputs, state = _run_baseline_resolver(
		_valid_issue_body(),
		feature_enabled="true",
		repo="",
	)

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "repo-missing"}
	assert state["calls"] == []
	assert "repository context unavailable for PR validation" in result.stdout


def test_resolver_rejects_non_closed_prior_pr_states_after_lookup() -> None:
	for pr_state in ("OPEN", "MERGED"):
		result, outputs, state = _run_baseline_resolver(
			_valid_issue_body(),
			feature_enabled="true",
			pr_state=pr_state,
		)

		assert result.returncode == 0, result.stderr
		assert outputs == {"branch": "", "status": "pr-open"}
		assert state["calls"] == [["pr", "view", VALID_PR_NUMBER, "--repo", "owner/repo", "--json", "state,headRefOid"]]
		assert "not in the closed review-blocked state" in result.stdout


def test_resolver_rejects_pr_head_sha_mismatch_after_lookup() -> None:
	mismatched_head_oid = "bbbbbb1234567890abcdef1234567890abcdef12"
	result, outputs, state = _run_baseline_resolver(
		_valid_issue_body(),
		feature_enabled="true",
		pr_head_oid=mismatched_head_oid,
	)

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "head-mismatch"}
	assert state["calls"] == [["pr", "view", VALID_PR_NUMBER, "--repo", "owner/repo", "--json", "state,headRefOid"]]
	assert "does not match the closed PR head SHA" in result.stdout


def test_resolver_fails_open_on_github_lookup_failure() -> None:
	result, outputs, state = _run_baseline_resolver(
		_valid_issue_body(),
		feature_enabled="true",
		pr_view_exit_code=1,
	)

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "lookup-failed"}
	assert state["calls"] == [["pr", "view", VALID_PR_NUMBER, "--repo", "owner/repo", "--json", "state,headRefOid"]]
	assert "failed to verify prior PR baseline branch against GitHub" in result.stdout


def test_resolver_fails_open_on_malformed_github_lookup_response() -> None:
	result, outputs, state = _run_baseline_resolver(
		_valid_issue_body(),
		feature_enabled="true",
		pr_view_stdout="not-json",
	)

	assert result.returncode == 0, result.stderr
	assert outputs == {"branch": "", "status": "lookup-failed"}
	assert state["calls"] == [["pr", "view", VALID_PR_NUMBER, "--repo", "owner/repo", "--json", "state,headRefOid"]]
	assert "GitHub PR validation response was malformed" in result.stdout


def main() -> int:
	# Direct `python3 tests/<file>.py` entrypoint — CI runs tests via this
	# pattern, so avoid a pytest-only harness that would silently skip or fail.
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
