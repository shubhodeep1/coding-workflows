"""Issue #6680: stall recovery re-issues a PR whose only unresolvable conflict is
a retired host-only file the base no longer has.

Covers the pure classifier in ``scripts/orchestrate_lib.py``, its
``retired-conflict-check`` CLI, the poller helper
``_stall_retired_host_only_conflict_check`` against a scratch git origin, the
call-site wiring in both stall-recovery paths, and the unchanged fail-closed
resolver sandbox check.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
POLLER = SCRIPTS / "orchestrate_poll_process.sh"
sys.path.insert(0, str(SCRIPTS))

import orchestrate_lib as lib  # noqa: E402
import review_untrusted_workspace as ruw  # noqa: E402

RETIRED = ".claude/scripts/permission_prompts.py"
LIVE_HOOK = ".claude/hooks/pr_merge_status_guard.py"
SHA = "a" * 64
MANIFEST = f"# comment\n\n{RETIRED} {SHA}\n.claude/scripts/edit_comment.py {SHA} {'b' * 64}\n"


def classify(paths, base_present=(), manifest=MANIFEST):
	return lib.classify_retired_host_only_conflicts(paths, manifest, list(base_present), ruw.allowed)


# --- classifier ---------------------------------------------------------------


def test_retired_path_absent_from_base_reissues() -> None:
	result = classify([RETIRED])
	assert result["decision"] == "reissue"
	assert result["retired_paths"] == [RETIRED] and result["blocking_paths"] == []


def test_retired_path_plus_ordinary_conflict_reissues() -> None:
	result = classify([RETIRED, "scripts/review_apply_fixes.sh"])
	assert result["decision"] == "reissue"
	assert result["other_paths"] == ["scripts/review_apply_fixes.sh"]


def test_retired_path_present_on_base_keeps_resolver() -> None:
	result = classify([RETIRED], base_present=[RETIRED])
	assert result["decision"] == "no_match" and result["reason"] == "retired_present_on_base"


@pytest.mark.parametrize("paths", [[LIVE_HOOK], [LIVE_HOOK, RETIRED]])
def test_live_host_only_conflict_keeps_resolver(paths) -> None:
	result = classify(paths)
	assert result["decision"] == "no_match" and result["reason"] == "host_only_not_retired"
	assert result["blocking_paths"] == [LIVE_HOOK]


def test_only_sandbox_allowed_paths_keeps_resolver() -> None:
	result = classify(["scripts/a.py", "README.md"])
	assert result["decision"] == "no_match" and result["reason"] == "no_host_only"


@pytest.mark.parametrize("paths", [[], ["../etc/passwd"], ["a//b.py"], ["bad name.py"], ["-x.py"], ["a\nb.py"]])
def test_unsafe_or_empty_conflict_paths_are_unavailable(paths) -> None:
	assert classify(paths)["decision"] == "unavailable"


@pytest.mark.parametrize("manifest", [f"{RETIRED}\n", f"{RETIRED} nothex\n", f"../x {SHA}\n"])
def test_malformed_manifest_is_unavailable(manifest) -> None:
	result = classify([RETIRED], manifest=manifest)
	assert result["decision"] == "unavailable" and result["reason"] == "manifest_malformed"


def test_policy_error_is_unavailable() -> None:
	def boom(_name):
		raise RuntimeError("policy")

	result = lib.classify_retired_host_only_conflicts([RETIRED], MANIFEST, [], boom)
	assert result["decision"] == "unavailable" and result["reason"] == "policy_error"


def test_shipped_manifest_parses() -> None:
	text = (REPO_ROOT / "workflow-templates" / "retired_files.txt").read_text(encoding="utf-8")
	retired = lib.parse_retired_files_manifest(text)
	assert retired and RETIRED in retired
	# Every shipped entry is host-only, so the classifier can act on it.
	assert all(not ruw.allowed(path) for path in retired)


# --- CLI ----------------------------------------------------------------------


def _support(tmp_path: Path, *, manifest: str | None = MANIFEST, module: bool = True) -> Path:
	support = tmp_path / "support"
	(support / "scripts").mkdir(parents=True)
	(support / "workflow-templates").mkdir()
	shutil.copy2(SCRIPTS / "orchestrate_lib.py", support / "scripts" / "orchestrate_lib.py")
	if module:
		shutil.copy2(SCRIPTS / "review_untrusted_workspace.py", support / "scripts" / "review_untrusted_workspace.py")
	if manifest is not None:
		(support / "workflow-templates" / "retired_files.txt").write_text(manifest, encoding="utf-8")
	return support


def _run_cli(tmp_path: Path, support: Path, paths: list[str], base_present: list[str] = ()) -> dict:
	conflicts = tmp_path / "conflicts.txt"
	present = tmp_path / "present.txt"
	conflicts.write_text("".join(p + "\n" for p in paths), encoding="utf-8")
	present.write_text("".join(p + "\n" for p in base_present), encoding="utf-8")
	result = subprocess.run(
		[sys.executable, "-I", "-B", str(support / "scripts" / "orchestrate_lib.py"), "retired-conflict-check",
			"--support-dir", str(support), "--conflict-paths-file", str(conflicts), "--base-present-file", str(present)],
		capture_output=True, text=True, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def test_cli_reissue(tmp_path: Path) -> None:
	assert _run_cli(tmp_path, _support(tmp_path), [RETIRED])["decision"] == "reissue"


def test_cli_missing_manifest_is_unavailable(tmp_path: Path) -> None:
	result = _run_cli(tmp_path, _support(tmp_path, manifest=None), [RETIRED])
	assert result["decision"] == "unavailable" and result["reason"] == "support_untrusted"


def test_cli_symlinked_manifest_is_unavailable(tmp_path: Path) -> None:
	support = _support(tmp_path, manifest=None)
	real = tmp_path / "elsewhere.txt"
	real.write_text(MANIFEST, encoding="utf-8")
	(support / "workflow-templates" / "retired_files.txt").symlink_to(real)
	assert _run_cli(tmp_path, support, [RETIRED])["decision"] == "unavailable"


def test_cli_missing_policy_module_is_unavailable(tmp_path: Path) -> None:
	assert _run_cli(tmp_path, _support(tmp_path, module=False), [RETIRED])["decision"] == "unavailable"


# --- resolver fail-closed check stays unchanged ---------------------------------


def test_resolver_check_paths_still_reports_retired_path_host_only(tmp_path: Path) -> None:
	host = tmp_path / "host"
	host.mkdir()
	paths = tmp_path / "paths.txt"
	report = tmp_path / "report.txt"
	paths.write_text(RETIRED + "\n", encoding="utf-8")
	result = subprocess.run(
		[sys.executable, "-B", str(SCRIPTS / "review_untrusted_workspace.py"), "check-paths", str(host), str(paths), str(report)],
		capture_output=True, text=True, check=False,
	)
	assert result.returncode == 1
	assert report.read_text(encoding="utf-8") == f"host_only\t{RETIRED}\n"


def test_resolver_never_reads_the_retired_manifest() -> None:
	assert "retired_files" not in (SCRIPTS / "review_conflict_resolve.sh").read_text(encoding="utf-8")
	assert "retired_files" not in (SCRIPTS / "review_untrusted_workspace.py").read_text(encoding="utf-8")


# --- call-site wiring -------------------------------------------------------------


POLLER_TEXT = POLLER.read_text(encoding="utf-8")


def test_managed_resolve_merge_conflict_reroutes_before_dispatch() -> None:
	start = POLLER_TEXT.index('    resolve_merge_conflict)\n      local target_pr="${STALL_JUDGE_TARGET_PR:-}"')
	dispatch = POLLER_TEXT.index('_dispatch_review_for_conflicts "${target_pr}" "${head_ref}"', start)
	block = POLLER_TEXT[start:dispatch]
	check = block.index('_stall_retired_host_only_conflict_check "${issue_num}" "${target_pr}" "${pr_json}"')
	assert check < block.index("update-branch")
	assert '"close_and_reissue"' in block[check:]


def test_retrigger_review_override_keeps_reissue_outcome() -> None:
	call = POLLER_TEXT.index('execute_stall_recovery_action "${issue_num}" "${phase}" "resolve_merge_conflict" "${recovery_count}" "${local_id}" "${stall_minutes}" || _rtr_rc=$?')
	following = POLLER_TEXT[call:call + 600]
	assert 'if [ "${STALL_RECOVERY_EFFECTIVE_ACTION}" = "close_and_reissue" ]; then' in following


def test_managed_open_pr_guard_reroutes_before_dispatch() -> None:
	dispatch = POLLER_TEXT.index('_dispatch_review_for_conflicts "${_lpr_num}" "${_opr_head_ref}" || _opr_dispatch_rc=$?')
	block = POLLER_TEXT[dispatch - 1500:dispatch]
	assert '_stall_retired_host_only_conflict_check "${issue_num}" "${_lpr_num}" "${_opr_json}"' in block
	assert 'execute_stall_recovery_action "${issue_num}" "${phase}" "close_and_reissue"' in block


def test_standalone_guard_reroutes_to_close_and_reissue_case() -> None:
	guard = POLLER_TEXT.index('&& _stall_retired_host_only_conflict_check "${issue_num}" "${STALL_CONFLICT_PR_NUM}" "${_std_conflict_linked}"; then')
	reroute = POLLER_TEXT.index('action="close_and_reissue"', guard)
	dispatch = POLLER_TEXT.index('_dispatch_review_for_conflicts "${STALL_CONFLICT_PR_NUM}" "${STALL_CONFLICT_HEAD_REF}"', guard)
	case_start = POLLER_TEXT.index('    case "${action}" in\n      run_stall_judge)', guard)
	reissue_case = POLLER_TEXT.index("      close_and_reissue)\n", case_start)
	assert guard < reroute < dispatch < case_start < reissue_case
	assert "elif _check_open_pr_conflict_guard" in POLLER_TEXT[reroute:dispatch]


def test_both_reissue_bodies_append_issue_bound_guidance() -> None:
	assert POLLER_TEXT.count('[ "${STALL_REISSUE_EXTRA_GUIDANCE_ISSUE}" = "${issue_num}" ]') == 2
	assert 'close_linked_pr "${issue_num}" "${_stall_reissue_close_msg}"' in POLLER_TEXT
	assert 'close_linked_pr "${issue_num}" "${_std_reissue_close_msg}"' in POLLER_TEXT


# --- poller helper against a scratch git origin ---------------------------------


def _extract_function(name: str) -> str:
	match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", POLLER_TEXT, re.S | re.M)
	assert match, name
	return match.group(0)


HELPER_SOURCE = "\n".join(_extract_function(n) for n in (
	"_list_integration_conflict_files",
	"_stall_retired_conflict_branch_ok",
	"_stall_retired_host_only_conflict_check",
))

GIT_ENV = {
	"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
	"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
	"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
	env = {**os.environ, **GIT_ENV}
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
		env.pop(key, None)
	return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


def _scratch(tmp_path: Path, *, base_deletes: str, conflict_file: str = RETIRED) -> tuple[Path, str]:
	origin = tmp_path / "origin.git"
	_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
	work = tmp_path / "work"
	_git(tmp_path, "clone", "-q", str(origin), str(work))
	for rel in (conflict_file, "scripts/a.py"):
		(work / rel).parent.mkdir(parents=True, exist_ok=True)
		(work / rel).write_text("base\n", encoding="utf-8")
	_git(work, "add", "-A")
	_git(work, "commit", "-qm", "base")
	_git(work, "push", "-q", "origin", "HEAD:main")
	_git(work, "checkout", "-qb", "ai/issue-7")
	(work / conflict_file).write_text("head edit\n", encoding="utf-8")
	(work / "scripts/a.py").write_text("head\n", encoding="utf-8")
	_git(work, "commit", "-qam", "head")
	_git(work, "push", "-q", "origin", "ai/issue-7")
	head_sha = _git(work, "rev-parse", "HEAD")
	_git(work, "checkout", "-q", "main")
	if base_deletes:
		_git(work, "rm", "-q", conflict_file)
	else:
		(work / conflict_file).write_text("base edit\n", encoding="utf-8")
	_git(work, "commit", "-qam", "base change", "--allow-empty")
	_git(work, "push", "-q", "origin", "main")
	poller = tmp_path / "poller"
	_git(tmp_path, "clone", "-q", str(origin), str(poller))
	return poller, head_sha


def _run_helper(tmp_path: Path, poller: Path, pr: dict, *, support: bool = True, extra_env: dict | None = None):
	workspace = tmp_path / "ws"
	workspace.mkdir(exist_ok=True)
	if support:
		src = workspace / ".codex-workflow-src"
		if not src.exists():
			(src / "scripts").mkdir(parents=True)
			(src / "workflow-templates").mkdir()
			shutil.copy2(SCRIPTS / "orchestrate_lib.py", src / "scripts" / "orchestrate_lib.py")
			shutil.copy2(SCRIPTS / "review_untrusted_workspace.py", src / "scripts" / "review_untrusted_workspace.py")
			shutil.copy2(REPO_ROOT / "workflow-templates" / "retired_files.txt", src / "workflow-templates" / "retired_files.txt")
	pr_file = tmp_path / "pr.json"
	pr_file.write_text(json.dumps(pr), encoding="utf-8")
	script = (
		"set -uo pipefail\n"
		'_fetch_pr_json() { cat "${PR_FILE}"; }\n'
		+ HELPER_SOURCE
		+ '\nif _stall_retired_host_only_conflict_check 7 42 "$(cat "${PR_FILE}")"; then rc=0; else rc=1; fi\n'
		'printf "RC=%s\\nPATHS=%s\\nBASE=%s\\nISSUE=%s\\n" "$rc" "${STALL_RETIRED_CONFLICT_PATHS}" "${STALL_RETIRED_CONFLICT_BASE}" "${STALL_REISSUE_EXTRA_GUIDANCE_ISSUE:-}"\n'
		'printf "GUIDANCE=%s\\n" "${STALL_REISSUE_EXTRA_GUIDANCE}"\n'
	)
	env = {**os.environ, **GIT_ENV, "GITHUB_WORKSPACE": str(workspace), "GITHUB_REPOSITORY": "o/r",
		"RUNTIME_DIR": str(tmp_path), "PR_FILE": str(pr_file), **(extra_env or {})}
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "STALL_RETIRED_CONFLICT_REISSUE_ENABLED"):
		if key not in (extra_env or {}):
			env.pop(key, None)
	return subprocess.run(["bash", "-c", script], cwd=poller, env=env, capture_output=True, text=True, check=False)


def _pr(head_sha: str, *, repo: str = "o/r") -> dict:
	return {"state": "open", "head": {"ref": "ai/issue-7", "sha": head_sha, "repo": {"full_name": repo}}, "base": {"ref": "main"}}


needs_tools = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("git") is None, reason="jq and git are required")


@needs_tools
def test_helper_reissues_retired_conflict_absent_from_base(tmp_path: Path) -> None:
	poller, head_sha = _scratch(tmp_path, base_deletes=RETIRED)
	result = _run_helper(tmp_path, poller, _pr(head_sha))
	assert "RC=0" in result.stdout, result.stdout + result.stderr
	assert f"PATHS={RETIRED}" in result.stdout and "BASE=main" in result.stdout and "ISSUE=7" in result.stdout
	assert f"`{RETIRED}`" in result.stdout and "`scripts/a.py`" in result.stdout
	assert "outcome=reissue" in result.stdout


@needs_tools
def test_helper_live_host_only_conflict_fails_closed(tmp_path: Path) -> None:
	poller, head_sha = _scratch(tmp_path, base_deletes="", conflict_file=LIVE_HOOK)
	result = _run_helper(tmp_path, poller, _pr(head_sha))
	assert "RC=1" in result.stdout and "reason=host_only_not_retired" in result.stdout, result.stdout + result.stderr


@needs_tools
def test_helper_head_moved_fails_closed(tmp_path: Path) -> None:
	poller, _head_sha = _scratch(tmp_path, base_deletes=RETIRED)
	result = _run_helper(tmp_path, poller, _pr("c" * 40))
	assert "RC=1" in result.stdout and "reason=head_moved" in result.stdout, result.stdout + result.stderr


@needs_tools
def test_helper_kill_switch_fails_closed(tmp_path: Path) -> None:
	poller, head_sha = _scratch(tmp_path, base_deletes=RETIRED)
	result = _run_helper(tmp_path, poller, _pr(head_sha), extra_env={"STALL_RETIRED_CONFLICT_REISSUE_ENABLED": "false"})
	assert "RC=1" in result.stdout and "reason=disabled" in result.stdout


@needs_tools
def test_helper_fork_head_fails_closed(tmp_path: Path) -> None:
	poller, head_sha = _scratch(tmp_path, base_deletes=RETIRED)
	result = _run_helper(tmp_path, poller, _pr(head_sha, repo="fork/r"))
	assert "RC=1" in result.stdout and "reason=head_not_same_repo" in result.stdout


@needs_tools
def test_helper_missing_support_fails_closed(tmp_path: Path) -> None:
	poller, head_sha = _scratch(tmp_path, base_deletes=RETIRED)
	result = _run_helper(tmp_path, poller, _pr(head_sha), support=False)
	assert "RC=1" in result.stdout and "reason=support_unavailable" in result.stdout
