#!/usr/bin/env python3
"""Sync-quick final CI and the early final-CI dispatch at a freshness update.

Merge-base sync churn (2026-11-17, operator decision Q3: B): an approved PR
that the freshness gate updates from its base gets its final CI run dispatched
by the same merge path, right after the update commit is verified
(`_pr_base_sync_dispatch_final_ci` in scripts/pr_checks_lib.sh). When the base
overlapped the PR only in CI-wiring files and the approved head already passed
a full final run, that run is a sync-quick one: ci.yml skips the seven gated
test jobs and `tests-sync-quick` re-checks the claim (scripts/ci_sync_quick.sh
verify) and runs the PR's own test files.

Covered here:
  * scripts/ci_sync_quick.sh classify / full-ci-green / verify / run-tests,
    verify and run-tests against real git merges in a temporary repository;
  * `_pr_base_sync_dispatch_final_ci` with stubbed API transports;
  * the sync_quick input of `_pr_ensure_final_ci`;
  * ci.yml: the input, run name, job gates and the `lint` aggregate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_sync_quick.sh"
LIB = ROOT / "scripts" / "pr_checks_lib.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

GATED_JOBS = (
	"tests-hooks-and-orchestrator",
	"tests-heal-plan-and-validation",
	"tests-promote-stall-and-review",
	"tests-release-and-log-analysis",
	"orchestrate-poll",
	"tests-system-python",
	"tests-sandbox-python",
)
SYNC_JOB = "tests-sync-quick"
APPROVED = "a" * 40
UPDATE = "e" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not available")


def _clean_env(**extra: str) -> dict[str, str]:
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": os.environ.get("HOME", "/tmp"),
		"PYTHONDONTWRITEBYTECODE": "1",
		"GIT_CONFIG_NOSYSTEM": "1",
		"GIT_AUTHOR_NAME": "t",
		"GIT_AUTHOR_EMAIL": "t@example.invalid",
		"GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@example.invalid",
	}
	env.update(extra)
	return env


def _classify(paths: str) -> str:
	result = subprocess.run(["bash", str(SCRIPT), "classify"], input=paths, capture_output=True, text=True, env=_clean_env(), check=True)
	return result.stdout.strip()


# --- classify ---------------------------------------------------------------


@pytest.mark.parametrize("paths", [
	".github/workflows/ci.yml\n",
	".github/workflows/ci.yml\ntests/test_ci_poll_test_sharding.py\n",
	"tests/test_ci_wires_every_test_file.py",
	".github/workflows/ci.yml\0tests/test_ci_job_split_contract.py\0",
])
def test_ci_wiring_paths_classify_as_wiring_only(paths: str) -> None:
	assert _classify(paths) == "ci_wiring_only"


@pytest.mark.parametrize("paths, first", [
	(".github/workflows/ci.yml\nscripts/gh_helpers.sh\n", "scripts/gh_helpers.sh"),
	("README.md", "README.md"),
	(".github/workflows/ci.yml.bak", ".github/workflows/ci.yml.bak"),
	("(base diff lists 300 files; extent unknown)", "\\(base\\ diff\\ lists\\ 300\\ files\\;\\ extent\\ unknown\\)"),
])
def test_anything_else_is_other(paths: str, first: str) -> None:
	assert _classify(paths) == f"other {first}"


def test_no_paths_is_other() -> None:
	assert _classify("") == "other empty"


# --- full-ci-green (fake gh) -----------------------------------------------

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
	log.write(json.dumps(args) + "\n")
if args[:2] == ["workflow", "run"]:
	if "sync_quick=true" in args and os.environ.get("FAKE_GH_REFUSE_SYNC_QUICK") == "true":
		sys.exit(1)
	sys.exit(0)
if args[:1] == ["api"] and "/actions/workflows/ci.yml/runs?head_sha=" in args[1]:
	if os.environ.get("FAKE_GH_RUNS_FAIL") == "true":
		sys.exit(1)
	print(os.environ.get("FAKE_GH_RUNS", '{"workflow_runs": []}'))
	sys.exit(0)
sys.exit(1)
'''


def _fake_gh(tmp_path: Path) -> tuple[Path, Path]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	log = tmp_path / "gh.log"
	log.write_text("", encoding="utf-8")
	return bin_dir, log


def _run_entry(sha: str, title: str = "CI [pr:7]", conclusion: str = "success", event: str = "workflow_dispatch",
		path: str = ".github/workflows/ci.yml@refs/heads/ai/issue-7", status: str = "completed") -> dict:
	return {"head_sha": sha, "display_title": title, "conclusion": conclusion, "event": event, "path": path, "status": status}


def _green(tmp_path: Path, runs: list[dict] | None, sha: str = APPROVED, fail: bool = False) -> subprocess.CompletedProcess:
	bin_dir, log = _fake_gh(tmp_path)
	env = _clean_env(PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", FAKE_GH_LOG=str(log),
		FAKE_GH_RUNS=json.dumps({"workflow_runs": runs or []}), FAKE_GH_RUNS_FAIL="true" if fail else "false")
	return subprocess.run(["bash", str(SCRIPT), "full-ci-green", "o/r", "7", sha], capture_output=True, text=True, env=env, check=False)


def test_a_successful_full_final_run_is_green(tmp_path: Path) -> None:
	result = _green(tmp_path, [_run_entry(APPROVED)])
	assert result.returncode == 0, result.stdout
	assert "outcome=pass" in result.stdout


@pytest.mark.parametrize("entry", [
	_run_entry(APPROVED, title="CI [pr:7] [sync-quick]"),
	_run_entry(APPROVED, title="CI [pr:8]"),
	_run_entry(APPROVED, conclusion="failure"),
	_run_entry(APPROVED, conclusion="cancelled"),
	_run_entry(APPROVED, status="in_progress", conclusion=""),
	_run_entry(APPROVED, event="push"),
	_run_entry(APPROVED, path=".github/workflows/other.yml"),
	_run_entry("b" * 40),
])
def test_anything_but_a_full_green_final_run_is_not_green(tmp_path: Path, entry: dict) -> None:
	result = _green(tmp_path, [entry])
	assert result.returncode == 1
	assert "reason=no_full_green_run" in result.stdout


def test_a_failed_read_is_not_green(tmp_path: Path) -> None:
	result = _green(tmp_path, [_run_entry(APPROVED)], fail=True)
	assert result.returncode == 1 and "reason=read_failed" in result.stdout


def test_invalid_input_is_not_green(tmp_path: Path) -> None:
	result = _green(tmp_path, [_run_entry("xyz")], sha="xyz")
	assert result.returncode == 1 and "reason=invalid_input" in result.stdout


# --- verify / run-tests on real git merges ---------------------------------


def _git(repo: Path, *args: str) -> str:
	return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=_clean_env(), check=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
	path = repo / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


PASSING_TEST = "def test_ok():\n\tassert True\n"
FAILING_TEST = "def test_bad():\n\tassert False\n"


def _build(tmp_path: Path, *, base_touches: dict[str, str], pr_touches: dict[str, str], extra_base_after: dict[str, str] | None = None) -> tuple[Path, str]:
	"""main -> approved PR head -> update merge [approved, base tip] -> PR merge ref [newer base, update]."""
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_write(repo, ".github/workflows/ci.yml", "jobs: {}\n# a\n\n\n\n\n\n\n\n\n# z\n")
	_write(repo, "scripts/tool.sh", "echo one\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	_git(repo, "checkout", "-q", "-b", "pr")
	for rel, text in pr_touches.items():
		_write(repo, rel, text)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "pr change")
	_git(repo, "checkout", "-q", "main")
	for rel, text in base_touches.items():
		_write(repo, rel, text)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base moved")
	_git(repo, "checkout", "-q", "pr")
	_git(repo, "merge", "-q", "--no-ff", "--no-edit", "main")
	update = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "-q", "main")
	for rel, text in (extra_base_after or {"NOTES.md": "later\n"}).items():
		_write(repo, rel, text)
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base moved again")
	_git(repo, "merge", "-q", "--no-ff", "--no-edit", update)
	return repo, update


def _verify(tmp_path: Path, repo: Path, head: str, *, green: bool = True, docs_only: str = "false") -> subprocess.CompletedProcess:
	bin_dir, log = _fake_gh(tmp_path)
	parent = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{head}^1"], capture_output=True, text=True, env=_clean_env(), check=False)
	approved = parent.stdout.strip() if parent.returncode == 0 else APPROVED
	runs = [_run_entry(approved)] if green else []
	env = _clean_env(PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", FAKE_GH_LOG=str(log), FAKE_GH_RUNS=json.dumps({"workflow_runs": runs}),
		CI_SYNC_QUICK_HEAD_SHA=head, CI_SYNC_QUICK_REPOSITORY="o/r", CI_SYNC_QUICK_PR_NUMBER="7", CI_SYNC_QUICK_DOCS_ONLY=docs_only)
	return subprocess.run(["bash", str(SCRIPT), "verify"], cwd=repo, capture_output=True, text=True, env=env, check=False)


def _ci_yml(edit: str) -> str:
	lines = "jobs: {}\n# a\n\n\n\n\n\n\n\n\n# z\n".splitlines(keepends=True)
	if edit == "top":
		lines[1] = "# a changed by base\n"
	else:
		lines[-1] = "# z changed by pr\n"
	return "".join(lines)


def test_verify_passes_when_only_ci_yml_overlaps(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top")},
		pr_touches={".github/workflows/ci.yml": _ci_yml("bottom"), "scripts/tool.sh": "echo two\n", "tests/test_new.py": PASSING_TEST})
	result = _verify(tmp_path, repo, update)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=pass reason=ci_wiring_overlap" in result.stdout
	assert "overlap=.github/workflows/ci.yml" in result.stdout


def test_verify_passes_with_no_overlap_at_all(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={"docs/x.md": "x\n"}, pr_touches={"scripts/tool.sh": "echo two\n"})
	assert _verify(tmp_path, repo, update).returncode == 0


def test_verify_fails_when_a_code_file_overlaps(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top"), "scripts/tool.sh": "echo one\n\necho base\n"},
		pr_touches={".github/workflows/ci.yml": _ci_yml("bottom"), "scripts/tool.sh": "echo zero\necho one\n"})
	result = _verify(tmp_path, repo, update)
	assert result.returncode == 1
	assert "reason=overlap_not_ci_wiring first=scripts/tool.sh" in result.stdout


def test_verify_fails_when_the_approved_head_had_no_full_green_run(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top")}, pr_touches={".github/workflows/ci.yml": _ci_yml("bottom")})
	result = _verify(tmp_path, repo, update, green=False)
	assert result.returncode == 1 and "reason=approved_head_not_green" in result.stdout


def test_verify_fails_when_the_run_head_is_not_the_merge_refs_head(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top")}, pr_touches={".github/workflows/ci.yml": _ci_yml("bottom")})
	result = _verify(tmp_path, repo, "f" * 40)
	assert result.returncode == 1 and "reason=head_moved" in result.stdout


def test_verify_fails_when_the_update_head_is_not_a_merge(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_write(repo, "a.txt", "1\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	_git(repo, "checkout", "-q", "-b", "pr")
	_write(repo, "a.txt", "2\n")
	_git(repo, "commit", "-q", "-am", "pr")
	head = _git(repo, "rev-parse", "HEAD")
	_git(repo, "checkout", "-q", "main")
	_write(repo, "b.txt", "1\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base 2")
	_git(repo, "merge", "-q", "--no-ff", "--no-edit", head)
	result = _verify(tmp_path, repo, head)
	assert result.returncode == 1 and "reason=update_not_a_merge" in result.stdout


def test_verify_fails_on_a_non_merge_checkout(tmp_path: Path) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	_git(repo, "init", "-q", "-b", "main")
	_write(repo, "a.txt", "1\n")
	_git(repo, "add", "-A")
	_git(repo, "commit", "-q", "-m", "base")
	result = _verify(tmp_path, repo, _git(repo, "rev-parse", "HEAD"))
	assert result.returncode == 1 and "reason=not_a_merge" in result.stdout


def test_verify_refuses_mixed_modes(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top")}, pr_touches={".github/workflows/ci.yml": _ci_yml("bottom")})
	result = _verify(tmp_path, repo, update, docs_only="true")
	assert result.returncode == 1 and "reason=mixed_modes" in result.stdout


def test_verify_fails_on_an_unusable_head_sha(tmp_path: Path) -> None:
	repo, _update = _build(tmp_path, base_touches={"docs/x.md": "x\n"}, pr_touches={"scripts/tool.sh": "echo two\n"})
	result = _verify(tmp_path, repo, "not-a-sha")
	assert result.returncode == 1 and "reason=head_sha_invalid" in result.stdout


def _run_tests(repo: Path, head: str) -> subprocess.CompletedProcess:
	return subprocess.run(["bash", str(SCRIPT), "run-tests"], cwd=repo, capture_output=True, text=True,
		env=_clean_env(CI_SYNC_QUICK_HEAD_SHA=head), check=False)


def test_run_tests_runs_the_prs_own_test_files(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top")},
		pr_touches={"tests/test_new.py": PASSING_TEST, "tests/test_shell.sh": "#!/usr/bin/env bash\necho shell-test-ran\n"})
	result = _run_tests(repo, update)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "python=1 shell=1" in result.stdout
	assert "shell-test-ran" in result.stdout
	assert "outcome=pass" in result.stdout


def test_run_tests_fails_when_a_pr_test_fails(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={".github/workflows/ci.yml": _ci_yml("top")}, pr_touches={"tests/test_new.py": FAILING_TEST})
	result = _run_tests(repo, update)
	assert result.returncode == 1 and "reason=tests_failed" in result.stdout


def test_run_tests_includes_the_ci_structure_tests_from_the_tree(tmp_path: Path) -> None:
	repo, update = _build(tmp_path, base_touches={"tests/test_ci_job_split_contract.py": PASSING_TEST},
		pr_touches={"scripts/tool.sh": "echo two\n"})
	result = _run_tests(repo, update)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "python=1 shell=0" in result.stdout


# --- _pr_base_sync_dispatch_final_ci ---------------------------------------


def _dispatch(tmp_path: Path, *, overlap: str = "agents.md", head_repo: str = "owner/repo", head_ref: str = "ai/issue-7",
		mergeable: str = "true", polls: list[dict] | None = None, green: bool = False, env: dict[str, str] | None = None,
		pr_files: list[str] | None = None) -> tuple[subprocess.CompletedProcess, list[list[str]], list[str]]:
	bin_dir, log = _fake_gh(tmp_path)
	poll_file = tmp_path / "polls.json"
	poll_file.write_text(json.dumps(polls or []), encoding="utf-8")
	call_log = tmp_path / "api.log"
	call_log.write_text("", encoding="utf-8")
	files_json = json.dumps([{"filename": name} for name in (pr_files or ["scripts/tool.sh"])])
	body = f'''
set -uo pipefail
sleep() {{ :; }}
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{
  printf '%s\\n' "$*" >> "{call_log}"
  case "$*" in
    *"/actions/workflows/ci.yml/runs?head_sha="*) printf '%s' '{{"workflow_runs": []}}' ;;
    *"/pulls/7/files"*) printf '%s' {files_json!r} ;;
    *"repos/owner/repo/pulls/7")
      python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); print(json.dumps(p.pop(0)) if p else "{{}}"); json.dump(p, open(sys.argv[1], "w"))' "{poll_file}" ;;
    *) printf '%s' '{{}}' ;;
  esac
}}
source {str(LIB)!r}
PR_BASE_SYNC_HEAD_REF={head_ref!r}
PR_BASE_SYNC_HEAD_REPO={head_repo!r}
PR_BASE_SYNC_MERGEABLE={mergeable!r}
PR_BASE_FRESHNESS_OVERLAP={overlap!r}
_pr_base_sync_dispatch_final_ci 7 {APPROVED} {UPDATE}
echo "rc=$?"
'''
	run_env = _clean_env(
		PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
		FAKE_GH_LOG=str(log),
		FAKE_GH_RUNS=json.dumps({"workflow_runs": [_run_entry(APPROVED)] if green else []}),
		PR_CHECKS_REPOSITORY="owner/repo",
		REVIEW_FINAL_CI_WORKFLOW="ci.yml",
		MERGE_BASE_SYNC_HEAD_POLL_SECONDS="1",
	)
	run_env.update(env or {})
	for name in [key for key, value in run_env.items() if value == "__unset__"]:
		run_env.pop(name)
	result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=run_env, check=False, timeout=60)
	gh_calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
	return result, gh_calls, call_log.read_text(encoding="utf-8").splitlines()


def _dispatches(gh_calls: list[list[str]]) -> list[list[str]]:
	return [call for call in gh_calls if call[:2] == ["workflow", "run"]]


def test_a_verified_update_gets_its_full_final_ci_run_right_away(tmp_path: Path) -> None:
	result, gh_calls, _ = _dispatch(tmp_path)
	assert "rc=0" in result.stdout
	runs = _dispatches(gh_calls)
	assert len(runs) == 1, result.stdout + result.stderr
	assert runs[0][:6] == ["workflow", "run", "ci.yml", "--repo", "owner/repo", "--ref"]
	assert f"head_sha={UPDATE}" in runs[0] and "pr_number=7" in runs[0]
	assert not any("sync_quick" in arg for arg in runs[0])
	assert f"MERGE_BASE_SYNC_FINAL_CI pr=7 head={UPDATE} outcome=dispatched reason=overlap_other sync_quick=false" in result.stdout


def test_ci_wiring_overlap_with_a_green_approved_head_dispatches_sync_quick(tmp_path: Path) -> None:
	result, gh_calls, _ = _dispatch(tmp_path, overlap=".github/workflows/ci.yml,tests/test_ci_poll_test_sharding.py", green=True)
	runs = _dispatches(gh_calls)
	assert len(runs) == 1 and "sync_quick=true" in runs[0], result.stdout + result.stderr
	assert "REVIEW_FINAL_CI_SYNC_QUICK pr=7 head=" in result.stdout
	assert "reason=ci_wiring_only sync_quick=true" in result.stdout


@pytest.mark.parametrize("overlap, green, env", [
	(".github/workflows/ci.yml", False, {}),
	(".github/workflows/ci.yml,scripts/gh_helpers.sh", True, {}),
	(".github/workflows/ci.yml", True, {"MERGE_BASE_SYNC_QUICK_CI_ENABLED": "false"}),
	("", True, {}),
])
def test_anything_else_dispatches_the_full_suite(tmp_path: Path, overlap: str, green: bool, env: dict) -> None:
	result, gh_calls, _ = _dispatch(tmp_path, overlap=overlap, green=green, env=env)
	runs = _dispatches(gh_calls)
	assert len(runs) == 1, result.stdout + result.stderr
	assert not any("sync_quick" in arg for arg in runs[0])


def test_a_refused_sync_quick_dispatch_falls_back_to_the_full_suite(tmp_path: Path) -> None:
	result, gh_calls, _ = _dispatch(tmp_path, overlap=".github/workflows/ci.yml", green=True, env={"FAKE_GH_REFUSE_SYNC_QUICK": "true"})
	runs = _dispatches(gh_calls)
	assert len(runs) == 2
	assert "sync_quick=true" in runs[0] and not any("sync_quick" in arg for arg in runs[1])
	assert "outcome=dispatch_refused" in result.stdout + result.stderr


def test_a_docs_only_pr_keeps_its_docs_only_run(tmp_path: Path) -> None:
	result, gh_calls, _ = _dispatch(tmp_path, overlap=".github/workflows/ci.yml", green=True, pr_files=["docs/plans/x.md"])
	runs = _dispatches(gh_calls)
	assert len(runs) == 1 and "docs_only=true" in runs[0] and not any("sync_quick" in arg for arg in runs[0])


def test_the_merge_ref_is_awaited_before_dispatching(tmp_path: Path) -> None:
	polls = [{"head": {"sha": UPDATE}, "mergeable": None}, {"head": {"sha": UPDATE}, "mergeable": True}]
	result, gh_calls, api_calls = _dispatch(tmp_path, mergeable="null", polls=polls)
	assert len([c for c in api_calls if c.endswith("repos/owner/repo/pulls/7")]) == 2
	assert len(_dispatches(gh_calls)) == 1, result.stdout + result.stderr


def test_reason_names_why_the_quick_mode_was_refused(tmp_path: Path) -> None:
	result, _, _ = _dispatch(tmp_path, overlap=".github/workflows/ci.yml", green=False)
	assert "outcome=dispatched reason=approved_head_not_full_green sync_quick=false" in result.stdout
	result, _, _ = _dispatch(tmp_path, overlap="", green=True)
	assert "outcome=dispatched reason=full_suite sync_quick=false" in result.stdout


def test_a_failed_poll_read_keeps_polling_instead_of_reporting_a_head_move(tmp_path: Path) -> None:
	polls = [{}, {"head": {"sha": UPDATE}, "mergeable": True}]
	result, gh_calls, _ = _dispatch(tmp_path, mergeable="null", polls=polls)
	assert "head_moved" not in result.stdout
	assert len(_dispatches(gh_calls)) == 1, result.stdout + result.stderr


@pytest.mark.parametrize("mergeable, polls, reason", [
	("null", [{"head": {"sha": UPDATE}, "mergeable": None}] * 3, "merge_ref_null"),
	("false", [], "merge_ref_false"),
	("null", [{"head": {"sha": "f" * 40}, "mergeable": True}], "head_moved"),
])
def test_no_dispatch_without_a_merge_ref_for_this_head(tmp_path: Path, mergeable: str, polls: list, reason: str) -> None:
	result, gh_calls, _ = _dispatch(tmp_path, mergeable=mergeable, polls=polls)
	assert not _dispatches(gh_calls)
	assert f"outcome=skipped reason={reason}" in result.stdout


@pytest.mark.parametrize("kwargs, env, reason", [
	({}, {"REVIEW_FINAL_CI_WORKFLOW": "__unset__"}, "workflow_unset"),
	({}, {"MERGE_BASE_SYNC_FINAL_CI_ENABLED": "false"}, "disabled"),
	({"head_repo": "fork/repo"}, {}, "head_branch_unusable"),
	({"head_ref": "-x"}, {}, "head_branch_unusable"),
	({"head_ref": ""}, {}, "head_branch_unusable"),
])
def test_skips_leave_the_dispatch_to_the_checks_wait(tmp_path: Path, kwargs: dict, env: dict, reason: str) -> None:
	result, gh_calls, api_calls = _dispatch(tmp_path, env=env, **kwargs)
	assert not _dispatches(gh_calls) and not api_calls
	assert f"outcome=skipped reason={reason}" in result.stdout


def test_the_merge_path_dispatches_after_a_verified_update() -> None:
	text = LIB.read_text(encoding="utf-8")
	marker_post = text.index('_pr_base_sync_marker_post "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}"')
	dispatch = text.index('_pr_base_sync_dispatch_final_ci "${pr_number}" "${PR_BASE_FRESHNESS_HEAD_SHA}" "${PR_BASE_SYNC_MERGE_SHA}"')
	verified = text.index('if _pr_base_sync_observe_update_commit "${pr_number}"')
	assert verified < marker_post < dispatch
	# Only the verified branch dispatches: the else branch logs the skipped marker.
	assert text.index("outcome=skipped reason=${PR_BASE_SYNC_OBSERVE_REASON:-head_not_observed}") > dispatch


# --- ci.yml ------------------------------------------------------------------


def _ci() -> dict:
	# PyYAML is installed in CI; only the ci.yml tests need it, so a host or
	# container without it still runs the classify / verify / dispatch tests above.
	yaml = pytest.importorskip("yaml")
	return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
	return workflow.get("on") or workflow.get(True)


def test_dispatch_input_defaults_to_the_full_suite() -> None:
	sync_input = _on(_ci())["workflow_dispatch"]["inputs"]["sync_quick"]
	assert sync_input["default"] == "false" and sync_input["required"] is False and sync_input["type"] == "string"


def test_run_name_marks_sync_quick_runs_only() -> None:
	run_name = _ci()["run-name"]
	assert "format('CI [pr:{0}]{1}', inputs.pr_number, inputs.sync_quick == 'true' && ' [sync-quick]' || '')" in run_name


def test_the_gated_jobs_skip_in_sync_quick_runs() -> None:
	jobs = _ci()["jobs"]
	gated = sorted(job_id for job_id, job in jobs.items() if "inputs.sync_quick != 'true'" in str(job.get("if", "")))
	assert gated == sorted(GATED_JOBS)


def test_sync_job_runs_only_in_sync_quick_dispatches_and_verifies_first() -> None:
	job = _ci()["jobs"][SYNC_JOB]
	assert job["if"] == "inputs.mode != 'quick' && github.event_name == 'workflow_dispatch' && inputs.sync_quick == 'true'"
	assert job["permissions"] == {"contents": "read", "actions": "read"}
	steps = job["steps"]
	assert steps[0]["with"]["ref"] == "${{ github.event_name == 'workflow_dispatch' && format('refs/pull/{0}/merge', inputs.pr_number) || '' }}"
	assert steps[0]["with"]["fetch-depth"] == 3
	assert steps[0]["with"]["persist-credentials"] is False
	runs = [step.get("run", "") for step in steps]
	verify_at = runs.index("bash scripts/ci_sync_quick.sh verify")
	assert runs.index("bash scripts/ci_sync_quick.sh run-tests") > verify_at
	verify_env = steps[verify_at]["env"]
	assert verify_env["CI_SYNC_QUICK_HEAD_SHA"] == "${{ github.sha }}"
	assert verify_env["CI_SYNC_QUICK_PR_NUMBER"] == "${{ inputs.pr_number }}"
	assert verify_env["CI_SYNC_QUICK_DOCS_ONLY"] == "${{ inputs.docs_only }}"


def test_lint_needs_the_sync_job() -> None:
	assert SYNC_JOB in _ci()["jobs"]["lint"]["needs"]


def _aggregate(results: dict, requested: str, docs_verified: str = "false") -> subprocess.CompletedProcess:
	step = next(step for step in _ci()["jobs"]["lint"]["steps"] if step.get("name") == "Require every CI job to succeed")
	env = _clean_env(
		CI_NEEDS_JSON=json.dumps({job_id: {"result": result, "outputs": {}} for job_id, result in results.items()}),
		CI_DOCS_ONLY_VERIFIED=docs_verified,
		CI_DOCS_ONLY_GATED_JOBS=step["env"]["CI_DOCS_ONLY_GATED_JOBS"],
		CI_SYNC_QUICK_REQUESTED=requested,
		CI_SYNC_QUICK_JOB=step["env"]["CI_SYNC_QUICK_JOB"],
	)
	return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, check=False)


def _results(gated: str, sync: str, static: str = "success") -> dict:
	return {"static-checks": static, SYNC_JOB: sync, **{job_id: gated for job_id in GATED_JOBS}}


@pytest.mark.parametrize("requested, gated, sync, static, passes", [
	# Sync-quick run: the gated skips count only after tests-sync-quick passed.
	("true", "skipped", "success", "success", True),
	("true", "skipped", "failure", "success", False),
	("true", "skipped", "skipped", "success", False),
	("true", "skipped", "cancelled", "success", False),
	("true", "skipped", "success", "failure", False),
	("true", "failure", "success", "success", False),
	# Every other run: tests-sync-quick's skip is fine, the gated jobs must pass.
	("false", "success", "skipped", "success", True),
	("false", "skipped", "skipped", "success", False),
	("false", "success", "failure", "success", False),
	("", "success", "skipped", "success", True),
	("", "skipped", "success", "success", False),
])
def test_aggregate_matrix(requested: str, gated: str, sync: str, static: str, passes: bool) -> None:
	result = _aggregate(_results(gated, sync, static), requested)
	assert (result.returncode == 0) is passes, result.stdout + result.stderr


def test_the_sync_job_missing_from_needs_fails_a_sync_quick_run() -> None:
	results = _results("skipped", "success")
	del results[SYNC_JOB]
	assert _aggregate(results, "true").returncode == 1


def test_a_failed_sync_quick_run_tells_final_ci_failed_to_run_the_full_suite() -> None:
	steps = _ci()["jobs"]["final-ci-failed"]["steps"]
	step = next(s for s in steps if s.get("run") == "bash scripts/review_final_ci_failed.sh")
	assert step["env"]["FINAL_CI_FAILED_SYNC_QUICK"] == "${{ inputs.sync_quick == 'true' }}"
