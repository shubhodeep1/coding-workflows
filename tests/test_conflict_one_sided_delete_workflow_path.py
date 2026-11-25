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

#10120 adds the verifier side: a must_contain fingerprint on the file is
satisfied when the base branch deleted it after capture (logged as
FINGERPRINT_BASE_DELETION_SUPERSEDED_V1), while a deletion before capture or
by an `[ai-merge-resolve]` commit still fails.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
CHECK_DIFF = REPO_ROOT / "scripts" / "check_resolver_diff.sh"
VERIFIER = REPO_ROOT / "scripts" / "verify_integration_fingerprints.py"
WORKFLOW = ".github/workflows/x.yml"
REPLACEMENT = ".github/workflows/y.yml"
CONFLICTED_SET_CMD = "git ls-files --unmerged | awk '{print $4}' | sort -u >> \"${CONFLICTED_PATHS_FILE}\""


def _env() -> dict[str, str]:
	return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BASH_ENV"}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _conflict(repo: Path, *, reverse: bool = False, delete_subject: str = "fold x.yml into y.yml") -> None:
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
		_git(repo, "commit", "-qm", delete_subject)

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


# --- #10120: the fingerprint verifier accepts a base deletion made after capture.

BEFORE_ALL_COMMITS = "2000-01-01T00:00:00Z"
AFTER_ALL_COMMITS = "2999-01-01T00:00:00Z"
SUPERSEDED_MARKER = "FINGERPRINT_BASE_DELETION_SUPERSEDED_V1"
INVALID_FLAG_WARNING = "invalid INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED"


def _write_fingerprints(tmp: Path, captured_at: str, file: str = WORKFLOW) -> Path:
	fp_path = tmp / "fingerprints.json"
	fp_path.write_text(
		json.dumps(
			{
				"9001": {
					"issue": 9001,
					"pr": 9002,
					"captured_at": captured_at,
					"must_contain": [{"file": file, "regex": re.escape("runs-on: ubuntu-24.04")}],
					"must_not_contain": [],
					"must_not_exist": [],
				}
			}
		),
		encoding="utf-8",
	)
	return fp_path


def _run_verifier(
	repo: Path,
	tmp: Path,
	fp_path: Path,
	*args: str,
	extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
	env = _env()
	for key in (
		"INTEGRATION_VERIFY_REF",
		"INTEGRATION_FINGERPRINTS_FILE",
		"INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED",
		"BASE_BRANCH",
		"FINGERPRINT_QUARANTINE_RUNS_M",
	):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["RUNNER_TEMP"] = str(tmp)
	env.update(extra_env or {})
	return subprocess.run(
		["python3", str(VERIFIER), *args, str(fp_path)],
		check=False,
		cwd=repo,
		env=env,
		text=True,
		capture_output=True,
	)


def _main_tip(repo: Path) -> str:
	return _git(repo, "rev-parse", "main").stdout.strip()


def test_fingerprint_base_deletion_after_capture_passes() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		_git(repo, "rm", "-q", "--", WORKFLOW)
		result = _run_verifier(repo, tmp, fp_path)
		assert result.returncode == 0, result.stdout + result.stderr
		expected = (
			f"{SUPERSEDED_MARKER} issue=#9001 pr=#9002 file={WORKFLOW} "
			f"commit={_main_tip(repo)[:12]} base_ref=MERGE_HEAD"
		)
		assert expected in result.stderr, result.stderr


def test_fingerprint_base_deletion_after_capture_passes_compare_mode() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		baseline = tmp / "baseline.json"
		capture = _run_verifier(repo, tmp, fp_path, "--baseline-fingerprints-state", str(baseline))
		assert capture.returncode == 0, capture.stdout + capture.stderr
		state = json.loads(baseline.read_text(encoding="utf-8"))
		assert state["fingerprints"]["9001"]["must_contain"][0]["satisfied"] is True
		_git(repo, "rm", "-q", "--", WORKFLOW)
		result = _run_verifier(repo, tmp, fp_path, "--compare-against-baseline", str(baseline))
		assert result.returncode == 0, result.stdout + result.stderr
		assert f"{SUPERSEDED_MARKER} issue=#9001 pr=#9002 file={WORKFLOW}" in result.stderr, result.stderr
		assert "base_ref=MERGE_HEAD" in result.stderr


def test_fingerprint_base_deletion_before_capture_still_fails() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, AFTER_ALL_COMMITS)
		_git(repo, "rm", "-q", "--", WORKFLOW)
		result = _run_verifier(repo, tmp, fp_path)
		assert result.returncode == 1, result.stdout + result.stderr
		assert SUPERSEDED_MARKER not in result.stderr
		assert "file does not exist in post-resolve tree" in result.stdout


def test_fingerprint_resolver_deletion_still_fails() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name, delete_subject="[ai-merge-resolve] drop x.yml")
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		_git(repo, "rm", "-q", "--", WORKFLOW)
		result = _run_verifier(repo, tmp, fp_path)
		assert result.returncode == 1, result.stdout + result.stderr
		assert SUPERSEDED_MARKER not in result.stderr


def test_fingerprint_edit_side_still_present_in_base_fails() -> None:
	"""Reverse conflict: MERGE_HEAD still has the file, so a resolver deletion stays a violation."""
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name, reverse=True)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		_git(repo, "rm", "-q", "--", WORKFLOW)
		result = _run_verifier(repo, tmp, fp_path)
		assert result.returncode == 1, result.stdout + result.stderr
		assert SUPERSEDED_MARKER not in result.stderr


def test_fingerprint_base_deletion_supersede_off_switch() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		_git(repo, "rm", "-q", "--", WORKFLOW)
		flag = "INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED"

		off = _run_verifier(repo, tmp, fp_path, extra_env={flag: "false"})
		assert off.returncode == 1, off.stdout + off.stderr
		assert SUPERSEDED_MARKER not in off.stderr
		assert INVALID_FLAG_WARNING not in off.stdout + off.stderr

		bogus = _run_verifier(repo, tmp, fp_path, extra_env={flag: "bogus"})
		assert bogus.returncode == 1, bogus.stdout + bogus.stderr
		assert SUPERSEDED_MARKER not in bogus.stderr
		assert (bogus.stdout + bogus.stderr).count(INVALID_FLAG_WARNING) == 1, bogus.stderr

		for on_value in ("TRUE", "", " true "):
			on = _run_verifier(repo, tmp, fp_path, extra_env={flag: on_value})
			assert on.returncode == 0, (on_value, on.stdout + on.stderr)
			assert SUPERSEDED_MARKER in on.stderr


def test_fingerprint_base_deletion_origin_base_branch_fallback() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		_git(repo, "merge", "--abort")
		_git(repo, "checkout", "-q", "main")
		_git(repo, "update-ref", "refs/remotes/origin/main", "main")
		assert not (repo / WORKFLOW).exists()

		no_base = _run_verifier(repo, tmp, fp_path)
		assert no_base.returncode == 1, no_base.stdout + no_base.stderr

		ok = _run_verifier(repo, tmp, fp_path, extra_env={"BASE_BRANCH": "main"})
		assert ok.returncode == 0, ok.stdout + ok.stderr
		assert f"commit={_main_tip(repo)[:12]} base_ref=origin/main" in ok.stderr, ok.stderr

		for unsafe in ("../main", "-c", "main..feat", "main.lock"):
			bad = _run_verifier(repo, tmp, fp_path, extra_env={"BASE_BRANCH": unsafe})
			assert bad.returncode == 1, (unsafe, bad.stdout + bad.stderr)
			assert SUPERSEDED_MARKER not in bad.stderr


def test_fingerprint_base_deletion_ref_mode() -> None:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		_git(repo, "merge", "--abort")
		result = _run_verifier(repo, tmp, fp_path, "--ref", "main")
		assert result.returncode == 0, result.stdout + result.stderr
		assert f"commit={_main_tip(repo)[:12]} base_ref=main" in result.stderr, result.stderr

		late = _write_fingerprints(tmp, AFTER_ALL_COMMITS)
		late_result = _run_verifier(repo, tmp, late, "--ref", "main")
		assert late_result.returncode == 1, late_result.stdout + late_result.stderr
		assert SUPERSEDED_MARKER not in late_result.stderr


def test_fingerprint_base_deletion_glob_path_is_literal() -> None:
	"""A glob-like fingerprint path must not match the deleted sibling `x.yml`."""
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS, file=".github/workflows/[x].yml")
		_git(repo, "rm", "-q", "--", WORKFLOW)
		result = _run_verifier(repo, tmp, fp_path)
		assert result.returncode == 1, result.stdout + result.stderr
		assert SUPERSEDED_MARKER not in result.stderr


def test_fingerprint_base_deletion_invalid_flag_warns_without_missing_file() -> None:
	"""An invalid off-switch value warns once even when every must_contain file is present."""
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp, repo = _setup(tmp_name)
		fp_path = _write_fingerprints(tmp, BEFORE_ALL_COMMITS)
		flag = "INTEGRATION_FINGERPRINT_BASE_DELETION_SUPERSEDE_ENABLED"
		result = _run_verifier(repo, tmp, fp_path, extra_env={flag: "flase"})
		assert result.returncode == 0, result.stdout + result.stderr
		assert SUPERSEDED_MARKER not in result.stderr
		assert (result.stdout + result.stderr).count(INVALID_FLAG_WARNING) == 1, result.stderr
