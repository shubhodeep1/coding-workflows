"""Delta final CI after a clean merge-base update (merge-lease short critical
section, Phase 2, decision D1 of docs/plans/merge-lease-short-critical-section-plan.md).

When the base and the PR both changed a file that is not CI wiring, the
update head still gets the `[sync-quick]` final CI run when
MERGE_BASE_SYNC_DELTA_CI_ENABLED is on, the approved head passed a full final
run and the pre-push selector picks at most MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES
test files for the overlap. Covered here:
  * scripts/ci_sync_quick.sh delta-supported / verify / run-tests on real git
    merges in a temporary repository (the real selector,
    workflow-templates/.claude/hooks/prepush_affected_tests.py);
  * `_pr_base_sync_dispatch_final_ci`'s delta arm (probe, switch, full-ci-green);
  * the tests-sync-quick job's env lines and timeout in ci.yml.

The helpers come from tests/test_ci_sync_quick.py, which pins the pre-delta
verdicts with the switch off.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("ci_sync_quick_base_helpers", Path(__file__).with_name("test_ci_sync_quick.py"))
sq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sq)

ROOT = sq.ROOT
SCRIPT = sq.SCRIPT
LIB = sq.LIB

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not available")

# Each test appends its name to $DELTA_MARKER, so a run can count executions.
def _marker_test(name: str, mention: str = "") -> str:
	return (f"# {mention}\n" if mention else "") + (
		"import os\n\n\n"
		f"def test_{name}_marker():\n"
		"\twith open(os.environ['DELTA_MARKER'], 'a', encoding='utf-8') as handle:\n"
		f"\t\thandle.write('{name}\\n')\n"
	)


BASE_TOOL = "echo one\n\necho base\n"
PR_TOOL = "echo zero\necho one\n"


def _code_overlap(tmp_path: Path, *, pr_extra: dict[str, str] | None = None) -> tuple[Path, str]:
	"""scripts/tool.sh changed on both sides; the base adds two tests the selector picks for it."""
	base = {
		"scripts/tool.sh": BASE_TOOL,
		# Picked by naming (test_<stem>) and by a text reference.
		"tests/test_tool.py": _marker_test("tool"),
		"tests/test_tool_more.py": _marker_test("more", "covers scripts/tool.sh"),
	}
	pr = {"scripts/tool.sh": PR_TOOL, **(pr_extra or {})}
	return sq._build(tmp_path, base_touches=base, pr_touches=pr)


def _verify(tmp_path: Path, repo: Path, head: str, *, green: bool = True, script: Path = SCRIPT, **env: str) -> subprocess.CompletedProcess:
	bin_dir, log = sq._fake_gh(tmp_path)
	parent = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{head}^1"], capture_output=True, text=True, env=sq._clean_env(), check=False)
	approved = parent.stdout.strip() if parent.returncode == 0 else sq.APPROVED
	runs = [sq._run_entry(approved)] if green else []
	run_env = sq._clean_env(PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", FAKE_GH_LOG=str(log), FAKE_GH_RUNS=json.dumps({"workflow_runs": runs}),
		CI_SYNC_QUICK_HEAD_SHA=head, CI_SYNC_QUICK_REPOSITORY="o/r", CI_SYNC_QUICK_PR_NUMBER="7", CI_SYNC_QUICK_DOCS_ONLY="false", **env)
	return subprocess.run(["bash", str(script), "verify"], cwd=repo, capture_output=True, text=True, env=run_env, check=False)


def _run_tests(tmp_path: Path, repo: Path, head: str, *, script: Path = SCRIPT, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
	marker = tmp_path / "marker.txt"
	marker.write_text("", encoding="utf-8")
	result = subprocess.run(["bash", str(script), "run-tests"], cwd=repo, capture_output=True, text=True,
		env=sq._clean_env(CI_SYNC_QUICK_HEAD_SHA=head, DELTA_MARKER=str(marker), **env), check=False)
	return result, marker.read_text(encoding="utf-8").split()


def _script_with_selector(tmp_path: Path, selector_source: str) -> Path:
	"""A copy of ci_sync_quick.sh next to a stub selector (the script resolves it by its own path)."""
	root = tmp_path / "tool-root"
	(root / "scripts").mkdir(parents=True)
	shutil.copy(SCRIPT, root / "scripts" / "ci_sync_quick.sh")
	hooks = root / "workflow-templates" / ".claude" / "hooks"
	hooks.mkdir(parents=True)
	(hooks / "prepush_affected_tests.py").write_text(selector_source, encoding="utf-8")
	return root / "scripts" / "ci_sync_quick.sh"


FAILING_SELECTOR = "import sys\nsys.exit(1)\n"
BAD_JSON_SELECTOR = (
	"import sys\n"
	"args = sys.argv[1:]\n"
	"open(args[args.index('--out') + 1], 'w').write('not json')\n"
)
WRONG_SHAPE_SELECTOR = (
	"import json, sys\n"
	"args = sys.argv[1:]\n"
	"json.dump({'affected_tests': [{'path': 7}]}, open(args[args.index('--out') + 1], 'w'))\n"
)


# --- delta-supported ---------------------------------------------------------


def test_delta_supported_probe() -> None:
	result = subprocess.run(["bash", str(SCRIPT), "delta-supported"], capture_output=True, text=True, env=sq._clean_env(), check=False)
	assert result.returncode == 0
	assert result.stdout == "delta_supported\n"


# --- verify ------------------------------------------------------------------


@pytest.mark.parametrize("switch", ["true", "false"])
def test_wiring_only_overlap_passes_unchanged(tmp_path: Path, switch: str) -> None:
	repo, update = sq._build(tmp_path, base_touches={".github/workflows/ci.yml": sq._ci_yml("top")},
		pr_touches={".github/workflows/ci.yml": sq._ci_yml("bottom"), "scripts/tool.sh": "echo two\n"})
	result = _verify(tmp_path, repo, update, MERGE_BASE_SYNC_DELTA_CI_ENABLED=switch)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "outcome=pass reason=ci_wiring_overlap" in result.stdout
	assert "delta_overlap" not in result.stdout


def test_other_overlap_with_switch_on_and_green_head_passes_as_delta(tmp_path: Path) -> None:
	repo, update = _code_overlap(tmp_path)
	result = _verify(tmp_path, repo, update)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "CI_SYNC_QUICK mode=verify outcome=ok reason=delta_overlap" in result.stdout
	assert "selected=2 cap=60" in result.stdout
	assert "overlap=scripts/tool.sh" in result.stdout


@pytest.mark.parametrize("switch", ["false", "0", "off", "nope"])
def test_switch_off_fails(tmp_path: Path, switch: str) -> None:
	repo, update = _code_overlap(tmp_path)
	result = _verify(tmp_path, repo, update, MERGE_BASE_SYNC_DELTA_CI_ENABLED=switch)
	assert result.returncode == 1
	assert "outcome=fail reason=delta_disabled first=scripts/tool.sh" in result.stdout
	assert "::error::" in result.stdout


def test_approved_head_not_full_green_fails(tmp_path: Path) -> None:
	repo, update = _code_overlap(tmp_path)
	result = _verify(tmp_path, repo, update, green=False)
	assert result.returncode == 1
	assert "reason=approved_head_not_green" in result.stdout


@pytest.mark.parametrize("cap", ["1", "0"])
def test_selection_above_the_cap_fails(tmp_path: Path, cap: str) -> None:
	repo, update = _code_overlap(tmp_path)
	result = _verify(tmp_path, repo, update, MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES=cap)
	assert result.returncode == 1
	assert f"reason=delta_selection_too_large selected=2 cap={cap}" in result.stdout


@pytest.mark.parametrize("cap", ["abc", "1000", "-1", ""])
def test_an_invalid_cap_means_sixty(tmp_path: Path, cap: str) -> None:
	repo, update = _code_overlap(tmp_path)
	result = _verify(tmp_path, repo, update, MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES=cap)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "cap=60" in result.stdout


def test_update_head_with_an_extra_commit_fails(tmp_path: Path) -> None:
	repo, _update = _code_overlap(tmp_path)
	sq._git(repo, "checkout", "-q", "pr")
	sq._write(repo, "scripts/extra.sh", "echo extra\n")
	sq._git(repo, "add", "-A")
	sq._git(repo, "commit", "-q", "-m", "extra on top of the update")
	head = sq._git(repo, "rev-parse", "HEAD")
	sq._git(repo, "checkout", "-q", "main")
	sq._git(repo, "merge", "-q", "--no-ff", "--no-edit", head)
	result = _verify(tmp_path, repo, head)
	assert result.returncode == 1
	assert "reason=update_not_a_merge" in result.stdout


@pytest.mark.parametrize("selector", [FAILING_SELECTOR, BAD_JSON_SELECTOR, WRONG_SHAPE_SELECTOR])
def test_a_selector_error_fails_verify(tmp_path: Path, selector: str) -> None:
	repo, update = _code_overlap(tmp_path)
	result = _verify(tmp_path, repo, update, script=_script_with_selector(tmp_path, selector))
	assert result.returncode == 1
	assert "reason=delta_selector_failed" in result.stdout


# --- run-tests ---------------------------------------------------------------


def test_run_tests_runs_each_selected_file_once(tmp_path: Path) -> None:
	# The PR adds tests/test_tool_pr.py, which the selector picks too: it runs
	# once (as a PR test), and the delta adds the base's two tests.
	repo, update = _code_overlap(tmp_path, pr_extra={"tests/test_tool_pr.py": _marker_test("pr", "covers scripts/tool.sh")})
	result, ran = _run_tests(tmp_path, repo, update)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "python=3 shell=0 delta=2" in result.stdout
	assert sorted(ran) == ["more", "pr", "tool"]


def test_run_tests_adds_nothing_with_the_switch_off(tmp_path: Path) -> None:
	repo, update = _code_overlap(tmp_path)
	result, ran = _run_tests(tmp_path, repo, update, MERGE_BASE_SYNC_DELTA_CI_ENABLED="false")
	assert result.returncode == 0, result.stdout + result.stderr
	assert "python=0 shell=0 delta=0" in result.stdout
	assert ran == []


def test_run_tests_runs_selected_shell_tests_with_bash(tmp_path: Path) -> None:
	# A shell test the selector picks by name (tests/test_<stem>.sh).
	repo, update = sq._build(tmp_path, base_touches={"scripts/tool.sh": BASE_TOOL,
		"tests/test_tool.sh": "#!/usr/bin/env bash\necho shell >> \"$DELTA_MARKER\"\n"}, pr_touches={"scripts/tool.sh": PR_TOOL})
	result, ran = _run_tests(tmp_path, repo, update)
	assert result.returncode == 0, result.stdout + result.stderr
	assert "shell=1 delta=1" in result.stdout
	assert ran == ["shell"]


def test_run_tests_fails_above_the_cap(tmp_path: Path) -> None:
	repo, update = _code_overlap(tmp_path)
	result, ran = _run_tests(tmp_path, repo, update, MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES="1")
	assert result.returncode == 1
	assert "reason=delta_selection_too_large" in result.stdout
	assert ran == []


@pytest.mark.parametrize("selector", [FAILING_SELECTOR, BAD_JSON_SELECTOR])
def test_a_selector_error_fails_run_tests(tmp_path: Path, selector: str) -> None:
	repo, update = _code_overlap(tmp_path)
	result, ran = _run_tests(tmp_path, repo, update, script=_script_with_selector(tmp_path, selector))
	assert result.returncode == 1
	assert "reason=selector_failed" in result.stdout
	assert ran == []


# --- _pr_base_sync_dispatch_final_ci -----------------------------------------

DELTA_ON = {"MERGE_BASE_SYNC_DELTA_CI_ENABLED": "true"}


def test_dispatcher_asks_for_the_quick_run_when_probe_switch_and_green_pass(tmp_path: Path) -> None:
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap="scripts/tool.sh", green=True, env=DELTA_ON)
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 1 and "sync_quick=true" in runs[0], result.stdout + result.stderr
	assert "reason=delta_overlap sync_quick=true" in result.stdout


def test_dispatcher_delta_is_on_by_default(tmp_path: Path) -> None:
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap="scripts/tool.sh", green=True, env={"MERGE_BASE_SYNC_DELTA_CI_ENABLED": "__unset__"})
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 1 and "sync_quick=true" in runs[0], result.stdout + result.stderr


def test_dispatcher_runs_the_full_suite_with_the_switch_off(tmp_path: Path) -> None:
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap="scripts/tool.sh", green=True, env={"MERGE_BASE_SYNC_DELTA_CI_ENABLED": "false"})
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 1 and not any("sync_quick" in arg for arg in runs[0])
	assert "reason=overlap_other sync_quick=false" in result.stdout


def test_dispatcher_runs_the_full_suite_when_the_approved_head_is_not_green(tmp_path: Path) -> None:
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap="scripts/tool.sh", green=False, env=DELTA_ON)
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 1 and not any("sync_quick" in arg for arg in runs[0])
	assert "reason=approved_head_not_full_green sync_quick=false" in result.stdout


def test_dispatcher_runs_the_full_suite_without_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	lib_dir = tmp_path / "old-scripts"
	lib_dir.mkdir()
	shutil.copy(LIB, lib_dir / "pr_checks_lib.sh")
	shutil.copy(ROOT / "scripts" / "ci_docs_only_paths.sh", lib_dir / "ci_docs_only_paths.sh")
	old_script = SCRIPT.read_text(encoding="utf-8").replace("\t\tdelta-supported) echo delta_supported ;;\n", "")
	assert "delta-supported) echo" not in old_script
	(lib_dir / "ci_sync_quick.sh").write_text(old_script, encoding="utf-8")
	monkeypatch.setattr(sq, "LIB", lib_dir / "pr_checks_lib.sh")
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap="scripts/tool.sh", green=True, env=DELTA_ON)
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 1 and not any("sync_quick" in arg for arg in runs[0]), result.stdout + result.stderr
	assert "reason=overlap_other sync_quick=false" in result.stdout


def test_dispatcher_wiring_only_is_unchanged_with_the_switch_on(tmp_path: Path) -> None:
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap=".github/workflows/ci.yml", green=True, env=DELTA_ON)
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 1 and "sync_quick=true" in runs[0]
	assert "reason=ci_wiring_only sync_quick=true" in result.stdout


def test_a_refused_delta_dispatch_still_retries_the_full_suite(tmp_path: Path) -> None:
	result, gh_calls, _ = sq._dispatch(tmp_path, overlap="scripts/tool.sh", green=True, env={**DELTA_ON, "FAKE_GH_REFUSE_SYNC_QUICK": "true"})
	runs = sq._dispatches(gh_calls)
	assert len(runs) == 2, result.stdout + result.stderr
	assert "sync_quick=true" in runs[0] and not any("sync_quick" in arg for arg in runs[1])


# --- ci.yml ------------------------------------------------------------------


def test_tests_sync_quick_passes_the_delta_switch_and_cap_to_both_steps() -> None:
	job = sq._ci()["jobs"][sq.SYNC_JOB]
	assert job["timeout-minutes"] == 30
	steps = {step.get("run", ""): step for step in job["steps"]}
	for command in ("bash scripts/ci_sync_quick.sh verify", "bash scripts/ci_sync_quick.sh run-tests"):
		env = steps[command]["env"]
		assert env["MERGE_BASE_SYNC_DELTA_CI_ENABLED"] == "${{ vars.MERGE_BASE_SYNC_DELTA_CI_ENABLED || 'true' }}"
		assert env["MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES"] == "${{ vars.MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES || '60' }}"
