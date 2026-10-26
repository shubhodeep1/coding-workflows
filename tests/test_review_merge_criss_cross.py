"""Criss-cross merge history leaves a PR dirty on GitHub forever (PR #8170).

Two forward-merge PRs (#8169, #8170) each merged `stable` and `main` with
their own resolution commit. After #8169 merged, #8170's head and `main`
had two merge bases. git's merge folds both into a virtual base and merged
cleanly, so "Detect merge conflicts" reported no conflicts and pushed
nothing. GitHub merges from a single base, found conflicts and kept
reporting mergeable_state=dirty, so the review-blocked judge could never
merge the PR and the conflict sweep re-dispatched no-op runs.

These tests build that history in a scratch repository and run the real
detect step and conflict-prepare scripts against it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT = REPO_ROOT / "scripts" / "review_autofix_step_detect_merge_conflicts.sh"
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"


def _env() -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}
	for key in ("TARGET_BRANCH", "HEAD_REF", "MERGE_CRISS_CROSS_CLEAN", "MERGE_CONFLICT", "CONFLICT_RESOLVED"):
		env.pop(key, None)
	env.update(
		{
			"GIT_AUTHOR_NAME": "t",
			"GIT_AUTHOR_EMAIL": "t@example.com",
			"GIT_COMMITTER_NAME": "t",
			"GIT_COMMITTER_EMAIL": "t@example.com",
			"GIT_CONFIG_NOSYSTEM": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
		}
	)
	return env


def _git(repo: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=True, text=True, capture_output=True).stdout.strip()


def _write(repo: Path, name: str, text: str) -> None:
	(repo / name).write_text(text, encoding="utf-8")


def _build(tmp: Path, *, criss_cross: bool, single_base_conflict: bool = True, head_ref: str = "pr") -> Path:
	"""Return a checkout of `head_ref` whose `origin` holds `main` and `head_ref`.

	With criss_cross=True the history matches #8170: `stable` and `main`
	each change a different file, two branches merge them separately, `main`
	merges the first one and then moves on. With single_base_conflict=True,
	`main` edits file A again after that, so a merge from the `stable` base
	alone conflicts while git's virtual-base merge stays clean.
	"""
	origin = tmp / "origin.git"
	subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=_env(), check=True)
	work = tmp / "work"
	work.mkdir()
	_git(work, "init", "-q", "-b", "main")
	_write(work, "A", "a\n")
	_write(work, "B", "b\n")
	_git(work, "add", ".")
	_git(work, "commit", "-qm", "O")
	if criss_cross:
		_git(work, "checkout", "-qb", "stable")
		_write(work, "B", "s1\n")
		_git(work, "commit", "-qam", "S1")
		_git(work, "checkout", "-q", "main")
		_write(work, "A", "m1\n")
		_git(work, "commit", "-qam", "M1")
		_git(work, "checkout", "-qb", "sibling", "stable")
		_git(work, "merge", "-q", "--no-edit", "main")
		_git(work, "checkout", "-qb", head_ref, "stable")
		_git(work, "merge", "-q", "--no-edit", "main")
		_write(work, "C", "only on the PR\n")
		_git(work, "add", "C")
		_git(work, "commit", "-qm", "P2")
		_git(work, "checkout", "-q", "main")
		_git(work, "merge", "-q", "--no-ff", "--no-edit", "sibling")
		if single_base_conflict:
			_write(work, "A", "m2\n")
		else:
			_write(work, "D", "unrelated\n")
			_git(work, "add", "D")
		_git(work, "commit", "-qam", "M2")
	else:
		_git(work, "checkout", "-qb", head_ref)
		_write(work, "C", "only on the PR\n")
		_git(work, "add", "C")
		_git(work, "commit", "-qm", "P")
		_git(work, "checkout", "-q", "main")
		_write(work, "A", "m1\n")
		_git(work, "commit", "-qam", "M1")
	_git(work, "push", "-q", str(origin), "main", head_ref)
	checkout = tmp / "checkout"
	subprocess.run(["git", "clone", "-q", "-b", head_ref, str(origin), str(checkout)], env=_env(), check=True)
	return checkout


def _github_env(path: Path) -> dict[str, str]:
	values: dict[str, str] = {}
	for line in path.read_text(encoding="utf-8").splitlines():
		if "=" in line:
			key, value = line.split("=", 1)
			values[key] = value
	return values


def _run_detect(checkout: Path, tmp: Path, *, head_ref: str = "pr") -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
	github_env = tmp / "detect.env"
	github_env.write_text("", encoding="utf-8")
	env = _env()
	env.update(
		{
			"GITHUB_ENV": str(github_env),
			"BASE_BRANCH": "main",
			"TARGET_BRANCH": head_ref,
			"PR_NUMBER": "42",
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"GITHUB_WORKSPACE": str(checkout),
		}
	)
	proc = subprocess.run(["bash", "-c", f'source "{DETECT}"'], cwd=checkout, env=env, text=True, capture_output=True, timeout=120, check=False)
	return proc, _github_env(github_env)


def _run_prepare(checkout: Path, tmp: Path, *, head_ref: str = "pr") -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
	github_env = tmp / "prepare.env"
	github_env.write_text("", encoding="utf-8")
	runtime = tmp / "runtime"
	runtime.mkdir(exist_ok=True)
	env = _env()
	env.update(
		{
			"GITHUB_ENV": str(github_env),
			"BASE_BRANCH": "main",
			"TARGET_BRANCH": head_ref,
			"HEAD_REF": head_ref,
			"PR_NUMBER": "42",
			"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
			"SUPPORT_PROMPTS_DIR": str(REPO_ROOT / "prompts"),
			"IS_WORKFLOW_SOURCE_REPO": "true",
			"RUNTIME_DIR": str(runtime),
			"GITHUB_REPOSITORY": "o/r",
			"CONFLICT_RESOLVER_PROMPT_FILE": str(runtime / "prompt.txt"),
			"MERGE_CONFLICT": "true",
			"MERGE_CRISS_CROSS_CLEAN": "true",
			"CONFLICT_RESOLVED": "false",
		}
	)
	proc = subprocess.run(["bash", str(PREPARE)], cwd=checkout, env=env, text=True, capture_output=True, timeout=120, check=False)
	return proc, _github_env(github_env)


def test_criss_cross_clean_merge_with_single_base_conflict_is_routed_to_prepare() -> None:
	with tempfile.TemporaryDirectory(prefix="criss_cross_") as td:
		tmp = Path(td)
		checkout = _build(tmp, criss_cross=True)
		assert len(_git(checkout, "merge-base", "--all", "HEAD", "origin/main").split()) == 2
		head_before = _git(checkout, "rev-parse", "HEAD")
		proc, values = _run_detect(checkout, tmp)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "AUTOFIX_MERGE_CRISS_CROSS pr=42 outcome=route_clean_merge bases=2" in proc.stdout, proc.stdout
		assert values["MERGE_CONFLICT"] == "true"
		assert values["MERGE_CRISS_CROSS_CLEAN"] == "true"
		# The detect step only routes; it must leave the checkout untouched.
		assert _git(checkout, "rev-parse", "HEAD") == head_before
		assert not (checkout / ".git" / "MERGE_HEAD").exists()
		assert _git(checkout, "status", "--porcelain") == ""


def test_prepare_commits_the_clean_merge_so_the_base_becomes_an_ancestor() -> None:
	with tempfile.TemporaryDirectory(prefix="criss_cross_") as td:
		tmp = Path(td)
		checkout = _build(tmp, criss_cross=True)
		head_before = _git(checkout, "rev-parse", "HEAD")
		base = _git(checkout, "rev-parse", "origin/main")
		proc, values = _run_prepare(checkout, tmp)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "AUTOFIX_MERGE_CRISS_CROSS pr=42 outcome=commit_clean_merge phase=prepare" in proc.stdout, proc.stdout
		assert "Criss-cross clean merge: no other unmerged paths" in proc.stdout, proc.stdout
		assert values.get("CONFLICT_RESOLVED") == "true"
		parents = _git(checkout, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
		assert parents == [head_before, base]
		assert _git(checkout, "log", "-1", "--format=%s") == "[ai-merge-resolve] resolve merge conflicts"
		# git's virtual-base result: main's newest A, stable's B, the PR's C.
		assert (checkout / "A").read_text(encoding="utf-8") == "m2\n"
		assert (checkout / "B").read_text(encoding="utf-8") == "s1\n"
		assert (checkout / "C").read_text(encoding="utf-8") == "only on the PR\n"
		# With origin/main an ancestor of the head, a single-base merge is trivial.
		assert len(_git(checkout, "merge-base", "--all", "HEAD", "origin/main").split()) == 1
		subprocess.run(
			["git", "merge-tree", "--write-tree", f"--merge-base={base}", "HEAD", "origin/main"],
			cwd=checkout, env=_env(), check=True, capture_output=True,
		)


def test_prepare_without_the_criss_cross_flag_keeps_nothing_to_resolve() -> None:
	with tempfile.TemporaryDirectory(prefix="criss_cross_") as td:
		tmp = Path(td)
		checkout = _build(tmp, criss_cross=True)
		head_before = _git(checkout, "rev-parse", "HEAD")
		github_env = tmp / "prepare.env"
		runtime = tmp / "runtime"
		runtime.mkdir()
		github_env.write_text("", encoding="utf-8")
		env = _env()
		env.update(
			{
				"GITHUB_ENV": str(github_env),
				"BASE_BRANCH": "main",
				"TARGET_BRANCH": "pr",
				"SUPPORT_SCRIPTS_DIR": str(REPO_ROOT / "scripts"),
				"SUPPORT_PROMPTS_DIR": str(REPO_ROOT / "prompts"),
				"IS_WORKFLOW_SOURCE_REPO": "true",
				"RUNTIME_DIR": str(runtime),
				"GITHUB_REPOSITORY": "o/r",
				"CONFLICT_RESOLVER_PROMPT_FILE": str(runtime / "prompt.txt"),
			}
		)
		proc = subprocess.run(["bash", str(PREPARE)], cwd=checkout, env=env, text=True, capture_output=True, timeout=120, check=False)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "nothing to resolve" in proc.stdout
		assert _github_env(github_env).get("MERGE_CONFLICT") == "false"
		assert _git(checkout, "rev-parse", "HEAD") == head_before


def test_criss_cross_without_single_base_conflict_stays_clean() -> None:
	with tempfile.TemporaryDirectory(prefix="criss_cross_") as td:
		tmp = Path(td)
		checkout = _build(tmp, criss_cross=True, single_base_conflict=False)
		assert len(_git(checkout, "merge-base", "--all", "HEAD", "origin/main").split()) == 2
		proc, values = _run_detect(checkout, tmp)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "AUTOFIX_MERGE_CRISS_CROSS pr=42 outcome=no_single_base_conflict bases=2" in proc.stdout, proc.stdout
		assert "No merge conflicts detected" in proc.stdout
		assert values["MERGE_CONFLICT"] == "false"
		assert values["MERGE_CRISS_CROSS_CLEAN"] == "false"


def test_ordinary_clean_merge_is_unchanged() -> None:
	with tempfile.TemporaryDirectory(prefix="criss_cross_") as td:
		tmp = Path(td)
		checkout = _build(tmp, criss_cross=False)
		proc, values = _run_detect(checkout, tmp)
		assert proc.returncode == 0, proc.stdout + proc.stderr
		assert "AUTOFIX_MERGE_CRISS_CROSS" not in proc.stdout
		assert "No merge conflicts detected" in proc.stdout
		assert values["MERGE_CONFLICT"] == "false"
		assert values["MERGE_CRISS_CROSS_CLEAN"] == "false"
