"""A workflow file one side deleted and the other modified (heal of PR 9442).

Review run 44646017321 merged `main` into `orchestrator/project-9421` and
stopped on `.github/workflows/lint-pr-body-auto-close.yml` at index stages
1 2: `main` had deleted the file (folded into `pr-checks.yml`), the branch
still edited it. The resolver left the file present and unchanged, the
touched-set detection did not record it, and `check_resolver_diff.sh`
refused the `[ai-merge-resolve]` commit with "keep or delete it explicitly".

These tests pin the guard as it is today, so the later prompt and keep-line
fix cannot weaken it:
- prepare hands the path to the resolver (no deterministic handler takes it);
- leaving the path present and unchanged fails, for stages 1 2 and 1 3;
- deleting it counts as touched, passes, and is staged into the
  two-parent `[ai-merge-resolve]` commit;
- editing an unrelated file instead still fails.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
CHECK_DIFF = REPO_ROOT / "scripts" / "check_resolver_diff.sh"
WORKFLOW = ".github/workflows/x.yml"
REPLACEMENT = ".github/workflows/y.yml"
CONFLICTED_SET_CMD = "git ls-files --unmerged | awk '{print $4}' | sort -u >> \"${CONFLICTED_PATHS_FILE}\""


def _env() -> dict[str, str]:
	return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _conflict(repo: Path, *, reverse: bool = False) -> None:
	"""`feat` (HEAD) edits the workflow and `main` deletes it; `reverse` swaps the sides."""
	_git(repo, "init", "-q", "-b", "main")
	_git(repo, "config", "user.name", "t")
	_git(repo, "config", "user.email", "t@t")
	(repo / WORKFLOW).parent.mkdir(parents=True, exist_ok=True)
	(repo / WORKFLOW).write_text("name: X\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo base\n", encoding="utf-8")
	(repo / "README.md").write_text("# Repo\n", encoding="utf-8")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")

	def edit_side() -> None:
		(repo / WORKFLOW).write_text("name: X\non: push\njobs:\n  x:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo edited\n", encoding="utf-8")
		_git(repo, "commit", "-qam", "edit workflow")

	def delete_side() -> None:
		_git(repo, "rm", "-q", "--", WORKFLOW)
		(repo / REPLACEMENT).parent.mkdir(parents=True, exist_ok=True)
		# Content unlike x.yml, so git does not pair the delete and add as a rename.
		(repo / REPLACEMENT).write_text(
			"name: Per-PR checks\non:\n  pull_request:\n    types: [opened, edited, synchronize]\npermissions:\n  contents: read\n"
			"concurrency: per-pr-checks\ndefaults:\n  run:\n    shell: bash\n",
			encoding="utf-8",
		)
		_git(repo, "add", "-A")
		_git(repo, "commit", "-qm", "fold x.yml into y.yml")

	_git(repo, "checkout", "-q", "-b", "feat")
	if reverse:
		delete_side()
		_git(repo, "checkout", "-q", "main")
		edit_side()
	else:
		edit_side()
		_git(repo, "checkout", "-q", "main")
		delete_side()
	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0
	stages = sorted({line.split()[2] for line in _git(repo, "ls-files", "-u", "--", WORKFLOW).stdout.splitlines()})
	assert stages == (["1", "3"] if reverse else ["1", "2"]), stages


def _setup(tmp_name: str, **kwargs) -> tuple[Path, Path]:
	tmp = Path(tmp_name)
	repo = tmp / "repo"
	repo.mkdir()
	_conflict(repo, **kwargs)
	return tmp, repo


def _slice(path: Path, start_marker: str, end_marker: str, *, include_end: bool) -> str:
	text = path.read_text(encoding="utf-8")
	start = text.index(start_marker)
	end = text.index(end_marker, start)
	if include_end:
		end += len(end_marker)
	return text[start:end]


def _conflicted_list_block() -> str:
	return _slice(
		PREPARE,
		'CONFLICTED_FILES_RAW="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"',
		'echo "Conflict resolver: ${CONFLICTED_FILES_COUNT} unmerged path(s) to resolve in this run."',
		include_end=True,
	)


def _snapshot_block() -> str:
	return _slice(PREPARE, "while IFS= read -r -d '' snap_path; do", 'echo "Pre-resolver snapshot captured:', include_end=False)


def _touched_block() -> str:
	return _slice(
		RESOLVE,
		'PRE_UNTRACKED_LIST="$(mktemp)"',
		'rm -f "${PRE_UNTRACKED_LIST}" "${POST_UNTRACKED_LIST}"',
		include_end=True,
	)


def _staging_functions_block() -> str:
	"""`stage_resolver_touched_path_or_fail` and `verify_resolver_index_complete_or_fail`."""
	return _slice(RESOLVE, "stage_resolver_touched_path_or_fail() {", "# Deterministic-resolution short-circuit:", include_end=False)


def _staging_loop_block() -> str:
	"""Resolve's per-path staging loop over RESOLVER_TOUCHED_FILE (workflow source repo branch)."""
	return _slice(
		RESOLVE,
		"git rm -r --cached --ignore-unmatch -- node_modules 2>/dev/null || true",
		'done < "${RESOLVER_TOUCHED_FILE}"',
		include_end=True,
	)


def _folded_fragment_block() -> str:
	return _slice(
		PREPARE,
		"# Deterministic resolution for folded changelog / agents fragments.",
		"# Deterministic resolution for the generated workspace manifest.",
		include_end=False,
	)


def test_prepare_builds_the_conflicted_set_from_unmerged_entries() -> None:
	assert CONFLICTED_SET_CMD in PREPARE.read_text(encoding="utf-8")


def test_conflicted_list_names_the_one_sided_delete_workflow_path() -> None:
	for reverse in (False, True):
		with tempfile.TemporaryDirectory() as tmp_name:
			_, repo = _setup(tmp_name, reverse=reverse)
			script = f"""set -euo pipefail
{_conflicted_list_block()}
echo "COUNT=${{CONFLICTED_FILES_COUNT}}"
printf '%s\\n' "${{CONFLICTED_FILES_LIST}}"
"""
			result = subprocess.run(["bash", "-c", script], check=False, cwd=repo, env=_env(), text=True, capture_output=True)
			assert result.returncode == 0, result.stdout + result.stderr
			assert "COUNT=1" in result.stdout, (reverse, result.stdout)
			# startswith: a later stage note appended to the line keeps this green.
			assert any(line.startswith(f"          - {WORKFLOW}") for line in result.stdout.splitlines()), (reverse, result.stdout)


def _run_folded_block(repo: Path, tmp: Path, *, head_ref: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
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
{_folded_fragment_block()}
echo "AFTER_BLOCK count=${{_resolver_allowlist_count}} defer=${{_ff_defer_commit}}"
"""
	env = _env()
	env.pop("TARGET_BRANCH", None)
	env.pop("CONFLICT_FOLDED_FRAGMENT_KEEP_ENABLED", None)
	env.pop("CONFLICT_FORWARD_MERGE_FRAGMENT_KEEP_ENABLED", None)
	return subprocess.run(["bash", "-c", script], check=False, cwd=repo, env=env, text=True, capture_output=True), github_env, allowlist


def test_folded_fragment_keep_leaves_the_workflow_path_to_the_resolver() -> None:
	for head_ref in ("feat", "orchestrator/project-42"):
		with tempfile.TemporaryDirectory() as tmp_name:
			tmp, repo = _setup(tmp_name)
			result, github_env, allowlist = _run_folded_block(repo, tmp, head_ref=head_ref)
			assert result.returncode == 0, result.stdout + result.stderr
			assert "AFTER_BLOCK count=1 defer=false" in result.stdout, (head_ref, result.stdout)
			assert allowlist.read_text(encoding="utf-8") == f"{WORKFLOW}\n"
			assert "Folded fragment keep: kept" not in result.stdout
			assert not github_env.exists()


def _run_pipeline(repo: Path, tmp: Path, action: str, *, commit: bool = False) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
	"""Run prepare's snapshot, the mocked resolver, resolve's touched detection and the check.

	With `commit`, a passing check is followed by resolve's staging loop, the
	unmerged-index check and the `[ai-merge-resolve]` commit.
	"""
	commit_block = ""
	if commit:
		commit_block = f"""[ "${{rc}}" -eq 0 ]
GITHUB_ENV={tmp / "github.env"!s}
{_staging_functions_block()}
{_staging_loop_block()}
verify_resolver_index_complete_or_fail
git commit -q -m "[ai-merge-resolve] resolve merge conflicts"
echo "COMMITTED"
"""
	runtime = tmp / "runtime"
	runtime.mkdir()
	state = runtime / "pre_resolver_state.tsv"
	conflicted = runtime / "conflicted_paths.txt"
	touched = runtime / "codex_touched_resolver.txt"
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime!s}
PRE_RESOLVER_STATE_FILE={state!s}
CONFLICTED_PATHS_FILE={conflicted!s}
RESOLVER_TOUCHED_FILE={touched!s}
: > "${{PRE_RESOLVER_STATE_FILE}}"
{_snapshot_block()}
: > "${{CONFLICTED_PATHS_FILE}}"
{CONFLICTED_SET_CMD}
# Like resolve (#5627), the mocked model gets a private copy of the merge
# index, so its `git rm` leaves the real index for resolve's staging loop.
cp -- "$(git rev-parse --path-format=absolute --git-path index)" "${{RUNTIME_DIR}}/resolver_model_index"
(
export GIT_INDEX_FILE="${{RUNTIME_DIR}}/resolver_model_index"
{action}
)
: > "${{RESOLVER_TOUCHED_FILE}}"
{_touched_block()}
sort -u -o "${{RESOLVER_TOUCHED_FILE}}" "${{RESOLVER_TOUCHED_FILE}}"
rc=0
bash {str(CHECK_DIFF)!s} --conflicted-set "${{CONFLICTED_PATHS_FILE}}" --touched-set "${{RESOLVER_TOUCHED_FILE}}" --repo-root {str(repo)!s} || rc=$?
echo "RC=${{rc}}"
{commit_block}"""
	result = subprocess.run(["bash", "-c", script], check=False, cwd=repo, env=_env(), text=True, capture_output=True)
	return result, touched, conflicted


def _assert_untouched_fails(reverse: bool, stages: str) -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name, reverse=reverse)
		result, touched, conflicted = _run_pipeline(repo, tmp, ":")
		assert result.returncode == 0, result.stdout + result.stderr
		assert conflicted.read_text(encoding="utf-8") == f"{WORKFLOW}\n"
		assert WORKFLOW not in touched.read_text(encoding="utf-8").splitlines()
		assert "RC=1" in result.stdout, result.stdout + result.stderr
		assert f"  - {WORKFLOW} (index stages: {stages})" in result.stderr
		assert "keep or delete it explicitly" in result.stderr
		assert "check_resolver_diff: OK" not in result.stdout


def test_untouched_one_sided_delete_fails_check_resolver_diff() -> None:
	_assert_untouched_fails(reverse=False, stages="1 2")


def test_untouched_one_sided_delete_fails_on_stages_1_3() -> None:
	_assert_untouched_fails(reverse=True, stages="1 3")


def test_deleting_the_path_counts_as_touched_and_passes() -> None:
	for reverse in (False, True):
		for action in (f"rm -f -- {WORKFLOW}", f"git rm -q -- {WORKFLOW}"):
			with tempfile.TemporaryDirectory() as tmp_name:
				tmp, repo = _setup(tmp_name, reverse=reverse)
				result, touched, _ = _run_pipeline(repo, tmp, action, commit=True)
				assert result.returncode == 0, (reverse, action, result.stdout + result.stderr)
				assert WORKFLOW in touched.read_text(encoding="utf-8").splitlines(), (reverse, action)
				assert "RC=0" in result.stdout, (reverse, action, result.stdout + result.stderr)
				assert "check_resolver_diff: OK" in result.stdout
				# The staged deletion produces the two-parent [ai-merge-resolve] commit.
				assert "COMMITTED" in result.stdout, (reverse, action, result.stdout + result.stderr)
				assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "[ai-merge-resolve] resolve merge conflicts"
				assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3
				assert _git(repo, "ls-files", "-u").stdout == ""
				assert _git(repo, "ls-tree", "--name-only", "HEAD", "--", WORKFLOW).stdout == ""
				assert _git(repo, "ls-tree", "--name-only", "HEAD", "--", REPLACEMENT).stdout.strip() == REPLACEMENT


def test_unrelated_edit_does_not_satisfy_the_guard() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		result, touched, _ = _run_pipeline(repo, tmp, "printf 'edited\\n' >> README.md")
		assert result.returncode == 0, result.stdout + result.stderr
		assert "README.md" in touched.read_text(encoding="utf-8").splitlines()
		assert "RC=1" in result.stdout, result.stdout + result.stderr
		assert "Conflict resolver edited files outside the conflicted set" in result.stderr
		assert "check_resolver_diff: OK" not in result.stdout
