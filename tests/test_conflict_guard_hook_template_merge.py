#!/usr/bin/env python3
"""Deterministic resolution of the merged-PR guard hook from its template.

Background
==========

``.claude/hooks/pr_merge_status_guard.py`` is a host-executed safety hook.
``scripts/review_untrusted_workspace.py`` keeps it out of the resolver
sandbox, so a merge conflict on it made ``scripts/review_conflict_resolve.sh``
fail closed with ``sandbox_path_host_only`` on every run (heal #6800,
PR #6555; activation gap of PR #6803, issue #6882).

The live hook must stay byte- and mode-identical to its
``workflow-templates/`` twin. ``scripts/review_conflict_prepare.sh`` now
stages the template's clean merge result for the live path, but only when
each side's live copy equals that side's template (same blob, same mode)
and the template itself merged cleanly. Every other shape is refused and
left unmerged, so the existing fail-closed path still applies.

These tests run the live shell regions of the prepare script against real
conflicted ``git merge --no-commit`` scratch repositories.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"

LIVE = ".claude/hooks/pr_merge_status_guard.py"
TEMPLATE = "workflow-templates/.claude/hooks/pr_merge_status_guard.py"
MERGE_RESOLVE_COMMIT_MESSAGE = "[ai-merge-resolve] resolve merge conflicts"
BASE_LINES = [f"line {i}" for i in range(1, 11)]


def _prepare_text() -> str:
	return PREPARE.read_text(encoding="utf-8")


def _guard_and_union_region(text: str) -> str:
	start = text.index('GUARD_HOOK_LIVE_PATH=".claude/hooks/pr_merge_status_guard.py"')
	end = text.index("# When the allowlist is empty there is nothing for Codex to", start)
	return text[start:end]


def _guard_region(text: str) -> str:
	start = text.index('GUARD_HOOK_LIVE_PATH=".claude/hooks/pr_merge_status_guard.py"')
	end = text.index("# Deterministic resolution for the generated workspace manifest.", start)
	return text[start:end]


def _hook_only_commit_region(text: str) -> str:
	start = text.index("# First, a guard-hook-only conflict")
	end = text.index(
		'if [ "${_resolver_allowlist_count}" -eq 0 ] && [ "${_mu_defer_commit}" != "true" ]; then', start
	)
	return text[start:end]


def _fingerprint_and_deferred_block(text: str) -> str:
	start = text.index("# Fingerprint-violation expansion of the resolver working set.")
	end = text.index('CONFLICT_RESOLVER_SEMBLE_QUERY_FILE="${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-', start)
	return text[start:end]


def _env() -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}
	for name in ("CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED", "CONFLICT_MANIFEST_UNION_ENABLED", "TARGET_BRANCH"):
		env.pop(name, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _write(repo: Path, rel: str, text: str, mode: int = 0o755) -> None:
	path = repo / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	if path.is_symlink():
		path.unlink()
	path.write_text(text, encoding="utf-8")
	os.chmod(path, mode)


def _body(change_first: str | None = None, change_last: str | None = None) -> str:
	lines = list(BASE_LINES)
	if change_first is not None:
		lines[0] = change_first
	if change_last is not None:
		lines[-1] = change_last
	return "\n".join(lines) + "\n"


def _make_conflict(repo: Path, variant: str = "pass", *, other_conflict: bool = False) -> None:
	"""Leave ``repo`` on ``feat`` mid-merge of ``main`` with the live hook conflicted."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	_git(repo, "config", "core.fileMode", "true")
	_write(repo, LIVE, "old\n")
	if variant == "template_symlink":
		(repo / TEMPLATE).parent.mkdir(parents=True)
		os.symlink("../../../" + LIVE, repo / TEMPLATE)
	elif variant != "template_missing":
		_write(repo, TEMPLATE, _body())
	_write(repo, "other.txt", "base\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	ours = _body(change_first="ours first line")
	theirs = _body(change_last="theirs last line")

	_git(repo, "checkout", "-q", "-b", "feat")
	if variant == "one_sided_delete":
		_git(repo, "rm", "-q", "--", LIVE)
	else:
		_write(repo, LIVE, ours + ("x" if variant == "ours_mismatch" else ""))
	if variant == "template_unmerged":
		_write(repo, TEMPLATE, _body(change_first="ours template"))
		_write(repo, LIVE, _body(change_first="ours template"))
	elif variant not in ("template_missing", "template_symlink"):
		_write(repo, TEMPLATE, ours)
	if other_conflict:
		_write(repo, "other.txt", "ours\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "ours")

	_git(repo, "checkout", "-q", "main")
	_write(repo, LIVE, theirs, 0o644 if variant == "theirs_mode_mismatch" else 0o755)
	if variant == "template_unmerged":
		_write(repo, TEMPLATE, _body(change_first="theirs template"))
		_write(repo, LIVE, _body(change_first="theirs template"))
	elif variant not in ("template_missing", "template_symlink"):
		_write(repo, TEMPLATE, theirs)
	if other_conflict:
		_write(repo, "other.txt", "theirs\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "theirs")

	_git(repo, "checkout", "-q", "feat")
	merge = _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
	assert merge.returncode != 0, "fixture must produce a conflicted merge"
	assert _git(repo, "ls-files", "-u", "--", LIVE).stdout.strip(), "live hook must be unmerged"


def _run(
	repo: Path,
	tmp: Path,
	*,
	region: str,
	enabled: str | None = None,
	head_ref: str = "feat",
	integration_sync: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
	runtime_dir = tmp / "runtime"
	runtime_dir.mkdir()
	github_env = tmp / "github.env"
	stash = tmp / "stash"
	stash.mkdir()
	allowlist = runtime_dir / "resolver_unmerged_allowlist.txt"
	extra = ""
	if integration_sync:
		fp_file = runtime_dir / "integration_fingerprints.json"
		fp_file.write_text(json.dumps({
			"1500": {"issue": 1500, "pr": 1501, "must_contain": [{"file": "other.txt", "regex": "^base$"}], "must_not_contain": []},
		}), encoding="utf-8")
		extra = f"""CONFLICTED_FILES_RAW=""
IS_INTEGRATION_SYNC=true
INTEGRATION_FINGERPRINTS_FILE={fp_file!s}
SUPPORT_SCRIPTS_DIR={REPO_ROOT / "scripts"!s}
RESOLVER_FINGERPRINT_ONLY_PATHS_FILE={runtime_dir / "resolver_fingerprint_only_paths.txt"!s}
{_fingerprint_and_deferred_block(_prepare_text())}"""
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime_dir!s}
GITHUB_ENV={github_env!s}
RESOLVE_STASH={stash!s}
_merge_stderr_file="$(mktemp)"
_merge_exit=1
IS_WORKFLOW_SOURCE_REPO=true
HEAD_REF={head_ref}
RESOLVER_ALLOWLIST_FILE={allowlist!s}
git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_ALLOWLIST_FILE}}"
_resolver_allowlist_count="$(wc -l < "${{RESOLVER_ALLOWLIST_FILE}}" | tr -d '[:space:]')"
{region}
{extra}
echo "GHM_RESOLVED=${{_ghm_resolved}}"
echo "REGION_FELL_THROUGH"
"""
	env = _env()
	if enabled is not None:
		env["CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED"] = enabled
	if integration_sync:
		env["TARGET_BRANCH"] = head_ref
	result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
	return result, github_env, allowlist


def _assert_resolved_from_template(repo: Path) -> None:
	staged = _git(repo, "ls-files", "-s", "--", LIVE).stdout.strip()
	tpl = _git(repo, "ls-files", "-s", "--", TEMPLATE).stdout.strip()
	tpl_mode, tpl_oid, tpl_stage = tpl.split("\t")[0].split()
	assert tpl_stage == "0", tpl
	assert staged == f"{tpl_mode} {tpl_oid} 0\t{LIVE}", staged
	assert _git(repo, "ls-files", "-u", "--", LIVE).stdout == ""
	live_bytes = (repo / LIVE).read_bytes()
	assert live_bytes == (repo / TEMPLATE).read_bytes()
	assert b"<<<<<<<" not in live_bytes
	assert live_bytes == _body(change_first="ours first line", change_last="theirs last line").encode()
	assert os.access(repo / LIVE, os.X_OK) == (tpl_mode == "100755")


def _with_repo(variant: str = "pass", **kwargs):
	tmpdir = tempfile.TemporaryDirectory()
	tmp = Path(tmpdir.name)
	repo = tmp / "repo"
	repo.mkdir()
	_make_conflict(repo, variant, **kwargs)
	return tmpdir, tmp, repo


# ---------------------------------------------------------------- functional


def test_hook_only_conflict_is_resolved_and_committed() -> None:
	tmpdir, tmp, repo = _with_repo()
	with tmpdir:
		region = _guard_and_union_region(_prepare_text()) + _hook_only_commit_region(_prepare_text())
		result, github_env, _ = _run(repo, tmp, region=region)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "Guard-hook template merge: resolved " + LIVE in result.stdout, out
		assert "REGION_FELL_THROUGH" not in result.stdout, out
		assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
		parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
		assert len(parents) == 3, parents
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == MERGE_RESOLVE_COMMIT_MESSAGE
		_assert_resolved_from_template(repo)
		assert _git(repo, "ls-files", "-s", "--", LIVE).stdout.split()[0] == "100755"


def test_hook_resolved_with_other_conflict_leaves_only_other_for_resolver() -> None:
	tmpdir, tmp, repo = _with_repo(other_conflict=True)
	with tmpdir:
		region = _guard_and_union_region(_prepare_text()) + _hook_only_commit_region(_prepare_text())
		result, github_env, allowlist = _run(repo, tmp, region=region)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "GHM_RESOLVED=true" in result.stdout, out
		assert "REGION_FELL_THROUGH" in result.stdout, out
		assert allowlist.read_text(encoding="utf-8") == "other.txt\n"
		assert not github_env.exists()
		_assert_resolved_from_template(repo)
		assert _git(repo, "diff", "--name-only", "--diff-filter=U", "--").stdout.split() == ["other.txt"]


def test_hook_only_integration_sync_commits_after_fingerprint_check() -> None:
	tmpdir, tmp, repo = _with_repo()
	with tmpdir:
		region = _guard_and_union_region(_prepare_text()) + _hook_only_commit_region(_prepare_text())
		result, github_env, _ = _run(
			repo, tmp, region=region, head_ref="orchestrator/project-123", integration_sync=True,
		)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "Guard-hook template merge: integration-sync branch; deferring" in result.stdout, out
		assert "Guard-hook template merge: integration fingerprints verified" in result.stdout, out
		assert "REGION_FELL_THROUGH" not in result.stdout, out
		assert "CONFLICT_RESOLVED=true" in github_env.read_text(encoding="utf-8").splitlines()
		assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3
		_assert_resolved_from_template(repo)


def _assert_refused(variant: str, reason: str, *, enabled: str | None = None, markers: bool = True) -> None:
	tmpdir, tmp, repo = _with_repo(variant)
	with tmpdir:
		stages_before = _git(repo, "ls-files", "-u", "--", LIVE).stdout
		result, github_env, allowlist = _run(repo, tmp, region=_guard_region(_prepare_text()), enabled=enabled)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert f"Guard-hook template merge: skipped reason={reason}" in result.stdout, out
		assert "GHM_RESOLVED=false" in result.stdout, out
		assert _git(repo, "ls-files", "-u", "--", LIVE).stdout == stages_before
		assert LIVE in allowlist.read_text(encoding="utf-8").splitlines()
		assert not github_env.exists()
		if markers:
			assert b"<<<<<<<" in (repo / LIVE).read_bytes()


def test_refuses_when_kill_switch_off() -> None:
	_assert_refused("pass", "disabled", enabled="false")


def test_refuses_when_template_absent() -> None:
	_assert_refused("template_missing", "template_missing")


def test_refuses_when_template_also_conflicted() -> None:
	_assert_refused("template_unmerged", "template_unmerged")


def test_refuses_when_ours_live_differs_from_ours_template() -> None:
	_assert_refused("ours_mismatch", "ours_mismatch")


def test_refuses_when_theirs_mode_differs_from_theirs_template() -> None:
	_assert_refused("theirs_mode_mismatch", "theirs_mismatch")


def test_refuses_one_sided_delete() -> None:
	_assert_refused("one_sided_delete", "stage_shape", markers=False)


def test_refuses_symlinked_template() -> None:
	_assert_refused("template_symlink", "template_not_regular")


# ---------------------------------------------------------------- contract


def test_block_runs_before_manifest_block_and_empty_allowlist_check() -> None:
	text = _prepare_text()
	call = "_conflict_prepare_guard_hook_template_merge || _ghm_rc=$?"
	assert call in text
	assert text.index('RESOLVER_INITIAL_UNMERGED_PATHS_FILE="${RUNTIME_DIR}/resolver_initial_unmerged_paths.txt"') < text.index(call)
	assert text.index(call) < text.index("# Deterministic resolution for the generated workspace manifest.")
	assert text.index("# First, a guard-hook-only conflict") < text.index(
		'if [ "${_resolver_allowlist_count}" -eq 0 ] && [ "${_mu_defer_commit}" != "true" ]; then'
	)
	assert 'CONFLICT_GUARD_HOOK_TEMPLATE_MERGE_ENABLED:-true' in _guard_region(text)


def test_hook_only_commit_uses_shared_helper() -> None:
	text = _prepare_text()
	helper_start = text.index("_conflict_prepare_commit_deterministic_merge()\n{")
	helper = text[helper_start:text.index("\n}\n", helper_start)]
	assert f'git commit -m "{MERGE_RESOLVE_COMMIT_MESSAGE}"' in helper
	assert 'echo "CONFLICT_RESOLVED=true" >> "$GITHUB_ENV"' in helper
	assert "MERGE_CONFLICT=false" not in helper
	region = _hook_only_commit_region(text)
	assert '_conflict_prepare_commit_deterministic_merge "Guard-hook template merge"' in region
	assert "_mu_defer_commit=true" in region
	assert "orchestrator/project-*) _ghm_defer_commit=true ;;" in _guard_region(text)


def test_workflows_register_conflict_resolve_tests() -> None:
	for name in ("test_review_conflict_resolve_sandbox_only.py", "test_conflict_guard_hook_template_merge.py"):
		expected = f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/{name}"
		for workflow in ("ci.yml", "test-and-mark-stable.yml"):
			text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
			assert expected in text, f"{workflow} must run {name}"


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
		except Exception as e:  # noqa: BLE001
			print(f"  FAIL  {func.__name__}: {e}")
			failed += 1
	print(f"\n{len(test_funcs) - failed} passed, {failed} failed, {len(test_funcs)} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
