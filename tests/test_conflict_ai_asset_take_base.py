#!/usr/bin/env python3
"""Integration-sync take-base of `.ai/` validation config.

Background
==========

The resolver sandbox excludes `.ai/` (``scripts/review_untrusted_workspace.py``
``EXCLUDED``), and the host-only take-base in
``scripts/review_conflict_prepare.sh`` is off for ``orchestrator/project-*``
heads. A conflict on ``.ai/validate.yml`` therefore failed every
integration-sync resolver run closed with ``sandbox_path_host_only``
(tele-funtoken-msg-scoring final PR #3933, 2026-10-23).

The prepare script now takes the default branch's version of the allowlisted
`.ai/` files on ``orchestrator/project-*`` heads, defers the merge commit until
the integration fingerprint check has run, and posts the dropped
integration-branch diff on the PR. These tests run the live shell regions of
the prepare script against real conflicted ``git merge --no-commit`` scratch
repositories.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"

VALIDATE = ".ai/validate.yml"
PROJECT_BRANCH = "orchestrator/project-123"
BASE_YAML = "type: python-repo-checks\ncustom_tests:\n  - python3 -m pytest -q\n"
OURS_YAML = "type: python-repo-checks\ncustom_tests:\n  - python3 -m pytest -q --timeout=120 tests/test_a*.py\n"
THEIRS_YAML = "type: python-repo-checks\ncustom_tests:\n  - python3 -m pytest -q --timeout=300 tests\n"


def _prepare_text() -> str:
	return PREPARE.read_text(encoding="utf-8")


def _take_base_region(text: str) -> str:
	start = text.index("# >>> .ai/ validation asset take-base")
	end = text.index("# Deterministic resolution for the generated workspace manifest.", start)
	return text[start:end]


def _fallthrough_region(text: str) -> str:
	start = text.index(
		'if [ "${_resolver_allowlist_count}" -eq 0 ] && [ "${_mu_defer_commit}" != "true" ] && [ "${_ff_defer_commit}" != "true" ] \\\n'
	)
	end = text.index("# The stderr file is no longer needed past this point", start)
	return text[start:end]


def _fingerprint_and_deferred_block(text: str) -> str:
	start = text.index("# Fingerprint-violation expansion of the resolver working set.")
	end = text.index('CONFLICT_RESOLVER_SEMBLE_QUERY_FILE="${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-', start)
	return text[start:end]


def _env() -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}
	for name in ("CONFLICT_AI_ASSET_TAKE_BASE_ENABLED", "TARGET_BRANCH", "HEAD_REF", "PR_NUMBER"):
		env.pop(name, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
	path = repo / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _make_conflict(repo: Path, *, head: str = PROJECT_BRANCH, other_conflict: bool = False, base_deletes: bool = False) -> None:
	"""Leave ``repo`` on ``head`` mid-merge of ``main`` with ``.ai/validate.yml`` conflicted."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	_write(repo, VALIDATE, BASE_YAML)
	_write(repo, "other.txt", "base\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	_git(repo, "checkout", "-q", "-b", head)
	_write(repo, VALIDATE, OURS_YAML)
	if other_conflict:
		_write(repo, "other.txt", "ours\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "ours")

	_git(repo, "checkout", "-q", "main")
	if base_deletes:
		_git(repo, "rm", "-q", "--", VALIDATE)
	else:
		_write(repo, VALIDATE, THEIRS_YAML)
	if other_conflict:
		_write(repo, "other.txt", "theirs\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "theirs")

	_git(repo, "checkout", "-q", head)
	merge = _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
	assert merge.returncode != 0, "fixture must produce a conflicted merge"
	assert _git(repo, "ls-files", "-u", "--", VALIDATE).stdout.strip(), ".ai/validate.yml must be unmerged"


def _run(
	repo: Path,
	tmp: Path,
	*,
	head: str = PROJECT_BRANCH,
	switch: str | None = None,
	fingerprints: dict | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
	runtime_dir = tmp / "runtime"
	runtime_dir.mkdir()
	github_env = tmp / "github.env"
	gh_log = tmp / "gh.log"
	stash = tmp / "stash"
	stash.mkdir()
	allowlist = runtime_dir / "resolver_unmerged_allowlist.txt"
	fp_file = runtime_dir / "integration_fingerprints.json"
	fp_file.write_text(json.dumps(fingerprints or {
		"1500": {"issue": 1500, "pr": 1501, "must_contain": [{"file": "other.txt", "regex": "^base$"}], "must_not_contain": []},
	}), encoding="utf-8")
	text = _prepare_text()
	integration = head.startswith("orchestrator/project-")
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime_dir!s}
GITHUB_ENV={github_env!s}
RESOLVE_STASH={stash!s}
_merge_stderr_file="$(mktemp)"
_merge_exit=1
IS_WORKFLOW_SOURCE_REPO=true
HEAD_REF={head}
TARGET_BRANCH={head}
BASE_BRANCH=main
PR_NUMBER=42
GITHUB_REPOSITORY=owner/repo
gh_retry() {{ printf '%s\\n' "$*" >> {gh_log!s}; cat "${{@: -1}}" >/dev/null 2>&1 || true; return 0; }}
RESOLVER_ALLOWLIST_FILE={allowlist!s}
RESOLVER_FINGERPRINT_ONLY_PATHS_FILE={runtime_dir / "resolver_fingerprint_only_paths.txt"!s}
git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_ALLOWLIST_FILE}}"
_resolver_allowlist_count="$(wc -l < "${{RESOLVER_ALLOWLIST_FILE}}" | tr -d '[:space:]')"
_mu_defer_commit=false
_ff_defer_commit=false
_mu_resolved=false
_ghm_resolved=false
{_take_base_region(text)}
{_fallthrough_region(text)}
CONFLICTED_FILES_RAW=""
IS_INTEGRATION_SYNC={"true" if integration else "false"}
INTEGRATION_FINGERPRINTS_FILE={fp_file!s}
SUPPORT_SCRIPTS_DIR={REPO_ROOT / "scripts"!s}
{_fingerprint_and_deferred_block(text)}
echo "AI_RESOLVED=${{_ai_resolved}}"
echo "REGION_FELL_THROUGH"
"""
	env = _env()
	if switch is not None:
		env["CONFLICT_AI_ASSET_TAKE_BASE_ENABLED"] = switch
	result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
	return result, runtime_dir, gh_log


def test_validate_only_conflict_takes_base_and_commits_after_fingerprint_check() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo)
		result, runtime_dir, gh_log = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "CONFLICT_RESOLVER_AI_ASSET_TAKE_BASE pr=42 path=.ai/validate.yml outcome=taken base=main stages=1 2 3" in out
		assert "integration fingerprints verified on the merged tree" in out
		assert "REGION_FELL_THROUGH" not in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
		assert len(_git(repo, "log", "-1", "--format=%P").stdout.split()) == 2, "must be a two-parent merge commit"
		assert _git(repo, "show", f"HEAD:{VALIDATE}").stdout == THEIRS_YAML
		assert "CONFLICT_RESOLVED=true" in (tmp / "github.env").read_text(encoding="utf-8")
		posted = runtime_dir / "ai_asset_take_base_comment.md.posted"
		assert posted.is_file(), out
		body = posted.read_text(encoding="utf-8")
		assert body.startswith("<!-- ai:ai-asset-take-base:v1 pr=42 head=")
		assert "--timeout=120" in body, "the dropped integration-branch diff must be shown"
		assert "repos/owner/repo/issues/42/comments" in gh_log.read_text(encoding="utf-8")


def test_other_conflicts_remain_for_resolver_and_comment_waits_for_commit() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, other_conflict=True)
		result, runtime_dir, gh_log = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "REGION_FELL_THROUGH" in out
		assert "AI_RESOLVED=true" in out
		assert not _git(repo, "ls-files", "-u", "--", VALIDATE).stdout.strip()
		assert _git(repo, "ls-files", "-u", "--", "other.txt").stdout.strip()
		assert (repo / VALIDATE).read_text(encoding="utf-8") == THEIRS_YAML
		assert (runtime_dir / "resolver_unmerged_allowlist.txt").read_text(encoding="utf-8").split() == ["other.txt"]
		assert (runtime_dir / "ai_asset_take_base_comment.md").is_file()
		assert not gh_log.exists(), "the comment is posted only after the merge commit"


def test_base_deletion_is_honoured() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, base_deletes=True)
		result, _runtime_dir, _gh_log = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "outcome=taken base=main stages=1 2" in out
		assert _git(repo, "ls-tree", "HEAD", "--", VALIDATE).stdout == ""


def test_non_integration_head_is_untouched() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, head="feat")
		result, _runtime_dir, _gh_log = _run(repo, tmp, head="feat")
		out = result.stdout + result.stderr
		assert "CONFLICT_RESOLVER_AI_ASSET_TAKE_BASE" not in out
		assert _git(repo, "ls-files", "-u", "--", VALIDATE).stdout.strip()


def test_kill_switch_leaves_conflict_fail_closed() -> None:
	for switch, warns in (("false", False), ("FALSE", False), ("maybe", True)):
		with tempfile.TemporaryDirectory() as tmp_name:
			tmp = Path(tmp_name)
			repo = tmp / "repo"
			repo.mkdir()
			_make_conflict(repo, other_conflict=True)
			result, _runtime_dir, _gh_log = _run(repo, tmp, switch=switch)
			out = result.stdout + result.stderr
			assert "outcome=taken" not in out, switch
			assert ("unrecognised value" in out) is warns, switch
			assert _git(repo, "ls-files", "-u", "--", VALIDATE).stdout.strip(), switch


def test_fingerprint_violation_blocks_the_deferred_commit() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo)
		fingerprints = {"1500": {"issue": 1500, "pr": 1501, "must_contain": [{"file": "other.txt", "regex": "^never$"}], "must_not_contain": []}}
		result, runtime_dir, gh_log = _run(repo, tmp, fingerprints=fingerprints)
		out = result.stdout + result.stderr
		assert result.returncode == 1, out
		assert ".ai asset take-base: unhandled reason=integration_sync" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "ours"
		assert not gh_log.exists()
		assert not (runtime_dir / "ai_asset_take_base_comment.md.posted").exists()


def test_symlinked_base_copy_is_refused() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_git(repo, "init", "-q", "-b", "main")
		_git(repo, "config", "user.name", "t")
		_git(repo, "config", "user.email", "t@t")
		_write(repo, VALIDATE, BASE_YAML)
		_git(repo, "add", "-A")
		_git(repo, "commit", "-qm", "base")
		_git(repo, "checkout", "-q", "-b", PROJECT_BRANCH)
		_write(repo, VALIDATE, OURS_YAML)
		_git(repo, "commit", "-qam", "ours")
		_git(repo, "checkout", "-q", "main")
		(repo / VALIDATE).unlink()
		os.symlink("/etc/passwd", repo / VALIDATE)
		_git(repo, "add", "-A")
		_git(repo, "commit", "-qm", "theirs")
		_git(repo, "checkout", "-q", PROJECT_BRANCH)
		_git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
		assert _git(repo, "ls-files", "-u", "--", VALIDATE).stdout.strip()
		result, _runtime_dir, _gh_log = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert "outcome=skip reason=base_not_regular" in out or "outcome=skip reason=unsafe_path" in out, out
		assert "outcome=taken" not in out


def test_resolver_commit_tail_posts_ai_asset_comment() -> None:
	text = RESOLVE.read_text(encoding="utf-8")
	tail = text[text.index('git commit -m "[ai-merge-resolve] resolve merge conflicts"\n  # Host-only paths'):]
	assert '"${RUNTIME_DIR}/ai_asset_take_base_comment.md"' in tail.split("CONFLICT_RESOLVED=true", 1)[0]


def test_guard_hook_commit_defers_when_ai_asset_was_taken() -> None:
	text = _prepare_text()
	region = text[text.index("# First, a guard-hook-only conflict"):]
	assert '&& [ "${_ai_resolved:-false}" != "true" ]; then' in region.split("_conflict_prepare_commit_deterministic_merge \"Guard-hook template merge\"", 1)[0]
