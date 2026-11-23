#!/usr/bin/env python3
"""Host-side `.gitleaksignore` union in the conflict-resolver prepare step (#9659).

Background
==========

A one-line `.gitleaksignore` fingerprint landed on the default branch from two
pull requests with different comment blocks. The integration-sync resolver
then failed closed with ``sandbox_path_host_only`` on `.gitleaksignore`
(shubhodeep1/binance-blessings PR #585): the resolver sandbox excludes root
dotfiles, and both sides had added the identical fingerprint line.

``scripts/review_conflict_prepare.sh`` now resolves that path on the trusted
runner: the base branch's file is kept byte for byte, and each fingerprint
line the PR side added that is not already present is appended with its
comment lines. Every unsafe shape (deletions, malformed lines, symlinks, an
off switch) leaves the path unmerged. These tests run the live shell regions
of the prepare script against real conflicted ``git merge --no-commit``
scratch repositories.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"

GL = ".gitleaksignore"
MANIFEST = ".ai/.workspace_source_manifest.txt"
PROJECT_BRANCH = "orchestrator/project-576"
PR_BRANCH = "ai/issue-615"
FP_BASE = "a" * 40 + ":docs/old.md:generic-api-key:3"
FP_SHARED = "b60720d4" + "0" * 32 + ":docs/plan.md:generic-api-key:12"
FP_OURS = "c" * 40 + ":README.md:generic-api-key:7"
FP_THEIRS = "d" * 40 + ":scripts/x.sh:generic-api-key:9"
BASE_TEXT = f"# gitleaks false positives\n{FP_BASE}\n"


def _prepare_text() -> str:
	return PREPARE.read_text(encoding="utf-8")


def _union_region(text: str) -> str:
	# The manifest region holds the deterministic-merge commit helper the union
	# calls, the union block itself, and the manifest union-merge after it.
	start = text.index('MANIFEST_UNION_PATH=".ai/.workspace_source_manifest.txt"')
	end = text.index("# When the allowlist is empty there is nothing for Codex to", start)
	region = text[start:end]
	assert "# >>> .gitleaksignore union" in region and "# <<< .gitleaksignore union" in region
	return region


def _fallthrough_region(text: str) -> str:
	# Starts at the guard-hook fallthrough so a hook resolved earlier is covered.
	start = text.index('_det_commit_label="${_det_commit_label:-Manifest union-merge}"\n')
	end = text.index("# The stderr file is no longer needed past this point", start)
	return text[start:end]


def _fingerprint_and_deferred_block(text: str) -> str:
	start = text.index("# Fingerprint-violation expansion of the resolver working set.")
	end = text.index('CONFLICT_RESOLVER_SEMBLE_QUERY_FILE="${CONFLICT_RESOLVER_SEMBLE_QUERY_FILE:-', start)
	return text[start:end]


def _env() -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}
	for name in ("CONFLICT_GITLEAKSIGNORE_UNION_ENABLED", "CONFLICT_MANIFEST_UNION_ENABLED", "TARGET_BRANCH", "HEAD_REF", "PR_NUMBER"):
		env.pop(name, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
	path = repo / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _make_conflict(
	repo: Path,
	*,
	ours: str,
	theirs: str,
	head: str = PR_BRANCH,
	base: str | None = BASE_TEXT,
	other_conflict: bool = False,
	manifest_conflict: bool = False,
) -> None:
	"""Leave ``repo`` on ``head`` mid-merge of ``main`` with ``.gitleaksignore`` conflicted."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	if base is not None:
		_write(repo, GL, base)
	_write(repo, "other.txt", "base\n")
	if manifest_conflict:
		_write(repo, MANIFEST, "a.py\nz.py\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	_git(repo, "checkout", "-q", "-b", head)
	_write(repo, GL, ours)
	if other_conflict:
		_write(repo, "other.txt", "ours\n")
	if manifest_conflict:
		_write(repo, MANIFEST, "a.py\nm.py\nz.py\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "ours")

	_git(repo, "checkout", "-q", "main")
	_write(repo, GL, theirs)
	if other_conflict:
		_write(repo, "other.txt", "theirs\n")
	if manifest_conflict:
		_write(repo, MANIFEST, "a.py\nn.py\nz.py\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "theirs")

	_git(repo, "checkout", "-q", head)
	merge = _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
	assert merge.returncode != 0, "fixture must produce a conflicted merge"
	assert _git(repo, "ls-files", "-u", "--", GL).stdout.strip(), ".gitleaksignore must be unmerged"


def _run(
	repo: Path,
	tmp: Path,
	*,
	head: str = PR_BRANCH,
	switch: str | None = None,
	fingerprints: dict | None = None,
	ghm_resolved: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
	runtime_dir = tmp / "runtime"
	runtime_dir.mkdir()
	github_env = tmp / "github.env"
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
PR_NUMBER=585
RESOLVER_ALLOWLIST_FILE={allowlist!s}
RESOLVER_FINGERPRINT_ONLY_PATHS_FILE={runtime_dir / "resolver_fingerprint_only_paths.txt"!s}
git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_ALLOWLIST_FILE}}"
_resolver_allowlist_count="$(wc -l < "${{RESOLVER_ALLOWLIST_FILE}}" | tr -d '[:space:]')"
_ff_defer_commit=false
_ghm_resolved={"true" if ghm_resolved else "false"}
_ghm_resolved_paths={".claude/hooks/session-start.sh" if ghm_resolved else '""'}
_ghm_defer_commit=false
GUARD_HOOK_LIVE_PATH=.claude/hooks/session-start.sh
_ai_resolved=false
_ai_defer_commit=false
{_union_region(text)}
{_fallthrough_region(text)}
CONFLICTED_FILES_RAW="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
IS_INTEGRATION_SYNC={"true" if integration else "false"}
INTEGRATION_FINGERPRINTS_FILE={fp_file!s}
SUPPORT_SCRIPTS_DIR={REPO_ROOT / "scripts"!s}
{_fingerprint_and_deferred_block(text)}
echo "GL_RESOLVED=${{_gl_resolved}}"
echo "REGION_FELL_THROUGH"
"""
	env = _env()
	if switch is not None:
		env["CONFLICT_GITLEAKSIGNORE_UNION_ENABLED"] = switch
	result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True)
	return result, runtime_dir


def _union_lines(out: str) -> list[str]:
	return [line for line in out.splitlines() if line.startswith("CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION ")]


def _no_stages_left(repo: Path) -> None:
	assert _git(repo, "ls-files", "-u", "--", GL).stdout == ""
	staged = _git(repo, "ls-files", "-s", "--", GL).stdout.split()
	assert staged[0] == "100644" and staged[2] == "0", staged


PR585_OURS = BASE_TEXT + f"# prose in docs/plan.md flagged by generic-api-key (issue #615)\n{FP_SHARED}\n"
PR585_THEIRS = BASE_TEXT + f"# false positive: the plan quotes an example key shape (issue #618)\n# added by PR #563\n{FP_SHARED}\n"


def test_pr585_shape_identical_fingerprint_different_comments_merges_and_commits_with_base_comments() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS)
		result, _runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert _union_lines(out) == ["CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION pr=585 outcome=merged reason=union stages=1 2 3 added=0"]
		assert ".gitleaksignore union: no other unmerged paths" in out
		assert "REGION_FELL_THROUGH" not in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
		assert len(_git(repo, "log", "-1", "--format=%P").stdout.split()) == 2, "must be a two-parent merge commit"
		assert _git(repo, "show", f"HEAD:{GL}").stdout == PR585_THEIRS, "the base side is kept byte for byte"
		assert (repo / GL).read_text(encoding="utf-8") == PR585_THEIRS
		assert "CONFLICT_RESOLVED=true" in (tmp / "github.env").read_text(encoding="utf-8")


def test_disjoint_additions_are_unioned() -> None:
	ours = BASE_TEXT + f"# README example token\n{FP_OURS}\n"
	theirs = BASE_TEXT + f"# script sample\n{FP_THEIRS}\n"
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=ours, theirs=theirs)
		result, _runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert _union_lines(out) == ["CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION pr=585 outcome=merged reason=union stages=1 2 3 added=1"]
		assert _git(repo, "show", f"HEAD:{GL}").stdout == theirs + f"# README example token\n{FP_OURS}\n"


def test_add_add_without_base_is_unioned_and_base_without_trailing_newline_gets_one() -> None:
	ours = f"{FP_OURS}\n"
	theirs = f"# main\n{FP_THEIRS}"
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=ours, theirs=theirs, base=None)
		result, _runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "outcome=merged reason=union stages=2 3 added=1" in out
		assert _git(repo, "show", f"HEAD:{GL}").stdout == f"# main\n{FP_THEIRS}\n{FP_OURS}\n"


@pytest.mark.parametrize("side", ["ours", "theirs"])
def test_deletion_on_either_side_skips(side: str) -> None:
	deleted = f"# gitleaks false positives\n{FP_OURS if side == 'ours' else FP_THEIRS}\n"
	other = BASE_TEXT + f"{FP_THEIRS if side == 'ours' else FP_OURS}\n"
	ours, theirs = (deleted, other) if side == "ours" else (other, deleted)
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=ours, theirs=theirs)
		result, _runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert _union_lines(out) == ["CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION pr=585 outcome=skip reason=deletion stages=1 2 3 added=0"]
		assert _git(repo, "ls-files", "-u", "--", GL).stdout.strip(), "the path stays unmerged (fail closed)"
		assert "REGION_FELL_THROUGH" in out


@pytest.mark.parametrize(
	"bad_line",
	[
		"not-a-fingerprint",
		FP_OURS + "\r",
		"::error::" + FP_OURS,
		"C" * 40 + ":README.md:generic-api-key:7",
		"  # indented comment",
	],
)
def test_malformed_added_line_skips(bad_line: str) -> None:
	ours = BASE_TEXT + f"{bad_line}\n"
	theirs = BASE_TEXT + f"{FP_THEIRS}\n"
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=ours, theirs=theirs)
		result, _runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert _union_lines(out) == ["CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION pr=585 outcome=skip reason=malformed stages=1 2 3 added=0"]
		assert _git(repo, "ls-files", "-u", "--", GL).stdout.strip()


def test_symlink_stage_skips() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_git(repo, "init", "-q", "-b", "main")
		_git(repo, "config", "user.name", "t")
		_git(repo, "config", "user.email", "t@t")
		_write(repo, GL, BASE_TEXT)
		_git(repo, "add", "-A")
		_git(repo, "commit", "-qm", "base")
		_git(repo, "checkout", "-q", "-b", PR_BRANCH)
		(repo / GL).unlink()
		os.symlink("/etc/passwd", repo / GL)
		_git(repo, "add", "-A")
		_git(repo, "commit", "-qm", "ours")
		_git(repo, "checkout", "-q", "main")
		_write(repo, GL, BASE_TEXT + f"{FP_THEIRS}\n")
		_git(repo, "commit", "-qam", "theirs")
		_git(repo, "checkout", "-q", PR_BRANCH)
		_git(repo, "merge", "--no-commit", "--no-ff", "main", check=False)
		assert _git(repo, "ls-files", "-u", "--", GL).stdout.strip()
		result, _runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		lines = _union_lines(out)
		assert len(lines) == 1 and "outcome=skip" in lines[0], out
		assert "reason=not_regular" in lines[0] or "reason=stage_shape" in lines[0], out
		assert _git(repo, "ls-files", "-u", "--", GL).stdout.strip()


@pytest.mark.parametrize("switch, reason, warns", [("false", "disabled", False), ("FALSE", "disabled", False), ("maybe", "switch_invalid", True)])
def test_kill_switch_false_and_unrecognised_value_skip(switch: str, reason: str, warns: bool) -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS)
		result, _runtime_dir = _run(repo, tmp, switch=switch)
		out = result.stdout + result.stderr
		assert _union_lines(out) == [f"CONFLICT_RESOLVER_GITLEAKSIGNORE_UNION pr=585 outcome=skip reason={reason} stages=1 2 3 added=0"]
		assert ("::warning::.gitleaksignore union: CONFLICT_GITLEAKSIGNORE_UNION_ENABLED has an unrecognised value" in out) is warns
		assert _git(repo, "ls-files", "-u", "--", GL).stdout.strip()


def test_switch_true_in_any_case_is_on() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS)
		result, _runtime_dir = _run(repo, tmp, switch="TRUE")
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "outcome=merged" in out


def test_mixed_conflict_stages_gitleaksignore_and_leaves_others_for_resolver() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, other_conflict=True)
		result, runtime_dir = _run(repo, tmp)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "outcome=merged reason=union" in out
		assert "REGION_FELL_THROUGH" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "ours", "no commit while other paths remain"
		_no_stages_left(repo)
		assert _git(repo, "ls-files", "-u", "--", "other.txt").stdout.strip()
		assert (runtime_dir / "resolver_unmerged_allowlist.txt").read_text(encoding="utf-8").split() == ["other.txt"]
		assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split() == ["other.txt"]


def test_integration_branch_defers_commit_until_fingerprint_check() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, head=PROJECT_BRANCH)
		result, _runtime_dir = _run(repo, tmp, head=PROJECT_BRANCH)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert ".gitleaksignore union: integration-sync branch; deferring the merge commit" in out
		assert out.index("deferring the merge commit") < out.index("integration fingerprints verified on the merged tree")
		assert ".gitleaksignore union: integration fingerprints verified on the merged tree" in out
		assert "REGION_FELL_THROUGH" not in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
		assert len(_git(repo, "log", "-1", "--format=%P").stdout.split()) == 2
		assert _git(repo, "show", f"HEAD:{GL}").stdout == PR585_THEIRS


def test_guard_hook_resolved_earlier_plus_gitleaksignore_on_integration_sync_waits_for_fingerprint_check() -> None:
	# A paired guard hook resolved while .gitleaksignore was still unmerged
	# leaves _ghm_defer_commit false; the guard-hook fallthrough must not
	# commit before the integration fingerprint check (review of #9659).
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, head=PROJECT_BRANCH)
		result, _runtime_dir = _run(repo, tmp, head=PROJECT_BRANCH, ghm_resolved=True)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "Guard-hook template merge: no other unmerged paths" not in out
		assert ".gitleaksignore union: deferring the merge commit until the integration fingerprint check" in out
		assert ".gitleaksignore union: integration fingerprints verified on the merged tree" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"


def test_guard_hook_resolved_earlier_plus_gitleaksignore_violation_hands_off_to_resolver() -> None:
	fingerprints = {"1500": {"issue": 1500, "pr": 1501, "must_contain": [{"file": "other.txt", "regex": "^never$"}], "must_not_contain": []}}
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, head=PROJECT_BRANCH)
		result, runtime_dir = _run(repo, tmp, head=PROJECT_BRANCH, fingerprints=fingerprints, ghm_resolved=True)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "Guard-hook template merge: no other unmerged paths" not in out
		assert ".gitleaksignore union: the merged tree fails merged sub-issue fingerprints; handing the violating path(s) to the resolver" in out
		assert "REGION_FELL_THROUGH" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "ours"
		assert "other.txt" in (runtime_dir / "resolver_unmerged_allowlist.txt").read_text(encoding="utf-8").split()


def test_integration_fingerprint_flag_on_gitleaksignore_is_exempt_after_union() -> None:
	# The PR-side comment text is dropped by the base-wins union, so a
	# must_contain fingerprint on it fails; the union's own check stands in.
	fingerprints = {"615": {"issue": 615, "pr": 621, "must_contain": [{"file": GL, "regex": "issue #615"}], "must_not_contain": []}}
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, head=PROJECT_BRANCH)
		result, runtime_dir = _run(repo, tmp, head=PROJECT_BRANCH, fingerprints=fingerprints)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "::warning::.gitleaksignore union: waived a merged sub-issue fingerprint violation on .gitleaksignore" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
		assert not (runtime_dir / "resolver_fingerprint_only_paths.txt").exists()


def test_integration_violation_on_other_file_hands_off_to_resolver_without_committing() -> None:
	# Like the .ai take-base: the violating path goes to the resolver (which
	# records retry state) instead of failing the prepare step; .gitleaksignore
	# stays staged and out of the resolver set.
	fingerprints = {"1500": {"issue": 1500, "pr": 1501, "must_contain": [{"file": "other.txt", "regex": "^never$"}], "must_not_contain": []}}
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, head=PROJECT_BRANCH)
		result, runtime_dir = _run(repo, tmp, head=PROJECT_BRANCH, fingerprints=fingerprints)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "unhandled reason=integration_sync detail=fingerprint_violations" not in out
		assert ".gitleaksignore union: the merged tree fails merged sub-issue fingerprints; handing the violating path(s) to the resolver" in out
		assert "REGION_FELL_THROUGH" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "ours"
		_no_stages_left(repo)
		allowlist = (runtime_dir / "resolver_unmerged_allowlist.txt").read_text(encoding="utf-8").split()
		assert "other.txt" in allowlist and GL not in allowlist


def test_manifest_and_gitleaksignore_on_integration_sync_resolve_together() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_make_conflict(repo, ours=PR585_OURS, theirs=PR585_THEIRS, head=PROJECT_BRANCH, manifest_conflict=True)
		assert _git(repo, "ls-files", "-u", "--", MANIFEST).stdout.strip()
		result, _runtime_dir = _run(repo, tmp, head=PROJECT_BRANCH)
		out = result.stdout + result.stderr
		assert result.returncode == 0, out
		assert "outcome=merged reason=union" in out
		assert ".gitleaksignore union: 1 unmerged path(s) remain." in out
		assert "integration fingerprints verified on the merged tree" in out
		assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
		assert _git(repo, "show", f"HEAD:{GL}").stdout == PR585_THEIRS
		assert _git(repo, "show", f"HEAD:{MANIFEST}").stdout == "a.py\nm.py\nn.py\nz.py\n"


def test_union_block_sits_before_the_manifest_block_and_after_the_commit_helper() -> None:
	text = _prepare_text()
	helper = text.index("_conflict_prepare_commit_deterministic_merge()\n{")
	start = text.index("# >>> .gitleaksignore union")
	end = text.index("# <<< .gitleaksignore union")
	assert helper < start < end < text.index("_mu_resolved=false\n")
	assert end < text.index("# >>> host-only take-base (issue #6748)")
	block = text[start:end]
	assert 'CONFLICT_GITLEAKSIGNORE_UNION_ENABLED-' in block
	# Index-only reads: blobs come from `git show :<stage>:`, never the worktree.
	assert 'git show ":2:${_gl_path}"' in block and 'git show ":3:${_gl_path}"' in block
	assert "cat .gitleaksignore" not in block and '< "${_gl_path}"' not in block
