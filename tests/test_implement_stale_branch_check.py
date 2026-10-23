#!/usr/bin/env python3
"""Tests for replacing a leftover ai/issue-<N> branch in implement.yml's push.

Background: shubhodeep1/tele-funtoken-msg-scoring#4752. Six phases of issue
#4726 reused ``ai/issue-4726``. Each implement run built its commit on the
fresh base branch, the push was rejected because the earlier phase's branch
was still on origin, and the "Push branch" step rebased the fresh commit onto
that branch. Git dropped the squash commit ("patch contents already
upstream"), so the new PR sat on already-merged history, re-showed the earlier
phase's diff and conflicted with the base.

``scripts/implement_stale_branch_check.sh`` tells the step whether any open PR
uses the branch; when none does, the step supersedes the branch with an
ours-strategy merge (this run's tree, the leftover tip as second parent) and a
plain fast-forward push instead of rebasing onto it. No force push is used,
because the consumer's "Copilot review" ruleset blocks non-fast-forward
updates on every branch. These tests cover the helper's decisions with a fake
``gh`` and run the live "Push branch" step against a real local remote that
rejects non-fast-forward pushes the same way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "implement_stale_branch_check.sh"
HEALTH_CHECK = REPO_ROOT / "scripts" / "git_ref_health_check.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
PUSH_STEP_NAME = "Push branch"
FAKE_REPO = "octo/widgets"
FAKE_TOKEN = "test-token"

FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_GH_LOG}"
printf '%s\\n' "${FAKE_GH_OUT:-0}"
exit "${FAKE_GH_RC:-0}"
"""


def _clean_env() -> dict[str, str]:
	return {
		env_name: env_value
		for env_name, env_value in os.environ.items()
		if not env_name.startswith("GIT_") and env_name not in {"BASH_ENV", "GH_TOKEN", "GITHUB_TOKEN"}
	}


def _install_fake_gh(tmp: Path) -> tuple[Path, Path]:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	fake = bin_dir / "gh"
	fake.write_text(FAKE_GH, encoding="utf-8")
	fake.chmod(0o755)
	return bin_dir, tmp / "gh.log"


def _staged_helper_dir(tmp: Path) -> Path:
	"""A run dir holding only the helper, as implement.yml stages it (no gh_helpers.sh: one raw call)."""
	run_dir = tmp / "staged_support_run" / "scripts"
	run_dir.mkdir(parents=True)
	shutil.copy2(HELPER, run_dir / HELPER.name)
	return run_dir


def _run_helper(tmp: Path, *args: str, out: str = "0", rc: int = 0) -> tuple[subprocess.CompletedProcess[str], list[str]]:
	bin_dir, gh_log = _install_fake_gh(tmp)
	run_dir = _staged_helper_dir(tmp)
	env = _clean_env()
	env.update({
		"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
		"FAKE_GH_LOG": str(gh_log),
		"FAKE_GH_OUT": out,
		"FAKE_GH_RC": str(rc),
	})
	result = subprocess.run(
		["bash", str(run_dir / HELPER.name), *args], env=env, text=True, capture_output=True,
	)
	calls = gh_log.read_text(encoding="utf-8").splitlines() if gh_log.exists() else []
	return result, calls


def test_no_open_pr_means_replace_with_one_call() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_helper(Path(tmp), FAKE_REPO, "ai/issue-4726", out="0")
	assert result.returncode == 0, result.stdout + result.stderr
	assert "IMPLEMENT_STALE_BRANCH branch=ai/issue-4726 outcome=replace reason=no_open_pr" in result.stdout
	assert calls == [f"api repos/{FAKE_REPO}/pulls?state=open&head=octo:ai/issue-4726&per_page=1 --jq if type == \"array\" then length else -1 end"]


def test_open_pr_keeps_rebase_fallback() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_helper(Path(tmp), FAKE_REPO, "ai/issue-7", out="1")
	assert result.returncode == 1
	assert "outcome=keep reason=open_pr count=1" in result.stdout
	assert len(calls) == 1


def test_api_failure_keeps_rebase_fallback() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, _calls = _run_helper(Path(tmp), FAKE_REPO, "ai/issue-7", out='{"message":"Bad credentials"}', rc=1)
	assert result.returncode == 1
	assert "outcome=keep reason=api_unavailable rc=1" in result.stdout


def test_malformed_count_keeps_rebase_fallback() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, _calls = _run_helper(Path(tmp), FAKE_REPO, "ai/issue-7", out="null")
	assert result.returncode == 1
	assert "outcome=keep reason=malformed_response" in result.stdout


def test_non_issue_branch_and_bad_repo_make_no_call() -> None:
	for args, reason in (
		((FAKE_REPO, "orchestrator/project-12"), "not_issue_branch"),
		((FAKE_REPO, "ai/issue-7-extra"), "not_issue_branch"),
		(("not a repo", "ai/issue-7"), "invalid_repo"),
	):
		with tempfile.TemporaryDirectory() as tmp:
			result, calls = _run_helper(Path(tmp), *args)
		assert result.returncode == 1, args
		assert f"outcome=keep reason={reason}" in result.stdout, args
		assert calls == [], args


def _push_step_script() -> str:
	lines = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8").splitlines()
	start = lines.index(f"      - name: {PUSH_STEP_NAME}")
	end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("      - name: "))
	block = lines[start:end]
	run_at = block.index("        run: |")
	return textwrap.dedent("\n".join(block[run_at + 1:])) + "\n"


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", *args], cwd=cwd, env=_clean_env(), check=True, text=True, capture_output=True,
	).stdout.strip()


def _commit(cwd: Path, text: str, message: str) -> str:
	(cwd / "app.txt").write_text(text, encoding="utf-8")
	_git(cwd, "add", "app.txt")
	_git(cwd, "commit", "-q", "-m", message)
	return _git(cwd, "rev-parse", "HEAD")


def _set_identity(cwd: Path) -> None:
	_git(cwd, "config", "user.name", "Test")
	_git(cwd, "config", "user.email", "test@example.com")
	_git(cwd, "config", "commit.gpgsign", "false")


def _leftover_branch_scenario(tmp: Path, depth: int = 0) -> dict[str, str | Path]:
	"""Origin holds an earlier phase's ai/issue-7 whose PR was squash-merged into main.

	The workspace is a fresh clone of main with this run's new commit on a
	local ai/issue-7, exactly as implement.yml's "Create implementation
	branch" step leaves it.
	"""
	remote = tmp / "remote.git"
	_git(tmp, "init", "-q", "--bare", "-b", "main", str(remote))
	# Mirror the consumer ruleset's non_fast_forward rule: no force pushes.
	_git(remote, "config", "receive.denyNonFastForwards", "true")
	seed = tmp / "seed"
	_git(tmp, "clone", "-q", str(remote), str(seed))
	_set_identity(seed)
	_commit(seed, "base\n", "base")
	_git(seed, "push", "-q", "origin", "HEAD:main")
	_git(seed, "checkout", "-q", "-b", "ai/issue-7")
	_commit(seed, "phase 1 draft\n", "phase 1 draft")
	old_tip = _commit(seed, "phase 1\n", "phase 1 fix")
	_git(seed, "push", "-q", "origin", "ai/issue-7")
	_git(seed, "checkout", "-q", "main")
	squash = _commit(seed, "phase 1\n", "phase 1 (#1) squash")
	_git(seed, "push", "-q", "origin", "main")

	workspace = tmp / "workspace"
	# implement.yml checks out with fetch-depth: 1; depth=1 reproduces that
	# grafted root, where the leftover branch shares no merge base with HEAD.
	if depth:
		_git(tmp, "clone", "-q", "--depth", str(depth), "--single-branch", "-b", "main", f"file://{remote}", str(workspace))
	else:
		_git(tmp, "clone", "-q", "--single-branch", "-b", "main", str(remote), str(workspace))
	_set_identity(workspace)
	_git(workspace, "config", f"url.{remote}.insteadOf", f"https://x-access-token:{FAKE_TOKEN}@github.com/{FAKE_REPO}")
	_git(workspace, "checkout", "-q", "-B", "ai/issue-7")
	fresh = _commit(workspace, "phase 1\nphase 2\n", "phase 2")
	support = workspace / ".codex-workflow-src" / "scripts"
	support.mkdir(parents=True)
	shutil.copy2(HEALTH_CHECK, support / HEALTH_CHECK.name)
	with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
		exclude.write(".codex-workflow-src/\n")
	return {"remote": remote, "workspace": workspace, "old_tip": old_tip, "squash": squash, "fresh": fresh}


def _run_push_step(tmp: Path, workspace: Path, *, open_prs: str, stage_helper: bool = True) -> subprocess.CompletedProcess[str]:
	bin_dir, gh_log = _install_fake_gh(tmp)
	run_dir = _staged_helper_dir(tmp) if stage_helper else tmp / "empty_run_dir"
	run_dir.mkdir(parents=True, exist_ok=True)
	env = _clean_env()
	env.update({
		"PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
		"FAKE_GH_LOG": str(gh_log),
		"FAKE_GH_OUT": open_prs,
		"GH_TOKEN": FAKE_TOKEN,
		"GITHUB_WORKSPACE": str(workspace),
		"GITHUB_REPOSITORY": FAKE_REPO,
		"TARGET_BRANCH": "ai/issue-7",
		"IMPLEMENT_STAGED_SUPPORT_RUN_DIR": str(run_dir),
		"ALLOW_FORCE_PUSH": "false",
	})
	return subprocess.run(
		["bash", "-c", _push_step_script()], cwd=workspace, env=env, text=True, capture_output=True,
	)


def _is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
	return subprocess.run(
		["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd, env=_clean_env(),
	).returncode == 0


def test_push_replaces_leftover_branch_when_no_open_pr() -> None:
	_assert_push_replaces_leftover_branch(depth=0)


def test_push_replaces_leftover_branch_in_shallow_workspace() -> None:
	_assert_push_replaces_leftover_branch(depth=1)


def _assert_push_replaces_leftover_branch(depth: int) -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		scenario = _leftover_branch_scenario(tmp, depth=depth)
		result = _run_push_step(tmp, scenario["workspace"], open_prs="0")
		output = result.stdout + result.stderr
		assert result.returncode == 0, output
		assert "outcome=replace reason=no_open_pr" in output
		assert "Attempting rebase onto origin" not in output
		remote = scenario["remote"]
		tip = _git(remote, "rev-parse", "refs/heads/ai/issue-7")
		parents = _git(remote, "rev-list", "--parents", "-n", "1", tip).split()[1:]
		assert parents == [scenario["fresh"], scenario["old_tip"]], output
		assert _git(remote, "rev-parse", f"{tip}^{{tree}}") == _git(remote, "rev-parse", f"{scenario['fresh']}^{{tree}}")
		assert _git(remote, "merge-base", "main", tip) == scenario["squash"]
		pr_diff = _git(remote, "diff", f"main...{tip}")
		added = [line for line in pr_diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
		assert added == ["+phase 2"], f"PR diff must show only this run's work: {added}"
		assert _git(remote, "merge-tree", "--write-tree", "main", tip), "merging the base must stay clean"


def test_push_falls_back_to_rebase_when_superseding_push_is_rejected() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		scenario = _leftover_branch_scenario(tmp)
		hook = scenario["remote"] / "hooks" / "pre-receive"
		hook.write_text(
			"#!/usr/bin/env bash\n"
			"while read -r _old new _ref; do\n"
			"  if [ \"$(git rev-list --parents -n 1 \"$new\" | wc -w)\" -gt 2 ]; then echo 'merge commits refused' >&2; exit 1; fi\n"
			"done\n",
			encoding="utf-8",
		)
		hook.chmod(0o755)
		result = _run_push_step(tmp, scenario["workspace"], open_prs="0")
		output = result.stdout + result.stderr
		assert result.returncode == 0, output
		assert "Superseding leftover ai/issue-7 failed" in output
		assert "Attempting rebase onto origin/ai/issue-7" in output
		tip = _git(scenario["remote"], "rev-parse", "refs/heads/ai/issue-7")
		assert len(_git(scenario["remote"], "rev-list", "--parents", "-n", "1", tip).split()) == 2
		assert _is_ancestor(scenario["remote"], scenario["old_tip"], tip)
		assert _git(scenario["workspace"], "status", "--porcelain") == ""


def test_push_keeps_rebase_when_an_open_pr_uses_the_branch() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		scenario = _leftover_branch_scenario(tmp)
		result = _run_push_step(tmp, scenario["workspace"], open_prs="1")
		output = result.stdout + result.stderr
		assert result.returncode == 0, output
		assert "outcome=keep reason=open_pr count=1" in output
		assert "Attempting rebase onto origin/ai/issue-7" in output
		tip = _git(scenario["remote"], "rev-parse", "refs/heads/ai/issue-7")
		assert _is_ancestor(scenario["remote"], scenario["old_tip"], tip), "legacy rebase path must be unchanged"


def test_push_keeps_rebase_when_helper_is_not_staged() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		scenario = _leftover_branch_scenario(tmp)
		result = _run_push_step(tmp, scenario["workspace"], open_prs="0", stage_helper=False)
		output = result.stdout + result.stderr
		assert result.returncode == 0, output
		assert "implement_stale_branch_check.sh is not staged" in output
		tip = _git(scenario["remote"], "rev-parse", "refs/heads/ai/issue-7")
		assert _is_ancestor(scenario["remote"], scenario["old_tip"], tip)


def test_helper_is_staged_with_the_other_support_scripts() -> None:
	text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	staging_line = next(line for line in text.splitlines() if line.strip().startswith("for f in gh_helpers.sh "))
	assert " implement_stale_branch_check.sh " in f"{staging_line} "
	assert os.access(HELPER, os.X_OK)


def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	failed = 0
	for func in test_funcs:
		try:
			func()
			print(f"  PASS  {func.__name__}")
		except Exception as exc:
			print(f"  FAIL  {func.__name__}: {exc}")
			failed += 1
	print(f"\n{len(test_funcs) - failed} passed, {failed} failed, {len(test_funcs)} total")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
