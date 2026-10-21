"""Folded changelog / agents fragments in a modify/delete conflict (#7130).

PR #6844 rewrote `changelog.d/6763-wrapped-git-write-guard-operator-step.md`
after the v1.31.0 release fold had deleted it on main. Merging main gave a
modify/delete conflict (index stages 1+2). The model resolver only touched
`agents.md`, `check_resolver_diff.sh` reported OK, and the commit was refused
because the fragment stayed unmerged, on every autofix run.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
CHECK_DIFF = REPO_ROOT / "scripts" / "check_resolver_diff.sh"
FRAGMENT = "changelog.d/6763-wrapped-git-write-guard-operator-step.md"
PR_TEXT = "<!-- changelog: removed -->\n- **Operator step withdrawn.**\n"


def _env() -> dict[str, str]:
	return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _conflict(repo: Path, *, fragment: str = FRAGMENT, reverse: bool = False, other_conflict: bool = False) -> None:
	"""HEAD (`feat`, the PR) edits the fragment; `main` folds (deletes) it."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	(repo / fragment).parent.mkdir(parents=True, exist_ok=True)
	(repo / fragment).write_text("<!-- changelog: security -->\n- **Old entry.**\n", encoding="utf-8")
	(repo / "agents.md").write_text("# Agents\n\nbase\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	def edit_side() -> None:
		(repo / fragment).write_text(PR_TEXT, encoding="utf-8")
		if other_conflict:
			(repo / "agents.md").write_text("# Agents\n\npr side\n", encoding="utf-8")
		_git(repo, "commit", "-qam", "edit fragment")

	def fold_side() -> None:
		_git(repo, "rm", "-q", "--", fragment)
		(repo / "CHANGELOG.md").write_text("## v1.31.0\n- **Old entry.**\n", encoding="utf-8")
		if other_conflict:
			(repo / "agents.md").write_text("# Agents\n\nmain side\n", encoding="utf-8")
		_git(repo, "add", "-A")
		_git(repo, "commit", "-qm", "chore: assemble changelog fragments")

	_git(repo, "checkout", "-q", "-b", "feat")
	if reverse:
		fold_side()
		_git(repo, "checkout", "-q", "main")
		edit_side()
	else:
		edit_side()
		_git(repo, "checkout", "-q", "main")
		fold_side()
	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0


def _block() -> str:
	text = PREPARE.read_text(encoding="utf-8")
	start = text.index("# Deterministic resolution for folded changelog / agents fragments.")
	end = text.index("# Deterministic resolution for the generated workspace manifest.", start)
	return text[start:end]


def _run_block(
	repo: Path,
	tmp: Path,
	*,
	head_ref: str = "feat",
	enabled: str | None = None,
	forward_merge_enabled: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
	runtime = tmp / "runtime"
	runtime.mkdir()
	stash = tmp / "stash"
	stash.mkdir()
	github_env = tmp / "github.env"
	allowlist = runtime / "allowlist.txt"
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime!s}
GITHUB_ENV={github_env!s}
RESOLVE_STASH={stash!s}
_merge_stderr_file="$(mktemp)"
IS_WORKFLOW_SOURCE_REPO=true
HEAD_REF={head_ref}
RESOLVER_ALLOWLIST_FILE={allowlist!s}
git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_ALLOWLIST_FILE}}"
_resolver_allowlist_count="$(wc -l < "${{RESOLVER_ALLOWLIST_FILE}}" | tr -d '[:space:]')"
{_block()}
echo "AFTER_BLOCK count=${{_resolver_allowlist_count}} defer=${{_ff_defer_commit}}"
"""
	env = _env()
	env.pop("TARGET_BRANCH", None)
	env.pop("CONFLICT_FOLDED_FRAGMENT_KEEP_ENABLED", None)
	env.pop("CONFLICT_FORWARD_MERGE_FRAGMENT_KEEP_ENABLED", None)
	if enabled is not None:
		env["CONFLICT_FOLDED_FRAGMENT_KEEP_ENABLED"] = enabled
	if forward_merge_enabled is not None:
		env["CONFLICT_FORWARD_MERGE_FRAGMENT_KEEP_ENABLED"] = forward_merge_enabled
	return subprocess.run(["bash", "-c", script], cwd=repo, env=env, text=True, capture_output=True), github_env, allowlist


def _setup(tmp_name: str, **kwargs) -> tuple[Path, Path]:
	tmp = Path(tmp_name)
	repo = tmp / "repo"
	repo.mkdir()
	_conflict(repo, **kwargs)
	return tmp, repo


def test_fragment_only_conflict_keeps_the_prs_copy_and_commits() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		result, github_env, _ = _run_block(repo, tmp)
		assert result.returncode == 0, result.stdout + result.stderr
		assert f"Folded fragment keep: kept the PR's {FRAGMENT}" in result.stdout
		assert "AFTER_BLOCK" not in result.stdout, "the block must commit and exit"
		assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
		parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
		assert len(parents) == 3
		assert _git(repo, "show", f"HEAD:{FRAGMENT}").stdout == PR_TEXT
		assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout == ""


def test_fragment_with_other_conflicts_is_staged_and_the_rest_goes_to_the_resolver() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name, other_conflict=True)
		head = _git(repo, "rev-parse", "HEAD").stdout
		result, github_env, allowlist = _run_block(repo, tmp)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "AFTER_BLOCK count=1 defer=false" in result.stdout
		assert allowlist.read_text(encoding="utf-8") == "agents.md\n"
		assert not github_env.exists()
		assert _git(repo, "rev-parse", "HEAD").stdout == head
		assert _git(repo, "ls-files", "-s", "--", FRAGMENT).stdout.split()[2] == "0"


def test_other_shapes_and_the_kill_switch_leave_the_fragment_alone() -> None:
	cases = (
		({"reverse": True}, None),  # the PR deleted it, main changed it: stages 1 3
		({"fragment": "changelog.d/nested/6763.md"}, None),  # not a top-level fragment
		({"fragment": "agents.d/README.md"}, None),  # never folded by the assemblers
		({}, "false"),  # kill switch
	)
	for kwargs, enabled in cases:
		with tempfile.TemporaryDirectory() as tmp_name:
			tmp, repo = _setup(tmp_name, **kwargs)
			result, github_env, allowlist = _run_block(repo, tmp, enabled=enabled)
			assert result.returncode == 0, result.stdout + result.stderr
			assert "AFTER_BLOCK count=1 defer=false" in result.stdout, (kwargs, enabled, result.stdout)
			assert "Folded fragment keep: kept" not in result.stdout
			assert not github_env.exists()


def test_integration_sync_branch_defers_the_commit_to_the_fingerprint_check() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		head = _git(repo, "rev-parse", "HEAD").stdout
		result, github_env, _ = _run_block(repo, tmp, head_ref="orchestrator/project-42")
		assert result.returncode == 0, result.stdout + result.stderr
		assert "AFTER_BLOCK count=0 defer=true" in result.stdout
		assert not github_env.exists()
		assert _git(repo, "rev-parse", "HEAD").stdout == head


def test_deferred_fragment_commit_is_honoured_by_the_later_gates() -> None:
	text = PREPARE.read_text(encoding="utf-8")
	assert text.index("# Deterministic resolution for folded changelog / agents fragments.") < text.index(
		"# Deterministic resolution for the generated workspace manifest."
	)
	assert '[ "${_ff_defer_commit}" != "true" ]; then' in text
	assert 'if [ "${_mu_defer_commit}" = "true" ] || [ "${_ff_defer_commit}" = "true" ]; then' in text


def test_check_resolver_diff_names_an_untouched_unmerged_path() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name, other_conflict=True)
		conflicted = tmp / "conflicted.txt"
		touched = tmp / "touched.txt"
		conflicted.write_text(f"agents.md\n{FRAGMENT}\n", encoding="utf-8")
		touched.write_text("agents.md\n", encoding="utf-8")
		(repo / "agents.md").write_text("# Agents\n\nresolved\n", encoding="utf-8")
		args = ["bash", str(CHECK_DIFF), "--conflicted-set", str(conflicted), "--touched-set", str(touched), "--repo-root", str(repo)]
		result = subprocess.run(args, cwd=repo, env=_env(), text=True, capture_output=True)
		assert result.returncode == 1, result.stdout + result.stderr
		assert f"  - {FRAGMENT} (index stages: 1 2)" in result.stderr
		assert "check_resolver_diff: OK" not in result.stdout
		touched.write_text(f"agents.md\n{FRAGMENT}\n", encoding="utf-8")
		result = subprocess.run(args, cwd=repo, env=_env(), text=True, capture_output=True)
		assert result.returncode == 0, result.stdout + result.stderr
		assert "check_resolver_diff: OK" in result.stdout


FORWARD_MERGE_HEAD = "auto/forward-merge-stable-123-1"


def test_forward_merge_head_keeps_the_base_copy_on_stages_1_3() -> None:
	"""#7796: the forward-merge head carries the fold; main changed the fragment."""
	for flag in (None, "", "true", "TRUE"):
		with tempfile.TemporaryDirectory() as tmp_name:
			tmp, repo = _setup(tmp_name, reverse=True)
			result, github_env, _ = _run_block(repo, tmp, head_ref=FORWARD_MERGE_HEAD, forward_merge_enabled=flag)
			assert result.returncode == 0, result.stdout + result.stderr
			assert f"Folded fragment keep: kept the base branch's {FRAGMENT}" in result.stdout, (flag, result.stdout)
			assert "(index stages: 1 3)" in result.stdout
			assert "AFTER_BLOCK" not in result.stdout, "the block must commit and exit"
			assert "::warning::" not in result.stdout
			assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
			parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
			assert len(parents) == 3
			assert _git(repo, "show", f"HEAD:{FRAGMENT}").stdout == PR_TEXT
			assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout == ""


def test_ai_issue_head_leaves_stages_1_3_to_the_resolver() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name, reverse=True)
		result, github_env, _ = _run_block(repo, tmp, head_ref="ai/issue-7796")
		assert result.returncode == 0, result.stdout + result.stderr
		assert "AFTER_BLOCK count=1 defer=false" in result.stdout
		assert "Folded fragment keep: kept" not in result.stdout
		assert not github_env.exists()


def test_forward_merge_keep_switch_false_leaves_stages_1_3_to_the_resolver() -> None:
	for flag, warns in (("false", False), ("FALSE", False), ("bogus-value", True)):
		with tempfile.TemporaryDirectory() as tmp_name:
			tmp, repo = _setup(tmp_name, reverse=True)
			result, github_env, _ = _run_block(repo, tmp, head_ref=FORWARD_MERGE_HEAD, forward_merge_enabled=flag)
			assert result.returncode == 0, result.stdout + result.stderr
			assert "AFTER_BLOCK count=1 defer=false" in result.stdout, (flag, result.stdout)
			assert "Folded fragment keep: kept" not in result.stdout
			assert not github_env.exists()
			assert ("::warning::" in result.stdout) is warns, (flag, result.stdout)
			assert "bogus-value" not in result.stdout + result.stderr


def test_forward_merge_head_still_keeps_stages_1_2() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		result, github_env, _ = _run_block(repo, tmp, head_ref=FORWARD_MERGE_HEAD, forward_merge_enabled="false")
		assert result.returncode == 0, result.stdout + result.stderr
		assert f"Folded fragment keep: kept the PR's {FRAGMENT}" in result.stdout
		assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"


# PR #7794's exact shape (Refs #7802): two fragments, one in agents.d and one
# in changelog.d, deleted by the forward-merge head's release fold and changed
# on main, so one merge leaves both at index stages 1 3.
PR7794_AGENTS_FRAGMENT = "agents.d/7604-synced-security-finding-intake.md"
PR7794_CHANGELOG_FRAGMENT = "changelog.d/wfopt-0922-validate-dispatch-notice.md"
PR7794_FRAGMENTS = (PR7794_AGENTS_FRAGMENT, PR7794_CHANGELOG_FRAGMENT)
PR7794_BASE_TEXT = {
	PR7794_AGENTS_FRAGMENT: '<!-- agents: section="Workflow architecture" -->\nBase agents text.\n',
	PR7794_CHANGELOG_FRAGMENT: "<!-- changelog: fixed -->\n- **Base entry.**\n",
}
PR7794_MAIN_TEXT = {
	PR7794_AGENTS_FRAGMENT: '<!-- agents: section="Workflow architecture" -->\nMain edit of the agents text.\n',
	PR7794_CHANGELOG_FRAGMENT: "<!-- changelog: fixed -->\n- **Main edit.**\n",
}


def _conflict_two_fragments_forward_merge(repo: Path) -> None:
	"""HEAD (`feat`, the forward-merge head) folds both fragments; `main` edits them."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	for path in PR7794_FRAGMENTS:
		(repo / path).parent.mkdir(parents=True, exist_ok=True)
		(repo / path).write_text(PR7794_BASE_TEXT[path], encoding="utf-8")
	(repo / "agents.md").write_text("# Agents\n\nbase\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	_git(repo, "checkout", "-q", "-b", "feat")
	_git(repo, "rm", "-q", "--", *PR7794_FRAGMENTS)
	(repo / "CHANGELOG.md").write_text("## v1.32.0\n- **Base entry.**\n", encoding="utf-8")
	(repo / "agents.md").write_text("# Agents\n\nbase\n\nBase agents text.\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "chore: assemble changelog fragments")

	_git(repo, "checkout", "-q", "main")
	for path in PR7794_FRAGMENTS:
		(repo / path).write_text(PR7794_MAIN_TEXT[path], encoding="utf-8")
	_git(repo, "commit", "-qam", "edit fragments")

	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0
	for path in PR7794_FRAGMENTS:
		stages = sorted({line.split()[2] for line in _git(repo, "ls-files", "-u", "--", path).stdout.splitlines()})
		assert stages == ["1", "3"], (path, stages)
	assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split() == sorted(PR7794_FRAGMENTS)


def test_forward_merge_head_keeps_both_pr7794_fragments_on_stages_1_3() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		repo = tmp / "repo"
		repo.mkdir()
		_conflict_two_fragments_forward_merge(repo)
		result, github_env, _ = _run_block(repo, tmp, head_ref=FORWARD_MERGE_HEAD)
		assert result.returncode == 0, result.stdout + result.stderr
		for path in PR7794_FRAGMENTS:
			assert f"Folded fragment keep: kept the base branch's {path}" in result.stdout, result.stdout
		assert result.stdout.count("(index stages: 1 3)") == 2, result.stdout
		assert "AFTER_BLOCK" not in result.stdout, "the block must commit and exit"
		assert "::warning::" not in result.stdout
		assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout == ""
		parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
		assert len(parents) == 3
		for path in PR7794_FRAGMENTS:
			kept = _git(repo, "show", f"HEAD:{path}").stdout
			assert kept == PR7794_MAIN_TEXT[path]
			assert kept == _git(repo, "show", f"main:{path}").stdout
		assert github_env.read_text(encoding="utf-8") == "CONFLICT_RESOLVED=true\n"
