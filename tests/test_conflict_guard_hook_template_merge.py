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

try:
	import pytest
except ImportError:  # script mode without pytest: record the mark main() reads
	import types

	def _xfail_without_pytest(**kwargs):
		def wrap(func):
			func.pytestmark = [*getattr(func, "pytestmark", []), types.SimpleNamespace(name="xfail", kwargs=kwargs)]
			return func
		return wrap

	pytest = types.SimpleNamespace(mark=types.SimpleNamespace(xfail=_xfail_without_pytest))

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"

LIVE = ".claude/hooks/pr_merge_status_guard.py"
TEMPLATE = "workflow-templates/.claude/hooks/pr_merge_status_guard.py"
# A second paired live hook (#7137). The template merge covers it the same
# way as pr_merge_status_guard.py (#8973, issue #9008).
PREPUSH_LIVE = ".claude/hooks/prepush_affected_tests.py"
PREPUSH_TEMPLATE = "workflow-templates/.claude/hooks/prepush_affected_tests.py"
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


def _make_conflict(
	repo: Path,
	variant: str = "pass",
	*,
	other_conflict: bool = False,
	live_rel: str = LIVE,
	template_rel: str = TEMPLATE,
) -> None:
	"""Leave ``repo`` on ``feat`` mid-merge of ``main`` with the live hook conflicted."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	_git(repo, "config", "core.fileMode", "true")
	_write(repo, live_rel, "old\n")
	if variant == "template_symlink":
		(repo / template_rel).parent.mkdir(parents=True)
		os.symlink("../../../" + live_rel, repo / template_rel)
	elif variant != "template_missing":
		_write(repo, template_rel, _body())
	_write(repo, "other.txt", "base\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	ours = _body(change_first="ours first line")
	theirs = _body(change_last="theirs last line")

	_git(repo, "checkout", "-q", "-b", "feat")
	if variant == "one_sided_delete":
		_git(repo, "rm", "-q", "--", live_rel)
	else:
		_write(repo, live_rel, ours + ("x" if variant == "ours_mismatch" else ""))
	if variant == "template_unmerged":
		_write(repo, template_rel, _body(change_first="ours template"))
		_write(repo, live_rel, _body(change_first="ours template"))
	elif variant not in ("template_missing", "template_symlink"):
		_write(repo, template_rel, ours)
	if other_conflict:
		_write(repo, "other.txt", "ours\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "ours")

	_git(repo, "checkout", "-q", "main")
	_write(repo, live_rel, theirs, 0o644 if variant == "theirs_mode_mismatch" else 0o755)
	if variant == "template_unmerged":
		_write(repo, template_rel, _body(change_first="theirs template"))
		_write(repo, live_rel, _body(change_first="theirs template"))
	elif variant not in ("template_missing", "template_symlink"):
		_write(repo, template_rel, theirs)
	if other_conflict:
		_write(repo, "other.txt", "theirs\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "theirs")

	_git(repo, "checkout", "-q", "feat")
	merge = _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
	assert merge.returncode != 0, "fixture must produce a conflicted merge"
	assert _git(repo, "ls-files", "-u", "--", live_rel).stdout.strip(), "live hook must be unmerged"


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


def _assert_resolved_from_template(repo: Path, *, live_rel: str = LIVE, template_rel: str = TEMPLATE) -> None:
	staged = _git(repo, "ls-files", "-s", "--", live_rel).stdout.strip()
	tpl = _git(repo, "ls-files", "-s", "--", template_rel).stdout.strip()
	tpl_mode, tpl_oid, tpl_stage = tpl.split("\t")[0].split()
	assert tpl_stage == "0", tpl
	assert staged == f"{tpl_mode} {tpl_oid} 0\t{live_rel}", staged
	assert _git(repo, "ls-files", "-u", "--", live_rel).stdout == ""
	live_bytes = (repo / live_rel).read_bytes()
	assert live_bytes == (repo / template_rel).read_bytes()
	assert b"<<<<<<<" not in live_bytes
	assert live_bytes == _body(change_first="ours first line", change_last="theirs last line").encode()
	assert os.access(repo / live_rel, os.X_OK) == (tpl_mode == "100755")


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


# ------------------------------------------------- second paired hook (#8973)


def test_prepush_hook_fixture_has_the_clean_template_shape() -> None:
	"""The prepush fixture has the shape the template merge accepts."""
	tmpdir, _tmp, repo = _with_repo(live_rel=PREPUSH_LIVE, template_rel=PREPUSH_TEMPLATE)
	with tmpdir:
		live_unmerged = _git(repo, "ls-files", "-u", "--", PREPUSH_LIVE).stdout.splitlines()
		assert {line.split()[2] for line in live_unmerged} == {"1", "2", "3"}
		assert _git(repo, "ls-files", "-u", "--", PREPUSH_TEMPLATE).stdout == ""
		tpl_entries = _git(repo, "ls-files", "-s", "--", PREPUSH_TEMPLATE).stdout.splitlines()
		assert len(tpl_entries) == 1 and tpl_entries[0].split("\t")[0].split()[2] == "0", tpl_entries
		for ref, stage in (("HEAD", "2"), ("MERGE_HEAD", "3")):
			tree = _git(repo, "ls-tree", "--full-tree", ref, "--", PREPUSH_TEMPLATE).stdout.strip()
			t_mode, t_type, t_oid = tree.split("\t")[0].split()
			assert t_type == "blob"
			live = [line.split()[:2] for line in live_unmerged if line.split()[2] == stage]
			assert live == [[t_mode, t_oid]], (ref, live, tree)


def test_prepush_hook_integration_sync_conflict_is_resolved_from_template() -> None:
	"""PR #8436 (run 42355218108): both sides changed the prepush hook and its template in step."""
	tmpdir, tmp, repo = _with_repo(live_rel=PREPUSH_LIVE, template_rel=PREPUSH_TEMPLATE)
	with tmpdir:
		region = _guard_and_union_region(_prepare_text()) + _hook_only_commit_region(_prepare_text())
		result, github_env, _ = _run(
			repo, tmp, region=region, head_ref="orchestrator/project-8434", integration_sync=True,
		)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "Guard-hook template merge: resolved " + PREPUSH_LIVE in result.stdout, out
		assert "REGION_FELL_THROUGH" not in result.stdout, out
		assert "CONFLICT_RESOLVED=true" in github_env.read_text(encoding="utf-8").splitlines()
		assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3
		_assert_resolved_from_template(repo, live_rel=PREPUSH_LIVE, template_rel=PREPUSH_TEMPLATE)


# ------------------------------------------ remaining paired hooks (#9298)

# The other live hooks with a template twin. session-start.sh is a shell
# script at mode 100755: the merge compares blob ids and index modes only, so
# a non-Python hook resolves the same way (issue #9298, suggested fix step 3).
OTHER_PAIRED_HOOKS = ("gh_api_write_guard.py", "pr_watch_guard.py", "session-start.sh")


def test_other_paired_hooks_are_resolved_from_template() -> None:
	for name in OTHER_PAIRED_HOOKS:
		live_rel, template_rel = f".claude/hooks/{name}", f"workflow-templates/.claude/hooks/{name}"
		tmpdir, tmp, repo = _with_repo(live_rel=live_rel, template_rel=template_rel)
		with tmpdir:
			region = _guard_and_union_region(_prepare_text()) + _hook_only_commit_region(_prepare_text())
			result, github_env, _ = _run(repo, tmp, region=region)
			out = result.stdout + result.stderr
			assert result.returncode == 0, (name, out)
			assert "Guard-hook template merge: resolved " + live_rel in result.stdout, out
			assert "REGION_FELL_THROUGH" not in result.stdout, out
			assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
			_assert_resolved_from_template(repo, live_rel=live_rel, template_rel=template_rel)
			assert _git(repo, "ls-files", "-s", "--", live_rel).stdout.split()[0] == "100755"


def _make_dual_conflict(repo: Path) -> None:
	"""Leave ``repo`` mid-merge with both paired live hooks conflicted in step with their templates."""
	pairs = ((LIVE, TEMPLATE), (PREPUSH_LIVE, PREPUSH_TEMPLATE))
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	_git(repo, "config", "core.fileMode", "true")
	for live_rel, template_rel in pairs:
		_write(repo, live_rel, "old\n")
		_write(repo, template_rel, _body())
	_write(repo, "other.txt", "base\n", 0o644)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-q", "-b", "feat")
	for live_rel, template_rel in pairs:
		_write(repo, live_rel, _body(change_first="ours first line"))
		_write(repo, template_rel, _body(change_first="ours first line"))
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "ours")
	_git(repo, "checkout", "-q", "main")
	for live_rel, template_rel in pairs:
		_write(repo, live_rel, _body(change_last="theirs last line"))
		_write(repo, template_rel, _body(change_last="theirs last line"))
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "theirs")
	_git(repo, "checkout", "-q", "feat")
	merge = _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
	assert merge.returncode != 0, "fixture must produce a conflicted merge"
	for live_rel, _template_rel in pairs:
		assert _git(repo, "ls-files", "-u", "--", live_rel).stdout.strip(), f"{live_rel} must be unmerged"


def test_both_paired_hooks_conflicting_are_resolved_in_one_pass() -> None:
	tmpdir = tempfile.TemporaryDirectory()
	with tmpdir:
		tmp = Path(tmpdir.name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_dual_conflict(repo)
		region = _guard_and_union_region(_prepare_text()) + _hook_only_commit_region(_prepare_text())
		result, github_env, _ = _run(
			repo, tmp, region=region, head_ref="orchestrator/project-9008", integration_sync=True,
		)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "Guard-hook template merge: resolved " + LIVE in result.stdout, out
		assert "Guard-hook template merge: resolved " + PREPUSH_LIVE in result.stdout, out
		assert result.stdout.index("resolved " + LIVE) < result.stdout.index("resolved " + PREPUSH_LIVE), out
		assert "REGION_FELL_THROUGH" not in result.stdout, out
		assert "CONFLICT_RESOLVED=true" in github_env.read_text(encoding="utf-8").splitlines()
		assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3
		_assert_resolved_from_template(repo)
		_assert_resolved_from_template(repo, live_rel=PREPUSH_LIVE, template_rel=PREPUSH_TEMPLATE)


# ---------------------------------------------------------------- contract


def test_guard_hook_pairs_mirror_paired_live_copies() -> None:
	import importlib.util
	import re
	import sys

	spec = importlib.util.spec_from_file_location(
		"review_untrusted_workspace_pairs", REPO_ROOT / "scripts" / "review_untrusted_workspace.py"
	)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	previous = sys.dont_write_bytecode
	sys.dont_write_bytecode = True
	try:
		spec.loader.exec_module(module)
	finally:
		sys.dont_write_bytecode = previous
	expected = list(module.PAIRED_LIVE_COPIES.items())

	region = _guard_region(_prepare_text())
	match = re.search(r"^GUARD_HOOK_TEMPLATE_PAIRS=\(\n(.*?)\n\)$", region, re.S | re.M)
	assert match, "GUARD_HOOK_TEMPLATE_PAIRS array not found"
	pairs = []
	for raw in match.group(1).splitlines():
		entry = raw.strip()
		assert entry.startswith('"') and entry.endswith('"'), raw
		entry = entry[1:-1].replace("${GUARD_HOOK_LIVE_PATH}", LIVE).replace("${GUARD_HOOK_TEMPLATE_PATH}", TEMPLATE)
		live, _, template = entry.partition("|")
		pairs.append((live, template))
	# Strict, same-order equality with the registry (issue #9298): a missing,
	# extra or reordered pair fails here; add a new pair to both.
	assert pairs == expected, (pairs, expected)
	assert len(set(pairs)) == len(pairs), pairs
	assert pairs[0] == (LIVE, TEMPLATE)
	assert (PREPUSH_LIVE, PREPUSH_TEMPLATE) in pairs



def test_block_runs_before_manifest_block_and_empty_allowlist_check() -> None:
	text = _prepare_text()
	call = '_conflict_prepare_guard_hook_template_merge "${_ghm_pair_live}" "${_ghm_pair_tpl}" || _ghm_rc=$?'
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
		expected = (
			f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/{name}",
			f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/{name}",
		)
		for workflow in ("ci.yml", "test-and-mark-stable.yml"):
			text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
			assert any(line in text for line in expected), f"{workflow} must run {name}"


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	failed = 0
	for func in test_funcs:
		expect_fail = any(getattr(mark, "name", "") == "xfail" for mark in getattr(func, "pytestmark", []))
		try:
			func()
		except AssertionError as e:
			if expect_fail:
				print(f"  XFAIL  {func.__name__}")
				continue
			print(f"  FAIL  {func.__name__}: {e}")
			failed += 1
			continue
		except Exception as e:  # noqa: BLE001
			print(f"  FAIL  {func.__name__}: {e}")
			failed += 1
			continue
		if expect_fail:
			print(f"  XPASS(strict)  {func.__name__}")
			failed += 1
		else:
			print(f"  PASS  {func.__name__}")
	print(f"\n{len(test_funcs) - failed} passed, {failed} failed, {len(test_funcs)} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
