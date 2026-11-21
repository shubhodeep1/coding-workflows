#!/usr/bin/env python3
"""Contract tests for the modify/delete take-base and stage context (issue #9886).

Phase 4 of docs/plans/pipeline-human-wait-elimination-plan.md. PR #9452 applied
a `runs-on:` pin to every workflow while a squash-merged base PR deleted one of
them. Merging the base gave a modify/delete conflict (index stages 1 2) that
the model resolver left unmerged on every run.

- scripts/review_conflict_prepare.sh takes the base deletion without a model
  call when the PR's edit to the file is a sweep repeated in other files.
- scripts/review_conflict_resolve.sh gives the model stage context for the
  modify/delete paths it still gets and, on the workflow source repo, retries
  inside the loop when a conflicted path is left unchanged and unmerged.
- CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED=false restores the previous
  behaviour.

The shell blocks and functions run against real temporary git repositories
with an in-progress conflicting merge.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE = REPO_ROOT / "scripts" / "review_conflict_prepare.sh"
RESOLVE = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README = REPO_ROOT / "README.md"
AGENTS_FRAGMENT = REPO_ROOT / "agents.d" / "9886-modify-delete-take-base.md"
PROMPT = REPO_ROOT / "prompts" / "conflict-resolver.txt"

_STRIP_ENV = (
	"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
	"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "BASH_ENV", "ENV",
	"CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED", "CONFLICT_MODIFY_DELETE_MAX_CHANGED_LINES",
	"RESOLVER_DROPPED_EDITS_FILE", "TARGET_BRANCH", "HEAD_REF", "PR_NUMBER", "BASE_BRANCH",
	"IS_WORKFLOW_SOURCE_REPO", "GITHUB_REPOSITORY",
)

SWEPT = "c.yml"
WORKFLOWS = (".github/workflows/a.yml", ".github/workflows/b.yml", f".github/workflows/{SWEPT}")


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
	env = {key: value for key, value in os.environ.items() if key not in _STRIP_ENV}
	env.update({
		"GIT_CONFIG_GLOBAL": os.devnull,
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t",
		"GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@example.invalid",
		"PYTHONDONTWRITEBYTECODE": "1",
	})
	env.update(extra or {})
	return env


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
	return subprocess.run(["git", *args], cwd=repo, env=_env(), check=check, text=True, capture_output=True)


def _workflow(name: str, runs_on: str) -> str:
	lines = [f"name: {name}", "on: push", "jobs:", "  build:", f"    runs-on: {runs_on}", "    steps:"]
	lines += [f"      - run: echo step {i}" for i in range(12)]
	return "\n".join(lines) + "\n"


def _write(repo: Path, files: dict[str, str]) -> None:
	for name, text in files.items():
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")


def _sweep_repo(
	tmp: Path,
	*,
	delete_subject: str = "Remove the c workflow (#123)",
	pr_extra: dict[str, str] | None = None,
	swept_override: str | None = None,
	other_conflict: bool = False,
	extra_deleted: tuple[str, ...] = (),
) -> Path:
	"""main deletes c.yml; feat (the PR) pins runs-on in all three workflows."""
	if shutil.which("git") is None or shutil.which("bash") is None:
		pytest.skip("git/bash not available")
	repo = tmp / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	base = {path: _workflow(Path(path).stem, "ubuntu-latest") for path in WORKFLOWS}
	base["notes.txt"] = "base\n"
	for path in extra_deleted:
		base[path] = "base copy\n"
	_write(repo, base)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-q", "-b", "feat")
	pr = {path: _workflow(Path(path).stem, "ubuntu-24.04") for path in WORKFLOWS}
	if swept_override is not None:
		pr[WORKFLOWS[2]] = swept_override
	if other_conflict:
		pr["notes.txt"] = "pr side\n"
	for path in extra_deleted:
		pr[path] = "pr copy\n"
	pr.update(pr_extra or {})
	_write(repo, pr)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "Pin runs-on")
	_git(repo, "checkout", "-q", "main")
	_git(repo, "rm", "-q", "--", WORKFLOWS[2], *extra_deleted)
	if other_conflict:
		_write(repo, {"notes.txt": "main side\n"})
		_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", delete_subject)
	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0
	return repo


def _prepare_block() -> str:
	text = PREPARE.read_text(encoding="utf-8")
	start = text.index("# >>> non-fragment modify/delete take-base")
	end = text.index("# <<< non-fragment modify/delete take-base", start)
	return text[start:end]


def _run_prepare_block(repo: Path, extra: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], Path]:
	runtime = repo.parent / "runtime"
	runtime.mkdir(exist_ok=True)
	allowlist = runtime / "allowlist.txt"
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime!s}
RESOLVER_ALLOWLIST_FILE={allowlist!s}
git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_ALLOWLIST_FILE}}"
_resolver_allowlist_count="$(wc -l < "${{RESOLVER_ALLOWLIST_FILE}}" | tr -d '[:space:]')"
{_prepare_block()}
echo "AFTER_BLOCK count=${{_resolver_allowlist_count}} resolved=${{_mdd_resolved}} defer=${{_mdd_defer_commit}} paths=${{_mdd_taken_paths}}"
"""
	result = subprocess.run(["bash", "-c", script], cwd=repo, env=_env(extra), text=True, capture_output=True)
	return result, runtime


def _unmerged(repo: Path) -> list[str]:
	return [line for line in _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if line]


# --- prepare: deterministic take-base -------------------------------------


def test_pr9452_runs_on_sweep_takes_base_without_model(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path)
	assert _unmerged(repo) == [WORKFLOWS[2]]
	result, runtime = _run_prepare_block(repo, {"PR_NUMBER": "9452", "BASE_BRANCH": "main"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete take-base: path={WORKFLOWS[2]} deleted_by=#123 pr_lines=2" in result.stdout
	assert "AFTER_BLOCK count=0 resolved=true defer=true" in result.stdout
	assert _unmerged(repo) == []
	assert not (repo / WORKFLOWS[2]).exists()
	assert _git(repo, "ls-files", "--", WORKFLOWS[2]).stdout == ""
	assert (runtime / "resolver_dropped_edits.tsv").read_text(encoding="utf-8") == f"{WORKFLOWS[2]}\t#123\t2\n"
	comment = (runtime / "modify_delete_take_base_comment.md").read_text(encoding="utf-8")
	head = _git(repo, "rev-parse", "HEAD").stdout.strip()
	assert comment.startswith(f"<!-- ai:modify-delete-take-base:v1 pr=9452 head={head} -->\n")
	assert "by #123" in comment and f"`{WORKFLOWS[2]}`" in comment
	fenced = re.search(r"^(`{3,})diff\n(.*?)^\1$", comment, re.M | re.S)
	assert fenced, comment
	excerpt = fenced.group(2).splitlines()
	assert len(excerpt) == 10
	assert excerpt[0].startswith("diff --git ")


def test_take_base_with_other_conflicts_leaves_the_rest_to_the_resolver(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path, other_conflict=True)
	result, _ = _run_prepare_block(repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "deleted_by=#123 pr_lines=2" in result.stdout
	assert "AFTER_BLOCK count=1 resolved=true defer=false" in result.stdout
	assert _unmerged(repo) == ["notes.txt"]


def test_substantive_edit_is_not_a_sweep(tmp_path: Path) -> None:
	swept = _workflow("c", "ubuntu-24.04").replace("echo step 3", "echo only-here")
	repo = _sweep_repo(tmp_path, swept_override=swept)
	result, runtime = _run_prepare_block(repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete take-base: path={WORKFLOWS[2]} outcome=skip reason=not_a_sweep" in result.stdout
	assert "AFTER_BLOCK count=1 resolved=false defer=false" in result.stdout
	assert _unmerged(repo) == [WORKFLOWS[2]]
	assert not (runtime / "modify_delete_take_base_comment.md").exists()


def test_trailing_whitespace_edit_is_not_a_sweep(tmp_path: Path) -> None:
	# Only leading whitespace is normalised away: a trailing-space change that
	# no other file repeats stays with the model.
	repo = _sweep_repo(tmp_path, swept_override=_workflow("c", "ubuntu-24.04  "))
	result, _ = _run_prepare_block(repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete take-base: path={WORKFLOWS[2]} outcome=skip reason=not_a_sweep" in result.stdout
	assert _unmerged(repo) == [WORKFLOWS[2]]


def test_change_over_the_cap_is_too_large(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path)
	result, _ = _run_prepare_block(repo, {"CONFLICT_MODIFY_DELETE_MAX_CHANGED_LINES": "1"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "reason=too_large" in result.stdout
	assert _unmerged(repo) == [WORKFLOWS[2]]


def test_invalid_cap_falls_back_to_default(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path)
	result, _ = _run_prepare_block(repo, {"CONFLICT_MODIFY_DELETE_MAX_CHANGED_LINES": "lots"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "CONFLICT_MODIFY_DELETE_MAX_CHANGED_LINES is not a non-negative integer; falling back to 20" in result.stdout
	assert "deleted_by=#123 pr_lines=2" in result.stdout


def test_deleting_commit_without_pr_number_is_skipped(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path, delete_subject="Remove the c workflow")
	result, _ = _run_prepare_block(repo)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete take-base: path={WORKFLOWS[2]} outcome=skip reason=no_pr_number" in result.stdout
	assert _unmerged(repo) == [WORKFLOWS[2]]


def test_fragments_and_ai_assets_are_left_to_their_own_rules(tmp_path: Path) -> None:
	owned = ("changelog.d/1-x.md", ".ai/validate.yml", ".gitleaksignore")
	repo = _sweep_repo(tmp_path, extra_deleted=owned)
	result, _ = _run_prepare_block(repo)
	assert result.returncode == 0, result.stdout + result.stderr
	for path in owned:
		assert f"path={path}" not in result.stdout
	assert sorted(_unmerged(repo)) == sorted(owned)
	assert "deleted_by=#123" in result.stdout


def test_integration_sync_head_leaves_modify_delete_to_resolver(tmp_path: Path) -> None:
	# A merged sub-issue fingerprint may name the file; the fingerprints load
	# after the block, so integration-sync heads keep the pre-#9886 path.
	repo = _sweep_repo(tmp_path)
	result, runtime = _run_prepare_block(repo, {"TARGET_BRANCH": "orchestrator/project-9650"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete take-base: path={WORKFLOWS[2]} outcome=skip reason=integration_sync" in result.stdout
	assert "AFTER_BLOCK count=1 resolved=false defer=false" in result.stdout
	assert _unmerged(repo) == [WORKFLOWS[2]]
	assert not (runtime / "resolver_dropped_edits.tsv").exists()


def test_switch_false_leaves_modify_delete_to_resolver(tmp_path: Path) -> None:
	for value in ("false", "FALSE"):
		case = tmp_path / value
		case.mkdir()
		repo = _sweep_repo(case)
		result, runtime = _run_prepare_block(repo, {"CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED": value})
		assert result.returncode == 0, result.stdout + result.stderr
		assert "Modify/delete take-base:" not in result.stdout
		assert "AFTER_BLOCK count=1 resolved=false defer=false" in result.stdout
		assert _unmerged(repo) == [WORKFLOWS[2]]
		assert not (runtime / "resolver_dropped_edits.tsv").exists()
	odd = tmp_path / "odd"
	odd.mkdir()
	repo = _sweep_repo(odd)
	result, _ = _run_prepare_block(repo, {"CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED": "maybe"})
	assert result.returncode == 0, result.stdout + result.stderr
	assert "::warning::Modify/delete take-base: CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED has an unrecognised value" in result.stdout
	assert _unmerged(repo) == [WORKFLOWS[2]]


def test_prepare_commit_tail_wiring() -> None:
	text = PREPARE.read_text(encoding="utf-8")
	block_at = text.index("# >>> non-fragment modify/delete take-base")
	assert text.index("# Deterministic resolution for folded changelog / agents fragments.") < block_at
	assert block_at < text.index("# >>> .ai/ validation asset take-base (integration sync)")
	tail = text.index('_det_commit_scope="modify-delete-only"')
	assert text.index('_det_commit_scope=".ai-asset-only"') < tail < text.index("# Criss-cross clean merge (PR #8170)")
	branch = text[text.rindex("if [", 0, tail):tail + 600]
	assert '"${_mdd_defer_commit:-false}" = "true"' in branch
	assert "orchestrator/project-*)" in branch
	assert '_conflict_prepare_commit_deterministic_merge "Modify/delete take-base"' in branch
	# The guard-hook tail leaves a modify/delete resolution to its own tail.
	ghm_at = text.index('if [ "${_ghm_resolved:-false}" = "true" ] && [ "${_resolver_allowlist_count}" -eq 0 ]')
	assert '&& [ "${_mdd_resolved:-false}" != "true" ]' in text[ghm_at:ghm_at + 500]
	# The host-only inline commit posts the modify/delete comment too.
	ho_commit = text.index('-F "body=@${HOST_ONLY_TAKE_BASE_COMMENT_FILE}"')
	assert "_conflict_prepare_post_modify_delete_comment" in text[ho_commit:text.index("exit 0", ho_commit)]
	helper = text[text.index("_conflict_prepare_commit_deterministic_merge()\n{"):]
	helper = helper[:helper.index("\n}\n")]
	assert "_conflict_prepare_post_modify_delete_comment" in helper
	deferred = text[text.index("# Deferred deterministic commit on integration-sync branches."):]
	assert "_conflict_prepare_post_modify_delete_comment" in deferred[:deferred.index("CONFLICT_RESOLVER_SEMBLE_QUERY_FILE")]
	assert 'CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED-' in _prepare_block()
	assert '"${CONFLICT_MODIFY_DELETE_MAX_CHANGED_LINES:-20}"' in _prepare_block()


# --- resolve: stage context, unmerged_left, explicit keep ------------------


def _resolve_src() -> str:
	return RESOLVE.read_text(encoding="utf-8")


def _function(name: str) -> str:
	match = re.search(rf"^{re.escape(name)}\(\)\n\{{\n.*?\n\}}\n", _resolve_src(), re.M | re.S)
	assert match, f"function {name} not found"
	return match.group(0)


_HELPERS = (
	"_resolver_mdd_switch_on",
	"_resolver_mdd_log_path",
	"_resolver_unmerged_left_state",
	"_resolver_modify_delete_context",
	"_capture_unmerged_left_baseline",
	"_verify_no_unmerged_left",
)


def _run_resolver(repo: Path, body: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	runtime = repo.parent / "runtime"
	runtime.mkdir(exist_ok=True)
	functions = "".join(_function(name) for name in _HELPERS)
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime!s}
RESOLVER_TARGETED_PATHS_FILE="${{RUNTIME_DIR}}/targeted.txt"
CONFLICT_RESOLVER_PROMPT_FILE="${{RUNTIME_DIR}}/prompt.txt"
CONFLICT_RESOLVER_SUMMARY_FILE="${{RUNTIME_DIR}}/summary.txt"
RESOLVER_MODIFY_DELETE_PATHS_FILE="${{RUNTIME_DIR}}/resolver_modify_delete_paths.tsv"
RESOLVER_UNMERGED_LEFT_FILE="${{RUNTIME_DIR}}/resolver_unmerged_left.txt"
RESOLVER_UNMERGED_LEFT_BASELINE_FILE="${{RUNTIME_DIR}}/resolver_unmerged_left_baseline.tsv"
RESOLVER_MODIFY_DELETE_KEEP_FILE="${{RUNTIME_DIR}}/resolver_modify_delete_keep.txt"
[ -f "${{RESOLVER_TARGETED_PATHS_FILE}}" ] || git diff --name-only --diff-filter=U | sort -u > "${{RESOLVER_TARGETED_PATHS_FILE}}"
[ -f "${{CONFLICT_RESOLVER_PROMPT_FILE}}" ] || printf 'ORIGINAL PROMPT\\n' > "${{CONFLICT_RESOLVER_PROMPT_FILE}}"
{functions}
{body}
"""
	return subprocess.run(["bash", "-c", script], cwd=repo, env=_env(extra), text=True, capture_output=True)


def test_modify_delete_context_renders_both_directions(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path)
	result = _run_resolver(repo, "_resolver_modify_delete_context\ncat \"${RESOLVER_MODIFY_DELETE_PATHS_FILE}\"\necho ---\ncat \"${CONFLICT_RESOLVER_PROMPT_FILE}\"")
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete context: path={WORKFLOWS[2]} stages=1 2 deleted_by=#123" in result.stdout
	assert f"{WORKFLOWS[2]}\t1 2\n---" in result.stdout
	assert result.stdout.count("ORIGINAL PROMPT") == 1
	assert "Modify/delete conflicts (stage context):" in result.stdout
	assert f"- `{WORKFLOWS[2]}`: deleted on the base branch by #123; modified on this PR." in result.stdout
	assert '"Conflicts resolved:"' in result.stdout
	assert "exception to the rules above about leaving files without conflict markers alone" in result.stdout


def test_modify_delete_context_reverse_direction_uses_commit_sha(tmp_path: Path) -> None:
	if shutil.which("git") is None:
		pytest.skip("git not available")
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_write(repo, {"e.txt": "e base\n", "keep.txt": "k\n"})
	_git(repo, "add", "-A")
	_git(repo, "commit", "-qm", "base")
	_git(repo, "checkout", "-q", "-b", "feat")
	_git(repo, "rm", "-q", "--", "e.txt")
	_git(repo, "commit", "-qm", "drop e")
	_git(repo, "checkout", "-q", "main")
	_write(repo, {"e.txt": "e main\n"})
	_git(repo, "commit", "-qam", "edit e")
	_git(repo, "checkout", "-q", "feat")
	assert _git(repo, "merge", "--no-commit", "--no-ff", "main", check=False).returncode != 0
	sha = _git(repo, "rev-parse", "HEAD").stdout.strip()[:12]
	result = _run_resolver(repo, "_resolver_modify_delete_context\ncat \"${CONFLICT_RESOLVER_PROMPT_FILE}\"")
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"Modify/delete context: path=e.txt stages=1 3 deleted_by={sha}" in result.stdout
	assert f"- `e.txt`: deleted on this PR by {sha}; modified on the base branch." in result.stdout


def test_modify_delete_context_strips_backticks_from_prompt_paths(tmp_path: Path) -> None:
	# PR-controlled paths must not close their inline code span in the prompt.
	odd = "x`y.yml"
	repo = _sweep_repo(tmp_path, extra_deleted=(odd,))
	result = _run_resolver(repo, "_resolver_modify_delete_context\ncat \"${RESOLVER_MODIFY_DELETE_PATHS_FILE}\"\necho ---\ncat \"${CONFLICT_RESOLVER_PROMPT_FILE}\"")
	assert result.returncode == 0, result.stdout + result.stderr
	records, prompt = result.stdout.split("---\n", 1)
	assert f"{odd}\t1 2" in records
	assert "Modify/delete context: path=redacted stages=1 2" in result.stdout
	assert "- `xy.yml`: deleted on the base branch by #123" in prompt
	assert odd not in prompt


def test_modify_delete_context_off_writes_nothing(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path)
	result = _run_resolver(
		repo,
		"_resolver_modify_delete_context\n_capture_unmerged_left_baseline\nwc -c < \"${RESOLVER_MODIFY_DELETE_PATHS_FILE}\"\nwc -c < \"${RESOLVER_UNMERGED_LEFT_BASELINE_FILE}\"\ncat \"${CONFLICT_RESOLVER_PROMPT_FILE}\"\n_verify_no_unmerged_left && echo VERIFY_OK",
		{"CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED": "false", "IS_WORKFLOW_SOURCE_REPO": "true"},
	)
	assert result.returncode == 0, result.stdout + result.stderr
	assert result.stdout == "0\n0\nORIGINAL PROMPT\nVERIFY_OK\n"


def _verify_step(repo: Path, summary: str | None, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
	runtime = repo.parent / "runtime"
	runtime.mkdir(exist_ok=True)
	summary_file = runtime / "summary.txt"
	if summary is None:
		summary_file.unlink(missing_ok=True)
	else:
		summary_file.write_text(summary, encoding="utf-8")
	env = {"IS_WORKFLOW_SOURCE_REPO": "true"}
	env.update(extra or {})
	return _run_resolver(
		repo,
		"if _verify_no_unmerged_left; then echo VERIFY_OK; else echo \"VERIFY_FAIL count=${_resolver_unmerged_left_count}\"; cat \"${RESOLVER_UNMERGED_LEFT_FILE}\"; fi\necho KEEP; cat \"${RESOLVER_MODIFY_DELETE_KEEP_FILE}\"",
		env,
	)


def test_unmerged_left_retries_inside_loop(tmp_path: Path) -> None:
	repo = _sweep_repo(tmp_path)
	swept = repo / WORKFLOWS[2]
	original = swept.read_text(encoding="utf-8")
	setup = _run_resolver(repo, "_resolver_modify_delete_context\n_capture_unmerged_left_baseline", {"IS_WORKFLOW_SOURCE_REPO": "true"})
	assert setup.returncode == 0, setup.stdout + setup.stderr

	# Untouched and undeclared: flagged with its index stages.
	result = _verify_step(repo, None)
	assert result.returncode == 0, result.stdout + result.stderr
	assert f"VERIFY_FAIL count=1\n{WORKFLOWS[2]} (index stages: 1 2)\nKEEP\n" == result.stdout

	# Deleted (accepts the deletion): passes.
	swept.unlink()
	result = _verify_step(repo, "Conflicts resolved:\n- something\n")
	assert result.stdout == "VERIFY_OK\nKEEP\n", result.stdout + result.stderr
	swept.write_text(original, encoding="utf-8")

	# Present, unchanged, listed under another section only: still flagged.
	result = _verify_step(repo, f"Conflicts resolved:\n- notes.txt\n\nNotes:\n- {WORKFLOWS[2]}\n")
	assert "VERIFY_FAIL count=1" in result.stdout, result.stdout + result.stderr

	# A heading with digits ends the section too.
	result = _verify_step(repo, f"Conflicts resolved:\n- notes.txt\n\nStep 2:\n- {WORKFLOWS[2]}\n")
	assert "VERIFY_FAIL count=1" in result.stdout, result.stdout + result.stderr

	# A non-matching declared path is ignored.
	result = _verify_step(repo, f"Conflicts resolved:\n- {WORKFLOWS[2][:-1]}\n- ./{WORKFLOWS[2]}\n")
	assert "VERIFY_FAIL count=1" in result.stdout, result.stdout + result.stderr

	# Declared keep (back-ticked): passes and lands in the keep file.
	result = _verify_step(repo, f"Conflicts resolved:\n- `{WORKFLOWS[2]}`\n\nSelf-check:\n- ok\n")
	assert result.stdout.endswith(f"VERIFY_OK\nKEEP\n{WORKFLOWS[2]}\n"), result.stdout + result.stderr
	assert f"Modify/delete context: path={WORKFLOWS[2]} stages=1 2 outcome=keep_declared" in result.stdout
	result = _verify_step(repo, f"Conflicts resolved:\n- {WORKFLOWS[2]}\n")
	assert result.stdout.endswith(f"VERIFY_OK\nKEEP\n{WORKFLOWS[2]}\n"), result.stdout + result.stderr

	# Edited: no longer unchanged, passes without a declaration.
	swept.write_text(original + "# edited\n", encoding="utf-8")
	result = _verify_step(repo, None)
	assert result.stdout == "VERIFY_OK\nKEEP\n", result.stdout + result.stderr
	swept.write_text(original, encoding="utf-8")

	# Consumer repos stage with git add -u: the check does not run there.
	result = _verify_step(repo, None, {"IS_WORKFLOW_SOURCE_REPO": "false"})
	assert result.stdout == "VERIFY_OK\nKEEP\n", result.stdout + result.stderr

	# Loop wiring: checked in the soft-validation success branch, before the
	# provenance check, retrying as unmerged_left and failing as scope.
	src = _resolve_src()
	verify_at = src.index("    if ! _verify_no_unmerged_left; then")
	assert verify_at < src.index("    if ! _verify_resolution_provenance; then")
	branch = src[verify_at:src.index("    if ! _verify_resolution_provenance; then")]
	assert '_prev_attempt_failure_kind="unmerged_left"' in branch
	assert 'RESOLVER_TERMINAL_EXIT_CLASS="scope"' in branch
	assert "::error::Conflict resolver left conflicted path(s) untouched" in branch
	assert "continue" in branch
	baseline_at = src.index("_capture_unmerged_left_baseline || true\n")
	assert src.index("_capture_resolution_provenance_stages || true\n") < baseline_at
	assert src.index("_resolver_modify_delete_context || true\n") < baseline_at


def test_keep_file_joins_touched_set_after_loop() -> None:
	src = _resolve_src()
	sort_at = src.index('    sort -u -o "${RESOLVER_TOUCHED_FILE}" "${RESOLVER_TOUCHED_FILE}"\n')
	keep_at = src.index('    if [ -s "${RESOLVER_MODIFY_DELETE_KEEP_FILE:-}" ]; then', sort_at)
	assert keep_at < src.index("touched_count=", sort_at)
	assert '"${RUNTIME_DIR}/modify_delete_take_base_comment.md"; do' in src


def _build_retry_prompt_source() -> str:
	src = _resolve_src()
	start = src.index("_build_retry_prompt() {")
	return src[start:src.index("\n}\n", start) + 2]


def test_retry_prompt_names_unmerged_paths(tmp_path: Path) -> None:
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	(runtime / "prompt.txt").write_text("ORIGINAL PROMPT\n", encoding="utf-8")
	(runtime / "left.txt").write_text(f"{WORKFLOWS[2]} (index stages: 1 2)\n", encoding="utf-8")
	script = f"""set -euo pipefail
RUNTIME_DIR={runtime!s}
CONFLICT_RESOLVER_PROMPT_FILE="${{RUNTIME_DIR}}/prompt.txt"
RESOLVER_RETRY_PROMPT_FILE="${{RUNTIME_DIR}}/retry.txt"
RESOLVER_UNMERGED_LEFT_FILE="${{RUNTIME_DIR}}/left.txt"
{_build_retry_prompt_source()}
_build_retry_prompt 1 /dev/null /dev/null unmerged_left
echo "OUTCOME=${{_retry_prompt_outcome}}"
cat "${{RESOLVER_RETRY_PROMPT_FILE}}"
"""
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=_env(), text=True, capture_output=True)
	assert result.returncode == 0, result.stdout + result.stderr
	assert result.stdout.startswith("OUTCOME=unmerged_left-prelude\n")
	assert f"{WORKFLOWS[2]} (index stages: 1 2)" in result.stdout
	assert "BEGIN UNTRUSTED UNMERGED PATH LIST" in result.stdout
	assert '"Conflicts resolved:"' in result.stdout
	assert "editing or deleting them does not abort the run" in result.stdout
	assert result.stdout.rstrip().endswith("=== ORIGINAL TASK ===\nORIGINAL PROMPT")


# --- static wiring ---------------------------------------------------------


def test_switch_defaults_and_forwarding() -> None:
	src = _resolve_src()
	assert 'case "$(printf \'%s\' "${CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED-}" | tr \'[:upper:]\' \'[:lower:]\')" in' in src
	for name in (
		"RESOLVER_MODIFY_DELETE_PATHS_FILE",
		"RESOLVER_UNMERGED_LEFT_FILE",
		"RESOLVER_UNMERGED_LEFT_BASELINE_FILE",
		"RESOLVER_MODIFY_DELETE_KEEP_FILE",
	):
		assert f'{name}="${{RUNTIME_DIR}}/' in src
	workflow = REVIEW_WORKFLOW.read_text(encoding="utf-8")
	forward = "CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED: ${{ vars.CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED || 'true' }}"
	assert workflow.count(forward) == 2
	prepare_step = workflow[workflow.index("bash \"${SUPPORT_SCRIPTS_DIR}/review_conflict_prepare.sh\"") - 4000:]
	assert forward in prepare_step[:4000]
	resolver_step = workflow[workflow.index("- name: Run Codex resolver, validate, stage, commit"):]
	assert forward in resolver_step[:resolver_step.index("run:")]
	assert "pytest:tests/test_review_conflict_modify_delete.py" in CI_WORKFLOW.read_text(encoding="utf-8")
	readme = README.read_text(encoding="utf-8")
	table = readme[readme.index("<!-- anchor:required-variables-table -->"):]
	assert "| `CONFLICT_MODIFY_DELETE_TAKE_BASE_ENABLED` | `true` |" in table
	fragment = AGENTS_FRAGMENT.read_text(encoding="utf-8")
	assert "LOG_PREFIX.name=Modify/delete take-base:" in fragment
	assert "LOG_PREFIX.name=Modify/delete context:" in fragment
	# The keep/delete rule lives only in the stage-context block, so the switch
	# set to false leaves the static prompt as it was.
	prompt = PROMPT.read_text(encoding="utf-8")
	assert "modify/delete conflict" not in prompt
	assert "stage-context block" not in prompt
