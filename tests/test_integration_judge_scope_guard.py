"""The integration judge may publish only verified merge resolutions."""

import os
import subprocess
from pathlib import Path

import pytest


POLL_SCRIPT = (Path(__file__).resolve().parent.parent / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")


def _extract_bash_function(signature: str) -> str:
	start = POLL_SCRIPT.index(signature)
	return POLL_SCRIPT[start:POLL_SCRIPT.index("\n}\n", start) + 3]


FUNCTIONS = "\n".join(_extract_bash_function(name + "() {") for name in (
	"_integration_judge_capture_baseline", "_integration_judge_verify_scope", "_integration_judge_commit_and_push",
))


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def judge_repo(tmp_path: Path, request: pytest.FixtureRequest):
	root = tmp_path / "source"
	root.mkdir()
	remote = tmp_path / "remote.git"
	git(tmp_path, "init", "--bare", str(remote))
	git(root, "init", "-b", "main")
	git(root, "config", "user.name", "test")
	git(root, "config", "user.email", "test@example.invalid")
	if request.param == "newline":
		conflict_path = "lines\nbreak.txt"
	else:
		conflict_path = ".github/workflows/ci.yml" if request.param else "conflict.txt"
	conflicted = root / conflict_path
	conflicted.parent.mkdir(parents=True, exist_ok=True)
	conflicted.write_text("first\nbase\nlast\n")
	(root / "untouched.txt").write_text("untouched\n")
	git(root, "add", ".")
	git(root, "commit", "-m", "base")
	git(root, "remote", "add", "origin", str(remote))
	git(root, "push", "origin", "main")
	git(root, "checkout", "-b", "integration")
	conflicted.write_text("first\n\nours\nlast\n" if request.param == "shared" else "first\nours\nlast\n")
	git(root, "commit", "-am", "ours")
	git(root, "push", "origin", "integration")
	before = git(remote, "rev-parse", "refs/heads/integration").stdout.strip()
	git(root, "checkout", "main")
	conflicted.write_text("first\n\ntheirs\nlast\n" if request.param == "shared" else "first\ntheirs\nlast\n")
	git(root, "commit", "-am", "theirs")
	git(root, "push", "origin", "main")
	wt = tmp_path / "judge"
	git(root, "worktree", "add", "--detach", str(wt), "integration")
	merge = git(wt, "merge", "--no-ff", "--no-commit", "main", check=False)
	assert merge.returncode != 0 and git(wt, "ls-files", "-u").stdout
	baseline = tmp_path / "baseline"
	baseline.mkdir()
	state = tmp_path / "state.json"
	state.write_text("{}")
	env = os.environ.copy()
	for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
		env.pop(key, None)
	env.update({"PYTHONDONTWRITEBYTECODE": "1", "STATE_FILE": str(state), "GH_TOKEN": "test",
	            "GITHUB_REPOSITORY": "local/judge", "ORCH_FINGERPRINT_VERIFIER": "",
	            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{remote.as_uri()}.insteadOf",
	            "GIT_CONFIG_VALUE_0": "https://github.com/local/judge"})
	def run(command: str) -> subprocess.CompletedProcess[str]:
		return subprocess.run(["bash", "-c", FUNCTIONS + "\n" + command], cwd=root,
		                      env=env, capture_output=True, text=True)
	assert run(f'_integration_judge_capture_baseline "{wt}" "{baseline}"').returncode == 0
	return wt, baseline, remote, before, conflict_path, run


@pytest.mark.parametrize("judge_repo", [False, True, "newline"], indirect=True)
def test_conflict_only_resolution_pushes(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\nours\ntheirs\nlast\n")
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode == 0, result.stderr + result.stdout
	assert "outcome=accepted" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() != before


@pytest.mark.parametrize("judge_repo", [False], indirect=True)
@pytest.mark.parametrize("change", ["edit", "new", "delete", "chmod"])
def test_out_of_scope_changes_do_not_push(judge_repo, change):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\nours\ntheirs\nlast\n")
	if change == "edit":
		(wt / "untouched.txt").write_text("changed\n")
	elif change == "new":
		(wt / ".github/workflows").mkdir(parents=True)
		(wt / ".github/workflows/evil.yml").write_text("run: unsafe\n")
	elif change == "delete":
		(wt / "untouched.txt").unlink()
	else:
		(wt / "untouched.txt").chmod(0o755)
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=out_of_scope" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [True], indirect=True)
@pytest.mark.parametrize("change", ["invented", "mode", "duplicate", "reorder"])
def test_protected_conflict_rejects_invented_content_or_mode(judge_repo, change):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	conflicted = wt / conflict_path
	content = {
		"invented": "first\nours\ntheirs\nrun: invented\nlast\n",
		"duplicate": "first\nours\nours\ntheirs\nlast\n",
		"reorder": "last\nours\ntheirs\nfirst\n",
	}.get(change, "first\nours\ntheirs\nlast\n")
	conflicted.write_text(content)
	if change == "mode":
		conflicted.chmod(0o755)
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [True], indirect=True)
def test_scope_verifier_uses_worktree_index_despite_ambient_git_overrides(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\nours\ntheirs\nlast\n")
	git(wt, "add", conflict_path)
	result = run(f'GIT_DIR="{remote}" GIT_INDEX_FILE="{baseline / "index"}" _integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert result.returncode == 0, result.stderr + result.stdout
	assert result.stdout.strip() == git(wt, "write-tree").stdout.strip()


@pytest.mark.parametrize("judge_repo", ["shared"], indirect=True)
@pytest.mark.parametrize("content,accepted", [
	("first\n\nours\n\ntheirs\nlast\n", True),
	("first\n\nours\n\n\ntheirs\nlast\n", False),
])
def test_protected_conflict_combines_shared_lines_from_both_sides(judge_repo, content, accepted):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text(content)
	git(wt, "add", conflict_path)
	result = run(f'_integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert (result.returncode == 0) == accepted, result.stderr + result.stdout
	if accepted:
		assert result.stdout.strip() == git(wt, "write-tree").stdout.strip()
	else:
		assert "reason=protected_path_provenance" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [False], indirect=True)
def test_missing_baseline_fails_closed(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\nours\ntheirs\nlast\n")
	(baseline / "tree").unlink()
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=baseline_unavailable" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before
