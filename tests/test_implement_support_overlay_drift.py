#!/usr/bin/env python3
"""Workflow-support overlay drift must never be committed by the implement phase.

On the workflow-source repository the "Stage workflow support files" step of
`.github/workflows/implement.yml` installs the SCRIPT_REF copies of the runtime
helper scripts over the checked-out tree. Those helpers are tracked there, so
when the implementation branch is cut from an integration branch that has
diverged from main in any of them (orchestrator/project-* branches) the overlay
leaves them modified vs HEAD before Codex runs. The commit step's `git add -u`
then staged that drift as if the model had authored it.

Observed on PR #4079 (issue #4075, base `orchestrator/project-3965`): the
commit reverted eight helper scripts to their `main` content, dropping the
`model_provider_broker_*` functions from `scripts/codex_helpers.sh`, and every
AI review run on the PR died with `model_provider_broker_start: command not
found` (runs 34658324579, 34663516507, 34666476138).

These tests execute the real `scripts/implement_commit_changes.sh` against a
throwaway git repo and pin the workflow wiring around it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
IMPLEMENT_COMMIT_SCRIPT = REPO_ROOT / "scripts" / "implement_commit_changes.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SELF_REPO = "shubhodeep1/coding-workflows"
CONSUMER_REPO = "owner/repo"
OVERLAY_PATH = "scripts/codex_helpers.sh"
BASE_CONTENT = "#!/usr/bin/env bash\nmodel_provider_broker_start() {\n\t:\n}\n"
OVERLAY_CONTENT = "#!/usr/bin/env bash\n# main copy: no broker functions\n"


def _isolated_env(extra: dict[str, str], *, cwd: Path) -> dict[str, str]:
	env = os.environ.copy()
	for key in ("BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
		env.pop(key, None)
	env.update(extra)
	env["PWD"] = str(cwd)
	env.pop("OLDPWD", None)
	return env


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(
		["git", *args],
		cwd=str(cwd),
		env=_isolated_env({}, cwd=cwd),
		check=True,
		capture_output=True,
		text=True,
	).stdout


def _make_repo(tmp: Path) -> Path:
	repo = tmp / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "tests")
	_git(repo, "config", "user.email", "tests@example.com")
	(repo / "README.md").write_text("base readme\n", encoding="utf-8")
	(repo / "docs").mkdir()
	(repo / "docs" / "notes.md").write_text("base notes\n", encoding="utf-8")
	(repo / "scripts").mkdir()
	(repo / OVERLAY_PATH).write_text(BASE_CONTENT, encoding="utf-8")
	(repo / OVERLAY_PATH).chmod(0o755)
	shutil.copy2(IMPLEMENT_COMMIT_SCRIPT, repo / "scripts" / "implement_commit_changes.sh")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base branch state")
	return repo


def _apply_overlay_and_snapshot(repo: Path, runtime_dir: Path, *, path: str = OVERLAY_PATH, content: str = OVERLAY_CONTENT) -> Path:
	"""Mimic the support-file overlay plus the pre-Codex dirty-tracked snapshot."""
	target = repo / path
	target.write_text(content, encoding="utf-8")
	snapshot = runtime_dir / "pre_codex_dirty_tracked.tsv"
	lines = []
	for dirty in _git(repo, "diff", "--name-only", "HEAD").splitlines():
		if not dirty:
			continue
		blob = _git(repo, "hash-object", "--", dirty).strip()
		lines.append(f"{blob}\t{dirty}")
	snapshot.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
	return snapshot


def _run_commit_script(repo: Path, runtime_dir: Path, *, repository: str, snapshot: Path | None) -> tuple[subprocess.CompletedProcess[str], str]:
	github_output = runtime_dir / "github_output.txt"
	github_output.write_text("", encoding="utf-8")
	extra = {
		"GITHUB_OUTPUT": str(github_output),
		"GITHUB_REPOSITORY": repository,
		"ISSUE_NUMBER": "4075",
		"RUNTIME_DIR": str(runtime_dir),
		"SERENA_PROJECT_BOOTSTRAP_HASH": "",
		"SERENA_PROJECT_PREEXISTED": "false",
		"TMPDIR": str(runtime_dir),
		"ENFORCE_FILES_TOUCHED": "false",
	}
	if snapshot is not None:
		extra["PRE_CODEX_DIRTY_TRACKED_FILE"] = str(snapshot)
	proc = subprocess.run(
		["bash", "scripts/implement_commit_changes.sh"],
		cwd=str(repo),
		env=_isolated_env(extra, cwd=repo),
		text=True,
		capture_output=True,
		timeout=120,
	)
	return proc, github_output.read_text(encoding="utf-8")


def _committed_paths(repo: Path) -> list[str]:
	return [line for line in _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() if line]


def test_overlay_drift_untouched_by_codex_is_not_committed() -> None:
	with tempfile.TemporaryDirectory(prefix="overlay-drift-") as raw:
		tmp = Path(raw)
		repo = _make_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		snapshot = _apply_overlay_and_snapshot(repo, runtime_dir)
		# Codex edits an unrelated file only.
		(repo / "README.md").write_text("codex readme\n", encoding="utf-8")

		proc, output = _run_commit_script(repo, runtime_dir, repository=SELF_REPO, snapshot=snapshot)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert "did_commit=true" in output
		assert "Workflow-support overlay drift excluded from staging" in proc.stdout
		assert f"  - {OVERLAY_PATH}" in proc.stdout

		committed = _committed_paths(repo)
		assert committed == ["README.md"], f"committed paths: {committed}\nstdout:\n{proc.stdout}"
		assert _git(repo, "show", f"HEAD:{OVERLAY_PATH}") == BASE_CONTENT, "base-branch helper content must survive the commit"
		# The overlay copy stays in the worktree for the later workflow steps.
		assert (repo / OVERLAY_PATH).read_text(encoding="utf-8") == OVERLAY_CONTENT
		assert (repo / OVERLAY_PATH).stat().st_mode & 0o111, "overlay copy must remain executable"

		excluded_file = runtime_dir / "support_overlay_excluded.tsv"
		rows = [line.split("\t") for line in excluded_file.read_text(encoding="utf-8").splitlines() if line]
		assert rows == [[rows[0][0], "755", OVERLAY_PATH]], f"unexpected excluded manifest: {rows}"
		# The stored blob re-materialises the overlay copy byte-for-byte.
		assert _git(repo, "cat-file", "blob", rows[0][0]) == OVERLAY_CONTENT


def test_overlay_path_edited_by_codex_is_committed_with_warning() -> None:
	with tempfile.TemporaryDirectory(prefix="overlay-drift-") as raw:
		tmp = Path(raw)
		repo = _make_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		snapshot = _apply_overlay_and_snapshot(repo, runtime_dir)
		edited = OVERLAY_CONTENT + "codex_added_helper() {\n\t:\n}\n"
		(repo / OVERLAY_PATH).write_text(edited, encoding="utf-8")

		proc, output = _run_commit_script(repo, runtime_dir, repository=SELF_REPO, snapshot=snapshot)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert "did_commit=true" in output
		assert _committed_paths(repo) == [OVERLAY_PATH]
		assert _git(repo, "show", f"HEAD:{OVERLAY_PATH}") == edited
		assert f"::warning::Codex edited {OVERLAY_PATH} on top of the workflow-support overlay copy" in proc.stdout
		assert (runtime_dir / "support_overlay_excluded.tsv").read_text(encoding="utf-8") == ""


def test_overlay_only_drift_is_a_clean_noop() -> None:
	with tempfile.TemporaryDirectory(prefix="overlay-drift-") as raw:
		tmp = Path(raw)
		repo = _make_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		snapshot = _apply_overlay_and_snapshot(repo, runtime_dir)
		head_before = _git(repo, "rev-parse", "HEAD")

		proc, output = _run_commit_script(repo, runtime_dir, repository=SELF_REPO, snapshot=snapshot)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert "did_commit=false" in output
		assert _git(repo, "rev-parse", "HEAD") == head_before, "overlay drift alone must not produce a commit"
		# The overlay path is expected worktree state, not a stripped Codex edit.
		assert "remaining_changes<<" not in output, f"overlay drift leaked into remaining_changes:\n{output}"
		assert "Files present in worktree but excluded from staging" not in proc.stdout


def test_snapshot_is_ignored_on_consumer_repos() -> None:
	with tempfile.TemporaryDirectory(prefix="overlay-drift-") as raw:
		tmp = Path(raw)
		repo = _make_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		# A consumer-owned tracked file dirty before Codex ran and untouched by it.
		snapshot = _apply_overlay_and_snapshot(repo, runtime_dir, path="docs/notes.md", content="overlay notes\n")
		(repo / "README.md").write_text("codex readme\n", encoding="utf-8")

		proc, output = _run_commit_script(repo, runtime_dir, repository=CONSUMER_REPO, snapshot=snapshot)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert "did_commit=true" in output
		committed = _committed_paths(repo)
		assert committed == ["README.md", "docs/notes.md"], f"consumer staging changed: {committed}"
		assert "Workflow-support overlay drift excluded from staging" not in proc.stdout


def test_missing_snapshot_keeps_legacy_self_repo_staging() -> None:
	with tempfile.TemporaryDirectory(prefix="overlay-drift-") as raw:
		tmp = Path(raw)
		repo = _make_repo(tmp)
		runtime_dir = tmp / "runtime"
		runtime_dir.mkdir()
		(repo / OVERLAY_PATH).write_text(OVERLAY_CONTENT, encoding="utf-8")

		proc, output = _run_commit_script(repo, runtime_dir, repository=SELF_REPO, snapshot=None)
		assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
		assert "did_commit=true" in output
		assert _committed_paths(repo) == [OVERLAY_PATH]


def _step_block(step_name: str) -> str:
	lines = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8").splitlines()
	needle = f"- name: {step_name}"
	for idx, line in enumerate(lines):
		if line.strip() != needle:
			continue
		step_indent = len(line) - len(line.lstrip(" "))
		end = len(lines)
		for j in range(idx + 1, len(lines)):
			candidate = lines[j]
			if candidate.strip().startswith("- name:"):
				indent = len(candidate) - len(candidate.lstrip(" "))
				if indent == step_indent:
					end = j
					break
		return "\n".join(lines[idx:end])
	raise AssertionError(f"Step not found in workflow: {step_name}")


def test_workflow_wiring_contract() -> None:
	codex_block = _step_block("Run Codex implementation")
	assert 'PRE_CODEX_DIRTY_TRACKED_FILE="${RUNTIME_DIR}/pre_codex_dirty_tracked.tsv"' in codex_block
	assert 'echo "PRE_CODEX_DIRTY_TRACKED_FILE=${PRE_CODEX_DIRTY_TRACKED_FILE}" >> "$GITHUB_ENV"' in codex_block
	assert "done < <(git diff --name-only -z HEAD 2>/dev/null || true)" in codex_block
	# The snapshot must be taken before the first Codex attempt runs.
	assert codex_block.find("pre_codex_dirty_tracked.tsv") < codex_block.find("Codex implement attempt")

	preflight_block = _step_block("Preflight destructive-commit guard")
	assert 'if [ "${is_self_repo}" = "true" ] && [ -s "${PRE_CODEX_DIRTY_TRACKED_FILE:-}" ]; then' in preflight_block
	assert 'add_u_excludes+=(":(exclude,literal)${overlay_path}")' in preflight_block
	assert preflight_block.find("PRE_CODEX_DIRTY_TRACKED_FILE") < preflight_block.find('git add -u -- "${add_u_excludes[@]}"')

	commit_helper = IMPLEMENT_COMMIT_SCRIPT.read_text(encoding="utf-8")
	assert 'if [ "${is_self_repo}" = "true" ] && [ -s "${PRE_CODEX_DIRTY_TRACKED_FILE:-}" ]; then' in commit_helper
	assert 'add_u_excludes+=(":(exclude,literal)${overlay_path}")' in commit_helper
	assert 'SUPPORT_OVERLAY_EXCLUDED_FILE="${RUNTIME_DIR}/support_overlay_excluded.tsv"' in commit_helper
	assert 'git hash-object -w -- "${overlay_path}"' in commit_helper
	assert commit_helper.find("workflow-support overlay drift guard (source repo) >>>") < commit_helper.find('git add -u -- "${add_u_excludes[@]}"')

	push_block = _step_block("Push branch")
	assert 'SUPPORT_OVERLAY_EXCLUDED_FILE="${RUNTIME_DIR:+${RUNTIME_DIR}/support_overlay_excluded.tsv}"' in push_block
	assert "restore_support_overlay_copies() {" in push_block
	assert 'git restore --source=HEAD --staged --worktree -- "${overlay_path}"' in push_block
	assert "trap restore_support_overlay_copies EXIT" in push_block
	# Every EXIT trap in the push step must chain the overlay restore.
	for trap_line in [line for line in push_block.splitlines() if line.strip().startswith("trap ")]:
		assert "restore_support_overlay_copies" in trap_line, f"push-step trap drops the overlay restore: {trap_line.strip()}"
	assert push_block.find("restore_support_overlay_copies() {") < push_block.find('git rebase "origin/${TARGET_BRANCH}"')

	ci_text = CI_WORKFLOW.read_text(encoding="utf-8")
	assert "python3 tests/test_implement_support_overlay_drift.py" in ci_text


def main() -> int:
	tests = [(name, func) for name, func in sorted(globals().items()) if name.startswith("test_") and callable(func)]
	passed = 0
	failed = 0
	for name, func in tests:
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:  # noqa: BLE001 - report every failure
			print(f"  FAIL  {name}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
