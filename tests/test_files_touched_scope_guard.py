#!/usr/bin/env python3
"""Tests for the files_touched scope-enforcement guard.

Three layers, mirroring how the destructive-commit guard is validated:

  1. Unit tests of scripts/files_touched_scope_guard.py (the parser + matcher),
     including the real incident from orchestrator project #244 / issue #254
     ("frontend-send-status") that motivated the guard.
  2. Behavioral extract-and-run of the real plan/implement/review guard
     fragments against a synthetic staged index, so block / override / skip
     behaviour is validated against production code rather than a
     reimplementation.
  3. Static assertions that the guard is wired in at all guard sites and into
     the alert / failure-gate / env / label / redispatch-refusal plumbing.

Runnable either under pytest or directly as `python3 tests/<this file>.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import files_touched_scope_guard as guard  # noqa: E402

IMPLEMENT = REPO_ROOT / ".github" / "workflows" / "implement.yml"
IMPLEMENT_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "implement_commit_changes.sh"
REVIEW_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "review_commit_changes.sh"
REVIEW_STAGE_SCRIPT = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
REVIEW_RB_JUDGE_SCRIPT = REPO_ROOT / "scripts" / "review_rb_judge.sh"
REVIEW_CONFLICT_PREPARE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
REVIEW_CONFLICT_RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
IMPLEMENT_GUARD_HANDLER = REPO_ROOT / "scripts" / "implement_handle_guard_block.sh"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "files_touched_scope_guard.py"
LABEL_CONTRACT = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
LABEL_HELPERS = REPO_ROOT / "scripts" / "label_helpers.sh"


def _body(*entries: str) -> str:
	lines = ["Implement the task. Stay inside the files_touched list.", "", "files_touched:"]
	lines.extend(f"  - {entry}" for entry in entries)
	return "\n".join(lines) + "\n"


def _generated_advisory_body(path: str = "src/security.py", audited_commit: str = "b" * 40) -> str:
	return (
		"Generated advisory.\n\n---\n"
		"**Generated security advisory metadata**\n"
		"- Schema: `generated-security-advisory.v1`\n"
		f"- Waiver match key: `sha256:{'a' * 64}`\n"
		f"- Audited commit: `{audited_commit}`\n"
		f"- Cited file: `{path}`\n"
		"files_touched:\n"
		f"  - {path}\n"
	)


# --------------------------------------------------------------------------
# Layer 1 — parser + matcher unit tests
# --------------------------------------------------------------------------


def test_all_within_allowlist_passes() -> None:
	status, _allow, oos = guard.evaluate(_body("frontend/", "README.md"), ["frontend/app.tsx", "README.md"])
	assert status == guard.STATUS_IN_SCOPE
	assert oos == []


def test_out_of_allowlist_modification_blocks() -> None:
	status, _allow, oos = guard.evaluate(_body("frontend/**"), ["frontend/app.tsx", "layerzero.config.ts"])
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert oos == ["layerzero.config.ts"]


def test_out_of_allowlist_addition_blocks_compiled_twin() -> None:
	# The incident: a stray compiled .js twin swept in alongside in-scope work.
	status, _allow, oos = guard.evaluate(_body("frontend/**"), ["frontend/app.tsx", "tasks/send.js"])
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert oos == ["tasks/send.js"]


def test_missing_files_touched_skips() -> None:
	status, allow, oos = guard.evaluate("No allowlist anywhere in this body.", ["anything.ts"])
	assert status == guard.STATUS_SKIP_NO_ALLOWLIST
	assert allow == []
	assert oos == []


def test_empty_files_touched_block_skips() -> None:
	# A `files_touched:` header with no entries must skip, never enforce-empty.
	status, _allow, _oos = guard.evaluate("files_touched:\n\nNext paragraph.\n", ["anything.ts"])
	assert status == guard.STATUS_SKIP_NO_ALLOWLIST


def test_directory_prefix_entry_matches() -> None:
	status, _allow, oos = guard.evaluate(_body("frontend/"), ["frontend/a/b/c.ts"])
	assert status == guard.STATUS_IN_SCOPE, oos
	status, _allow, oos = guard.evaluate(_body("frontend/"), ["frontend"])
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert oos == ["frontend"]
	# A sibling that merely shares the prefix string is NOT under the directory.
	status, _allow, oos = guard.evaluate(_body("frontend/"), ["frontend-build/x.ts"])
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert oos == ["frontend-build/x.ts"]


def test_glob_entry_matches_and_flags_nonmatch() -> None:
	status, _allow, oos = guard.evaluate(
		_body("frontend/**", "src/*.ts"),
		["frontend/deep/nested/file.tsx", "src/index.ts", "scripts/hyperliquid/finalize-evm-contract.ts"],
	)
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert oos == ["scripts/hyperliquid/finalize-evm-contract.ts"]


def test_lockfile_autoallowed_but_compiled_js_blocked() -> None:
	status, _allow, oos = guard.evaluate(
		_body("src/app.ts"),
		["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.sum", "src/app.js"],
	)
	assert status == guard.STATUS_OUT_OF_SCOPE
	# Lockfiles are auto-allowed; the compiled twin is not.
	assert oos == ["src/app.js"]


def test_incident_project_244_issue_254() -> None:
	# files_touched = frontend/** + two new workflow files; the Wave 5 commit
	# also touched root TS files and committed compiled .js twins.
	body = _body("frontend/**", ".github/workflows/send-status.yml", ".github/workflows/quote.yml")
	staged = [
		"frontend/components/SendStatus.tsx",
		".github/workflows/quote.yml",
		"tasks/send.ts",
		"tasks/quote.ts",
		"layerzero.config.ts",
		"scripts/hyperliquid/finalize-evm-contract.ts",
		"tasks/send.js",
		"hardhat.config.js",
		"deploy/001_deploy_adapter.js",
	]
	status, _allow, oos = guard.evaluate(body, staged)
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert oos == [
		"tasks/send.ts",
		"tasks/quote.ts",
		"layerzero.config.ts",
		"scripts/hyperliquid/finalize-evm-contract.ts",
		"tasks/send.js",
		"hardhat.config.js",
		"deploy/001_deploy_adapter.js",
	]


def test_leading_dot_slash_normalized_both_sides() -> None:
	status, _allow, _oos = guard.evaluate(_body("./src/"), ["./src/x.ts"])
	assert status == guard.STATUS_IN_SCOPE


def test_bare_entry_matches_exact_path_and_descendants() -> None:
	status, _allow, oos = guard.evaluate(_body("frontend"), ["frontend", "frontend/a/b/c.ts"])
	assert status == guard.STATUS_IN_SCOPE, oos


def test_explicit_allowlist_entries_support_scope_lock_glob() -> None:
	status, allow, oos = guard.evaluate(
		"ignored because explicit allowlist entries are provided",
		["scripts/run.sh", "scripts/orchestrate/deploy/run.sh", "README.md"],
		allowlist_entries=["scripts/**/*.sh"],
	)
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert allow == ["scripts/**/*.sh"]
	assert oos == ["README.md"]


def test_generated_advisory_requires_trusted_author_and_exact_path() -> None:
	metadata = guard.parse_generated_advisory(_generated_advisory_body())
	assert metadata is not None
	assert metadata["cited_file"] == "src/security.py"
	status, allowlist, oos = guard.evaluate_allowlist(
		[metadata["cited_file"]], ["src/security.py", "package-lock.json"], auto_allow_lockfiles=False
	)
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert allowlist == ["src/security.py"]
	assert oos == ["package-lock.json"]
	sha256_metadata = guard.parse_generated_advisory(_generated_advisory_body(audited_commit="c" * 64))
	assert sha256_metadata is not None
	assert sha256_metadata["audited_commit"] == "c" * 64


def test_generated_advisory_rejects_malformed_or_mismatched_footer() -> None:
	with pytest.raises(ValueError):
		guard.parse_generated_advisory(_generated_advisory_body().replace("  - src/security.py", "  - README.md"))


# --------------------------------------------------------------------------
# Layer 1b — CLI exit-code contract
# --------------------------------------------------------------------------


def _run_cli(
	body: str,
	staged: list[str],
	allowlist_out: Path | None = None,
	extra_args: tuple[str, ...] = (),
) -> tuple[int, str]:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		body_file = tdp / "body.txt"
		body_file.write_text(body, encoding="utf-8")
		staged_file = tdp / "staged.txt"
		staged_file.write_text("\n".join(staged) + "\n", encoding="utf-8")
		cmd = [
			sys.executable,
			str(GUARD_SCRIPT),
			"--issue-body-file",
			str(body_file),
			"--staged-file",
			str(staged_file),
		]
		if allowlist_out is not None:
			cmd += ["--allowlist-out", str(allowlist_out)]
		cmd.extend(extra_args)
		proc = subprocess.run(cmd, capture_output=True, text=True)
		return proc.returncode, proc.stdout


def test_cli_exit_codes_and_allowlist_dump() -> None:
	with tempfile.TemporaryDirectory() as td:
		al = Path(td) / "allow.txt"
		rc, out = _run_cli(_body("frontend/**"), ["frontend/a.tsx"], allowlist_out=al)
		assert rc == guard.EXIT_IN_SCOPE and out.strip() == ""
		assert al.read_text(encoding="utf-8").strip() == "frontend/**"

	rc, out = _run_cli(_body("frontend/**"), ["tasks/send.ts", "tasks/send.js"])
	assert rc == guard.EXIT_OUT_OF_SCOPE
	assert out.split() == ["tasks/send.ts", "tasks/send.js"]

	rc, _out = _run_cli("no allowlist here", ["whatever.ts"])
	assert rc == guard.EXIT_SKIP_NO_ALLOWLIST


def test_cli_explicit_allowlist_file_supports_scope_lock_glob() -> None:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		allowlist_file = tdp / "allowlist.txt"
		allowlist_file.write_text("scripts/**/*.sh\n", encoding="utf-8")
		staged_file = tdp / "staged.txt"
		staged_file.write_text("scripts/run.sh\nscripts/orchestrate/deploy/run.sh\nREADME.md\n", encoding="utf-8")
		allowlist_out = tdp / "allowlist_out.txt"
		proc = subprocess.run(
			[
				sys.executable,
				str(GUARD_SCRIPT),
				"--allowlist-file",
				str(allowlist_file),
				"--staged-file",
				str(staged_file),
				"--allowlist-out",
				str(allowlist_out),
			],
			capture_output=True,
			text=True,
		)
		assert proc.returncode == guard.EXIT_OUT_OF_SCOPE
		assert proc.stdout.split() == ["README.md"]
		assert allowlist_out.read_text(encoding="utf-8").strip() == "scripts/**/*.sh"


def test_generated_advisory_scope_matches_only_the_exact_cited_path() -> None:
	rc, out = _run_cli(
		_generated_advisory_body("src"),
		["src/security.py"],
		extra_args=(
			"--issue-author-association",
			"OWNER",
			"--generated-advisory-mode",
			"auto",
		),
	)
	assert rc == guard.EXIT_OUT_OF_SCOPE
	assert out.split() == ["src/security.py"]


def test_linked_issue_metadata_scope_is_exact_and_unresolved_collection_fails_closed() -> None:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		metadata_file = tdp / "linked.json"
		staged_file = tdp / "staged.txt"
		metadata_file.write_text(
			json.dumps(
				[
					{
						"body": _generated_advisory_body(),
						"author_association": "OWNER",
						"author_login": "octocat",
					}
				]
			),
			encoding="utf-8",
		)
		staged_file.write_text("src/security.py\n", encoding="utf-8")
		base_command = [
			sys.executable,
			str(GUARD_SCRIPT),
			"--linked-issue-metadata-file",
			str(metadata_file),
			"--staged-file",
			str(staged_file),
			"--generated-advisory-mode",
			"auto",
		]
		assert subprocess.run(base_command, capture_output=True, text=True).returncode == guard.EXIT_IN_SCOPE
		staged_file.write_text("README.md\n", encoding="utf-8")
		assert subprocess.run(base_command, capture_output=True, text=True).returncode == guard.EXIT_OUT_OF_SCOPE
		metadata_file.write_text('[{"_collection_status":"unresolved"}]\n', encoding="utf-8")
		assert subprocess.run(base_command, capture_output=True, text=True).returncode == guard.EXIT_INVALID_GENERATED_ADVISORY


def test_generated_advisory_plan_parser_accepts_numbered_contract() -> None:
	plan = (
		"1. Files likely to change\n"
		"- `src/security.py`\n\n"
		"2. Functions/modules to implement\n"
		"- `README.md` is mentioned only outside the files section.\n"
	)
	assert guard.extract_plan_files(plan) == ["src/security.py"]


# --------------------------------------------------------------------------
# Layer 2 — extract-and-run the real guard fragments
# --------------------------------------------------------------------------


def _scope_fragment(label: str) -> str:
	if label == "review":
		source = REVIEW_COMMIT_SCRIPT
	elif label == "commit":
		source = IMPLEMENT_COMMIT_SCRIPT
	else:
		source = IMPLEMENT
	text = source.read_text(encoding="utf-8")
	start = f"# >>> files_touched scope-enforcement guard ({label}) >>>"
	end = f"# <<< files_touched scope-enforcement guard ({label}) <<<"
	lines = text.splitlines()
	start_idx = next(i for i, line in enumerate(lines) if line.strip() == start)
	end_idx = next(i for i, line in enumerate(lines) if line.strip() == end)
	base = len(lines[start_idx]) - len(lines[start_idx].lstrip(" "))
	fragment = [line[base:] if len(line) >= base else line for line in lines[start_idx : end_idx + 1]]
	return "\n".join(fragment)


def _run_fragment(
	body: str,
	staged: list[str],
	*,
	enforce: str = "true",
	allow_out_of_scope: str = "false",
	label: str = "commit",
	issue_author_association: str = "OWNER",
	issue_author_login: str = "octocat",
	helper_source: str | None = None,
	linked_issue_metadata_available: bool = True,
	linked_issue_metadata_env: bool = True,
	linked_issue_metadata_unresolved: bool = False,
) -> tuple[int, str, str]:
	fragment = _scope_fragment(label)
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		(tdp / "scripts").mkdir()
		helper_path = tdp / "scripts" / "files_touched_scope_guard.py"
		if helper_source is None:
			shutil.copy(GUARD_SCRIPT, helper_path)
		else:
			helper_path.write_text(helper_source, encoding="utf-8")
		git_env = {
			key: value
			for key, value in os.environ.items()
			if key not in {"BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_WORK_TREE", "GIT_COMMON_DIR"}
		}
		subprocess.run(["git", "init", "-q"], cwd=tdp, check=True, env=git_env)
		subprocess.run(["git", "config", "user.email", "t@t"], cwd=tdp, check=True, env=git_env)
		subprocess.run(["git", "config", "user.name", "t"], cwd=tdp, check=True, env=git_env)
		for rel in staged:
			path = tdp / rel
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text("x\n", encoding="utf-8")
			subprocess.run(["git", "add", "--", rel], cwd=tdp, check=True, env=git_env)
		body_file = tdp / "issue_body.txt"
		body_file.write_text(body, encoding="utf-8")
		linked_issue_metadata_file = tdp / "linked_issue_metadata.json"
		if linked_issue_metadata_available:
			if linked_issue_metadata_unresolved:
				linked_issue_metadata_file.write_text('[{"_collection_status":"unresolved"}]\n', encoding="utf-8")
			else:
				linked_issue_metadata_file.write_text(
					json.dumps(
						[
							{
								"number": 1,
								"body": body,
								"author_association": issue_author_association,
								"author_login": issue_author_login,
							}
						]
					),
					encoding="utf-8",
				)
		gh_output = tdp / "gh_output.txt"
		gh_output.write_text("", encoding="utf-8")
		env = dict(git_env)
		env.update(
			{
				"ISSUE_BODY_FILE": str(body_file),
				"GITHUB_OUTPUT": str(gh_output),
				"TMPDIR": str(tdp),
				"ENFORCE_FILES_TOUCHED": enforce,
				"ALLOW_OUT_OF_SCOPE_FILES": allow_out_of_scope,
				"IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(tdp / "scripts"),
				"SUPPORT_SCRIPTS_DIR": str(tdp / "scripts"),
				"RUNTIME_DIR": str(tdp),
				"LINKED_ISSUE_METADATA_FILE": str(linked_issue_metadata_file),
				"STAGED_FILES": "\n".join(staged),
				"ISSUE_AUTHOR_ASSOCIATION": issue_author_association,
				"ISSUE_AUTHOR_LOGIN": issue_author_login,
			}
		)
		if not linked_issue_metadata_env:
			# Older workflow contract: only RUNTIME_DIR is exported; the
			# guard must default the artifact path itself.
			env.pop("LINKED_ISSUE_METADATA_FILE", None)
		proc = subprocess.run(
			["bash", "-c", "set -euo pipefail\n" + fragment],
			cwd=tdp,
			env=env,
			capture_output=True,
			text=True,
		)
		return proc.returncode, gh_output.read_text(encoding="utf-8"), proc.stdout + proc.stderr


def test_fragment_blocks_out_of_scope_incident() -> None:
	body = _body("frontend/**", ".github/workflows/send-status.yml", ".github/workflows/quote.yml")
	staged = ["tasks/send.ts", "tasks/send.js", "frontend/components/Send.tsx", ".github/workflows/quote.yml"]
	rc, gh_output, _log = _run_fragment(body, staged)
	assert rc == 1, gh_output
	assert "scope_violation_blocked=out-of-scope" in gh_output
	assert "tasks/send.ts" in gh_output and "tasks/send.js" in gh_output
	# In-scope paths must not be reported as violations.
	assert "frontend/components/Send.tsx" not in gh_output.split("scope_violation_allowlist")[0]


def test_fragment_allows_with_override() -> None:
	body = _body("frontend/**")
	rc, gh_output, log = _run_fragment(body, ["tasks/send.ts"], allow_out_of_scope="true")
	assert rc == 0, log
	assert "scope_violation_blocked" not in gh_output
	assert "ALLOW_OUT_OF_SCOPE_FILES=true" in log


def test_fragment_passes_in_scope() -> None:
	body = _body("frontend/**", "README.md")
	rc, gh_output, log = _run_fragment(body, ["frontend/app.tsx", "README.md"])
	assert rc == 0, log
	assert "scope_violation_blocked" not in gh_output


def test_fragment_skips_without_allowlist() -> None:
	rc, gh_output, log = _run_fragment("No files_touched block here.", ["tasks/send.ts"])
	assert rc == 0, log
	assert "scope_violation_blocked" not in gh_output
	assert "skipped" in log.lower()


def test_fragment_master_toggle_off_skips() -> None:
	rc, gh_output, log = _run_fragment(_body("frontend/**"), ["tasks/send.ts"], enforce="false")
	assert rc == 0, log
	assert "scope_violation_blocked" not in gh_output
	assert "disabled" in log.lower()


def test_generated_advisory_cannot_use_scope_bypasses_or_lockfile_allowance() -> None:
	rc, gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["package-lock.json"],
		enforce="false",
		allow_out_of_scope="true",
	)
	assert rc == 1, log
	assert "scope_violation_blocked=out-of-scope" in gh_output


def test_generated_advisory_accepts_exact_github_actions_bot_identity() -> None:
	rc, gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["src/security.py"],
		issue_author_association="NONE",
		issue_author_login="github-actions[bot]",
	)
	assert rc == 0, log
	assert "scope_violation_blocked" not in gh_output


def test_generated_advisory_review_scope_rejects_editor_path_drift() -> None:
	rc, _gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["README.md"],
		label="review",
	)
	assert rc == 1, log
	assert "README.md" in log
	assert "exact cited-file scope" in log

	rc, _gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["src/security.py"],
		label="review",
	)
	assert rc == 0, log
	assert "all staged paths match the exact cited file" in log


def test_review_scope_defaults_linked_issue_metadata_file_from_runtime_dir() -> None:
	# review_autofix.yml@main may not export LINKED_ISSUE_METADATA_FILE for a
	# self-repo PR review; the guard derives ${RUNTIME_DIR}/linked_issue_metadata.json.
	rc, _gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["src/security.py"],
		label="review",
		linked_issue_metadata_env=False,
	)
	assert rc == 0, log
	assert "all staged paths match the exact cited file" in log


def test_review_scope_fails_closed_without_linked_issue_metadata() -> None:
	rc, _gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["src/security.py"],
		label="review",
		linked_issue_metadata_available=False,
	)
	assert rc == 1, log
	assert "linked-issue metadata is unavailable" in log


def test_review_scope_fails_closed_when_linked_issue_collection_is_unresolved() -> None:
	rc, _gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["src/security.py"],
		label="review",
		linked_issue_metadata_unresolved=True,
	)
	assert rc == 1, log
	assert "linked-issue metadata collection is unresolved" in log


@pytest.mark.parametrize("label", ("preflight", "commit", "review"))
def test_generated_advisory_helper_failure_fails_closed(label: str) -> None:
	rc, gh_output, log = _run_fragment(
		_generated_advisory_body(),
		["src/security.py"],
		label=label,
		helper_source="raise RuntimeError('validator crashed')\n",
	)
	assert rc == 1, log
	if label == "review":
		assert "metadata validation failed" in log
	else:
		assert "scope_violation_blocked=generated-security-advisory" in gh_output


def _strip_comments(fragment: str) -> str:
	keep = [ln for ln in fragment.splitlines() if ln.strip() and not ln.strip().startswith("#")]
	return "\n".join(keep)


def test_preflight_and_commit_fragments_share_logic() -> None:
	# Both guard sites must run identical executable logic (only the marker
	# label and surrounding comments differ).
	assert _strip_comments(_scope_fragment("preflight")) == _strip_comments(_scope_fragment("commit"))


# --------------------------------------------------------------------------
# Layer 3 — static wiring assertions
# --------------------------------------------------------------------------


def _implement_text() -> str:
	return IMPLEMENT.read_text(encoding="utf-8")


def _implement_commit_text() -> str:
	return IMPLEMENT_COMMIT_SCRIPT.read_text(encoding="utf-8")


def _review_commit_text() -> str:
	return REVIEW_COMMIT_SCRIPT.read_text(encoding="utf-8")


def _implement_guard_handler_text() -> str:
	return IMPLEMENT_GUARD_HANDLER.read_text(encoding="utf-8")


def _scope_alert_block_text() -> str:
	text = _implement_guard_handler_text()
	start = text.index('if [ -n "${SVB_REASON:-}" ]; then')
	end = text.index("\nREASON_HUMAN=", start)
	return text[start:end]


def test_env_vars_mapped() -> None:
	text = _implement_text()
	assert "ENFORCE_FILES_TOUCHED: ${{ vars.ENFORCE_FILES_TOUCHED || 'true' }}" in text
	assert "ALLOW_OUT_OF_SCOPE_FILES: ${{ vars.ALLOW_OUT_OF_SCOPE_FILES || 'false' }}" in text


def test_both_guard_sites_invoke_script_and_emit_outputs() -> None:
	text = _implement_text()
	commit_text = _implement_commit_text()
	combined_text = text + "\n" + commit_text
	assert 'ISSUE_AUTHOR_LOGIN="$(printf \'%s\' "${ISSUE_PAYLOAD}" | jq -r \'.user.login // ""\')"' in text
	assert text.count("files_touched scope-enforcement guard (preflight)") >= 1
	assert commit_text.count("files_touched scope-enforcement guard (commit)") >= 1
	assert combined_text.count('python3 "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/files_touched_scope_guard.py"') == 3
	assert combined_text.count('--issue-author-login "${ISSUE_AUTHOR_LOGIN:-}"') == 2
	assert combined_text.count("scope_violation_blocked=out-of-scope") == 2
	assert "scope_violation_blocked=scope-lock-label" in commit_text


def test_review_guard_is_bootstrapped_and_uses_linked_issue_metadata() -> None:
	review_text = _review_commit_text()
	stage_text = REVIEW_STAGE_SCRIPT.read_text(encoding="utf-8")
	assert "files_touched scope-enforcement guard (review)" in review_text
	assert '"${SUPPORT_SCRIPTS_DIR:-scripts}/files_touched_scope_guard.py"' in review_text
	assert '"${LINKED_ISSUE_METADATA_FILE}"' in review_text
	assert "files_touched_scope_guard.py" in stage_text
	for commit_script in (
		REVIEW_RB_JUDGE_SCRIPT,
		REVIEW_CONFLICT_PREPARE_SCRIPT,
		REVIEW_CONFLICT_RESOLVE_SCRIPT,
	):
		commit_text = commit_script.read_text(encoding="utf-8")
		assert "--linked-issue-metadata-file" in commit_text, commit_script
		assert "files_touched_scope_guard.py" in commit_text, commit_script


def test_alert_step_handles_scope() -> None:
	workflow_text = _implement_text()
	handler_text = _implement_guard_handler_text()
	assert "scope_violation_blocked != ''" in workflow_text
	assert "gh label create 'ai:scope-blocked'" in handler_text
	assert "gh label edit 'ai:scope-blocked'" in handler_text
	assert "SCOPE_BLOCK_LABEL_DESCRIPTION='Implementation blocked: staged files fell outside files_touched scope; human review required'" in handler_text
	assert "if scope_latched_labels=\"$(gh issue view \"${ISSUE_NUMBER}\" --repo \"${GITHUB_REPOSITORY}\" --json labels -q '.labels[].name' 2>/dev/null)\"; then" in handler_text
	assert "Confirmed ai:scope-blocked is latched on #${ISSUE_NUMBER}; redispatch will be refused until a human removes it." in handler_text
	assert "::error::FAILED to latch ai:scope-blocked on #${ISSUE_NUMBER}; the redispatch block is NOT in effect. Apply it manually:" in handler_text
	assert "::warning::Could not verify ai:scope-blocked on #${ISSUE_NUMBER}; gh issue view failed, so the latch state is unknown." in handler_text
	assert r"The workflow attempted to label this issue \`ai:scope-blocked\`, but the follow-up label read failed. Future redispatch is **not confirmed blocked**; re-check the label manually before redispatch." in handler_text
	assert r"This issue is now labeled \`ai:scope-blocked\`" not in handler_text
	assert "Issue is now ai:scope-blocked." not in handler_text
	assert "SVB_REASON: ${{ steps.preflight_destructive_guard.outputs.scope_violation_blocked" in workflow_text


def test_scope_alert_step_verifies_latch_after_write() -> None:
	scope_text = _scope_alert_block_text()
	assert "if scope_latched_labels=\"$(gh issue view \"${ISSUE_NUMBER}\" --repo \"${GITHUB_REPOSITORY}\" --json labels -q '.labels[].name' 2>/dev/null)\"; then" in scope_text
	assert "grep -qxF 'ai:scope-blocked' <<< \"${scope_latched_labels}\"" in scope_text
	assert "::error::FAILED to latch ai:scope-blocked" in scope_text
	assert "::warning::Could not verify ai:scope-blocked" in scope_text
	assert 'echo "${SCOPE_COMMIT_STATE} ${SCOPE_LATCH_STATUS_LINE}"' in scope_text
	assert '"${SCOPE_TG_LATCH_LINE}"' in scope_text
	assert "This issue is now labeled" not in scope_text


def test_failure_gates_mirror_scope() -> None:
	text = _implement_text()
	# The five downstream failure-gate steps must suppress on a scope block the
	# same way they do on a destructive block.
	for needle in (
		"- name: Handle no-op implementation",
		"- name: Capture post-Codex validation errors",
		"- name: Diagnose post-Codex failure and create fix-up issues",
		"- name: Comment on issue failure",
		"- name: Telegram failure notification",
	):
		idx = text.index(needle)
		gate = text[idx : text.index("\n", text.index("if:", idx))]
		assert "scope_violation_blocked == ''" in gate, needle


def test_redispatch_refusal_checks_scope_label() -> None:
	text = _implement_text()
	assert 'index("ai:scope-blocked") != null' in text


def test_bootstrap_fetches_guard_helper() -> None:
	text = _implement_text()
	assert "implement_staged_support_workspace.sh files_touched_scope_guard.py; do" in text


def test_label_contract_and_helper_have_scope_blocked() -> None:
	contract = json.loads(LABEL_CONTRACT.read_text(encoding="utf-8"))
	assert "ai:scope-blocked" in contract["labels"]
	desc = contract["labels"]["ai:scope-blocked"]["description"]
	assert len(desc) <= 100
	helper = LABEL_HELPERS.read_text(encoding="utf-8")
	assert '["ai:scope-blocked"]="b60205"' in helper
	assert f'["ai:scope-blocked"]="{desc}"' in helper
	assert f"--description '{desc}'" in _implement_guard_handler_text()
	# It is a latch label, not a phase label.
	for group in contract.get("phase_groups", []):
		assert "ai:scope-blocked" not in group.get("members", [])


def _run_all() -> int:
	funcs = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
	failures = 0
	for fn in funcs:
		try:
			if fn is test_generated_advisory_helper_failure_fails_closed:
				for direct_guard_label in ("preflight", "commit", "review"):
					fn(direct_guard_label)
			else:
				fn()
			print(f"ok   {fn.__name__}")
		except Exception as exc:  # noqa: BLE001 — test harness surfaces any failure
			failures += 1
			print(f"FAIL {fn.__name__}: {exc!r}")
	print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
	return 1 if failures else 0


if __name__ == "__main__":
	raise SystemExit(_run_all())
