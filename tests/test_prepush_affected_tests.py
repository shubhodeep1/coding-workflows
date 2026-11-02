"""Tests for the pre-push affected-test gate (issue #7137, plan phase P1).

Covers workflow-templates/.claude/hooks/prepush_affected_tests.py (selection
rules 1-6, companion files, command resolution, the sharded time-boxed runner,
baseline classification, assertion-diff, the interactive stamp and hook, the
settings wiring, no network use, the measured cases from the plan) and the
container wrapper scripts/prepush_test_gate_run.sh with a fake `docker`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "workflow-templates" / ".claude" / "hooks" / "prepush_affected_tests.py"
WRAPPER = REPO_ROOT / "scripts" / "prepush_test_gate_run.sh"
TEMPLATE_SETTINGS = REPO_ROOT / "workflow-templates" / ".claude" / "settings.json"

_spec = importlib.util.spec_from_file_location("prepush_affected_tests", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


def _git(root: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str], name: str = "repo") -> Path:
	root = tmp_path / name
	root.mkdir()
	_git(root, "init", "-q", "-b", "main")
	for path, text in files.items():
		target = root / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
	return root


def _commit(root: Path, files: dict[str, str]) -> str:
	for path, text in files.items():
		target = root / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding="utf-8")
	_git(root, "add", "-A")
	_git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
	return _git(root, "rev-parse", "HEAD")


def _select(root: Path, *lines: str, planned: bool = False, **kwargs) -> dict:
	return gate.select_tests(root, gate.parse_changes(list(lines)), planned=planned, **kwargs)


def _by_path(selection: dict) -> dict[str, dict]:
	return {entry["path"]: entry for entry in selection["affected_tests"]}


# --- selection rules -------------------------------------------------------

def test_rule1_changed_test_files_are_selected(tmp_path):
	root = _repo(tmp_path, {"tests/test_alpha.py": "def test_a():\n\tpass\n", "src/__tests__/beta.js": "", "web/x.spec.ts": ""})
	selected = _by_path(_select(root, "tests/test_alpha.py", "src/__tests__/beta.js", "web/x.spec.ts"))
	for path in ("tests/test_alpha.py", "src/__tests__/beta.js", "web/x.spec.ts"):
		assert "changed_test" in selected[path]["reasons"]


def test_rule2_naming_convention_maps_scripts_workflows_and_generic_files(tmp_path):
	root = _repo(tmp_path, {
		"scripts/foo-bar.sh": "", "tests/test_foo_bar.py": "",
		".github/workflows/my-flow.yml": "", "tests/test_my_flow.sh": "",
		"src/widget.js": "", "src/widget.test.js": "", "src/widget_test.py": "",
	})
	selected = _by_path(_select(root, "scripts/foo-bar.sh", ".github/workflows/my-flow.yml", "src/widget.js"))
	assert "naming" in selected["tests/test_foo_bar.py"]["reasons"]
	assert "naming" in selected["tests/test_my_flow.sh"]["reasons"]
	assert "naming" in selected["src/widget.test.js"]["reasons"]
	assert "naming" in selected["src/widget_test.py"]["reasons"]


def test_rule3_path_basename_and_stem_references(tmp_path):
	root = _repo(tmp_path, {
		"scripts/deploy_tool.sh": "",
		"tests/test_path.py": "X = 'scripts/deploy_tool.sh'\n",
		"tests/test_base.py": "X = 'run deploy_tool.sh now'\n",
		"tests/test_stem.py": "deploy_tool(1)\n",
		"tests/test_partial.py": "my_deploy_tool_x = 1\n",
		"tests/test_none.py": "nothing here\n",
	})
	selected = _by_path(_select(root, "scripts/deploy_tool.sh"))
	assert selected["tests/test_path.py"]["reasons"] == ["path_text"]
	assert selected["tests/test_base.py"]["reasons"] == ["basename_text"]
	assert selected["tests/test_stem.py"]["reasons"] == ["stem_text"]
	assert "tests/test_partial.py" not in selected
	assert "tests/test_none.py" not in selected


def test_rule3_short_and_stop_list_stems_are_not_matched(tmp_path):
	root = _repo(tmp_path, {
		"lib/abc.py": "", "lib/utils.py": "", "lib/config.yml": "",
		"tests/test_words.py": "abc utils config\n",
	})
	assert "tests/test_words.py" not in _by_path(_select(root, "lib/abc.py", "lib/utils.py", "lib/config.yml"))


def test_rule3_helper_module_indirection(tmp_path):
	root = _repo(tmp_path, {
		".github/workflows/big.yml": "",
		"tests/workflow_text.py": "PATH = '.github/workflows/big.yml'\n",
		"tests/test_uses_helper.py": "from workflow_text import PATH\n",
		"tests/test_imports_module.py": "import workflow_text\n",
		"tests/test_unrelated.py": "import os\n",
	})
	selected = _by_path(_select(root, ".github/workflows/big.yml"))
	assert "helper:workflow_text" in selected["tests/test_uses_helper.py"]["reasons"]
	assert "helper:workflow_text" in selected["tests/test_imports_module.py"]["reasons"]
	assert "tests/test_unrelated.py" not in selected


def test_rule3_changed_helper_selects_its_importers(tmp_path):
	root = _repo(tmp_path, {"tests/agents_text.py": "X = 1\n", "tests/test_importer.py": "from agents_text import X\n"})
	assert "helper:agents_text" in _by_path(_select(root, "tests/agents_text.py"))["tests/test_importer.py"]["reasons"]


GUARDS = {
	gate.WIRING_GUARD: "", gate.INVENTORY_GUARD: "", gate.LIVE_PARITY_GUARD: "", gate.SIZE_GUARD: "",
	"scripts/assemble_agents.py": "", "Makefile": "generate-check:\n\ttrue\n",
	**{guard: "" for guard in gate.CI_GUARD_TESTS},
}


def test_rule4_registry_guards_trigger_on_their_changes(tmp_path):
	root = _repo(tmp_path, {**GUARDS, ".github/workflows/x.yml": "", "scripts/old.sh": ""})
	sel = _select(root, "A\ttests/test_new.py", "A\tscripts/new.sh", "M\t.github/workflows/x.yml", "M\tworkflow-templates/.claude/settings.json", "M\tagents.md")
	selected = _by_path(sel)
	assert "registry:ci_test_wiring" in selected[gate.WIRING_GUARD]["reasons"]
	assert "registry:inventory_parity" in selected[gate.INVENTORY_GUARD]["reasons"]
	assert "registry:claude_live_parity" in selected[gate.LIVE_PARITY_GUARD]["reasons"]
	assert "registry:workflow_file_size" in selected[gate.SIZE_GUARD]["reasons"]
	checks = {c["check"]: c["argv"] for c in sel["registry_checks"]}
	assert checks["generate-check"] == ["make", "generate-check"]
	assert checks["assemble_agents"] == ["python3", "scripts/assemble_agents.py", "check", "--repo-root", "."]


def test_rule4_modified_script_does_not_trigger_structural_guards(tmp_path):
	root = _repo(tmp_path, {**GUARDS, "scripts/old.sh": ""})
	sel = _select(root, "M\tscripts/old.sh")
	assert gate.INVENTORY_GUARD not in _by_path(sel)
	assert gate.WIRING_GUARD not in _by_path(sel)
	assert sel["registry_checks"] == []


def test_rule4_guards_absent_from_the_repo_are_not_selected(tmp_path):
	root = _repo(tmp_path, {"README.md": ""})
	sel = _select(root, "A\ttests/test_new.py", "A\tscripts/new.sh", "M\t.github/workflows/ci.yml", "M\tagents.md")
	assert all(not reason.startswith("registry:") and reason != "guard_inverse" for e in sel["affected_tests"] for reason in e["reasons"])
	assert sel["registry_checks"] == []


def test_rule4_ci_workflow_selects_the_guard_inverse(tmp_path):
	root = _repo(tmp_path, {**GUARDS, ".github/workflows/ci.yml": ""})
	selected = _by_path(_select(root, ".github/workflows/ci.yml"))
	for guard in gate.CI_GUARD_TESTS:
		assert "guard_inverse" in selected[guard]["reasons"]


def test_ci_guard_inverse_matches_the_heal_scope_mapping():
	tree = ast.parse((REPO_ROOT / "scripts" / "workflow_failure_heal.py").read_text(encoding="utf-8"))
	mapping = None
	for node in tree.body:
		if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "HEAL_SCOPE_GUARD_TEST_SUBJECTS":
			mapping = ast.literal_eval(node.value)
	assert mapping is not None
	assert sorted(test for test, subjects in mapping.items() if gate.CI_WORKFLOW in subjects) == sorted(gate.CI_GUARD_TESTS)


def test_rule5_static_checks_and_missing_tools_are_unverified(tmp_path, monkeypatch):
	root = _repo(tmp_path, {"lib/a.py": "x = 1\n", "lib/b.sh": "true\n"})
	sel = _select(root, "lib/a.py", "lib/b.sh")
	tools = {c["tool"]: c["argv"] for c in sel["static_checks"]}
	assert tools["ruff"] == ["ruff", "check", "--select", "E,F", "--ignore", "E501", "lib/a.py"]
	assert tools["shellcheck"] == ["shellcheck", "--severity=error", "lib/b.sh"]
	monkeypatch.setattr(gate.shutil, "which", lambda name: None)
	units, unverified = gate.plan_units(root, sel)
	assert units == []
	assert {"item": "ruff", "reason": "tool_unavailable"} in unverified
	assert {"item": "shellcheck", "reason": "tool_unavailable"} in unverified


def _big_test_file(count: int, special: str = "") -> str:
	body = [f"def test_case_{i}():\n\tassert {i} == {i}\n" for i in range(count)]
	if special:
		body.append(f"def test_special():\n\tassert {special}() is None\n")
	return "\n".join(body)


def test_rule6_large_files_keep_functions_that_mention_a_change(tmp_path):
	root = _repo(tmp_path, {"lib/engine.py": "", "tests/test_engine.py": "import engine\n" + _big_test_file(60, "compute_total")})
	sel = _select(root, "lib/engine.py", function_names={"compute_total"})
	assert _by_path(sel)["tests/test_engine.py"]["functions"] == ["test_special"]


def test_rule6_falls_back_to_the_whole_file_when_nothing_matches(tmp_path):
	root = _repo(tmp_path, {"lib/engine.py": "", "tests/test_engine.py": "import engine\n" + _big_test_file(60)})
	assert "functions" not in _by_path(_select(root, "lib/engine.py", function_names={"nothing_here"}))["tests/test_engine.py"]


def test_changed_function_names_from_diff_hunks():
	diff = "@@ -1,2 +1,2 @@ def compute_total(x):\n-def old_helper():\n+def new_helper():\n+my_shell_fn() {\n"
	assert gate.changed_function_names(diff) == {"compute_total", "old_helper", "new_helper", "my_shell_fn"}


def test_planned_mode_adds_companion_files_only_when_guards_exist(tmp_path):
	root = _repo(tmp_path, {gate.INVENTORY_GUARD: "", gate.WIRING_GUARD: ""})
	sel = _select(root, "scripts/new_tool.sh", "tests/test_new_tool.py", planned=True)
	assert sel["companion_files"] == ["docs/INVENTORY.md", ".github/workflows/ci.yml"]
	bare = _repo(tmp_path, {"README.md": ""}, name="bare")
	assert _select(bare, "scripts/new_tool.sh", "tests/test_new_tool.py", planned=True)["companion_files"] == []


def test_parse_changes_handles_name_status_and_renames():
	assert gate.parse_changes(["M\ta.py", "R087\told.sh\tnew.sh", "A\tb.py", "plain.txt", ""]) == {
		"a.py": "M", "old.sh": "D", "new.sh": "R", "b.py": "A", "plain.txt": "",
	}


# --- measured cases (plan P1 step 9), against the real tree ----------------

MEASURED_CASES = [
	(".github/workflows/review_autofix.yml", "tests/test_unblock_scan.py", {"helper:review_autofix_step_scripts"}),
	(".github/workflows/clarify.yml", "tests/test_workflow_failure_heal.py", {"path_text", "basename_text"}),
	("scripts/gh_api_retry.py", "tests/test_gh_api_retry.py", {"naming"}),
	("workflow-templates/.claude/hooks/gh_api_write_guard.py", "tests/test_claude_template_live_parity.py", {"registry:claude_live_parity"}),
	(".github/workflows/ci.yml", "tests/test_ci_wires_every_test_file.py", {"registry:ci_test_wiring"}),
	("A\tscripts/claude_pool_health_alert.sh", "tests/inventory_parity.py", {"registry:inventory_parity"}),
	(".github/workflows/ci.yml", "tests/test_ci_job_split_contract.py", {"guard_inverse"}),
]


@pytest.mark.parametrize(("changed", "expected", "reasons"), MEASURED_CASES)
def test_measured_cases_select_the_failing_test_file(changed, expected, reasons):
	selected = _by_path(gate.select_tests(REPO_ROOT, gate.parse_changes([changed])))
	assert expected in selected, f"{expected} not selected for {changed}"
	assert reasons & set(selected[expected]["reasons"]), selected[expected]["reasons"]


# --- command resolution -----------------------------------------------------

def _units(root: Path, *paths: str) -> list[dict]:
	units, _ = gate.plan_units(root, {"affected_tests": [{"path": p, "reasons": []} for p in paths]})
	return units


def test_resolution_1_validate_override_with_and_without_files(tmp_path):
	root = _repo(tmp_path, {".ai/validate.yml": "family: x\nprepush_test_command: \"pytest -x {files}\"\n", "tests/test_a.py": "", "tests/test b.py": ""})
	units = _units(root, "tests/test_a.py", "tests/test b.py")
	assert units == [{"id": "prepush_test_command", "file": None, "argv": ["bash", "-c", "pytest -x tests/test_a.py 'tests/test b.py'"], "kind": "test"}]
	(root / ".ai" / "validate.yml").write_text("prepush_test_command: make check\n", encoding="utf-8")
	assert [u["argv"] for u in _units(root, "tests/test_a.py")] == [["bash", "-c", "make check"]]


def test_resolution_1_line_parser_fallback(tmp_path, monkeypatch):
	root = _repo(tmp_path, {".ai/validate.yml": "prepush_test_command: 'run {files}'\n"})
	monkeypatch.setitem(sys.modules, "yaml", None)
	assert gate._validate_override(root) == "run {files}"


def test_resolution_2_workflow_script_and_pytest_style(tmp_path):
	root = _repo(tmp_path, {
		".github/workflows/ci.yml": "run: |\n  PYTHONDONTWRITEBYTECODE=1 python3 tests/test_script.py\n  python3 -m pytest -q \\\n    tests/test_py.py\n",
		"tests/test_script.py": "", "tests/test_py.py": "",
	})
	units, _ = gate.plan_units(root, {"affected_tests": [
		{"path": "tests/test_script.py", "reasons": [], "functions": ["test_one", "Klass::test_two"]},
		{"path": "tests/test_py.py", "reasons": []},
	]})
	assert units[0]["argv"] == ["python3", "tests/test_script.py", "test_one", "test_two"]
	assert units[1]["argv"] == ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_py.py"]


def test_resolution_3_pytest_detection(tmp_path):
	plain = _repo(tmp_path, {"tests/test_a.py": ""}, name="plain")
	assert _units(plain, "tests/test_a.py")[0]["argv"] == ["python3", "tests/test_a.py"]
	configured = _repo(tmp_path, {"tests/test_a.py": "", "pyproject.toml": "[tool.pytest.ini_options]\n"}, name="configured")
	units, _ = gate.plan_units(configured, {"affected_tests": [{"path": "tests/test_a.py", "reasons": [], "functions": ["test_x"]}]})
	assert units[0]["argv"] == ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_a.py::test_x"]


def test_resolution_4_shell_tests(tmp_path):
	root = _repo(tmp_path, {"tests/test_a.sh": ""})
	assert _units(root, "tests/test_a.sh")[0]["argv"] == ["bash", "tests/test_a.sh"]


def test_resolution_5_node_runners(tmp_path):
	jest = _repo(tmp_path, {"package.json": json.dumps({"scripts": {"test": "jest --ci"}}), "a.test.js": ""}, name="jest")
	assert _units(jest, "a.test.js")[0]["argv"] == ["npx", "--no-install", "jest", "a.test.js"]
	npm = _repo(tmp_path, {"package.json": json.dumps({"scripts": {"test": "node run.js"}}), "a.test.js": "", "b.test.js": ""}, name="npm")
	assert [u["argv"] for u in _units(npm, "a.test.js", "b.test.js")] == [["npm", "test"]]


def test_resolution_6_and_7_make_and_unresolved(tmp_path):
	make = _repo(tmp_path, {"Makefile": "test:\n\ttrue\n", "spec/a_test.rb": ""}, name="make")
	assert [u["argv"] for u in _units(make, "spec/a_test.rb")] == [["make", "test"]]
	bare = _repo(tmp_path, {"spec/a_test.rb": ""}, name="bare")
	units, unverified = gate.plan_units(bare, {"affected_tests": [{"path": "spec/a_test.rb", "reasons": []}]})
	assert units == [] and unverified == [{"item": "spec/a_test.rb", "reason": "no_test_command"}]


# --- running, baseline, assertion-diff ---------------------------------------

def test_run_units_shards_in_parallel(tmp_path):
	units = [{"id": f"u{i}", "file": None, "argv": [sys.executable, "-c", "import time; time.sleep(1.5)"], "kind": "test"} for i in range(2)]
	start = time.monotonic()
	results = gate.run_units(units, tmp_path, time.monotonic() + 60, 2)
	assert [r["status"] for r in results] == ["pass", "pass"]
	assert time.monotonic() - start < 2.8


def test_run_units_budget_exhaustion_is_a_timeout(tmp_path):
	units = [
		{"id": "slow", "file": None, "argv": [sys.executable, "-c", "import time; time.sleep(30)"], "kind": "test"},
		{"id": "late", "file": None, "argv": [sys.executable, "-c", "pass"], "kind": "test"},
	]
	start = time.monotonic()
	results = gate.run_units(units, tmp_path, time.monotonic() + 1, 1)
	assert [r["status"] for r in results] == ["timeout", "timeout"]
	assert time.monotonic() - start < 10


def test_run_gate_reports_timeouts_as_unverified(tmp_path, monkeypatch):
	root = _repo(tmp_path, {"tests/test_a.py": ""})
	monkeypatch.setattr(gate, "run_units", lambda units, cwd, deadline, shards: [{**u, "status": "timeout", "rc": None, "tail": ""} for u in units])
	result = gate.run_gate(root, {"changed_files": [], "affected_tests": [{"path": "tests/test_a.py", "reasons": []}]}, timeout_minutes=1, shards=1)
	assert result["status"] == "unverified"
	assert result["unverified"] == [{"item": "tests/test_a.py", "reason": "timeout"}]


FAILING = "import sys\nprint('FAIL: test_broken (mod.Case)')\nsys.exit(1)\n"


def test_run_gate_classifies_inherited_and_pr_caused_failures(tmp_path):
	root = _repo(tmp_path, {"tests/test_ok.py": "", "tests/test_old.py": FAILING, "tests/test_new.py": FAILING})
	baseline = _repo(tmp_path, {"tests/test_ok.py": "", "tests/test_old.py": FAILING}, name="baseline")
	selection = {"changed_files": [], "affected_tests": [{"path": p, "reasons": []} for p in ("tests/test_ok.py", "tests/test_old.py", "tests/test_new.py")]}
	result = gate.run_gate(root, selection, baseline, timeout_minutes=2, shards=2)
	assert result["schema"] == "prepush_test_gate.v1"
	assert result["status"] == "fail"
	assert [f["test"] for f in result["failures"]] == ["tests/test_new.py"]
	assert result["failures"][0]["names"] == ["test_broken"]
	assert [i["test"] for i in result["inherited"]] == ["tests/test_old.py"]
	assert sorted(result["ran"]) == ["tests/test_new.py", "tests/test_ok.py", "tests/test_old.py"]


@pytest.mark.parametrize("baseline_status", ["timeout", "error"])
def test_run_gate_reports_unfinished_baseline_rerun_as_unverified(tmp_path, monkeypatch, baseline_status):
	# Release v1.40.3: a slow test failed on the PR tree, its baseline re-run hit
	# the deadline, and the gate blamed the change (repair turn + second run).
	root = _repo(tmp_path, {"tests/test_slow.py": FAILING, "tests/test_new.py": FAILING})
	baseline = _repo(tmp_path, {"tests/test_slow.py": FAILING}, name="baseline")

	def fake_run_units(units, cwd, deadline, shards):
		status = baseline_status if Path(cwd).resolve() == baseline.resolve() else "fail"
		return [{**u, "status": status, "rc": None if status != "fail" else 1, "tail": "FAIL: test_x (m.C)"} for u in units]

	monkeypatch.setattr(gate, "run_units", fake_run_units)
	selection = {"changed_files": [], "affected_tests": [{"path": p, "reasons": []} for p in ("tests/test_slow.py", "tests/test_new.py")]}
	result = gate.run_gate(root, selection, baseline, timeout_minutes=1, shards=1)
	# test_new.py has no baseline copy, so it is still caused by the change.
	assert [f["test"] for f in result["failures"]] == ["tests/test_new.py"]
	assert result["inherited"] == []
	assert result["unverified"] == [{"item": "tests/test_slow.py", "reason": f"baseline_{baseline_status}", "names": ["test_x"]}]
	assert result["status"] == "fail"
	assert f"`tests/test_slow.py`: baseline_{baseline_status} (failed on this change: `test_x`)" in gate.write_summary(result)


def test_run_gate_unfinished_baseline_only_is_unverified_not_fail(tmp_path, monkeypatch):
	root = _repo(tmp_path, {"tests/test_slow.py": FAILING})
	baseline = _repo(tmp_path, {"tests/test_slow.py": FAILING}, name="baseline")

	def fake_run_units(units, cwd, deadline, shards):
		status = "timeout" if Path(cwd).resolve() == baseline.resolve() else "fail"
		return [{**u, "status": status, "rc": None, "tail": ""} for u in units]

	monkeypatch.setattr(gate, "run_units", fake_run_units)
	result = gate.run_gate(root, {"changed_files": [], "affected_tests": [{"path": "tests/test_slow.py", "reasons": []}]}, baseline, timeout_minutes=1, shards=1)
	assert result["status"] == "unverified"
	assert result["failures"] == [] and result["inherited"] == []
	assert result["unverified"] == [{"item": "tests/test_slow.py", "reason": "baseline_timeout", "names": []}]
	assert result["ran"] == ["tests/test_slow.py"]


def test_run_gate_inherited_only_passes(tmp_path):
	root = _repo(tmp_path, {"tests/test_old.py": FAILING})
	baseline = _repo(tmp_path, {"tests/test_old.py": FAILING}, name="baseline")
	result = gate.run_gate(root, {"changed_files": [], "affected_tests": [{"path": "tests/test_old.py", "reasons": []}]}, baseline, timeout_minutes=2, shards=1)
	assert result["status"] == "pass" and result["failures"] == [] and len(result["inherited"]) == 1


LINT_OLD = "import os\nimport sys\n"


@pytest.mark.skipif(gate.shutil.which("ruff") is None, reason="ruff not installed")
def test_run_gate_counts_only_lint_findings_the_change_added(tmp_path):
	# The file already has an unused import on the merge-base; a change that
	# adds an unrelated line must not fail for it.
	root = _repo(tmp_path, {"lib/a.py": LINT_OLD + "X = 1\n"})
	baseline = _repo(tmp_path, {"lib/a.py": LINT_OLD}, name="baseline")
	selection = {"changed_files": ["lib/a.py"], "affected_tests": [], "static_checks": [
		{"tool": "ruff", "files": ["lib/a.py"], "argv": ["ruff", *gate.RUFF_CHECK_ARGS, "lib/a.py"]}]}
	result = gate.run_gate(root, selection, baseline, timeout_minutes=2, shards=1)
	assert result["status"] == "pass" and result["failures"] == []
	assert [i["test"] for i in result["inherited"]] == ["ruff"]
	# A new unused import is a finding the change added.
	(root / "lib" / "a.py").write_text(LINT_OLD + "import json\nX = 1\n", encoding="utf-8")
	result = gate.run_gate(root, selection, baseline, timeout_minutes=2, shards=1)
	assert result["status"] == "fail" and [f["test"] for f in result["failures"]] == ["ruff"]
	tail = result["failures"][0]["tail"]
	assert tail.startswith("1 finding(s) added by this change:") and "lib/a.py: F401" in tail and "json" in tail
	assert "`os`" not in tail and "`sys`" not in tail


@pytest.mark.skipif(gate.shutil.which("ruff") is None, reason="ruff not installed")
def test_run_gate_counts_every_lint_finding_in_a_new_file(tmp_path):
	root = _repo(tmp_path, {"lib/new.py": LINT_OLD})
	baseline = _repo(tmp_path, {"README": ""}, name="baseline")
	selection = {"changed_files": ["lib/new.py"], "affected_tests": [], "static_checks": [
		{"tool": "ruff", "files": ["lib/new.py"], "argv": ["ruff", *gate.RUFF_CHECK_ARGS, "lib/new.py"]}]}
	result = gate.run_gate(root, selection, baseline, timeout_minutes=2, shards=1)
	assert result["status"] == "fail" and result["failures"][0]["tail"].startswith("2 finding(s) added")


@pytest.mark.skipif(gate.shutil.which("ruff") is None, reason="ruff not installed")
def test_run_gate_matches_moved_findings_whose_message_cites_a_line(tmp_path):
	# F811 says "from line N"; lines added above it must not make it new.
	old = "def f():\n\tpass\n\n\ndef f():\n\tpass\n"
	root = _repo(tmp_path, {"lib/a.py": "X = 1\nY = 2\n" + old})
	baseline = _repo(tmp_path, {"lib/a.py": old}, name="baseline")
	selection = {"changed_files": ["lib/a.py"], "affected_tests": [], "static_checks": [
		{"tool": "ruff", "files": ["lib/a.py"], "argv": ["ruff", *gate.RUFF_CHECK_ARGS, "lib/a.py"]}]}
	result = gate.run_gate(root, selection, baseline, timeout_minutes=2, shards=1)
	assert result["status"] == "pass" and [i["test"] for i in result["inherited"]] == ["ruff"]


def test_static_failure_stays_caused_by_the_change_when_findings_cannot_be_read(tmp_path, monkeypatch):
	baseline = tmp_path / "baseline"
	baseline.mkdir()
	monkeypatch.setattr(gate, "_static_findings", lambda tool, files, cwd, deadline: None)
	unit = {"id": "ruff", "file": None, "kind": "static", "files": ["a.py"], "status": "fail", "tail": "raw"}
	pr_caused, inherited = gate.classify_baseline([unit], baseline, time.monotonic() + 30, 1, tmp_path)
	assert pr_caused == [unit] and inherited == [] and unit["tail"] == "raw"


def test_static_failure_without_a_repo_keeps_the_old_classification(tmp_path):
	baseline = tmp_path / "baseline"
	baseline.mkdir()
	unit = {"id": "ruff", "file": None, "kind": "static", "files": ["a.py"], "status": "fail", "tail": "raw"}
	assert gate.classify_baseline([unit], baseline, time.monotonic() + 30, 1) == ([unit], [])


@pytest.mark.skipif(gate.shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_run_gate_compares_shellcheck_findings_against_the_baseline(tmp_path):
	old = "#!/bin/bash\nif [ 1 ]\nthen echo x\n"
	root = _repo(tmp_path, {"lib/b.sh": old})
	baseline = _repo(tmp_path, {"lib/b.sh": old}, name="baseline")
	selection = {"changed_files": ["lib/b.sh"], "affected_tests": [], "static_checks": [
		{"tool": "shellcheck", "files": ["lib/b.sh"], "argv": ["shellcheck", *gate.SHELLCHECK_ARGS, "lib/b.sh"]}]}
	result = gate.run_gate(root, selection, baseline, timeout_minutes=2, shards=1)
	assert result["status"] == "pass" and [i["test"] for i in result["inherited"]] == ["shellcheck"]


def test_failing_names_parses_the_heal_shapes():
	tail = "\n".join([
		'TEST_CASE_EVENT: {"status":"fail","test_name":"test_event"}',
		'TEST_CASE_EVENT: {"status":"pass","test_name":"test_fine"}',
		"  FAIL  test_runner:",
		"2026-10-09T10:00:01Z FAILED tests/x.py::TestThing::test_method - AssertionError",
		"ERROR: test_unit (mod.Case)",
	])
	assert gate.failing_names(tail) == ["test_event", "test_runner", "tests/x.py::TestThing::test_method", "test_unit"]


def test_assertion_diff_lists_removed_assertions_and_added_skips(tmp_path):
	baseline = _repo(tmp_path, {"tests/test_a.py": "def test_x():\n\tassert f() == 1\n\tok = 2\n"}, name="baseline")
	root = _repo(tmp_path, {"tests/test_a.py": "@pytest.mark.skip\ndef test_x():\n\tassert f() == 2\n\tok = 3\n", "tests/test_new.py": "assert 1\n"})
	findings = gate.assertion_diff(root, baseline, ["tests/test_a.py", "tests/test_new.py", "lib/x.py"])
	assert {"file": "tests/test_a.py", "kind": "removed_or_modified", "line": "assert f() == 1"} in findings
	assert {"file": "tests/test_a.py", "kind": "skip_added", "line": "@pytest.mark.skip"} in findings
	assert all(f["file"] == "tests/test_a.py" for f in findings)


def test_summary_neutralises_workflow_commands_and_lists_names_only():
	result = {"status": "fail", "ran": ["a"], "selected": 1, "unverified": [], "changed_assertions": [], "inherited": [],
		"failures": [{"test": "::error::forged", "names": ["test_x"], "tail": "SECRET TAIL"}]}
	summary = gate.write_summary(result)
	assert summary.startswith("## Pre-push test gate\n")
	assert "SECRET TAIL" not in summary
	assert not any(line.startswith("::") for line in summary.splitlines())


# --- stamp ------------------------------------------------------------------

def _stamp_repo(tmp_path: Path) -> Path:
	root = _repo(tmp_path, {"lib/calc.py": "X = 1\n", "tests/test_calc.py": "# covers lib/calc.py\n"})
	_git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
	_commit(root, {"lib/calc.py": "X = 2\n"})
	return root


def test_stamp_writes_a_head_keyed_stamp_under_tmpdir(tmp_path, monkeypatch):
	root = _stamp_repo(tmp_path)
	tmp = tmp_path / "tmp"
	tmp.mkdir()
	monkeypatch.setenv("TMPDIR", str(tmp))
	assert gate.cmd_stamp(root) == 0
	head = _git(root, "rev-parse", "HEAD")
	path = gate.stamp_path(root.resolve(), head)
	assert path.is_file() and path.name == f"{head}.json"
	assert gate._inside(path, tmp) and not gate._inside(path, root)
	assert stat.S_IMODE(path.stat().st_mode) == 0o600
	stamp = json.loads(path.read_text(encoding="utf-8"))
	assert stamp["head"] == head and stamp["status"] in gate.PASSING_STAMP_STATUSES
	assert "tests/test_calc.py" in stamp["ran"]


def test_stamp_refuses_uncommitted_tracked_changes(tmp_path, monkeypatch):
	root = _stamp_repo(tmp_path)
	monkeypatch.setenv("TMPDIR", str(tmp_path))
	(root / "lib" / "calc.py").write_text("X = 3\n", encoding="utf-8")
	assert gate.cmd_stamp(root) == 2


def test_stamp_without_a_merge_base_is_unverified(tmp_path, monkeypatch):
	root = _repo(tmp_path, {"a.txt": "1\n"})
	monkeypatch.setenv("TMPDIR", str(tmp_path))
	assert gate.cmd_stamp(root) == 0
	stamp = json.loads(gate.stamp_path(root.resolve(), _git(root, "rev-parse", "HEAD")).read_text(encoding="utf-8"))
	assert stamp["status"] == "unverified" and stamp["unverified"][0]["reason"] == "no_merge_base"


def test_export_tree_writes_the_merge_base_files(tmp_path):
	root = _repo(tmp_path, {"lib/a.py": "OLD\n", "tests/test_a.py": "x\n"})
	base = _git(root, "rev-parse", "HEAD")
	_commit(root, {"lib/a.py": "NEW\n", "lib/b.py": ""})
	dest = tmp_path / "baseline"
	dest.mkdir()
	gate.export_tree(root, base, dest)
	assert (dest / "lib" / "a.py").read_text(encoding="utf-8") == "OLD\n"
	assert (dest / "tests" / "test_a.py").is_file() and not (dest / "lib" / "b.py").exists()
	assert not (dest / ".git").exists()


def test_write_stamp_refuses_paths_inside_the_repository(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	with pytest.raises(ValueError):
		gate.write_stamp(root / "stamp.json", {}, root)
	with pytest.raises(ValueError):
		gate.write_stamp(root / ".git" / "stamp.json", {}, root)


# --- hook -------------------------------------------------------------------

def _hook(payload, env_extra: dict[str, str], module: Path = MODULE_PATH) -> dict:
	env = {**os.environ, **env_extra}
	proc = subprocess.run([sys.executable, str(module), "hook"], input=payload if isinstance(payload, str) else json.dumps(payload),
		capture_output=True, text=True, env=env, timeout=60)
	assert proc.returncode == 0, proc.stderr
	return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _push(root: Path, command: str = "git push origin HEAD") -> dict:
	return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)}


def _write(root: Path, tmp: Path, status: str, failures=()) -> None:
	head = _git(root, "rev-parse", "HEAD")
	path = tmp / gate.STAMP_DIR_NAME / gate.stamp_path(root.resolve(), head).parent.name / f"{head}.json"
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps({"head": head, "status": status, "failures": list(failures)}), encoding="utf-8")


def test_hook_asks_when_no_stamp_exists(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	out = _hook(_push(root), {"TMPDIR": str(tmp_path)})
	decision = out["hookSpecificOutput"]
	assert decision["permissionDecision"] == "ask"
	assert gate.STAMP_COMMAND in decision["permissionDecisionReason"]
	assert "CLAUDE_PREPUSH_TEST_GATE=off" in decision["permissionDecisionReason"]


def test_hook_allows_with_a_passing_stamp_and_reports_failures_without_asking(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	_write(root, tmp_path, "pass")
	assert _hook(_push(root), {"TMPDIR": str(tmp_path)}) == {}
	_write(root, tmp_path, "fail", [{"test": "tests/test_broken.py"}])
	out = _hook(_push(root), {"TMPDIR": str(tmp_path)})
	specific = out["hookSpecificOutput"]
	assert "permissionDecision" not in specific
	assert specific["hookEventName"] == "PreToolUse"
	assert "tests/test_broken.py" in specific["additionalContext"]
	assert "tests/test_broken.py" in out["systemMessage"]


def test_hook_ignores_non_push_commands_and_other_tools(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	assert _hook(_push(root, "git status && echo push"), {"TMPDIR": str(tmp_path)}) == {}
	assert _hook({**_push(root), "tool_name": "Edit"}, {"TMPDIR": str(tmp_path)}) == {}


def test_hook_off_switch_allows(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	assert _hook(_push(root), {"TMPDIR": str(tmp_path), "CLAUDE_PREPUSH_TEST_GATE": "off"}) == {}


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]"])
def test_hook_malformed_payload_allows_with_a_warning(payload):
	out = _hook(payload, {})
	assert "systemMessage" in out and "hookSpecificOutput" not in out


def test_hook_falls_back_when_the_guard_module_is_missing(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	lone = tmp_path / "lone"
	lone.mkdir()
	(lone / MODULE_PATH.name).write_bytes(MODULE_PATH.read_bytes())
	out = _hook(_push(root), {"TMPDIR": str(tmp_path)}, module=lone / MODULE_PATH.name)
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "conservative push check" in out["systemMessage"]


def test_hook_internal_error_fails_open(monkeypatch, capsys):
	monkeypatch.setattr(gate, "hook_decision", lambda payload: (_ for _ in ()).throw(RuntimeError("boom")))
	assert gate.cmd_hook(json.dumps({"tool_name": "Bash"})) == 0
	out = json.loads(capsys.readouterr().out)
	assert "internal error" in out["systemMessage"] and "hookSpecificOutput" not in out


# --- settings, network, CI wiring -------------------------------------------

def test_template_settings_wire_the_hook_once_in_its_own_entry():
	settings = json.loads(TEMPLATE_SETTINGS.read_text(encoding="utf-8"))
	entries = [e for e in settings["hooks"]["PreToolUse"] if any("prepush_affected_tests.py" in h.get("command", "") for h in e["hooks"])]
	assert len(entries) == 1
	entry = entries[0]
	assert entry["matcher"] == "Bash" and len(entry["hooks"]) == 1
	assert entry["hooks"][0] == {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/prepush_affected_tests.py hook', "timeout": 30}


def test_module_makes_no_network_or_github_calls(tmp_path, monkeypatch):
	calls: list[list[str]] = []
	real_run, real_popen = subprocess.run, subprocess.Popen

	def record_run(argv, *args, **kwargs):
		calls.append(list(argv))
		return real_run(argv, *args, **kwargs)

	def record_popen(argv, *args, **kwargs):
		calls.append(list(argv))
		return real_popen(argv, *args, **kwargs)

	monkeypatch.setattr(gate.subprocess, "run", record_run)
	monkeypatch.setattr(gate.subprocess, "Popen", record_popen)
	monkeypatch.setenv("TMPDIR", str(tmp_path))
	root = _stamp_repo(tmp_path)
	gate.cmd_stamp(root)
	gate.evaluate_hook(_push(root))
	assert calls
	assert not any(Path(c[0]).name in ("gh", "curl", "wget") for c in calls)
	source = MODULE_PATH.read_text(encoding="utf-8")
	assert "api.github.com" not in source and "urllib" not in source and "http.client" not in source


def test_ci_runs_this_suite_and_sets_the_parity_base_only_from_the_pr_event():
	ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
	assert "tests/test_prepush_affected_tests.py" in ci
	assert ci.count("CLAUDE_PARITY_PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}") == 5
	for gate_workflow in ("mark-stable.yml", "test-and-mark-stable.yml"):
		assert "CLAUDE_PARITY_PR_BASE_SHA" not in (REPO_ROOT / ".github" / "workflows" / gate_workflow).read_text(encoding="utf-8")


# --- container wrapper --------------------------------------------------------

FAKE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "__LOG__"
if [ "$1" = run ]; then
	results=""
	for arg in "$@"; do
		case "$arg" in
			type=bind,src=*,dst=/results) results="${arg#type=bind,src=}"; results="${results%,dst=/results}" ;;
		esac
	done
	printf '{"schema": "prepush_test_gate.v1", "status": "pass", "ran": ["x"]}\n' > "$results/result.json"
	printf '## Pre-push test gate\n' > "$results/summary.md"
	printf 'not copied\n' > "$results/evil.txt"
fi
exit 0
'''


def _wrapper_env(tmp_path: Path, with_docker: bool = True) -> tuple[dict[str, str], Path]:
	log = tmp_path / "docker.log"
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	seen: set[str] = set()
	for directory in os.environ.get("PATH", "").split(os.pathsep):
		if not directory or not os.path.isdir(directory):
			continue
		for name in os.listdir(directory):
			full = os.path.join(directory, name)
			if name in seen or name == "docker" or not os.access(full, os.X_OK) or os.path.isdir(full):
				continue
			seen.add(name)
			os.symlink(full, bin_dir / name)
	if with_docker:
		fake = bin_dir / "docker"
		fake.write_text(FAKE_DOCKER.replace("__LOG__", str(log)), encoding="utf-8")
		fake.chmod(0o755)
	env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "GIT_"))}
	env.update({"PATH": str(bin_dir), "RUNNER_TEMP": str(tmp_path), "TMPDIR": str(tmp_path)})
	return env, log


def _run_wrapper(tmp_path: Path, worktree: Path, module: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, Path]:
	out = tmp_path / "out"
	base = _git(worktree, "rev-parse", "HEAD~1")
	proc = subprocess.run(["bash", str(WRAPPER), "--worktree", str(worktree), "--module", str(module), "--image", "gate-image:test",
		"--base", base, "--out-dir", str(out), "--timeout-minutes", "2"], capture_output=True, text=True, env=env, timeout=120)
	return proc, out


def _worktree(tmp_path: Path) -> Path:
	root = _repo(tmp_path, {"lib/a.py": "X = 1\n", "tests/test_a.py": "# lib/a.py\n"}, name="worktree")
	_commit(root, {"lib/a.py": "X = 2\n"})
	return root


def test_wrapper_runs_an_isolated_container_and_copies_back_only_results(tmp_path):
	worktree = _worktree(tmp_path)
	env, log = _wrapper_env(tmp_path)
	proc, out = _run_wrapper(tmp_path, worktree, MODULE_PATH, env)
	assert proc.returncode == 0, proc.stderr
	run_line = next(line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run "))
	for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--rm", "--init"):
		assert flag in run_line
	# Docker's --tmpfs defaults to noexec; tests that run a stub script from
	# a temp dir need exec (PR #8371 loop).
	assert "--tmpfs /tmp:rw,exec,nosuid,nodev,size=512m" in run_line
	assert "--env" not in run_line and " -e " not in run_line
	assert f"src={MODULE_PATH.resolve()},dst=/gate/prepush_affected_tests.py,readonly" in run_line
	assert "dst=/baseline,readonly" in run_line
	assert sorted(p.name for p in out.iterdir()) == ["result.json", "summary.md"]
	assert json.loads((out / "result.json").read_text(encoding="utf-8"))["status"] == "pass"
	assert "PREPUSH_TEST_GATE phase=wrapper outcome=pass" in proc.stderr


@pytest.mark.parametrize("dockerfile_source", [
	"scripts/codex_isolated_exec.sh",  # implement gate (generated Dockerfile)
	"scripts/review_sandbox/Dockerfile",  # review and conflict-resolver gates
	"scripts/clarify_sandbox/Dockerfile",  # heal gate
])
def test_gate_images_install_jq(dockerfile_source):
	# Most repo scripts call jq; without it their tests fail on both trees in
	# the gate container and the gate reports noise instead of results.
	text = (REPO_ROOT / dockerfile_source).read_text(encoding="utf-8")
	installs = [line for line in text.splitlines() if "apt-get install" in line]
	assert installs, dockerfile_source
	assert any("jq" in line.split() for line in installs), dockerfile_source


def test_wrapper_refuses_a_module_inside_the_worktree(tmp_path):
	worktree = _worktree(tmp_path)
	module = worktree / "gate.py"
	module.write_bytes(MODULE_PATH.read_bytes())
	env, log = _wrapper_env(tmp_path)
	proc, out = _run_wrapper(tmp_path, worktree, module, env)
	assert proc.returncode == 0
	assert "reason=module_untrusted" in proc.stderr
	assert json.loads((out / "result.json").read_text(encoding="utf-8"))["reason"] == "module_untrusted"
	assert not log.exists()


def test_wrapper_without_docker_reports_unverified_and_exits_zero(tmp_path):
	worktree = _worktree(tmp_path)
	env, _ = _wrapper_env(tmp_path, with_docker=False)
	proc, out = _run_wrapper(tmp_path, worktree, MODULE_PATH, env)
	assert proc.returncode == 0
	result = json.loads((out / "result.json").read_text(encoding="utf-8"))
	assert result["status"] == "unverified" and result["reason"] == "docker_unavailable"


# --- review fixes -------------------------------------------------------------

def test_resolution_3_pytest_style_file_without_main_uses_pytest(tmp_path, monkeypatch):
	root = _repo(tmp_path, {"tests/test_a.py": "def test_x():\n\tassert False\n", "tests/test_b.py": "def test_y():\n\tpass\n\nif __name__ == '__main__':\n\ttest_y()\n"})
	assert _units(root, "tests/test_a.py")[0]["argv"] == ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_a.py"]
	assert _units(root, "tests/test_b.py")[0]["argv"] == ["python3", "tests/test_b.py"]
	monkeypatch.setattr(gate.importlib.util, "find_spec", lambda name: None)
	units, unverified = gate.plan_units(root, {"affected_tests": [{"path": "tests/test_a.py", "reasons": []}]})
	assert units == [] and unverified == [{"item": "tests/test_a.py", "reason": "pytest_unavailable"}]


def test_hook_checks_the_stamp_of_the_pushed_ref_not_head(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	_git(root, "branch", "other")
	_commit(root, {"a.txt": "changed\n"})
	_write(root, tmp_path, "pass")
	assert _hook(_push(root, "git push origin HEAD"), {"TMPDIR": str(tmp_path)}) == {}
	out = _hook(_push(root, "git push origin other"), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "other" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_asks_when_the_pushed_ref_does_not_resolve(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	_write(root, tmp_path, "pass")
	out = _hook(_push(root, "git push origin no-such-branch:main"), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "Could not resolve" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_falls_back_for_a_push_the_guard_does_not_parse(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	# Since #6038 the guard parses `command git push`; a bare `git push)`
	# closing a multi-segment subshell still reaches the fallback.
	out = _hook(_push(root, "(cd . && git push)"), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert _hook(_push(root, 'git commit -m "push later"'), {"TMPDIR": str(tmp_path)}) == {}


def test_wrapper_rejects_a_result_when_the_container_exits_non_zero(tmp_path):
	worktree = _worktree(tmp_path)
	env, log = _wrapper_env(tmp_path)
	fake = tmp_path / "bin" / "docker"
	fake.write_text(fake.read_text(encoding="utf-8").replace('\nfi\nexit 0\n', '\n\texit 1\nfi\nexit 0\n'), encoding="utf-8")
	proc, out = _run_wrapper(tmp_path, worktree, MODULE_PATH, env)
	assert proc.returncode == 0
	result = json.loads((out / "result.json").read_text(encoding="utf-8"))
	assert result["status"] == "unverified" and result["reason"] == "container_rc_1"


def test_wrapper_lists_uncommitted_tracked_edits_and_overlays_command_inputs(tmp_path):
	worktree = _worktree(tmp_path)
	(worktree / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
	_commit(worktree, {".ai/validate.yml": "prepush_test_command: make test\n"})
	(worktree / "tests" / "test_a.py").write_text("# edited, not committed\n", encoding="utf-8")
	env, log = _wrapper_env(tmp_path)
	fake = tmp_path / "bin" / "docker"
	capture = tmp_path / "capture"
	fake.write_text(fake.read_text(encoding="utf-8").replace(
		'\tprintf \'{"schema"',
		'\tsrc="${results%/results}/source"; cp "$results/changed.txt" "__CAP__.changed"; ls -a "$src" > "__CAP__.ls"; ls -a "$src/.ai" >> "__CAP__.ls"\n\tprintf \'{"schema"',
	).replace("__CAP__", str(capture)), encoding="utf-8")
	proc, _ = _run_wrapper(tmp_path, worktree, MODULE_PATH, env)
	assert proc.returncode == 0, proc.stderr
	changed = (tmp_path / "capture.changed").read_text(encoding="utf-8").split("\n")
	assert "M\ttests/test_a.py" in changed
	listing = (tmp_path / "capture.ls").read_text(encoding="utf-8").split()
	assert "Makefile" in listing and "validate.yml" in listing


def test_resolution_configured_pytest_without_pytest_is_unverified(tmp_path, monkeypatch):
	root = _repo(tmp_path, {"tests/test_a.py": "", "pytest.ini": "[pytest]\n"})
	monkeypatch.setattr(gate.importlib.util, "find_spec", lambda name: None)
	units, unverified = gate.plan_units(root, {"affected_tests": [{"path": "tests/test_a.py", "reasons": []}]})
	assert units == [] and unverified == [{"item": "tests/test_a.py", "reason": "pytest_unavailable"}]


def test_hook_asks_for_bulk_pushes_even_with_a_passing_head_stamp(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	_write(root, tmp_path, "pass")
	out = _hook(_push(root, "git push --all origin"), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "--all" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_asks_for_tag_only_pushes(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	_git(root, "tag", "v1")
	_write(root, tmp_path, "pass")
	out = _hook(_push(root, "git push origin refs/tags/v1"), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "tags" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_ignores_git_push_inside_quoted_text(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	assert _hook(_push(root, 'git commit -m "document git push behaviour"'), {"TMPDIR": str(tmp_path)}) == {}
	assert _hook(_push(root, "echo 'git push'"), {"TMPDIR": str(tmp_path)}) == {}


@pytest.mark.parametrize("command", ["git push origin main refs/tags/v1", "git push --tags origin main", "git push --follow-tags origin main"])
def test_hook_asks_for_mixed_branch_and_tag_pushes(tmp_path, command):
	root = _repo(tmp_path, {"a.txt": ""})
	_git(root, "tag", "v1")
	_write(root, tmp_path, "pass")
	assert _hook(_push(root, "git push origin main"), {"TMPDIR": str(tmp_path)}) == {}
	out = _hook(_push(root, command), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "tags" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", ["git push --follow-tag origin main", "git push --thin origin main"])
def test_hook_asks_when_the_guard_cannot_resolve_push_options(tmp_path, command):
	root = _repo(tmp_path, {"a.txt": ""})
	_write(root, tmp_path, "pass")
	assert _hook(_push(root, "git push origin main"), {"TMPDIR": str(tmp_path)}) == {}
	out = _hook(_push(root, command), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "Could not resolve which refs" in out["hookSpecificOutput"]["permissionDecisionReason"]


# --- Auto mode ----------------------------------------------------------------

def _auto(payload: dict, mode: str = "auto") -> dict:
	return {**payload, "permission_mode": mode}


def test_hook_denies_a_missing_stamp_in_auto_mode(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	out = _hook(_auto(_push(root)), {"TMPDIR": str(tmp_path)})
	decision = out["hookSpecificOutput"]
	assert decision["permissionDecision"] == "deny"
	reason = decision["permissionDecisionReason"]
	assert gate.STAMP_COMMAND in reason and "nothing ran" in reason and "tell the user" in reason
	# The session cannot set the hook's environment, so the off switch is not offered.
	assert "CLAUDE_PREPUSH_TEST_GATE=off" not in reason


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk", "Auto", None])
def test_hook_still_asks_for_a_missing_stamp_outside_auto_mode(tmp_path, mode):
	root = _repo(tmp_path, {"a.txt": ""})
	payload = _push(root) if mode is None else _auto(_push(root), mode)
	out = _hook(payload, {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("mode", ["auto", "default", "acceptEdits", "bypassPermissions", "dontAsk"])
def test_hook_never_holds_a_push_for_a_failing_stamp(tmp_path, mode):
	root = _repo(tmp_path, {"a.txt": ""})
	_write(root, tmp_path, "pass")
	assert _hook(_auto(_push(root), mode), {"TMPDIR": str(tmp_path)}) == {}
	_write(root, tmp_path, "fail", [{"test": "tests/test_broken.py"}])
	out = _hook(_auto(_push(root), mode), {"TMPDIR": str(tmp_path)})
	specific = out["hookSpecificOutput"]
	assert "permissionDecision" not in specific
	assert "tests/test_broken.py" in specific["additionalContext"]
	assert "confirm to push anyway" not in specific["additionalContext"]


@pytest.mark.parametrize("command", ["git push --tags origin", "git push --all origin"])
def test_hook_auto_mode_still_asks_when_a_person_must_decide(tmp_path, command):
	root = _repo(tmp_path, {"a.txt": ""})
	out = _hook(_auto(_push(root, command)), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
	"command",
	[
		"git push origin no-such-branch:main",
		# The branch only exists once the command has run, so the hook cannot see it yet.
		"git checkout -q -b claude/new-log && git commit -qam log && git push -u origin claude/new-log 2>&1 | tail -1",
	],
)
def test_hook_auto_mode_denies_a_pushed_branch_that_does_not_exist_yet(tmp_path, command):
	root = _repo(tmp_path, {"a.txt": ""})
	out = _hook(_auto(_push(root, command)), {"TMPDIR": str(tmp_path)})
	decision = out["hookSpecificOutput"]
	assert decision["permissionDecision"] == "deny"
	reason = decision["permissionDecisionReason"]
	assert "Could not resolve the pushed ref" in reason and "nothing ran" in reason
	assert "own plain command" in reason and gate.STAMP_COMMAND in reason


def test_hook_still_asks_for_a_pushed_branch_that_does_not_exist_yet_outside_auto_mode(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	out = _hook(_push(root, "git checkout -b claude/new-log && git push -u origin claude/new-log"), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "Could not resolve the pushed ref" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_auto_mode_asks_when_a_missing_stamp_comes_with_a_human_finding(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	out = _hook(_auto(_push(root, "git push origin HEAD && git push --tags origin")), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert "No pre-push test stamp" in reason and "tags" in reason


def test_hook_auto_mode_a_failing_stamp_next_to_a_missing_one_still_denies(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	_git(root, "branch", "other")
	_commit(root, {"a.txt": "changed\n"})
	_write(root, tmp_path, "fail", [{"test": "tests/test_broken.py"}])
	out = _hook(_auto(_push(root, "git push origin HEAD && git push origin other")), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
	reason = out["hookSpecificOutput"]["permissionDecisionReason"]
	assert "No pre-push test stamp" in reason and "tests/test_broken.py" in reason
	assert "not held" not in reason


@pytest.mark.parametrize("status", [None, "garbled", ["fail"], {"k": "v"}])
def test_hook_asks_for_a_stamp_without_a_valid_status(tmp_path, status):
	root = _repo(tmp_path, {"a.txt": ""})
	_write(root, tmp_path, status)
	out = _hook(_auto(_push(root)), {"TMPDIR": str(tmp_path)})
	assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
	assert "no valid status" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_auto_mode_off_switch_still_allows(tmp_path):
	root = _repo(tmp_path, {"a.txt": ""})
	assert _hook(_auto(_push(root)), {"TMPDIR": str(tmp_path), "CLAUDE_PREPUSH_TEST_GATE": "off"}) == {}


def test_evaluate_hook_keeps_its_two_value_contract(tmp_path, monkeypatch):
	monkeypatch.setenv("TMPDIR", str(tmp_path))
	root = _repo(tmp_path, {"a.txt": ""})
	ask, message = gate.evaluate_hook(_auto(_push(root)))
	assert "No pre-push test stamp" in ask and message == ""


# --- wrapper pass-through flags (issue #8190) ---------------------------------

def test_wrapper_forwards_venv_shards_and_large_file_limit(tmp_path):
	worktree = _worktree(tmp_path)
	venv = tmp_path / "venv"
	(venv / "bin").mkdir(parents=True)
	env, log = _wrapper_env(tmp_path)
	out = tmp_path / "out"
	base = _git(worktree, "rev-parse", "HEAD~1")
	proc = subprocess.run(["bash", str(WRAPPER), "--worktree", str(worktree), "--module", str(MODULE_PATH), "--image", "gate-image:test",
		"--base", base, "--out-dir", str(out), "--timeout-minutes", "2", "--venv", str(venv), "--shards", "3",
		"--large-file-tests", "7"], capture_output=True, text=True, env=env, timeout=120)
	assert proc.returncode == 0, proc.stderr
	run_line = next(line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run "))
	assert f"src={venv.resolve()},dst=/opt/codex-venv,readonly" in run_line
	assert "--env PATH=/opt/codex-venv/bin:" in run_line
	assert "--env PREPUSH_TEST_GATE_LARGE_FILE_TESTS=7" in run_line
	assert "--shards 3" in run_line
	for flag in ("--network none", "--read-only", "--cap-drop ALL"):
		assert flag in run_line


def test_wrapper_drops_invalid_shards_and_large_file_values(tmp_path):
	worktree = _worktree(tmp_path)
	env, log = _wrapper_env(tmp_path)
	out = tmp_path / "out"
	base = _git(worktree, "rev-parse", "HEAD~1")
	proc = subprocess.run(["bash", str(WRAPPER), "--worktree", str(worktree), "--module", str(MODULE_PATH), "--image", "gate-image:test",
		"--base", base, "--out-dir", str(out), "--shards", "4;rm -rf /", "--large-file-tests", "0", "--venv", str(tmp_path / "missing")],
		capture_output=True, text=True, env=env, timeout=120)
	assert proc.returncode == 0, proc.stderr
	assert "name=PREPUSH_TEST_GATE_SHARDS" in proc.stderr and "name=PREPUSH_TEST_GATE_LARGE_FILE_TESTS" in proc.stderr
	assert "reason=venv_missing" in proc.stderr
	run_line = next(line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run "))
	assert "--env" not in run_line and "--shards" not in run_line and "/opt/codex-venv" not in run_line


def test_wrapper_mounts_node_modules_read_only_on_both_trees(tmp_path):
	worktree = _worktree(tmp_path)
	node_modules = tmp_path / "prep" / "node_modules"
	node_modules.mkdir(parents=True)
	env, log = _wrapper_env(tmp_path)
	base = _git(worktree, "rev-parse", "HEAD~1")
	proc = subprocess.run(["bash", str(WRAPPER), "--worktree", str(worktree), "--module", str(MODULE_PATH), "--image", "gate-image:test",
		"--base", base, "--out-dir", str(tmp_path / "out"), "--node-modules", str(node_modules)], capture_output=True, text=True, env=env, timeout=120)
	assert proc.returncode == 0, proc.stderr
	run_line = next(line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run "))
	assert f"src={node_modules.resolve()},dst=/source/node_modules,readonly" in run_line
	assert f"src={node_modules.resolve()},dst=/baseline/node_modules,readonly" in run_line
