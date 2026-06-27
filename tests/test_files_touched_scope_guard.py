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


# --------------------------------------------------------------------------
# Layer 3 — static wiring assertions
# --------------------------------------------------------------------------


def _implement_text() -> str:
	return IMPLEMENT.read_text(encoding="utf-8")


def _implement_commit_text() -> str:
	return IMPLEMENT_COMMIT_SCRIPT.read_text(encoding="utf-8")


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
	assert combined_text.count("python3 scripts/files_touched_scope_guard.py") == 3
	assert combined_text.count("scope_violation_blocked=out-of-scope") == 2
	assert "scope_violation_blocked=scope-lock-label" in commit_text


def test_alert_step_handles_scope() -> None:
	text = _implement_text()
	assert "scope_violation_blocked != ''" in text
	assert "gh label create 'ai:scope-blocked'" in text
	assert "SVB_REASON: ${{ steps.preflight_destructive_guard.outputs.scope_violation_blocked" in text


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
	assert f"--description '{desc}'" in _implement_text()
	# It is a latch label, not a phase label.
	for group in contract.get("phase_groups", []):
		assert "ai:scope-blocked" not in group.get("members", [])


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
