#!/usr/bin/env python3
"""Tests for the files_touched scope-enforcement guard.

Three layers, mirroring how the destructive-commit guard is validated:

  1. Unit tests of scripts/files_touched_scope_guard.py (the parser + matcher),
     including the real incident from orchestrator project #244 / issue #254
     ("frontend-send-status") that motivated the guard.
  2. Behavioral extract-and-run of the real preflight scope-guard `run:`
     fragment from .github/workflows/implement.yml and the real commit-time
     scope-guard fragment from scripts/implement_commit_changes.sh against a
     synthetic staged index, so the block / override / skip behaviour is
     validated against production code rather than a reimplementation.
  3. Static assertions that the guard is wired in at both guard sites and into
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


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import files_touched_scope_guard as guard  # noqa: E402

IMPLEMENT = REPO_ROOT / ".github" / "workflows" / "implement.yml"
IMPLEMENT_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "implement_commit_changes.sh"
IMPLEMENT_GUARD_HANDLER = REPO_ROOT / "scripts" / "implement_handle_guard_block.sh"
GUARD_SCRIPT = REPO_ROOT / "scripts" / "files_touched_scope_guard.py"
LABEL_CONTRACT = REPO_ROOT / ".github" / "ai" / "label_contract.v1.json"
LABEL_HELPERS = REPO_ROOT / "scripts" / "label_helpers.sh"


def _body(*entries: str) -> str:
	lines = ["Implement the task. Stay inside the files_touched list.", "", "files_touched:"]
	lines.extend(f"  - {entry}" for entry in entries)
	return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Layer 1 — parser + matcher unit tests
# --------------------------------------------------------------------------


def test_all_within_allowlist_passes() -> None:
	status, _allow, oos = guard.evaluate(_body("frontend/", "README.md"), ["frontend/app.tsx", "README.md"])
	assert status == guard.STATUS_IN_SCOPE
	assert oos == []


def test_agents_md_scope_covers_its_top_level_fragments() -> None:
	"""CLAUDE.md §30: a scope naming agents.md (or AGENTS.md) covers agents.d/*.md."""
	for entry in ("agents.md", "AGENTS.md", "./agents.md"):
		status, _allow, oos = guard.evaluate(_body("scripts/x.sh", entry), ["scripts/x.sh", "agents.d/7100-x.md"])
		assert status == guard.STATUS_IN_SCOPE, entry
		assert oos == []
	status, _allow, oos = guard.evaluate_allowlist(["agents.md"], ["agents.d/7100-x.md"], allow_lockfiles=False)
	assert status == guard.STATUS_IN_SCOPE
	# Without agents.md in scope, nested paths, or other extensions: still out of scope.
	_status, _allow, oos = guard.evaluate(_body("scripts/x.sh"), ["agents.d/7100-x.md"])
	assert oos == ["agents.d/7100-x.md"]
	_status, _allow, oos = guard.evaluate(_body("agents.md"), ["agents.d/sub/x.md", "agents.d/x.sh"])
	assert oos == ["agents.d/sub/x.md", "agents.d/x.sh"]


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


def test_heal_strict_allowlist_does_not_auto_allow_lockfiles() -> None:
	status, _allow, out_of_scope = guard.evaluate_allowlist(["scripts/fix.py", "tests/**", "changelog.d/*.md"], ["package-lock.json"], allow_lockfiles=False)
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert out_of_scope == ["package-lock.json"]


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


def test_review_blocked_reissue_new_output_paths_allow_only_declared_files() -> None:
	# Heal #4665: #4664's spot-fix reissue footer now also carries the new
	# files the judge declared in new_output_paths.  They commit; an unrelated
	# staged path, and a sibling of a new fixture, still block.
	body = "\n".join(
		[
			"Complete the follow-up.",
			"",
			"---",
			"**Review-blocked reissue metadata**",
			"- Replaces: #4605 (PR #4607 closed — approach rework)",
			"- Type: review-blocked-reissue",
			"- prior_pr_baseline_branch: ai/reissue-baseline/pr-4607-4399da144a29-36333125977-1",
			"- files_touched:",
			"  - scripts/resolve_integration_ref.sh",
			"  - tests/fixtures/integration_ref_resolver/mixed_fence_tilde_footer.json",
			"  - changelog.d/4605-security-issue-base-binding.md",
			"",
		]
	)
	staged = [
		"scripts/resolve_integration_ref.sh",
		"tests/fixtures/integration_ref_resolver/mixed_fence_tilde_footer.json",
		"changelog.d/4605-security-issue-base-binding.md",
		"tests/fixtures/integration_ref_resolver/undeclared_sibling.json",
		"scripts/unrelated.sh",
	]
	status, allow, oos = guard.evaluate(body, staged)
	assert status == guard.STATUS_OUT_OF_SCOPE
	assert "changelog.d/4605-security-issue-base-binding.md" in allow
	assert oos == [
		"tests/fixtures/integration_ref_resolver/undeclared_sibling.json",
		"scripts/unrelated.sh",
	]
	status, _allow, oos = guard.evaluate(body, staged[:3])
	assert status == guard.STATUS_IN_SCOPE, oos


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


# --------------------------------------------------------------------------
# Layer 1b — CLI exit-code contract
# --------------------------------------------------------------------------


def _run_cli(body: str, staged: list[str], allowlist_out: Path | None = None) -> tuple[int, str]:
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


def test_automation_grant_requires_trusted_author_and_exact_paths() -> None:
	assert all(guard.is_automation_path(path) for path in (
		"scripts", "scripts/x.sh", ".github/actions/a.yml", ".GitHub/workflows/x.yml"))
	assert not any(guard.is_automation_path(path) for path in ("docs/scripts/x.md", "src/prompts.py"))
	meta = {"number": 42, "user": {"login": "maintainer"}, "author_association": "OWNER",
		"body": _body("./scripts/x.sh", ".GitHub/workflows/x.yml", "scripts/**", "prompts/",
			"scripts/../bad.sh", "docs/readme.md")}
	grant = guard.build_automation_grant(meta, "pipeline[bot]")
	assert grant["trusted"] and grant["paths"] == ["scripts/x.sh", ".GitHub/workflows/x.yml"]
	assert guard.check_automation_paths(["scripts/x.sh"], grant, "true", "42") == ("in-scope", [], "granted")
	assert guard.check_automation_paths(["scripts/y.sh"], grant, "true", "42") == (
		"out-of-scope", ["scripts/y.sh"], "path_not_granted")
	assert guard.check_automation_paths(["scripts/x.sh"], grant, "false", "42")[2] == "workflow_edits_disabled"
	assert guard.check_automation_paths(["scripts/x.sh"], grant, "true", "43")[2] == "grant_issue_mismatch"
	assert guard.check_automation_paths(["scripts/x.sh"], None, "true", "42")[2] == "grant_unavailable"
	assert guard.check_automation_paths(["docs/readme.md"], None, "false", "42")[0] == "in-scope"
	meta["author_association"] = "CONTRIBUTOR"
	assert guard.build_automation_grant(meta, "pipeline[bot]")["reason"] == "untrusted_author"
	meta["user"]["login"] = "pipeline[bot]"
	assert guard.build_automation_grant(meta, "pipeline[bot]")["trusted"]
	meta["author_association"] = "OWNER"
	assert guard.build_automation_grant(meta, "")["reason"] == "identity_unavailable"
	meta["user"]["login"] = ""
	assert guard.build_automation_grant(meta, "pipeline[bot]")["reason"] == "untrusted_author"
	meta["user"]["login"] = "maintainer"
	meta["body"] = "missing"
	assert guard.build_automation_grant(meta, "pipeline[bot]")["reason"] == "no_allowlist"
	meta["body"] = "files_touched:\n  not an item\n"
	assert guard.build_automation_grant(meta, "pipeline[bot]")["reason"] == "malformed_allowlist"


def test_pipeline_author_without_block_gets_open_grant() -> None:
	meta = {"number": 42, "user": {"login": "pipeline[bot]"}, "author_association": "NONE", "body": "missing"}
	grant = guard.build_automation_grant(meta, "pipeline[bot]")
	assert grant["trusted"] is True and grant["open"] is True
	assert grant["reason"] == "pipeline_author_no_allowlist" and grant["paths"] == []
	assert guard.check_automation_paths(["scripts/a.sh", ".github/workflows/x.yml"], grant, "true", "42") == (
		"in-scope", [], "pipeline_author_no_allowlist")
	assert guard.check_automation_paths(["scripts/a.sh"], grant, "false", "42")[2] == "workflow_edits_disabled"
	assert guard.check_automation_paths(["scripts/a.sh"], grant, "true", "43")[2] == "grant_issue_mismatch"
	# A malformed block still blocks the pipeline author.
	malformed = guard.build_automation_grant(dict(meta, body="files_touched:\n  not an item\n"), "pipeline[bot]")
	assert malformed["reason"] == "malformed_allowlist" and malformed["trusted"] is False and malformed["open"] is False
	assert guard.check_automation_paths(["scripts/a.sh"], malformed, "true", "42")[0] == "out-of-scope"
	# A declared block grants only its exact entries (no widening).
	declared = guard.build_automation_grant(dict(meta, body=_body("scripts/x.sh")), "pipeline[bot]")
	assert declared["reason"] == "granted" and declared["open"] is False
	assert guard.check_automation_paths(["scripts/y.sh"], declared, "true", "42") == (
		"out-of-scope", ["scripts/y.sh"], "path_not_granted")
	# A human OWNER without a block is still blocked.
	human = guard.build_automation_grant(dict(meta, user={"login": "maintainer"}, author_association="OWNER"),
		"pipeline[bot]")
	assert human["reason"] == "no_allowlist" and human["open"] is False
	assert guard.check_automation_paths(["scripts/a.sh"], human, "true", "42")[2] == "no_allowlist"
	# Case-different logins do not match the pipeline identity.
	assert guard.build_automation_grant(dict(meta, user={"login": "Pipeline[bot]"}), "pipeline[bot]")[
		"reason"] == "untrusted_author"


def test_open_grant_validation_is_strict() -> None:
	base = {"schema_version": guard.AUTOMATION_GRANT_SCHEMA, "issue_number": 42, "trusted": True,
		"open": True, "reason": "pipeline_author_no_allowlist", "paths": []}
	for override in ({"open": "true"}, {"open": 1}, {"trusted": False}, {"paths": ["scripts/a.sh"]},
		{"reason": "granted"}):
		assert guard.check_automation_paths(["scripts/a.sh"], dict(base, **override), "true", "42")[2] == \
			"grant_unavailable", override
	legacy = {key: value for key, value in base.items() if key != "open"}
	legacy.update(reason="granted", paths=["scripts/a.sh"])
	assert guard.check_automation_paths(["scripts/a.sh"], legacy, "true", "42") == ("in-scope", [], "granted")
	assert guard.check_automation_paths(["scripts/b.sh"], legacy, "true", "42")[2] == "path_not_granted"


def test_pipeline_author_open_grant_cli() -> None:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		meta = tdp / "meta.json"
		meta.write_text(json.dumps({"number": 42, "user": {"login": "bot"},
			"author_association": "NONE", "body": "no block"}), encoding="utf-8")
		grant_file = tdp / "grant.json"
		staged = tdp / "staged.txt"
		staged.write_text("scripts/a.sh\n.github/workflows/x.yml\n", encoding="utf-8")
		base = [sys.executable, str(GUARD_SCRIPT)]
		assert subprocess.run(base + ["--emit-automation-grant", str(grant_file),
			"--issue-meta-file", str(meta), "--pipeline-login", "bot"], check=False).returncode == 0
		record = json.loads(grant_file.read_text(encoding="utf-8"))
		assert record["open"] is True and record["reason"] == "pipeline_author_no_allowlist"
		proc = subprocess.run(base + ["--check-automation-paths", "--automation-grant-file", str(grant_file),
			"--staged-file", str(staged), "--allow-workflow-edits", "true", "--issue-number", "42"],
			capture_output=True, text=True)
		assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_automation_grant_cli_denies_invalid_file_and_preserves_old_exit_codes() -> None:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		meta = tdp / "meta.json"
		meta.write_text(json.dumps({"number": 42, "user": {"login": "bot"},
			"author_association": "NONE", "body": _body("scripts/a.sh")}), encoding="utf-8")
		grant_file = tdp / "grant.json"
		staged = tdp / "staged.txt"
		staged.write_text("scripts/a.sh\n", encoding="utf-8")
		base = [sys.executable, str(GUARD_SCRIPT)]
		assert subprocess.run(base + ["--emit-automation-grant", str(grant_file),
			"--issue-meta-file", str(meta), "--pipeline-login", "bot"], check=False).returncode == 0
		cmd = base + ["--check-automation-paths", "--automation-grant-file", str(grant_file),
			"--staged-file", str(staged), "--allow-workflow-edits", "true", "--issue-number", "42"]
		assert subprocess.run(cmd, capture_output=True).returncode == 0
		grant_file.write_text("not json", encoding="utf-8")
		proc = subprocess.run(cmd, capture_output=True, text=True)
		assert proc.returncode == 30 and proc.stdout.strip() == "scripts/a.sh"
		assert proc.stderr.splitlines()[0] == "reason=grant_unavailable"


# --------------------------------------------------------------------------
# Layer 2 — extract-and-run the real guard fragments
# --------------------------------------------------------------------------


def _scope_fragment(label: str) -> str:
	source = IMPLEMENT_COMMIT_SCRIPT if label == "commit" else IMPLEMENT
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
) -> tuple[int, str, str]:
	fragment = _scope_fragment(label)
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		(tdp / "scripts").mkdir()
		shutil.copy(GUARD_SCRIPT, tdp / "scripts" / "files_touched_scope_guard.py")
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
			}
		)
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


def _strip_comments(fragment: str) -> str:
	keep = [ln for ln in fragment.splitlines() if ln.strip() and not ln.strip().startswith("#")]
	return "\n".join(keep)


def test_preflight_and_commit_fragments_share_logic() -> None:
	# Both guard sites must run identical executable logic (only the marker
	# label and surrounding comments differ).
	assert _strip_comments(_scope_fragment("preflight")) == _strip_comments(_scope_fragment("commit"))


def _automation_fragment(label: str) -> str:
	source = IMPLEMENT_COMMIT_SCRIPT if label == "commit" else IMPLEMENT
	lines = source.read_text(encoding="utf-8").splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip() == f"# >>> automation-path grant guard ({label}) >>>")
	end = next(i for i, line in enumerate(lines) if line.strip() == f"# <<< automation-path grant guard ({label}) <<<")
	base = len(lines[start]) - len(lines[start].lstrip(" "))
	return "\n".join(line[base:] for line in lines[start : end + 1])


def _run_automation_fragment(label: str, body: str, staged: str, *, association: str = "OWNER",
	login: str = "maintainer", pipeline_login: str = "pipeline[bot]", allow_edits: str = "true",
	grant_present: bool = True, helper_present: bool = True, rename: bool = False) -> tuple[int, str]:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		(tdp / "scripts").mkdir()
		if helper_present:
			shutil.copy(GUARD_SCRIPT, tdp / "scripts" / GUARD_SCRIPT.name)
		git_env = {key: value for key, value in os.environ.items() if key not in {
			"BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR"}}
		for command in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
			["git", "config", "user.name", "t"]):
			subprocess.run(command, cwd=tdp, check=True, env=git_env)
		path = tdp / staged
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("x\n", encoding="utf-8")
		subprocess.run(["git", "add", "--", staged], cwd=tdp, check=True, env=git_env)
		if rename:
			subprocess.run(["git", "commit", "-qm", "base"], cwd=tdp, check=True, env=git_env)
			path.rename(tdp / "renamed.txt")
			subprocess.run(["git", "add", "-A"], cwd=tdp, check=True, env=git_env)
		grant_file = tdp / "grant.json"
		if grant_present:
			grant_file.write_text(json.dumps(guard.build_automation_grant({"number": 42,
				"user": {"login": login}, "author_association": association, "body": body}, pipeline_login)), encoding="utf-8")
		output = tdp / "output.txt"
		output.write_text("", encoding="utf-8")
		env = dict(git_env, GITHUB_OUTPUT=str(output), ISSUE_NUMBER="42", TMPDIR=str(tdp),
			AUTOMATION_PATH_GRANT_FILE=str(grant_file), ALLOW_WORKFLOW_EDITS=allow_edits,
			RUNTIME_DIR=str(tdp), ENFORCE_FILES_TOUCHED="false", ALLOW_OUT_OF_SCOPE_FILES="true")
		proc = subprocess.run(["bash", "-c", "set -euo pipefail\n" + _automation_fragment(label)],
			cwd=tdp, env=env, capture_output=True, text=True)
		return proc.returncode, output.read_text(encoding="utf-8")


def test_automation_fragments_share_logic_and_fail_closed() -> None:
	assert _strip_comments(_automation_fragment("preflight")) == _strip_comments(_automation_fragment("commit"))
	for label in ("preflight", "commit"):
		for options in ({"association": "CONTRIBUTOR"}, {"body": "missing"},
			{"pipeline_login": ""}, {"allow_edits": "false"}, {"grant_present": False},
			{"helper_present": False}, {"rename": True, "body": _body("docs/renamed.txt")},
			{"staged": "scripts/a\nb.sh"}):
			inputs = {"body": _body("scripts/a.sh"), "staged": "scripts/a.sh", **options}
			rc, output = _run_automation_fragment(label, **inputs)
			assert rc == 1 and "scope_violation_blocked=automation-path" in output, (label, options, output)
		rc, output = _run_automation_fragment(label, _body("scripts/a.sh"), "scripts/a.sh")
		assert rc == 0 and "scope_violation_blocked" not in output
		rc, output = _run_automation_fragment(label, _body("scripts/\u00e9.sh"), "scripts/\u00e9.sh")
		assert rc == 0 and "scope_violation_blocked" not in output, (label, output)
		rc, output = _run_automation_fragment(label, "missing", "docs/x.md", grant_present=False)
		assert rc == 0 and "scope_violation_blocked" not in output
		rc, output = _run_automation_fragment(label, "missing", "scripts/a.sh", login="pipeline[bot]")
		assert rc == 0 and "scope_violation_blocked" not in output, (label, output)
		rc, output = _run_automation_fragment(label, "files_touched:\n  not an item\n", "scripts/a.sh",
			login="pipeline[bot]")
		assert rc == 1 and "reason=malformed_allowlist" in output, (label, output)


# --------------------------------------------------------------------------
# Layer 3 — static wiring assertions
# --------------------------------------------------------------------------


def _implement_text() -> str:
	return IMPLEMENT.read_text(encoding="utf-8")


def _implement_commit_text() -> str:
	return IMPLEMENT_COMMIT_SCRIPT.read_text(encoding="utf-8")


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
	assert text.count("files_touched scope-enforcement guard (preflight)") >= 1
	assert commit_text.count("files_touched scope-enforcement guard (commit)") >= 1
	assert combined_text.count('python3 "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR:-scripts}/files_touched_scope_guard.py"') == 6
	assert combined_text.count("scope_violation_blocked=out-of-scope") == 2
	assert "scope_violation_blocked=scope-lock-label" in commit_text


def test_automation_grant_wiring() -> None:
	workflow = _implement_text()
	commit = _implement_commit_text()
	metadata = workflow.split("- name: Fetch issue metadata", 1)[1].split("- name: Detect smoke test", 1)[0]
	assert 'gh api user --jq .login' in metadata
	assert '--emit-automation-grant' in metadata and 'AUTOMATION_PATH_GRANT_FILE=' in metadata
	assert workflow.index("# >>> automation-path grant guard (preflight)") < workflow.index("# >>> files_touched scope-enforcement guard (preflight)")
	assert workflow.index('done < "${STAGED_SUPPORT_LEDGER}"', workflow.index("- name: Preflight destructive-commit guard")) < workflow.index("# >>> automation-path grant guard (preflight)")
	assert commit.index("# >>> automation-path grant guard (commit)") < commit.index("# >>> files_touched scope-enforcement guard (commit)")
	assert 'guard_rejection_marker automation-path' in _implement_guard_handler_text()


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
	assert "for f in files_touched_scope_guard.py; do" in text


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


# --------------------------------------------------------------------------
# Plan auto-approval gate (issue #6769)
# --------------------------------------------------------------------------

_PROTECTED_PLAN = """Implementation Plan

### 1. Files likely to change

- `scripts/security_audit.sh`: fix the finding.
- `./.GitHub/workflows/plan.yml`
- `README.md`
- `prompts/` and `scripts/**/*.py`

No changes to:
- `scripts/hidden_negated.sh`

### 2. Functions
Mention `scripts/outside_section.sh` in prose.
"""


def test_plan_extractor_reads_files_section() -> None:
	# Negated wording never hides a listed path (fail closed); the next
	# same-level heading ends the section.
	assert guard.extract_plan_automation_paths(_PROTECTED_PLAN) == [
		"scripts/security_audit.sh", ".GitHub/workflows/plan.yml", "prompts/", "scripts/**/*.py",
		"scripts/hidden_negated.sh"]


def test_plan_extractor_heading_lead_in_and_bullet_negatives() -> None:
	plan = ("## Files to change\n- `scripts/a.sh`\n- No changes to `scripts/b.sh`\n"
		"## Other\n- `scripts/c.sh`\n")
	# A bullet-prefixed negative is scanned normally (it cannot hide siblings).
	assert guard.extract_plan_automation_paths(plan) == ["scripts/a.sh", "scripts/b.sh"]


def test_plan_extractor_nested_heading_does_not_end_section() -> None:
	# Regression: a nested heading ended the section, the extraction came back
	# empty, and the plan was auto-approved.
	plan = ("### 1. Files likely to change\n#### Existing files\n- `scripts/security_audit.sh`\n"
		"### 2. Functions\n- `scripts/outside.sh`\n")
	assert guard.extract_plan_automation_paths(plan) == ["scripts/security_audit.sh"]
	plain = "1. Files likely to change\n#### Scripts\n- `scripts/a.sh`\n## Decisions\n- `scripts/b.sh`\n"
	assert guard.extract_plan_automation_paths(plain) == ["scripts/a.sh"]
	bold = "**1. Files likely to change**\n- `scripts/a.sh`\n## Decisions\n- `scripts/b.sh`\n"
	assert guard.extract_plan_automation_paths(bold) == ["scripts/a.sh"]


def test_plan_extractor_negated_lead_in_cannot_hide_paths() -> None:
	# Regression: a positive lead-in containing negation words suppressed the
	# protected bullets below it.
	plan = ("### 1. Files likely to change\nNot a new module; these existing files are modified:\n"
		"- `scripts/security_audit.sh`\n")
	assert guard.extract_plan_automation_paths(plan) == ["scripts/security_audit.sh"]
	assert guard.evaluate_plan_auto_approval(guard.extract_plan_automation_paths(plan), None,
		flag_enabled=False, allow_workflow_edits="true", issue_number="42")[0] == "hold"


def test_plan_extractor_reads_lead_in_line_paths() -> None:
	# Regression: a path on the lead-in line was dropped, so the plan was
	# auto-approved even with the flag off.
	for plan in ("### Files to change: `scripts/security_audit.sh`\n### Functions\n",
		"Files likely to change: `scripts/security_audit.sh`\n"):
		paths = guard.extract_plan_automation_paths(plan)
		assert paths == ["scripts/security_audit.sh"], plan
		assert guard.evaluate_plan_auto_approval(paths, None, flag_enabled=False,
			allow_workflow_edits="true", issue_number="42")[0] == "hold"


def test_plan_extractor_underscore_does_not_hide_paths() -> None:
	# Regression: `_` counted as a boundary character, so underscore emphasis
	# hid a protected path.
	plan = "### Files likely to change\n- _scripts/security_audit.sh_\n- my_scripts/x.sh\n"
	paths = guard.extract_plan_automation_paths(plan)
	assert paths == ["scripts/security_audit.sh_", "scripts/x.sh"]
	assert guard.evaluate_plan_auto_approval(paths, None, flag_enabled=False,
		allow_workflow_edits="true", issue_number="42")[0] == "hold"


def test_plan_extractor_falls_back_to_whole_text() -> None:
	plan = "Edit /scripts/x.sh, keep src/scripts/y.sh, and update scripts/z.py:12.\nAlso README.md."
	assert guard.extract_plan_automation_paths(plan) == ["scripts/x.sh", "scripts/z.py"]
	assert guard.extract_plan_automation_paths("### Files to change\n- `src/app.py`\n") == []


def test_plan_extractor_never_truncates_unusual_filenames() -> None:
	# Regression: `scripts/a+b.py` was cut to `scripts/a`, so a grant for the
	# prefix approved a plan that lists a different, ungranted path.
	plan = "### Files likely to change\n- scripts/a+b.py\n- **scripts/c.sh**\n- (scripts/d(1).sh:3).\n"
	paths = guard.extract_plan_automation_paths(plan)
	assert paths == ["scripts/a+b.py", "scripts/c.sh", "scripts/d(1).sh"]
	prefix_grant = guard.build_automation_grant(_owner_meta("scripts/a"), "pipeline[bot]")
	assert guard.evaluate_plan_auto_approval(["scripts/a+b.py"], prefix_grant, flag_enabled=True,
		allow_workflow_edits="true", issue_number="42")[0] == "hold"


def _owner_meta(*entries: str, number: int = 42) -> dict:
	return {"number": number, "user": {"login": "maintainer"}, "author_association": "OWNER",
		"body": _body(*entries) if entries else "no block"}


def _pipeline_meta(number: int = 42) -> dict:
	return {"number": number, "user": {"login": "pipeline[bot]"}, "author_association": "NONE",
		"body": "Security finding fix. No files_touched block."}


def test_plan_gate_flag_off_holds_protected_and_approves_ordinary() -> None:
	assert guard.evaluate_plan_auto_approval([], None, flag_enabled=False, allow_workflow_edits="true",
		issue_number="42") == ("approve", "none", [])
	assert guard.evaluate_plan_auto_approval(["README.md"], None, flag_enabled=False,
		allow_workflow_edits="true", issue_number="42") == ("approve", "none", [])
	granted = guard.build_automation_grant(_owner_meta("scripts/a.sh"), "pipeline[bot]")
	assert guard.evaluate_plan_auto_approval(["scripts/a.sh"], granted, flag_enabled=False,
		allow_workflow_edits="true", issue_number="42") == (
		"hold", "automation_path_plan_auto_approval_disabled", ["scripts/a.sh"])


def test_plan_gate_flag_on_requires_exact_trusted_grants() -> None:
	paths = ["scripts/a.sh", ".github/workflows/x.yml"]
	open_grant = guard.build_automation_grant(_pipeline_meta(), "pipeline[bot]")
	assert open_grant["open"] is True
	assert guard.evaluate_plan_auto_approval(paths, open_grant, flag_enabled=True, allow_workflow_edits="true",
		issue_number="42") == ("hold", "open_grant_not_exact", paths)
	full = guard.build_automation_grant(_owner_meta(*paths), "pipeline[bot]")
	assert guard.evaluate_plan_auto_approval(paths, full, flag_enabled=True, allow_workflow_edits="true",
		issue_number="42") == ("approve", "granted", [])
	partial = guard.build_automation_grant(_owner_meta("scripts/a.sh"), "pipeline[bot]")
	assert guard.evaluate_plan_auto_approval(paths, partial, flag_enabled=True, allow_workflow_edits="true",
		issue_number="42") == ("hold", "path_not_granted", [".github/workflows/x.yml"])
	untrusted = guard.build_automation_grant(dict(_owner_meta(*paths), author_association="NONE"), "pipeline[bot]")
	assert guard.evaluate_plan_auto_approval(paths, untrusted, flag_enabled=True, allow_workflow_edits="true",
		issue_number="42")[1] == "untrusted_author"
	assert guard.evaluate_plan_auto_approval(paths, full, flag_enabled=True, allow_workflow_edits="false",
		issue_number="42")[1] == "workflow_edits_disabled"
	assert guard.evaluate_plan_auto_approval(paths, full, flag_enabled=True, allow_workflow_edits="true",
		issue_number="43")[1] == "grant_issue_mismatch"
	# Directory and glob entries can never be granted exactly.
	dir_grant = guard.build_automation_grant(_owner_meta("prompts/"), "pipeline[bot]")
	assert guard.evaluate_plan_auto_approval(["prompts/"], dir_grant, flag_enabled=True,
		allow_workflow_edits="true", issue_number="42")[0] == "hold"


def test_plan_gate_preserves_implement_guard_open_grant() -> None:
	# Regression: the implement-time guard still accepts the open grant.
	open_grant = guard.build_automation_grant(_pipeline_meta(), "pipeline[bot]")
	assert guard.check_automation_paths(["scripts/security_audit.sh"], open_grant, "true", "42") == (
		"in-scope", [], "pipeline_author_no_allowlist")


def _run_plan_gate_cli(plan: str | None, flag: str, meta: dict | str | None, *,
	login: str = "pipeline[bot]") -> tuple[int, dict]:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		plan_file = tdp / "plan.md"
		if plan is not None:
			plan_file.write_text(plan, encoding="utf-8")
		args = [sys.executable, str(GUARD_SCRIPT), "--plan-auto-approval-gate", "--plan-file", str(plan_file),
			"--flag-enabled", flag, "--pipeline-login", login, "--allow-workflow-edits", "true",
			"--issue-number", "42"]
		if meta is not None:
			meta_file = tdp / "meta.json"
			meta_file.write_text(meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8")
			args += ["--issue-meta-file", str(meta_file)]
		proc = subprocess.run(args, capture_output=True, text=True, check=False)
		return proc.returncode, json.loads(proc.stdout)


def test_plan_gate_cli_ungranted_security_finding_held() -> None:
	plan = "### Files likely to change\n- `scripts/security_audit.sh`\n"
	for flag, reason in (("false", "automation_path_plan_auto_approval_disabled"), ("true", "open_grant_not_exact")):
		code, record = _run_plan_gate_cli(plan, flag, _pipeline_meta())
		assert code == guard.EXIT_AUTOMATION_UNGRANTED, (flag, record)
		assert record["decision"] == "hold" and record["reason"] == reason
		assert record["protected_paths"] == ["scripts/security_audit.sh"]
		assert record["log_paths"] == "scripts/security_audit.sh"


def test_plan_gate_cli_granted_and_ordinary_plans() -> None:
	plan = "### Files likely to change\n- `scripts/a.sh`\n- `.github/workflows/x.yml`\n"
	meta = _owner_meta("scripts/a.sh", ".github/workflows/x.yml")
	code, record = _run_plan_gate_cli(plan, "true", meta)
	assert code == 0 and record["decision"] == "approve" and record["reason"] == "granted"
	code, record = _run_plan_gate_cli(plan, "off", meta)
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["reason"] == "automation_path_plan_auto_approval_disabled"
	code, record = _run_plan_gate_cli("### Files likely to change\n- `src/app.py`\n", "false", None)
	assert code == 0 and record == {"decision": "approve", "reason": "none", "protected_paths": [],
		"denied_paths": [], "log_paths": "none"}


def test_plan_gate_flag_defaults_on() -> None:
	# An empty value is on, like the `|| 'true'` workflow defaults; only an
	# explicit off value holds every automation-path plan.
	plan = "## Files likely to change\n- `scripts/a.sh`\n\n## Functions or modules to implement\n- `scripts/b.sh`\n"
	meta = _owner_meta("scripts/a.sh")
	code, record = _run_plan_gate_cli(plan, "", meta)
	assert code == 0 and record["reason"] == "granted" and record["protected_paths"] == ["scripts/a.sh"]
	for off in ("false", "FALSE", "0", "no", "maybe"):
		assert _run_plan_gate_cli(plan, off, meta)[1]["reason"] == "automation_path_plan_auto_approval_disabled", off


def test_plan_gate_cli_fails_closed() -> None:
	plan = "### Files likely to change\n- `scripts/a.sh`\n"
	assert _run_plan_gate_cli(None, "false", None)[1]["reason"] == "plan_gate_error"
	assert _run_plan_gate_cli("   \n", "false", None)[1]["reason"] == "plan_gate_error"
	assert _run_plan_gate_cli(plan, "true", None)[1]["reason"] == "issue_meta_unavailable"
	assert _run_plan_gate_cli(plan, "true", "{not json")[1]["reason"] == "plan_gate_error"
	assert _run_plan_gate_cli(plan, "true", _owner_meta("scripts/a.sh"), login="")[1]["reason"] == \
		"identity_unavailable"


def test_plan_gate_log_paths_are_sanitized() -> None:
	rendered = guard.plan_gate_log_paths(["scripts/a\n::error::x.sh", "scripts/" + "b" * 300] + [
		f"scripts/{i}.sh" for i in range(30)])
	assert "\n" not in rendered and "::" not in rendered and " " not in rendered
	assert len(rendered.split(",")) == guard.PLAN_LOG_PATHS_MAX_ENTRIES
	assert all(len(item) <= guard.PLAN_LOG_PATH_MAX_CHARS for item in rendered.split(","))
	assert guard.plan_gate_log_paths([]) == "none"


def test_plan_gate_origin_repo_verifies_pipeline_source_chain() -> None:
	# Regression: the plan gate built the grant without an origin fetcher, so
	# an exact grant on a pipeline-authored derived issue always held with
	# origin_unverified, unlike the implement-time guard (--origin-repo).
	body = _body("scripts/a.sh") + ("\n---\n**Orchestrator metadata** (do not edit)\n"
		"- Type: implement-fix-up (post-codex-validation)\n- Source issue: #41\n")
	meta = {"number": 42, "user": {"login": "pipeline[bot]"}, "author_association": "NONE", "body": body}
	plan = "### Files likely to change\n- `scripts/a.sh`\n"
	reads: list[tuple[str, int]] = []

	def fake_fetch(repo: str, number: int):
		reads.append((repo, number))
		return {"number": number, "user": {"login": "maintainer"}, "author_association": "OWNER", "body": "x"}

	original = guard.fetch_issue_with_gh
	guard.fetch_issue_with_gh = fake_fetch
	try:
		with tempfile.TemporaryDirectory() as td:
			plan_file = Path(td) / "plan.md"
			plan_file.write_text(plan, encoding="utf-8")
			meta_file = Path(td) / "meta.json"
			meta_file.write_text(json.dumps(meta), encoding="utf-8")
			held, code = guard.run_plan_auto_approval_gate(str(plan_file), "true", str(meta_file),
				"pipeline[bot]", "true", "42")
			assert code == guard.EXIT_AUTOMATION_UNGRANTED and held["reason"] == "origin_unverified"
			assert reads == []
			record, code = guard.run_plan_auto_approval_gate(str(plan_file), "true", str(meta_file),
				"pipeline[bot]", "true", "42", "owner/repo")
			assert code == 0 and record["decision"] == "approve" and record["reason"] == "granted", record
			assert reads == [("owner/repo", 41)]
			bad, _ = guard.run_plan_auto_approval_gate(str(plan_file), "true", str(meta_file),
				"pipeline[bot]", "true", "42", "not a repo")
			assert bad["reason"] == "origin_unverified"
	finally:
		guard.fetch_issue_with_gh = original


# --- Plan files outside files_touched (consumer #4795) ---------------------

_WIDENED_PLAN = ("## Files likely to change\n- `backend/withdrawal_flow.py`\n- `backend/poll.py`\n"
	"- `package-lock.json`\n- `agents.d/1-x.md`\n- `src/` (directory, not checked)\n")


def _with_env(**values):
	names = ("ENFORCE_FILES_TOUCHED", "ALLOW_OUT_OF_SCOPE_FILES", "PLAN_FILES_TOUCHED_GATE_ENABLED")
	saved = {name: os.environ.get(name) for name in names}
	for name in names:
		os.environ.pop(name, None)
	os.environ.update(values)
	return saved


def _restore_env(saved) -> None:
	for name, value in saved.items():
		if value is None:
			os.environ.pop(name, None)
		else:
			os.environ[name] = value


def test_plan_files_outside_files_touched_uses_the_implement_matcher() -> None:
	body = _body("backend/poll.py", "agents.md")
	# Lockfiles and agents.d fragments (agents.md listed) are allowed as at
	# implement; a directory token is not an exact file and is not checked.
	assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, body) == ["backend/withdrawal_flow.py"]
	assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, _body("backend/")) == ["agents.d/1-x.md"]
	# No list, an empty list or a malformed one: implement skips, so no check.
	for skipped in ("no list here", "files_touched:\n", "files_touched:\nsee below\n- a.py\n"):
		assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, skipped) == [], skipped
	# A plan without a Files section names no exact files.
	assert guard.plan_files_outside_files_touched("Change `backend/x.py`.", body) == []
	# A heal-route issue (label or fingerprint marker, as implement decides):
	# implement enforces its scope marker, not files_touched.
	scope_marker = "\n<!-- ai:workflow-heal-scope:v1 paths=backend/poll.py runs=1 -->\n"
	fp_marker = "\n<!-- workflow-failure-heal:fp=" + "a" * 64 + " -->\n"
	assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, body + fp_marker) == []
	assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, body, [{"name": "ai:workflow-heal"}]) == []
	assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, body, ["ai:workflow-heal"]) == []
	# The scope marker alone is not the heal route: implement enforces files_touched.
	assert guard.plan_files_outside_files_touched(_WIDENED_PLAN, body + scope_marker) == ["backend/withdrawal_flow.py"]


def test_plan_files_touched_gate_switches() -> None:
	cases = (({}, True), ({"ENFORCE_FILES_TOUCHED": ""}, True), ({"ENFORCE_FILES_TOUCHED": "false"}, False),
		# implement enforces only on exactly "true".
		({"ENFORCE_FILES_TOUCHED": "TRUE"}, False), ({"ALLOW_OUT_OF_SCOPE_FILES": "true"}, False),
		({"ALLOW_OUT_OF_SCOPE_FILES": "false"}, True), ({"PLAN_FILES_TOUCHED_GATE_ENABLED": "false"}, False),
		({"PLAN_FILES_TOUCHED_GATE_ENABLED": "OFF"}, False), ({"PLAN_FILES_TOUCHED_GATE_ENABLED": "true"}, True))
	for values, expected in cases:
		saved = _with_env(**values)
		try:
			assert guard.plan_files_touched_gate_active() is expected, values
		finally:
			_restore_env(saved)


def test_plan_gate_cli_holds_plan_outside_files_touched() -> None:
	saved = _with_env()
	try:
		meta = _owner_meta("backend/poll.py", "agents.md")
		code, record = _run_plan_gate_cli(_WIDENED_PLAN, "true", meta)
		assert code == guard.EXIT_AUTOMATION_UNGRANTED, record
		assert record == {"decision": "hold", "reason": "plan_files_outside_files_touched", "protected_paths": [],
			"denied_paths": ["backend/withdrawal_flow.py"], "log_paths": "backend/withdrawal_flow.py"}
		code, record = _run_plan_gate_cli(_WIDENED_PLAN, "true", _owner_meta("backend/", "agents.md"))
		assert code == 0 and record["decision"] == "approve", record
		# No metadata (the poller cache had no body): no list to compare.
		code, record = _run_plan_gate_cli(_WIDENED_PLAN, "true", None)
		assert code == 0 and record["decision"] == "approve", record
		# Unreadable metadata holds, like every other gate error.
		assert _run_plan_gate_cli(_WIDENED_PLAN, "true", "{not json")[1]["reason"] == "plan_gate_error"
		assert _run_plan_gate_cli(_WIDENED_PLAN, "true", "[]")[1]["reason"] == "plan_gate_error"
	finally:
		_restore_env(saved)


def test_plan_gate_automation_hold_reason_wins_over_files_touched() -> None:
	saved = _with_env()
	try:
		plan = "### Files likely to change\n- `scripts/a.sh`\n- `src/b.py`\n"
		code, record = _run_plan_gate_cli(plan, "false", _owner_meta("scripts/a.sh"))
		assert record["reason"] == "automation_path_plan_auto_approval_disabled", record
		code, record = _run_plan_gate_cli(plan, "true", _owner_meta("scripts/a.sh"))
		assert record["reason"] == "plan_files_outside_files_touched" and record["denied_paths"] == ["src/b.py"]
		assert record["protected_paths"] == ["scripts/a.sh"]
	finally:
		_restore_env(saved)


# --- Q82: a pipeline-authored derived issue inherits its source's trust ---

PIPELINE = "pipeline[bot]"


def _origin_issues(**issues):
	"""Fake source issues keyed n<number>: (login, association, body)."""
	reads: list[int] = []

	def fetch(number: int):
		reads.append(number)
		entry = issues.get(f"n{number}")
		if entry is None:
			return None
		login, association, body = entry
		return {"number": number, "user": {"login": login}, "author_association": association, "body": body}

	return fetch, reads


def test_origin_references_cover_every_reissue_marker() -> None:
	body = (
		"<!-- ai:unblock-fixup:v1 item=72 round=1 -->\n"
		"Re-issued by the unblock judge from #73.\n"
		"**⚠️ Re-issued from #74** — the previous issue stalled\n"
		"---\n**Review-blocked reissue metadata**\n- Replaces: #75 (PR #80 closed — approach rework)\n- Type: review-blocked-reissue\n"
	)
	assert guard.origin_references(body) == ([74, 73, 72, 75], False)
	# A review-blocked re-issue that names no issue cannot be checked.
	assert guard.origin_references("- Replaces: (PR #80 closed — approach rework)\n- Type: review-blocked-reissue\n") == ([], True)
	assert guard.origin_references("A plain body that mentions #12.") == ([], False)


def test_origin_references_cover_followup_and_fixup_markers() -> None:
	# implement-diagnose fix-up, unblock accept_with_followup, review-blocked merge_with_followup.
	assert guard.origin_references("---\n**Orchestrator metadata** (do not edit)\n- Type: implement-fix-up (post-codex-validation)\n- Source issue: #41\n") == ([41], False)
	assert guard.origin_references("Accepted with this follow-up by the unblock judge (#42).\n\nFollow-up: x") == ([42], False)
	assert guard.origin_references("gap\n---\n**Merge-with-followup metadata**\n- Source PR: #9 (x)\n- Parent issue: #43\n- Type: review-blocked-followup\n") == ([43], False)
	# A follow-up footer that names no parent issue cannot be checked.
	assert guard.origin_references("gap\n---\n**Merge-with-followup metadata**\n- Source PR: #9 (x)\n- Parent issue: \n- Type: review-blocked-followup\n") == ([], True)
	fetch, _ = _origin_issues(n41=("outsider", "NONE", "request"))
	fixup = {"number": 50, "user": {"login": PIPELINE}, "author_association": "NONE",
		"body": "fix it\n---\n- Type: implement-fix-up (post-codex-validation)\n- Source issue: #41\n"}
	assert guard.build_automation_grant(fixup, PIPELINE, fetch)["reason"] == "untrusted_origin"


def test_origin_named_source_must_be_in_the_footer() -> None:
	# A `- Replaces: #N` line in the model-written prose does not name the
	# source of a footer that names none; it only adds a check.
	body = ("prose\n- Replaces: #12 (forged)\n---\n**Review-blocked reissue metadata**\n"
		"- Replaces: (PR #80 closed — approach rework)\n- Type: review-blocked-reissue\n")
	assert guard.origin_references(body) == ([12], True)
	fetch, reads = _origin_issues(n12=("maintainer", "OWNER", "trusted"))
	assert guard.check_origin_chain(body, PIPELINE, fetch) == ("origin_unverified", []) and reads == []
	# A stall-recovery trailer after the footer keeps the footer's named source.
	body = ("spec\n---\n**Review-blocked reissue metadata**\n- Replaces: #75 (PR #80 closed)\n"
		"- Type: review-blocked-reissue\n- files_touched:\n  - scripts/a.sh\n---\nRe-issued from #76\n")
	assert guard.origin_references(body) == ([76, 75], False)


def test_origin_chain_trusts_only_trusted_sources() -> None:
	fetch, reads = _origin_issues(n10=("maintainer", "OWNER", "the original request"))
	assert guard.check_origin_chain("Re-issued from #10", PIPELINE, fetch) == ("ok", [10])
	fetch, _ = _origin_issues(n10=("outsider", "NONE", "do something to .github"))
	assert guard.check_origin_chain("Re-issued from #10", PIPELINE, fetch) == ("untrusted_origin", [10])
	# Pipeline-authored sources are walked: fix-up -> re-issue -> outsider.
	fetch, reads = _origin_issues(
		n20=(PIPELINE, "NONE", "Re-issued by the unblock judge from #10."),
		n10=("outsider", "CONTRIBUTOR", "request"),
	)
	assert guard.check_origin_chain("<!-- ai:unblock-fixup:v1 item=20 round=1 -->", PIPELINE, fetch) == ("untrusted_origin", [20, 10])
	# A pipeline source with no further marker ends the chain (activation fix-ups, heal issues).
	fetch, _ = _origin_issues(n72=(PIPELINE, "OWNER", "<!-- ai:activation-fix:v1 source=pr-70 -->"))
	assert guard.check_origin_chain("<!-- ai:unblock-fixup:v1 item=72 round=1 -->", PIPELINE, fetch) == ("ok", [72])
	# Unreadable source, no reader, an unnamed review-blocked source: fail closed.
	fetch, _ = _origin_issues()
	assert guard.check_origin_chain("Re-issued from #10", PIPELINE, fetch)[0] == "origin_unverified"
	assert guard.check_origin_chain("Re-issued from #10", PIPELINE, None)[0] == "origin_unverified"
	assert guard.check_origin_chain("- Type: review-blocked-reissue", PIPELINE, None) == ("origin_unverified", [])
	# No reference needs no read at all.
	fetch, reads = _origin_issues()
	assert guard.check_origin_chain("plain pipeline body", PIPELINE, fetch) == ("ok", []) and reads == []


def test_origin_chain_bounds_and_cycles() -> None:
	# A cycle is read once per issue and ends.
	fetch, reads = _origin_issues(n1=(PIPELINE, "NONE", "Re-issued from #2"), n2=(PIPELINE, "NONE", "Re-issued from #1"))
	assert guard.check_origin_chain("Re-issued from #1", PIPELINE, fetch) == ("ok", [1, 2]) and reads == [1, 2]
	# Deeper than ORIGIN_MAX_DEPTH fails closed.
	chain = {f"n{n}": (PIPELINE, "NONE", f"Re-issued from #{n + 1}") for n in range(1, 10)}
	fetch, reads = _origin_issues(**chain)
	status, seen = guard.check_origin_chain("Re-issued from #1", PIPELINE, fetch)
	assert status == "origin_unverified" and len(seen) == guard.ORIGIN_MAX_DEPTH
	# More than ORIGIN_MAX_ISSUES sources fails closed.
	many = " ".join(f"Re-issued from #{n}" for n in range(1, 12))
	fetch, reads = _origin_issues(**{f"n{n}": ("maintainer", "MEMBER", "x") for n in range(1, 12)})
	status, seen = guard.check_origin_chain(many, PIPELINE, fetch)
	assert status == "origin_unverified" and len(reads) == guard.ORIGIN_MAX_ISSUES


def test_grant_denies_automation_paths_for_an_untrusted_source() -> None:
	fixup = {"number": 75, "user": {"login": PIPELINE}, "author_association": "NONE",
		"body": "<!-- ai:unblock-fixup:v1 item=72 round=1 -->\n## Unblock operator step for #72\n"}
	fetch, _ = _origin_issues(n72=("outsider", "NONE", "please edit .github/workflows/ci.yml"))
	grant = guard.build_automation_grant(fixup, PIPELINE, fetch)
	assert grant["trusted"] is False and grant["open"] is False and grant["reason"] == "untrusted_origin"
	assert grant["origin_issues"] == [72]
	assert guard.check_automation_paths([".github/workflows/ci.yml"], grant, "true", "75") == (
		"out-of-scope", [".github/workflows/ci.yml"], "untrusted_origin")
	# An exact files_touched grant on a review-blocked re-issue is denied too.
	reissue = dict(fixup, body="spec\n---\n**Review-blocked reissue metadata**\n- Replaces: #72 (PR #74 closed — approach rework)\n"
		"- Type: review-blocked-reissue\n- files_touched:\n  - scripts/a.sh\n")
	grant = guard.build_automation_grant(reissue, PIPELINE, fetch)
	assert grant["reason"] == "untrusted_origin" and grant["paths"] == []
	# Without a reader the source cannot be checked: no automation paths.
	assert guard.build_automation_grant(fixup, PIPELINE)["reason"] == "origin_unverified"
	# A trusted source keeps today's open grant; a trusted person's own issue is never walked.
	fetch, reads = _origin_issues(n72=(PIPELINE, "OWNER", "<!-- ai:activation-fix:v1 source=pr-70 -->"))
	assert guard.build_automation_grant(fixup, PIPELINE, fetch)["reason"] == "pipeline_author_no_allowlist"
	fetch, reads = _origin_issues()
	human = dict(fixup, user={"login": "maintainer"}, author_association="OWNER", body=_body("scripts/a.sh") + "\nRe-issued from #9\n")
	assert guard.build_automation_grant(human, PIPELINE, fetch)["reason"] == "granted" and reads == []
	# Non-pipeline grants carry no origin field.
	assert "origin_issues" not in guard.build_automation_grant(human, PIPELINE, fetch)


def test_origin_chain_cli_reads_sources_with_gh() -> None:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		bin_dir = tdp / "bin"
		bin_dir.mkdir()
		calls = tdp / "calls.txt"
		fake_gh = bin_dir / "gh"
		fake_gh.write_text(
			"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$GH_CALLS\"\n"
			"case \"$*\" in\n"
			"  'api repos/o/r/issues/72') printf '{\"number\":72,\"user\":{\"login\":\"outsider\"},\"author_association\":\"NONE\",\"body\":\"x\"}' ;;\n"
			"  *) exit 1 ;;\nesac\n", encoding="utf-8")
		fake_gh.chmod(0o755)
		meta = tdp / "meta.json"
		meta.write_text(json.dumps({"number": 75, "user": {"login": "bot"}, "author_association": "NONE",
			"body": "<!-- ai:unblock-fixup:v1 item=72 round=1 -->\nspec"}), encoding="utf-8")
		grant_file = tdp / "grant.json"
		env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", GH_CALLS=str(calls))
		base = [sys.executable, str(GUARD_SCRIPT), "--emit-automation-grant", str(grant_file),
			"--issue-meta-file", str(meta), "--pipeline-login", "bot"]
		assert subprocess.run(base + ["--origin-repo", "o/r"], env=env, check=False).returncode == 0
		record = json.loads(grant_file.read_text(encoding="utf-8"))
		assert record["reason"] == "untrusted_origin" and record["origin_issues"] == [72]
		assert calls.read_text(encoding="utf-8").splitlines() == ["api repos/o/r/issues/72"]
		# An invalid repo is ignored, so the source cannot be read: fail closed, no gh call.
		calls.write_text("", encoding="utf-8")
		assert subprocess.run(base + ["--origin-repo", "o/r; rm -rf /"], env=env, check=False).returncode == 0
		assert json.loads(grant_file.read_text(encoding="utf-8"))["reason"] == "origin_unverified"
		assert calls.read_text(encoding="utf-8") == ""


def test_origin_repo_is_passed_at_both_grant_call_sites() -> None:
	metadata = _implement_text().split("- name: Fetch issue metadata", 1)[1].split("- name: Detect smoke test", 1)[0]
	assert '--origin-repo "${GITHUB_REPOSITORY:-}" || true' in metadata
	assert 'origin_issues=\\((.origin_issues // [])' in metadata
	unblock = (REPO_ROOT / "scripts" / "unblock_judge.sh").read_text(encoding="utf-8")
	assert '--origin-repo "${REPOSITORY}" >/dev/null 2>&1; then' in unblock
	# Issue #7174: both plan auto-approval checks (plan.yml and the poller's
	# stall-recovery gate) verify a pipeline-authored issue's source chain.
	plan = (REPO_ROOT / ".github" / "workflows" / "plan.yml").read_text(encoding="utf-8")
	plan_gate = plan.split("run_plan_gate() {", 1)[1].split('plan_gate_result="$(run_plan_gate)"', 1)[0]
	assert '--origin-repo "${GITHUB_REPOSITORY:-}" \\' in plan_gate
	poller = (REPO_ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	poller_gate = poller.split('gate_result="$(PYTHONDONTWRITEBYTECODE=1 python3 -I -B "${guard_py}" --plan-auto-approval-gate', 1)[1].split('2>/dev/null || true)"', 1)[0]
	assert '--origin-repo "${GITHUB_REPOSITORY:-}" \\' in poller_gate


# --- #7209: unblock-judge re-issue that edits automation paths ---

_REISSUE_7209_FILES = (
	"workflow-templates/ai-review.yml",
	"scripts/workflow_wrapper_refs.py",
	"scripts/review_security_hold_sweep.py",
	"tests/test_update_workflows_guardrails.py",
	"tests/test_review_security_hold_sweep.py",
	"tests/test_files_touched_scope_guard.py",
	"changelog.d/*.md",
)


def _reissue_7209(body_tail: str) -> dict:
	return {"number": 7209, "user": {"login": PIPELINE}, "author_association": "NONE",
		"body": "Re-issued by the unblock judge from #7161.\n\nSpecification: fix the sweep.\n\n" + body_tail}


def test_unblock_reissue_with_block_grants_exactly_its_automation_paths() -> None:
	fetch, _ = _origin_issues(n7161=(PIPELINE, "NONE", "<!-- ai:workflow-heal-scope:v1 paths=tests/** -->"))
	grant = guard.build_automation_grant(_reissue_7209(_body(*_REISSUE_7209_FILES)), PIPELINE, fetch)
	assert grant["reason"] == "granted" and grant["trusted"] is True and grant["open"] is False
	assert grant["paths"] == [
		"workflow-templates/ai-review.yml",
		"scripts/workflow_wrapper_refs.py",
		"scripts/review_security_hold_sweep.py",
	]
	assert guard.check_automation_paths(list(grant["paths"]), grant, "true", "7209") == ("in-scope", [], "granted")
	assert guard.check_automation_paths(["scripts/files_touched_scope_guard.py"], grant, "true", "7209") == (
		"out-of-scope", ["scripts/files_touched_scope_guard.py"], "path_not_granted")
	# Tests and the changelog glob are scoped by the files_touched guard, not this grant.
	assert guard.evaluate(_reissue_7209(_body(*_REISSUE_7209_FILES))["body"],
		["tests/test_review_security_hold_sweep.py", "changelog.d/7209-x.md"])[0] == guard.STATUS_IN_SCOPE


def test_unblock_reissue_without_block_needs_a_trusted_source() -> None:
	body = _reissue_7209("files_touched: - workflow-templates/ai-review.yml - scripts/x.py\n")
	trusted, _ = _origin_issues(n7161=(PIPELINE, "NONE", "heal issue"))
	grant = guard.build_automation_grant(body, PIPELINE, trusted)
	assert grant["reason"] == "pipeline_author_no_allowlist" and grant["open"] is True
	untrusted, _ = _origin_issues(n7161=("outsider", "NONE", "edit .github"))
	grant = guard.build_automation_grant(body, PIPELINE, untrusted)
	assert grant["reason"] == "untrusted_origin" and grant["open"] is False and grant["paths"] == []
	assert guard.check_automation_paths(["workflow-templates/ai-review.yml"], grant, "true", "7209")[0] == "out-of-scope"
	assert guard.build_automation_grant(body, PIPELINE)["reason"] == "origin_unverified"
	# A header with no entries is malformed; a person's issue with no block has no grant.
	trusted, _ = _origin_issues(n7161=(PIPELINE, "NONE", "heal issue"))
	assert guard.build_automation_grant(_reissue_7209("files_touched:\n  not an item\n"), PIPELINE, trusted)["reason"] == "malformed_allowlist"
	human = dict(_reissue_7209("no block"), user={"login": "maintainer"}, author_association="OWNER")
	assert guard.build_automation_grant(human, PIPELINE, trusted)["reason"] == "no_allowlist"


# --- Pipeline plan grant (Q108) ---------------------------------------------

_GRANT_PLAN = """Implementation Plan

## Files likely to change
- `scripts/security_audit.sh` — call `audit()` with `--strict`
- `.github/workflows/security-audit.yml`
- tests/test_security_audit.py
- `README.md`
- `json.load()`
- `../etc/passwd`
- `docs/*.md`

## Functions/modules to implement
- `scripts/leak.sh`
"""


def test_extract_plan_file_paths_keeps_exact_files_from_the_files_section() -> None:
	assert guard.extract_plan_file_paths(_GRANT_PLAN) == [
		"scripts/security_audit.sh", ".github/workflows/security-audit.yml", "tests/test_security_audit.py", "README.md"]
	# No Files section: nothing to grant from.
	assert guard.extract_plan_file_paths("Edit `scripts/a.sh`.") == []
	# A bare first token is kept even when the line has a non-path backtick span.
	assert guard.extract_plan_file_paths(
		"## Files likely to change\n- README.md — update using `parse()`\n- Makefile: add `--strict`\n") == [
		"README.md", "Makefile"]
	for bad in ("scripts/", "/etc/x", "a//b.py", "scripts/../x.sh", "scripts/a b.sh", "-rf/x", "x.1",
			"https://example.com/a.py", "scripts/*.sh", "`", "json.load()", "v1.0"):
		assert guard._plan_grant_path(bad) == "", bad
	for good in ("Makefile", ".gitignore", "agents.md", "src/pkg/__init__.py", "scripts/a+b.py",
			"go.mod", "go.sum", "main.go", "build.rs", "pom.xml", "build.gradle"):
		assert guard._plan_grant_path(good) == good, good
	# Any letter-led extension marks a root file, so no planned root file is
	# left out of the grant; listed extensionless names and dotfiles count too.
	for good in ("main.c", "util.h", "app.php", "Pipfile", "Jenkinsfile", ".dockerignore", ".prettierrc",
			".gitlab-ci.yml", ".eslintrc.json", ".pre-commit-config.yaml"):
		assert guard._plan_grant_path(good) == good, good
	# Credential dotfiles and extensionless dot names (often directories) never are.
	for bad in (".env", ".env.local", ".env.production", ".npmrc", ".netrc", ".vscode", ".devcontainer"):
		assert guard._plan_grant_path(bad) == "", bad
	# Underscores are part of a name, never stripped as markdown emphasis.
	for good in ("__init__.py", "__main__.py", "_config.yml", "LICENSE", "scripts/LICENSE", "scripts/.hidden"):
		assert guard._plan_grant_path(good) == good, good
	# A slashless nested directory would cover every file beneath it
	# (entry_matches), so it is never written as an exact grant.
	for bad in ("scripts/lib", "docs/plans", ".github/ai", "src/conf.d", "scripts/v1.0", "api/v2.10",
			"docs/2024.01.15"):
		assert guard._plan_grant_path(bad) == "", bad
	# A letter-led extension still marks a file at any depth.
	for good in ("scripts/a.sh", "src/mod.rs", "web/x.mp3"):
		assert guard._plan_grant_path(good) == good, good


def test_build_pipeline_plan_grant_body_appends_a_canonical_block() -> None:
	body = guard.build_pipeline_plan_grant_body("Fix.\n\n---\n- Managed by: AI Orchestrator\n", ["a/b.py", "tests/**"])
	assert body.startswith("Fix.\n\n---\n- Managed by: AI Orchestrator\n\n---\n**Pipeline plan grant.**")
	assert body.endswith(guard.PIPELINE_PLAN_GRANT_MARKER + "\nfiles_touched:\n  - a/b.py\n  - tests/**\n"
		"  - changelog.d/*\n  - agents.d/*\n")
	assert guard.extract_files_touched(body) == ["a/b.py", "tests/**", "changelog.d/*", "agents.d/*"]


def _run_grant_cli(plan: str, meta: dict, flag: str = "true", allow_edits: str = "true") -> tuple[int, dict, str | None]:
	with tempfile.TemporaryDirectory() as td:
		tdp = Path(td)
		(tdp / "plan.md").write_text(plan, encoding="utf-8")
		(tdp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
		out = tdp / "grant_body.md"
		proc = subprocess.run([sys.executable, str(GUARD_SCRIPT), "--plan-auto-approval-gate",
			"--plan-file", str(tdp / "plan.md"), "--flag-enabled", flag, "--pipeline-login", "pipeline[bot]",
			"--allow-workflow-edits", allow_edits, "--issue-number", "42", "--issue-meta-file", str(tdp / "meta.json"),
			"--pipeline-grant-body-out", str(out)], capture_output=True, text=True, check=False)
		body = out.read_text(encoding="utf-8") if out.exists() and out.stat().st_size else None
		return proc.returncode, json.loads(proc.stdout), body


def test_plan_gate_proposes_pipeline_plan_grant_for_open_grant_only() -> None:
	code, record, body = _run_grant_cli(_GRANT_PLAN, _pipeline_meta())
	assert code == 0 and record["decision"] == "approve" and record["reason"] == "pipeline_plan_grant", record
	assert record["pipeline_grant_body"] is True and record["denied_paths"] == []
	assert body is not None and body.startswith(_pipeline_meta()["body"])
	assert guard.extract_files_touched(body)[:4] == guard.extract_plan_file_paths(_GRANT_PLAN)
	# The written body is an ordinary exact grant from then on.
	grant = guard.build_automation_grant(dict(_pipeline_meta(), body=body), "pipeline[bot]")
	assert grant["reason"] == "granted" and grant["open"] is False
	# Flag off, a maintainer issue without a block, or an issue that already
	# has a block: no proposal.
	assert _run_grant_cli(_GRANT_PLAN, _pipeline_meta(), flag="false")[1]["reason"] == "automation_path_plan_auto_approval_disabled"
	code, record, body = _run_grant_cli(_GRANT_PLAN, _owner_meta())
	assert record["reason"] == "no_allowlist" and body is None and "pipeline_grant_body" not in record
	code, record, body = _run_grant_cli(_GRANT_PLAN, dict(_pipeline_meta(), body=_body("scripts/security_audit.sh")))
	assert record["decision"] == "hold" and body is None


def test_plan_gate_pipeline_plan_grant_holds_when_the_plan_cannot_be_granted() -> None:
	# A listed directory stays protected and ungranted: hold, nothing written.
	plan = "## Files likely to change\n- `scripts/`\n- `scripts/a.sh`\n"
	code, record, body = _run_grant_cli(plan, _pipeline_meta())
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["denied_paths"] == ["scripts/"] and body is None
	plan = "## Files likely to change\n- `scripts/lib`\n- `scripts/a.sh`\n"
	code, record, body = _run_grant_cli(plan, _pipeline_meta())
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["denied_paths"] == ["scripts/lib"] and body is None
	plan = "## Files likely to change\n- `scripts/v1.0`\n- `scripts/a.sh`\n"
	code, record, body = _run_grant_cli(plan, _pipeline_meta())
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and record["denied_paths"] == ["scripts/v1.0"] and body is None
	# More files than the cap: no proposal; the hold names the size so the
	# plan stage can hand the plan to the orchestrator (Q151).
	many = "## Files likely to change\n" + "".join(f"- `scripts/f{i}.sh`\n" for i in range(51))
	code, record, body = _run_grant_cli(many, _pipeline_meta())
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and body is None
	assert record["reason"] == guard.PIPELINE_PLAN_TOO_LARGE_REASON == "plan_too_large_for_grant"
	assert record["plan_path_count"] == 51 and record["decision"] == "hold"
	# With workflow edits switched off the plan keeps that hold reason, so it
	# is never handed to a project whose child issues would be refused.
	code, record, body = _run_grant_cli(many, _pipeline_meta(), allow_edits="false")
	assert code == guard.EXIT_AUTOMATION_UNGRANTED and body is None
	assert record["reason"] == "workflow_edits_disabled" and "plan_path_count" not in record
	# Exactly at the cap the grant is still written; a repeated path counts once.
	at_cap = "## Files likely to change\n" + "".join(f"- `scripts/f{i}.sh`\n" for i in range(50)) + "- `scripts/f0.sh`\n"
	code, record, body = _run_grant_cli(at_cap, _pipeline_meta())
	assert record["reason"] == "pipeline_plan_grant" and "plan_path_count" not in record and body


# --- Flush-left files_touched lists (#7296, Q111) ---------------------------

_FLUSH_7296 = (
	"All must pass.\n\nfiles_touched:\n- scripts/pr_checks_lib.sh\n- scripts/orchestrate_poll_process.sh\n"
	"- scripts/implement_handle_guard_block.sh\n- changelog.d/*.md\n\nRefs #7126\n"
)


def test_flush_left_list_under_a_bare_key_parses() -> None:
	assert guard.extract_files_touched(_FLUSH_7296) == [
		"scripts/pr_checks_lib.sh", "scripts/orchestrate_poll_process.sh",
		"scripts/implement_handle_guard_block.sh", "changelog.d/*.md"]
	assert guard.files_touched_block_state(_FLUSH_7296)[0] == "ok"
	grant = guard.build_automation_grant(
		{"number": 7296, "user": {"login": "maintainer"}, "author_association": "OWNER", "body": _FLUSH_7296}, "pipeline[bot]")
	assert grant["reason"] == "granted"
	assert grant["paths"] == ["scripts/pr_checks_lib.sh", "scripts/orchestrate_poll_process.sh",
		"scripts/implement_handle_guard_block.sh"]


def test_flush_left_list_edges() -> None:
	# The first entry fixes the indent: mixing stops the list.
	assert guard.extract_files_touched("files_touched:\n  - a.py\n- b.py\n") == ["a.py"]
	assert guard.extract_files_touched("files_touched:\n- a.py\n  - b.py\n") == ["a.py"]
	# An indented key keeps its list flush with itself.
	assert guard.extract_files_touched("  files_touched:\n  - a.py\n  - b.py\n") == ["a.py", "b.py"]
	# `- files_touched:` is itself a list item: a flush `- x` is its sibling.
	assert guard.extract_files_touched("- files_touched:\n- a.py\n") is None
	assert guard.extract_files_touched("- files_touched:\n  - a.py\n") == ["a.py"]
	# Prose right after the key is still malformed.
	assert guard.files_touched_block_state("files_touched:\nsee below\n- a.py\n")[0] == "malformed"


def test_review_reject_verify_parser_matches_the_guard() -> None:
	# review_reject_verify.sh carries a verbatim copy of the parser.
	import inspect
	source = inspect.getsource(guard.extract_files_touched)
	body = source[source.index("\tlines = text.splitlines()"):]
	assert body in (REPO_ROOT / "scripts" / "review_reject_verify.sh").read_text(encoding="utf-8")


def _run_all() -> int:
	funcs = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
	failures = 0
	for fn in funcs:
		try:
			fn()
			print(f"ok   {fn.__name__}")
		except Exception as exc:  # noqa: BLE001 — test harness surfaces any failure
			failures += 1
			print(f"FAIL {fn.__name__}: {exc!r}")
	print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
	return 1 if failures else 0


if __name__ == "__main__":
	raise SystemExit(_run_all())
