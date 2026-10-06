"""The integration judge may publish only verified merge resolutions."""

import os
import re
import subprocess
from pathlib import Path

import pytest
from review_autofix_step_scripts import expanded_review_autofix_text


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
	if isinstance(request.param, tuple):
		conflict_path = request.param[1]
	elif request.param == "newline":
		conflict_path = "lines\nbreak.txt"
	else:
		conflict_path = ".github/workflows/ci.yml" if request.param else "conflict.txt"
	conflicted = root / conflict_path
	conflicted.parent.mkdir(parents=True, exist_ok=True)
	conflicted.write_text("first\nshared\nbase\nlast\n" if request.param == "base-shared" else "first\nbase\nlast\n")
	(root / "untouched.txt").write_text("untouched\n")
	git(root, "add", ".")
	git(root, "commit", "-m", "base")
	git(root, "remote", "add", "origin", str(remote))
	git(root, "push", "origin", "main")
	git(root, "checkout", "-b", "integration")
	if request.param == "deleted-ours":
		conflicted.unlink()
	else:
		conflicted.write_text("first\n" + "\n" * 2000 + "ours\nlast\n" if request.param == "repeated" else
		                      "first\nshared\nours\nlast\n" if request.param == "base-shared" else
		                      "first\n\nours\nlast\n" if request.param == "shared" else "first\nours\nlast\n")
	git(root, "commit", "-am", "ours")
	git(root, "push", "origin", "integration")
	before = git(remote, "rev-parse", "refs/heads/integration").stdout.strip()
	git(root, "checkout", "main")
	if request.param == "deleted-theirs":
		conflicted.unlink()
	else:
		conflicted.write_text("first\n" + "\n" * 2000 + "theirs\nlast\n" if request.param == "repeated" else
		                      "first\nshared\ntheirs\nlast\n" if request.param == "base-shared" else
		                      "first\n\ntheirs\nlast\n" if request.param == "shared" else "first\ntheirs\nlast\n")
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


@pytest.mark.parametrize("judge_repo", [False, True, "newline", ("path", "src/app.py")], indirect=True)
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


@pytest.mark.parametrize("judge_repo", [True, False, ("path", "src/app.py")], indirect=True)
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


@pytest.mark.parametrize("judge_repo", [("path", path) for path in (
	"scripts/helper.sh", "prompts/mode-x.txt", ".claude/hooks/guard.py",
	"workflow-templates/ai-x.yml", "docs/nested/AGENTS.md", "package.json",
	"src/Makefile", ".GitHub/Workflows/ci.yml",
)], indirect=True)
@pytest.mark.parametrize("change", ["invented", "duplicate", "reorder"])
def test_other_protected_conflicts_reject_unverified_lines(judge_repo, change):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	content = {
		"invented": "first\nours\ntheirs\nnew executable line\nlast\n",
		"duplicate": "first\nours\nours\ntheirs\nlast\n",
		"reorder": "last\nours\ntheirs\nfirst\n",
	}[change]
	(wt / conflict_path).write_text(content)
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert f"path='{conflict_path}'" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [("path", "scripts/helper.sh")], indirect=True)
def test_protected_script_accepts_interleaved_side_lines(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\nours\ntheirs\nlast\n")
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode == 0, result.stderr + result.stdout
	assert "outcome=accepted" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() != before


@pytest.mark.parametrize("judge_repo", [True], indirect=True)
@pytest.mark.parametrize("change", ["empty", "remove_shared", "delete"])
def test_protected_resolution_cannot_remove_shared_controls(judge_repo, change):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	conflicted = wt / conflict_path
	if change == "delete":
		conflicted.unlink()
	else:
		conflicted.write_text("" if change == "empty" else "first\nours\ntheirs\n")
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", ["deleted-ours", "deleted-theirs"], indirect=True)
def test_protected_one_sided_conflict_can_choose_deletion(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).unlink()
	git(wt, "add", "-A", "--", conflict_path)
	result = run(f'_integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert result.returncode == 0, result.stderr + result.stdout
	assert "outcome=accepted" in result.stderr
	assert result.stdout.strip() == git(wt, "write-tree").stdout.strip()
	assert git(wt, "ls-files", "--", conflict_path).stdout == ""
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", ["repeated"], indirect=True)
def test_protected_resolution_retains_shared_duplicate_counts(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\n\nours\ntheirs\nlast\n")
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo,content", [
	(True, ""),
	(("path", "scripts/helper.sh"), ""),
	("base-shared", "first\nours\ntheirs\nlast\n"),
	(True, "first\nours\ntheirs\n"),
	("shared", "first\nours\ntheirs\nlast\n"),
], indirect=["judge_repo"])
def test_protected_conflict_rejects_removed_shared_line_before_push(judge_repo, content):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text(content)
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert f"path='{conflict_path}'" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [True], indirect=True)
@pytest.mark.parametrize("content", ["first\nours\nlast\n", "first\ntheirs\nlast\n"])
def test_protected_conflict_accepts_either_whole_side(judge_repo, content):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text(content)
	git(wt, "add", conflict_path)
	result = run(f'_integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert result.returncode == 0, result.stderr + result.stdout
	assert result.stdout.strip() == git(wt, "write-tree").stdout.strip()
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [("path", "src/app.py")], indirect=True)
def test_unprotected_conflict_accepts_empty_resolution(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("")
	git(wt, "add", conflict_path)
	result = run(f'_integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert result.returncode == 0, result.stderr + result.stdout
	assert result.stdout.strip() == git(wt, "write-tree").stdout.strip()
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


def test_integration_judge_prompt_requires_shared_line_retention():
	assert "keep every line both merge sides contain" in POLL_SCRIPT
	assert "at least as many times as the side with fewer copies" in POLL_SCRIPT


@pytest.mark.parametrize("judge_repo", [("path", "src/app.py")], indirect=True)
def test_unprotected_source_rejects_invented_lines(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\nours\ntheirs\nnew line\nlast\n")
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", [("path", "src/app.py")], indirect=True)
def test_unprotected_conflict_deletion_rejected(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).unlink()
	result = run(f'_integration_judge_commit_and_push "{wt}" 42 integration main "{baseline}" 1')
	assert result.returncode != 0
	assert "reason=protected_path_provenance" in result.stderr
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


def test_protected_path_patterns_match_review_skip_gate():
	gate_patterns = re.findall(
		r'^\s*([^\n]+)\)\n\s*PROTECTED_SKIP_SUPPRESSED="true" ;;',
		expanded_review_autofix_text(), flags=re.MULTILINE,
	)
	assert len(gate_patterns) == 4
	verifier = _extract_bash_function("_integration_judge_verify_scope() {")
	for constant, gate_pattern in zip((
		"PROTECTED_BASENAMES", "PROTECTED_PATH_GLOBS", "PROTECTED_BASENAME_GLOBS",
		"PROTECTED_ROOT_BASENAME_GLOBS",
	), gate_patterns):
		match = re.search(rf'^{constant} = \(\n    "([^"]+)"\n\)\.split\("\|"\)', verifier, flags=re.MULTILINE)
		assert match, constant
		assert match.group(1) == gate_pattern.strip(), constant
	assert "if not is_protected_conflict_path(path):" not in verifier
	assert "def is_protected_conflict_path(path):" in verifier


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


@pytest.mark.parametrize("judge_repo", ["base-shared"], indirect=True)
@pytest.mark.parametrize("content,accepted", [
	("first\nshared\nours\ntheirs\nlast\n", True),
	("first\nshared\nours\ntheirs\nshared\nlast\n", False),
])
def test_protected_conflict_does_not_duplicate_unchanged_base_lines(judge_repo, content, accepted):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text(content)
	git(wt, "add", conflict_path)
	result = run(f'_integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert (result.returncode == 0) == accepted, result.stderr + result.stdout
	assert git(remote, "rev-parse", "refs/heads/integration").stdout.strip() == before


@pytest.mark.parametrize("judge_repo", ["repeated"], indirect=True)
def test_protected_conflict_rejects_excessive_provenance_work(judge_repo):
	wt, baseline, remote, before, conflict_path, run = judge_repo
	(wt / conflict_path).write_text("first\n" + "\n" * 3000 + "ours\ntheirs\nlast\n")
	git(wt, "add", conflict_path)
	result = run(f'_integration_judge_verify_scope "{wt}" "{baseline}" 42')
	assert result.returncode != 0
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
