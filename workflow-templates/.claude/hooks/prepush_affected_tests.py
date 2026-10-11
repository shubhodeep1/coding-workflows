#!/usr/bin/env python3
"""Pre-push affected-test gate (issue #7137, plan phase P1).

Finds the tests and registries a change affects, runs them, classifies
failures against the merge-base, and records the result. One stdlib-only file
serves the interactive session and, from plan phases P3-P5 on, every AI push
path (through scripts/prepush_test_gate_run.sh, which runs this module inside
a credential-free container from the trusted support checkout).

Subcommands
-----------
``select``          print the affected tests, companion files, registry checks
                    and static checks as JSON.
``run``             select, run every unit (sharded, time-boxed), re-run the
                    failures on ``--baseline-tree`` and write the result JSON
                    plus a Markdown summary.
``assertion-diff``  list assertion lines removed from pre-existing test files.
``stamp``           interactive: run the gate on the committed HEAD against
                    its merge-base with origin and write a pass/fail stamp
                    under ``${TMPDIR:-/tmp}/claude-prepush-gate/``.
``hook``            PreToolUse hook: ask for confirmation when a Bash command
                    pushes and no passing stamp exists for HEAD. It never runs
                    tests or repository code, and fails open with a warning.

Environment (each has a default here, §8): ``PREPUSH_TEST_GATE_TIMEOUT_MINUTES``
(15), ``PREPUSH_TEST_GATE_SHARDS`` (4), ``PREPUSH_TEST_GATE_LARGE_FILE_TESTS``
(50), ``CLAUDE_PREPUSH_TEST_GATE`` (unset = on, ``off`` disables the hook).
The module makes no GitHub API call and never writes into the repository or
its ``.git``. Log prefix: ``PREPUSH_TEST_GATE``.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import datetime
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

LOG_PREFIX = "PREPUSH_TEST_GATE"
RESULT_SCHEMA = "prepush_test_gate.v1"
STOP_STEMS = frozenset({
	"index", "main", "utils", "util", "common", "config", "setup", "__init__", "conftest", "test", "tests",
})
MIN_STEM_LENGTH = 4
TAIL_BYTES = 4096
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_HOOK_FAILURES_LISTED = 10
DEFAULT_TIMEOUT_MINUTES = 15
DEFAULT_SHARDS = 4
DEFAULT_LARGE_FILE_TESTS = 50
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"})
CI_WORKFLOW = ".github/workflows/ci.yml"
# Inverse of scripts/workflow_failure_heal.py HEAL_SCOPE_GUARD_TEST_SUBJECTS for
# ci.yml. Copied, not imported: consumers receive .claude/ but not scripts/.
# tests/test_prepush_affected_tests.py pins the copy against the original.
CI_GUARD_TESTS = (
	"tests/test_ci_job_split_contract.py",
	"tests/test_ci_inventory_parity_order_contract.py",
	"tests/test_ci_shared_shell_block_guard.py",
	"tests/test_ci_poll_test_sharding.py",
)
WIRING_GUARD = "tests/test_ci_wires_every_test_file.py"
INVENTORY_GUARD = "tests/inventory_parity.py"
LIVE_PARITY_GUARD = "tests/test_claude_template_live_parity.py"
SIZE_GUARD = "tests/test_workflow_file_size_limit.py"
AGENTS_ASSEMBLER = "scripts/assemble_agents.py"
STAMP_DIR_NAME = "claude-prepush-gate"
STAMP_COMMAND = "python3 .claude/hooks/prepush_affected_tests.py stamp"
PASSING_STAMP_STATUSES = frozenset({"pass", "unverified", "skipped"})
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")

_TEST_JS_RE = re.compile(r".+\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)$")
_ASSERTION_RE = re.compile(r"\bassert\b|self\.assert\w*\(|\bexpect\(|pytest\.raises|\bxfail\b|\bskip\b")
_SKIP_ADDED_RE = re.compile(r"pytest\.mark\.(?:skip|xfail)|unittest\.skip|\bxit\(|\b(?:it|test|describe)\.skip\(|\.skip\(")
_HUNK_FUNC_RE = re.compile(r"^@@[^@]*@@\s*(?:async\s+)?(?:def|function|class)?\s*([A-Za-z_][\w]*)")
_DEF_LINE_RE = re.compile(r"^[+-](?![+-])\s*(?:async\s+)?def\s+([A-Za-z_]\w*)|^[+-](?![+-])\s*function\s+([A-Za-z_]\w*)|^[+-](?![+-])\s*([A-Za-z_]\w*)\s*\(\)\s*\{?\s*$")
_FAILED_RE = re.compile(r"\bFAILED\s+(\S+::\S+)")
_FAIL_RUNNER_RE = re.compile(r"^\s*FAIL\s{2,}([A-Za-z_]\w*)")
_UNITTEST_RE = re.compile(r"^(?:FAIL|ERROR):\s+([A-Za-z_]\w*)\s+\(")
_EVENT_PREFIX = "TEST_CASE_EVENT:"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def log(**fields: object) -> None:
	parts = " ".join(f"{key}={str(value).replace(' ', '_')}" for key, value in fields.items())
	print(f"{LOG_PREFIX} {parts}", file=sys.stderr)


def _int_env(name: str, default: int, low: int, high: int) -> int:
	raw = os.environ.get(name, "").strip()
	if not raw:
		return default
	try:
		value = int(raw)
	except ValueError:
		value = low - 1
	if value < low or value > high:
		log(phase="config", outcome="config_invalid", name=name, fallback=default)
		return default
	return value


def gate_disabled() -> bool:
	return os.environ.get("CLAUDE_PREPUSH_TEST_GATE", "").strip().lower() == "off"


def _git(repo: Path, *args: str, check: bool = False, text: bool = True) -> subprocess.CompletedProcess:
	return subprocess.run(["git", *GIT_SAFE, *args], cwd=repo, capture_output=True, text=text, check=check)


# ---------------------------------------------------------------------------
# Repository index and change parsing
# ---------------------------------------------------------------------------

def list_repo_files(repo: Path) -> list[str]:
	proc = None
	try:
		proc = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
	except OSError:
		proc = None
	if proc is not None and proc.returncode == 0:
		return sorted({p for p in proc.stdout.split("\0") if p and (repo / p).is_file()})
	files: list[str] = []
	for dirpath, dirnames, filenames in os.walk(repo):
		dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
		for filename in filenames:
			full = Path(dirpath) / filename
			if full.is_file() and not full.is_symlink():
				files.append(full.relative_to(repo).as_posix())
	return sorted(files)


def is_test_file(path: str) -> bool:
	p = PurePosixPath(path)
	if "node_modules" in p.parts:
		return False
	name = p.name
	if "__tests__" in p.parts[:-1]:
		return True
	if name.startswith("test_") and name.endswith(".py"):
		return True
	if name.endswith("_test.py"):
		return True
	if p.parts and p.parts[0] == "tests" and name.startswith("test_") and name.endswith(".sh"):
		return True
	return bool(_TEST_JS_RE.fullmatch(name))


def parse_changes(lines: list[str]) -> dict[str, str]:
	"""Parse ``git diff --name-status`` style lines or plain paths into {path: status}."""
	changes: dict[str, str] = {}
	for raw in lines:
		line = raw.rstrip("\n").rstrip("\r")
		if not line.strip():
			continue
		parts = line.split("\t")
		if len(parts) >= 2 and re.fullmatch(r"[ACDMRTU]\d*", parts[0]):
			status = parts[0][0]
			if status in ("R", "C") and len(parts) >= 3:
				if status == "R":
					changes.setdefault(parts[1], "D")
				changes[parts[2]] = "R" if status == "R" else "A"
				continue
			changes[parts[1]] = status
			continue
		changes[line.strip()] = ""
	return changes


def _resolve_statuses(repo: Path, changes: dict[str, str], planned: bool) -> dict[str, str]:
	resolved: dict[str, str] = {}
	for path, status in changes.items():
		if status:
			resolved[path] = status
		elif not (repo / path).exists():
			resolved[path] = "A" if planned else "D"
		else:
			resolved[path] = "M"
	return resolved


def changes_from_base(repo: Path, base: str, include_untracked: bool = True) -> tuple[dict[str, str], str]:
	diff = _git(repo, "diff", "--name-status", "-M", f"{base}...HEAD")
	if diff.returncode != 0:
		raise RuntimeError("git diff against base failed")
	changes = parse_changes(diff.stdout.splitlines())
	if include_untracked:
		others = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
		for path in others.stdout.split("\0"):
			if path:
				changes[path] = "A"
	hunks = _git(repo, "diff", "-U0", f"{base}...HEAD")
	return changes, hunks.stdout if hunks.returncode == 0 else ""


def changed_function_names(diff_text: str) -> set[str]:
	names: set[str] = set()
	for line in diff_text.splitlines():
		if line.startswith("@@"):
			match = _HUNK_FUNC_RE.match(line)
			if match and len(match.group(1)) >= MIN_STEM_LENGTH:
				names.add(match.group(1))
			continue
		match = _DEF_LINE_RE.match(line)
		if match:
			name = next(group for group in match.groups() if group)
			if len(name) >= MIN_STEM_LENGTH:
				names.add(name)
	return names


# ---------------------------------------------------------------------------
# Selection (rules 1-6)
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
	try:
		if path.stat().st_size > MAX_SCAN_BYTES:
			return ""
		return path.read_text(encoding="utf-8", errors="ignore")
	except OSError:
		return ""


def _naming_names(path: str) -> set[str]:
	stem = PurePosixPath(path).stem
	return {stem, stem.replace("-", "_")}


def _naming_matches(path: str, test_file: str) -> bool:
	p = PurePosixPath(path)
	t = PurePosixPath(test_file)
	parent = p.parent.as_posix()
	dirs = {parent, "tests", (p.parent / "tests").as_posix(), (p.parent / "__tests__").as_posix()}
	if t.parent.as_posix() not in dirs:
		return False
	for name in _naming_names(path):
		escaped = re.escape(name)
		if re.fullmatch(rf"test_{escaped}\.[^.]+|{escaped}_test\.[^.]+|{escaped}\.(?:test|spec)\.[^.]+", t.name):
			return True
	return False


def _reference_patterns(path: str) -> list[tuple[str, re.Pattern]]:
	p = PurePosixPath(path)
	patterns = [("path_text", re.compile(re.escape(path)))]
	if p.name and p.name != path:
		patterns.append(("basename_text", re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(p.name)}(?![A-Za-z0-9_])")))
	stem = p.stem
	if len(stem) >= MIN_STEM_LENGTH and stem.lower() not in STOP_STEMS and stem != p.name:
		patterns.append(("stem_text", re.compile(rf"(?<![A-Za-z0-9_]){re.escape(stem)}(?![A-Za-z0-9_])")))
	return patterns


def _python_test_functions(path: Path) -> list[tuple[str, str]]:
	"""(qualified name, source) for each test function, or [] when unparsable."""
	text = _read_text(path)
	try:
		tree = ast.parse(text)
	except (SyntaxError, ValueError):
		return []
	# Slice the source by line numbers once; ast.get_source_segment re-splits
	# the whole file per call, which is quadratic on large test modules.
	lines = text.splitlines()

	def source(node: ast.AST) -> str:
		start = getattr(node, "lineno", 1) - 1
		end = getattr(node, "end_lineno", None) or start + 1
		return "\n".join(lines[start:end])

	found: list[tuple[str, str]] = []
	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
			found.append((node.name, source(node)))
		elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
			for item in node.body:
				if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
					found.append((f"{node.name}::{item.name}", source(item)))
	return found


def _has_make_target(repo: Path, target: str) -> bool:
	makefile = repo / "Makefile"
	if not makefile.is_file():
		return False
	return re.search(rf"^{re.escape(target)}\s*:", _read_text(makefile), re.MULTILINE) is not None


def select_tests(
	repo: Path,
	changes: dict[str, str],
	planned: bool = False,
	function_names: set[str] | None = None,
	large_file_tests: int | None = None,
) -> dict:
	repo = repo.resolve()
	changes = _resolve_statuses(repo, changes, planned)
	large_limit = large_file_tests if large_file_tests is not None else _int_env(
		"PREPUSH_TEST_GATE_LARGE_FILE_TESTS", DEFAULT_LARGE_FILE_TESTS, 1, 100000)
	files = list_repo_files(repo)
	file_set = set(files)
	test_files = [f for f in files if is_test_file(f)]
	selected: dict[str, list[str]] = {}

	def add(path: str, reason: str) -> None:
		reasons = selected.setdefault(path, [])
		if reason not in reasons:
			reasons.append(reason)

	present_changes = [p for p, s in changes.items() if s != "D" and (repo / p).is_file()]
	# Rule 1: changed test files themselves.
	for path in present_changes:
		if is_test_file(path):
			add(path, "changed_test")
	non_test_changes = [p for p in changes if not is_test_file(p)]
	# Rule 2: naming convention (the reverse of heal_scope_test_subjects).
	for path in non_test_changes:
		for test_file in test_files:
			if _naming_matches(path, test_file):
				add(test_file, "naming")
	# Rule 3: text references, with one level of test-helper indirection.
	patterns = {path: _reference_patterns(path) for path in changes}
	texts: dict[str, str] = {}

	def text_of(path: str) -> str:
		if path not in texts:
			texts[path] = _read_text(repo / path)
		return texts[path]

	for test_file in test_files:
		text = text_of(test_file)
		if not text:
			continue
		for path, plist in patterns.items():
			if path == test_file:
				continue
			for reason, pattern in plist:
				if pattern.search(text):
					add(test_file, reason)
					break
	test_dirs = {PurePosixPath(t).parent.as_posix() for t in test_files}
	for helper in files:
		hp = PurePosixPath(helper)
		if hp.suffix != ".py" or is_test_file(helper) or hp.name in ("__init__.py", "conftest.py"):
			continue
		if hp.parent.as_posix() not in test_dirs:
			continue
		matched = helper in changes
		if not matched:
			helper_text = text_of(helper)
			matched = any(
				pattern.search(helper_text)
				for path, plist in patterns.items() if path != helper
				for _reason, pattern in plist
			)
		if not matched:
			continue
		module = re.escape(hp.stem)
		import_re = re.compile(rf"^\s*(?:from\s+{module}\s+import\b|import\s+{module}\b)", re.MULTILINE)
		for test_file in test_files:
			if PurePosixPath(test_file).parent == hp.parent and import_re.search(text_of(test_file)):
				add(test_file, f"helper:{hp.stem}")
	# Rule 4: registry guards, only when the guard exists in this repository.
	registry_checks: list[dict] = []
	structural = {p for p, s in changes.items() if s in ("A", "D", "R")}
	workflow_changed = any(PurePosixPath(p).parent.as_posix() == ".github/workflows" and PurePosixPath(p).suffix in (".yml", ".yaml") for p in changes)
	if WIRING_GUARD in file_set and (workflow_changed or any(
		PurePosixPath(p).parts[:1] == ("tests",) and PurePosixPath(p).name.startswith("test_") for p in structural if changes[p] != "D"
	)):
		add(WIRING_GUARD, "registry:ci_test_wiring")
	if INVENTORY_GUARD in file_set and ("docs/INVENTORY.md" in changes or any(
		p.startswith("scripts/") or p.startswith(".github/workflows/") for p in structural
	)):
		add(INVENTORY_GUARD, "registry:inventory_parity")
	if LIVE_PARITY_GUARD in file_set and any(p.startswith(".claude/") or p.startswith("workflow-templates/.claude/") for p in changes):
		add(LIVE_PARITY_GUARD, "registry:claude_live_parity")
	if SIZE_GUARD in file_set and workflow_changed:
		add(SIZE_GUARD, "registry:workflow_file_size")
	if structural and _has_make_target(repo, "generate-check"):
		registry_checks.append({"check": "generate-check", "argv": ["make", "generate-check"]})
	if AGENTS_ASSEMBLER in file_set and any(p == "agents.md" or p.startswith("agents.d/") for p in changes):
		registry_checks.append({"check": "assemble_agents", "argv": ["python3", AGENTS_ASSEMBLER, "check", "--repo-root", "."]})
	if CI_WORKFLOW in changes:
		for guard in CI_GUARD_TESTS:
			if guard in file_set:
				add(guard, "guard_inverse")
	# Rule 5: static checks on changed files only.
	static_checks: list[dict] = []
	py_changed = sorted(p for p in present_changes if p.endswith(".py"))
	sh_changed = sorted(p for p in present_changes if p.endswith(".sh"))
	if py_changed:
		static_checks.append({"tool": "ruff", "files": py_changed, "argv": ["ruff", "check", "--select", "E,F", "--ignore", "E501", *py_changed]})
	if sh_changed:
		static_checks.append({"tool": "shellcheck", "files": sh_changed, "argv": ["shellcheck", "--severity=error", *sh_changed]})
	# Rule 6: large Python test files keep only the functions that mention a change.
	needles = set(function_names or ()) | {PurePosixPath(p).name for p in changes}
	affected: list[dict] = []
	for path in sorted(selected):
		entry: dict = {"path": path, "reasons": selected[path]}
		if path.endswith(".py") and "changed_test" not in selected[path]:
			functions = _python_test_functions(repo / path)
			if len(functions) > large_limit:
				word_res = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])") for n in sorted(needles) if n]
				keep = [name for name, source in functions if any(w.search(source) for w in word_res)]
				if keep:
					entry["functions"] = keep
		affected.append(entry)
	companions: list[str] = []
	if planned:
		for path, status in changes.items():
			if status != "A":
				continue
			if path.startswith("scripts/") and INVENTORY_GUARD in file_set and "docs/INVENTORY.md" not in companions:
				companions.append("docs/INVENTORY.md")
			name = PurePosixPath(path).name
			if PurePosixPath(path).parts[:1] == ("tests",) and name.startswith("test_") and WIRING_GUARD in file_set and CI_WORKFLOW not in companions:
				companions.append(CI_WORKFLOW)
		companions = [c for c in companions if c not in changes]
	return {
		"changed_files": sorted(changes),
		"affected_tests": affected,
		"companion_files": companions,
		"registry_checks": registry_checks,
		"static_checks": static_checks,
	}


# ---------------------------------------------------------------------------
# Test command resolution (Q20)
# ---------------------------------------------------------------------------

def _validate_override(repo: Path) -> str | None:
	path = repo / ".ai" / "validate.yml"
	if not path.is_file():
		return None
	text = _read_text(path)
	try:
		import yaml  # type: ignore[import-not-found]
	except ImportError:
		yaml = None
	if yaml is not None:
		try:
			data = yaml.safe_load(text)
		except Exception:  # noqa: BLE001 - malformed consumer config falls back
			data = None
		if isinstance(data, dict):
			value = data.get("prepush_test_command")
			return value.strip() if isinstance(value, str) and value.strip() else None
	match = re.search(r"^prepush_test_command:\s*(.+?)\s*$", text, re.MULTILINE)
	if not match:
		return None
	value = match.group(1)
	if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
		value = value[1:-1]
	return value.strip() or None


def _workflow_text(repo: Path) -> str:
	directory = repo / ".github" / "workflows"
	if not directory.is_dir():
		return ""
	chunks = []
	for path in sorted(directory.glob("*.y*ml")):
		chunks.append(_read_text(path).replace("\\\n", " "))
	return "\n".join(chunks)


def _workflow_style(workflows: str, test_file: str) -> str | None:
	file_re = re.compile(rf"(?<![\w./-]){re.escape(test_file)}(?![\w.-])")
	script_re = re.compile(rf"\bpython3?\s+(?:-[A-Za-z]+\s+)*{re.escape(test_file)}(?![\w.-])")
	style = None
	for line in workflows.splitlines():
		if not file_re.search(line):
			continue
		if "pytest" in line:
			return "pytest"
		if script_re.search(line):
			style = "script"
	return style


def _pytest_configured(repo: Path) -> bool:
	if any((repo / name).is_file() for name in ("pytest.ini", "conftest.py", "tests/conftest.py")):
		return True
	pyproject = repo / "pyproject.toml"
	return pyproject.is_file() and "[tool.pytest.ini_options]" in _read_text(pyproject)


def _node_runner(repo: Path) -> str | None:
	package = repo / "package.json"
	if not package.is_file():
		return None
	try:
		data = json.loads(_read_text(package))
	except ValueError:
		return None
	script = data.get("scripts", {}).get("test") if isinstance(data, dict) and isinstance(data.get("scripts"), dict) else None
	if not isinstance(script, str):
		return None
	for runner in ("jest", "vitest", "mocha"):
		if re.search(rf"\b{runner}\b", script):
			return runner
	return "npm"


def plan_units(repo: Path, selection: dict) -> tuple[list[dict], list[dict]]:
	"""Turn a selection into runnable units; unresolvable items become unverified."""
	units: list[dict] = []
	unverified: list[dict] = []
	affected = selection.get("affected_tests", [])
	override = _validate_override(repo)
	workflows = _workflow_text(repo)
	pytest_ok = _pytest_configured(repo)
	node = _node_runner(repo)
	whole_added: set[str] = set()
	if override is not None:
		if affected or "{files}" not in override:
			files = " ".join(shlex.quote(entry["path"]) for entry in affected)
			command = override.replace("{files}", files)
			units.append({"id": "prepush_test_command", "file": None, "argv": ["bash", "-c", command], "kind": "test"})
		affected = []
	for entry in affected:
		path = entry["path"]
		names = entry.get("functions") or []
		if path.endswith(".py"):
			style = _workflow_style(workflows, path)
			if style == "script":
				short = [name.split("::")[-1] for name in names]
				units.append({"id": path, "file": path, "argv": ["python3", path, *short], "kind": "test"})
			elif style == "pytest" or pytest_ok:
				targets = [f"{path}::{name}" for name in names] or [path]
				units.append({"id": path, "file": path, "argv": ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets], "kind": "test"})
			else:
				units.append({"id": path, "file": path, "argv": ["python3", path], "kind": "test"})
		elif path.endswith(".sh"):
			units.append({"id": path, "file": path, "argv": ["bash", path], "kind": "test"})
		elif re.search(r"\.(?:js|jsx|ts|tsx|mjs|cjs)$", path) and node:
			if node == "jest":
				units.append({"id": path, "file": path, "argv": ["npx", "--no-install", "jest", path], "kind": "test"})
			elif node == "vitest":
				units.append({"id": path, "file": path, "argv": ["npx", "--no-install", "vitest", "run", path], "kind": "test"})
			elif node == "mocha":
				units.append({"id": path, "file": path, "argv": ["npx", "--no-install", "mocha", path], "kind": "test"})
			elif "npm test" not in whole_added:
				whole_added.add("npm test")
				units.append({"id": "npm test", "file": None, "argv": ["npm", "test"], "kind": "test"})
		elif _has_make_target(repo, "test"):
			if "make test" not in whole_added:
				whole_added.add("make test")
				units.append({"id": "make test", "file": None, "argv": ["make", "test"], "kind": "test"})
		else:
			unverified.append({"item": path, "reason": "no_test_command"})
	for check in selection.get("registry_checks", []):
		units.append({"id": check["check"], "file": None, "argv": list(check["argv"]), "kind": "registry"})
	for check in selection.get("static_checks", []):
		if shutil.which(check["argv"][0]) is None:
			unverified.append({"item": check["tool"], "reason": "tool_unavailable"})
			continue
		units.append({"id": check["tool"], "file": None, "argv": list(check["argv"]), "kind": "static"})
	return units, unverified


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _child_env() -> dict[str, str]:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	# PYTHONSAFEPATH is deliberately not set: script-style test files import
	# their sibling helpers through the script directory, which safe-path would
	# drop from sys.path (CI runs them without it as well).
	return env


def _run_one(argv: list[str], cwd: Path, deadline: float) -> dict:
	remaining = deadline - time.monotonic()
	if remaining <= 0:
		return {"status": "timeout", "rc": None, "tail": ""}
	start = time.monotonic()
	try:
		proc = subprocess.Popen(
			argv, cwd=cwd, env=_child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			stdin=subprocess.DEVNULL, start_new_session=True,
		)
	except OSError:
		return {"status": "error", "rc": None, "tail": "", "reason": "command_unavailable"}
	try:
		output, _ = proc.communicate(timeout=remaining)
	except subprocess.TimeoutExpired:
		try:
			os.killpg(proc.pid, signal.SIGKILL)
		except OSError:
			pass
		output, _ = proc.communicate()
		return {"status": "timeout", "rc": None, "tail": output[-TAIL_BYTES:].decode("utf-8", "replace"), "elapsed": round(time.monotonic() - start, 2)}
	tail = output[-TAIL_BYTES:].decode("utf-8", "replace")
	return {"status": "pass" if proc.returncode == 0 else "fail", "rc": proc.returncode, "tail": tail, "elapsed": round(time.monotonic() - start, 2)}


def run_units(units: list[dict], cwd: Path, deadline: float, shards: int) -> list[dict]:
	results: list[dict] = [{} for _ in units]
	with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, shards)) as pool:
		futures = {pool.submit(_run_one, unit["argv"], cwd, deadline): index for index, unit in enumerate(units)}
		for future in concurrent.futures.as_completed(futures):
			index = futures[future]
			results[index] = {**units[index], **future.result()}
	return results


def _sanitize_name(text: str) -> str:
	cleaned = re.sub(r"[\x00-\x1f`<>]", "", text).strip()
	return cleaned[:200]


def failing_names(tail: str) -> list[str]:
	names: list[str] = []
	for line in tail.splitlines():
		line = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+", "", line)
		match = _FAILED_RE.search(line)
		if match:
			names.append(match.group(1))
			continue
		if line.startswith(_EVENT_PREFIX):
			try:
				event = json.loads(line[len(_EVENT_PREFIX):].strip())
			except ValueError:
				event = None
			if isinstance(event, dict) and event.get("status") == "fail" and isinstance(event.get("test_name"), str):
				names.append(event["test_name"])
			continue
		for regex in (_FAIL_RUNNER_RE, _UNITTEST_RE):
			match = regex.match(line)
			if match:
				names.append(match.group(1))
				break
	unique: list[str] = []
	for name in names:
		name = _sanitize_name(name)
		if name and name not in unique:
			unique.append(name)
	return unique[:50]


def classify_baseline(failed: list[dict], baseline: Path | None, deadline: float, shards: int) -> tuple[list[dict], list[dict]]:
	"""Split failed unit results into (pr_caused, inherited)."""
	if baseline is None or not baseline.is_dir():
		return failed, []
	rerun = [unit for unit in failed if unit.get("kind") != "static" and (unit.get("file") is None or (baseline / unit["file"]).is_file())]
	if not rerun:
		return failed, []
	reran = run_units(rerun, baseline, deadline, shards)
	inherited_ids = {unit["id"] for unit in reran if unit.get("status") == "fail"}
	pr_caused = [unit for unit in failed if unit["id"] not in inherited_ids]
	inherited = [unit for unit in failed if unit["id"] in inherited_ids]
	return pr_caused, inherited


def assertion_diff(repo: Path, baseline: Path, changed: list[str]) -> list[dict]:
	findings: list[dict] = []
	for path in sorted(changed):
		if not is_test_file(path):
			continue
		old_path = baseline / path
		if not old_path.is_file():
			continue
		new_lines = _read_text(repo / path).splitlines() if (repo / path).is_file() else []
		old_lines = _read_text(old_path).splitlines()
		for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
			if line.startswith("---") or line.startswith("+++"):
				continue
			if line.startswith("-") and _ASSERTION_RE.search(line[1:]):
				findings.append({"file": path, "kind": "removed_or_modified", "line": _sanitize_name(line[1:])})
			elif line.startswith("+") and _SKIP_ADDED_RE.search(line[1:]):
				findings.append({"file": path, "kind": "skip_added", "line": _sanitize_name(line[1:])})
	return findings


def run_gate(
	repo: Path,
	selection: dict,
	baseline: Path | None = None,
	timeout_minutes: int | None = None,
	shards: int | None = None,
) -> dict:
	repo = repo.resolve()
	timeout_minutes = timeout_minutes or _int_env("PREPUSH_TEST_GATE_TIMEOUT_MINUTES", DEFAULT_TIMEOUT_MINUTES, 1, 360)
	shards = shards or _int_env("PREPUSH_TEST_GATE_SHARDS", DEFAULT_SHARDS, 1, 64)
	start = time.monotonic()
	deadline = start + timeout_minutes * 60
	units, unverified = plan_units(repo, selection)
	results = run_units(units, repo, deadline, shards)
	ran = [r for r in results if r.get("status") in ("pass", "fail")]
	for r in results:
		if r.get("status") == "timeout":
			unverified.append({"item": r["id"], "reason": "timeout"})
		elif r.get("status") == "error":
			unverified.append({"item": r["id"], "reason": r.get("reason", "command_unavailable")})
	failed = [r for r in results if r.get("status") == "fail"]
	pr_caused, inherited = classify_baseline(failed, baseline, deadline, shards)
	changed_assertions = assertion_diff(repo, baseline, selection.get("changed_files", [])) if baseline is not None and baseline.is_dir() else []
	if pr_caused:
		status = "fail"
	elif not units and not unverified:
		status = "skipped"
	elif unverified:
		status = "unverified"
	else:
		status = "pass"
	result = {
		"schema": RESULT_SCHEMA,
		"status": status,
		"ran": [r["id"] for r in ran],
		"failures": [{"test": r["id"], "file": r.get("file"), "names": failing_names(r.get("tail", "")), "tail": r.get("tail", "")} for r in pr_caused],
		"inherited": [{"test": r["id"], "file": r.get("file"), "names": failing_names(r.get("tail", ""))} for r in inherited],
		"unverified": unverified,
		"changed_assertions": changed_assertions,
		"selected": len(selection.get("affected_tests", [])),
		"elapsed_secs": round(time.monotonic() - start, 2),
	}
	log(phase="run", outcome=status, selected=result["selected"], ran=len(ran), failures=len(pr_caused), inherited=len(inherited), unverified=len(unverified))
	return result


def _neutralise(text: str) -> str:
	return "\n".join((" " + line) if line.lstrip().startswith("::") else line for line in text.splitlines())


def write_summary(result: dict) -> str:
	lines = [
		"## Pre-push test gate",
		"",
		f"- Status: **{result.get('status', 'unverified')}**",
		f"- Tests and checks run: {len(result.get('ran', []))} (selected test files: {result.get('selected', 0)})",
		f"- Failures caused by this change: {len(result.get('failures', []))}",
		f"- Inherited failures (also failing on the merge-base): {len(result.get('inherited', []))}",
		f"- Unverified: {len(result.get('unverified', []))}",
	]
	for title, key in (("Failures caused by this change", "failures"), ("Inherited failures", "inherited")):
		entries = result.get(key) or []
		if entries:
			lines += ["", f"### {title}", ""]
			for entry in entries:
				names = ", ".join(f"`{_sanitize_name(n)}`" for n in entry.get("names", [])[:10])
				lines.append(f"- `{_sanitize_name(str(entry.get('test')))}`" + (f": {names}" if names else ""))
	if result.get("unverified"):
		lines += ["", "### Unverified", ""]
		lines += [f"- `{_sanitize_name(str(u.get('item')))}`: {_sanitize_name(str(u.get('reason')))}" for u in result["unverified"]]
	if result.get("changed_assertions"):
		lines += ["", "### Changed pre-existing assertions", ""]
		lines += [f"- `{_sanitize_name(str(a.get('file')))}` ({a.get('kind')}): `{_sanitize_name(str(a.get('line')))}`" for a in result["changed_assertions"]]
	return _neutralise("\n".join(lines)) + "\n"


# ---------------------------------------------------------------------------
# Interactive stamp and hook
# ---------------------------------------------------------------------------

def _toplevel(cwd: Path) -> Path | None:
	proc = _git(cwd, "rev-parse", "--show-toplevel")
	return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else None


def _head(repo: Path) -> str | None:
	proc = _git(repo, "rev-parse", "HEAD")
	value = proc.stdout.strip() if proc.returncode == 0 else ""
	return value if _SHA_RE.match(value) else None


def stamp_path(toplevel: Path, head: str) -> Path:
	digest = hashlib.sha256(str(toplevel).encode("utf-8")).hexdigest()[:16]
	base = Path(os.environ.get("TMPDIR") or "/tmp")
	return base / STAMP_DIR_NAME / digest / f"{head}.json"


def _inside(path: Path, root: Path) -> bool:
	try:
		path.resolve().relative_to(root.resolve())
		return True
	except ValueError:
		return False


def _merge_base(repo: Path) -> str | None:
	for ref in ("origin/HEAD", "origin/main", "origin/master"):
		if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
			continue
		proc = _git(repo, "merge-base", "HEAD", ref)
		value = proc.stdout.strip()
		if proc.returncode == 0 and _SHA_RE.match(value):
			return value
	return None


def export_tree(repo: Path, commit: str, dest: Path) -> None:
	# Stream the archive (a large repository would not fit comfortably in
	# memory) and extract only plain files and directories inside dest.
	extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
	proc = subprocess.Popen(["git", *GIT_SAFE, "archive", "--format=tar", commit], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
	try:
		with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
			for member in archive:
				name = PurePosixPath(member.name)
				if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
					continue
				archive.extract(member, dest, **extract_kwargs)
	finally:
		if proc.stdout is not None:
			proc.stdout.close()
		returncode = proc.wait()
	if returncode != 0:
		raise subprocess.CalledProcessError(returncode, "git archive")


def write_stamp(path: Path, payload: dict, toplevel: Path) -> None:
	git_dir_proc = _git(toplevel, "rev-parse", "--absolute-git-dir")
	git_dir = Path(git_dir_proc.stdout.strip()) if git_dir_proc.returncode == 0 and git_dir_proc.stdout.strip() else toplevel / ".git"
	if _inside(path, toplevel) or _inside(path, git_dir):
		raise ValueError("stamp path resolves inside the repository")
	path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	os.chmod(path.parent, 0o700)
	fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".stamp-", suffix=".json")
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump(payload, handle, indent=2, sort_keys=True)
		os.chmod(tmp, 0o600)
		os.replace(tmp, path)
	except BaseException:
		try:
			os.unlink(tmp)
		except OSError:
			pass
		raise


def cmd_stamp(cwd: Path) -> int:
	toplevel = _toplevel(cwd)
	if toplevel is None:
		print("prepush gate: not inside a git repository", file=sys.stderr)
		return 1
	dirty = _git(toplevel, "status", "--porcelain", "--untracked-files=no")
	if dirty.returncode != 0 or dirty.stdout.strip():
		print("prepush gate: commit or stash tracked changes first; the stamp is keyed to the committed HEAD.", file=sys.stderr)
		return 2
	head = _head(toplevel)
	if head is None:
		print("prepush gate: HEAD does not resolve", file=sys.stderr)
		return 1
	merge_base = _merge_base(toplevel)
	created = datetime.datetime.now(datetime.timezone.utc).isoformat()
	toplevel_hash = hashlib.sha256(str(toplevel).encode("utf-8")).hexdigest()[:16]
	if merge_base is None:
		result = {"schema": RESULT_SCHEMA, "status": "unverified", "ran": [], "failures": [], "inherited": [],
			"unverified": [{"item": "merge_base", "reason": "no_merge_base"}], "changed_assertions": [], "selected": 0, "elapsed_secs": 0}
	else:
		changes, diff_text = changes_from_base(toplevel, merge_base, include_untracked=False)
		selection = select_tests(toplevel, changes, function_names=changed_function_names(diff_text))
		with tempfile.TemporaryDirectory(prefix="prepush-baseline-") as baseline_dir:
			baseline = Path(baseline_dir)
			try:
				export_tree(toplevel, merge_base, baseline)
			except (OSError, subprocess.CalledProcessError, tarfile.TarError):
				baseline = None  # type: ignore[assignment]
			result = run_gate(toplevel, selection, baseline)
	payload = {**result, "head": head, "merge_base": merge_base, "toplevel_hash": toplevel_hash, "created_at": created}
	path = stamp_path(toplevel, head)
	try:
		write_stamp(path, payload, toplevel)
	except (OSError, ValueError) as exc:
		print(f"prepush gate: could not write the stamp: {exc}", file=sys.stderr)
		return 1
	sys.stdout.write(write_summary(result))
	print(f"Stamp: {path}")
	return 1 if result["status"] == "fail" else 0


def _load_guard_module():
	guard = Path(__file__).resolve().parent / "pr_merge_status_guard.py"
	spec = importlib.util.spec_from_file_location("prepush_pr_merge_status_guard", guard)
	if spec is None or spec.loader is None:
		raise ImportError("guard module unavailable")
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	try:
		spec.loader.exec_module(module)
	except BaseException:
		sys.modules.pop(spec.name, None)
		raise
	return module


_FALLBACK_PUSH_RE = re.compile(r"(?<![\w-])git\b[^;&|\n]*\bpush\b")


def detect_pushes(command: str, cwd: str) -> tuple[list[str], str]:
	"""Return (directories of the push invocations, warning)."""
	try:
		guard = _load_guard_module()
		invocations = guard._guarded_git_invocations(command, cwd)
	except Exception:  # noqa: BLE001 - fall back to a conservative check
		if _FALLBACK_PUSH_RE.search(command):
			return [cwd], "prepush gate: could not load pr_merge_status_guard.py; used a conservative push check."
		return [], ""
	dirs = []
	for invocation in invocations:
		if getattr(invocation, "subcommand", "") != "push":
			continue
		directory = getattr(invocation, "cwd", None)
		dirs.append(directory if isinstance(directory, str) and os.path.isdir(directory) else cwd)
	return dirs, ""


def _emit(ask_reason: str = "", system_message: str = "") -> None:
	response: dict = {}
	if system_message:
		response["systemMessage"] = system_message
	if ask_reason:
		response["hookSpecificOutput"] = {
			"hookEventName": "PreToolUse",
			"permissionDecision": "ask",
			"permissionDecisionReason": ask_reason,
		}
	if response:
		print(json.dumps(response))


def evaluate_hook(payload: object) -> tuple[str, str]:
	"""(ask reason, system message); both empty means no decision."""
	if not isinstance(payload, dict):
		return "", "prepush gate: hook payload is not a JSON object; push not checked."
	if payload.get("tool_name") != "Bash" or gate_disabled():
		return "", ""
	tool_input = payload.get("tool_input")
	command = tool_input.get("command") if isinstance(tool_input, dict) else None
	if not isinstance(command, str) or not command.strip():
		return "", ""
	cwd = payload.get("cwd")
	if not isinstance(cwd, str) or not os.path.isdir(cwd):
		cwd = os.getcwd()
	directories, warning = detect_pushes(command, cwd)
	if not directories:
		return "", warning
	reasons = []
	for directory in dict.fromkeys(directories):
		toplevel = _toplevel(Path(directory))
		head = _head(toplevel) if toplevel else None
		if toplevel is None or head is None:
			continue
		path = stamp_path(toplevel, head)
		try:
			stamp = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
		except (OSError, ValueError):
			stamp = None
		if not isinstance(stamp, dict) or stamp.get("head") != head:
			reasons.append(
				f"No pre-push test stamp for HEAD {head[:12]} in {toplevel}. Run `{STAMP_COMMAND}` after committing, "
				"fix failures this change caused, then push. Set CLAUDE_PREPUSH_TEST_GATE=off to disable this check."
			)
			continue
		status = stamp.get("status")
		if status in PASSING_STAMP_STATUSES:
			continue
		failures = stamp.get("failures") if isinstance(stamp.get("failures"), list) else []
		listed = ", ".join(_sanitize_name(str(f.get("test"))) for f in failures[:MAX_HOOK_FAILURES_LISTED] if isinstance(f, dict))
		reasons.append(
			f"The pre-push test stamp for HEAD {head[:12]} records {len(failures)} failure(s) this change caused"
			+ (f": {listed}" if listed else "") + ". Fix them and re-run the stamp, or confirm to push anyway."
		)
	return "\n\n".join(reasons), warning


def cmd_hook(raw: str) -> int:
	try:
		payload = json.loads(raw) if raw.strip() else {}
	except ValueError:
		_emit(system_message="prepush gate: hook payload is not valid JSON; push not checked.")
		return 0
	try:
		ask, message = evaluate_hook(payload)
	except Exception as exc:  # noqa: BLE001 - the hook fails open
		_emit(system_message=f"prepush gate: internal error ({type(exc).__name__}); push not checked.")
		return 0
	_emit(ask, message)
	return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _selection_from_args(args: argparse.Namespace) -> dict:
	repo = Path(args.repo_root).resolve()
	if args.base:
		changes, diff_text = changes_from_base(repo, args.base)
		return select_tests(repo, changes, function_names=changed_function_names(diff_text))
	source = args.paths_file or args.changed_files_file
	lines = Path(source).read_text(encoding="utf-8").splitlines()
	return select_tests(repo, parse_changes(lines), planned=bool(args.paths_file))


def _add_inputs(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--repo-root", default=".")
	group = parser.add_mutually_exclusive_group(required=True)
	group.add_argument("--changed-files-file")
	group.add_argument("--paths-file")
	group.add_argument("--base")


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(prog="prepush_affected_tests.py", description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	select_parser = sub.add_parser("select")
	_add_inputs(select_parser)
	select_parser.add_argument("--out")
	run_parser = sub.add_parser("run")
	_add_inputs(run_parser)
	run_parser.add_argument("--baseline-tree")
	run_parser.add_argument("--timeout-minutes", type=int)
	run_parser.add_argument("--shards", type=int)
	run_parser.add_argument("--out")
	run_parser.add_argument("--summary")
	diff_parser = sub.add_parser("assertion-diff")
	diff_parser.add_argument("--repo-root", default=".")
	diff_parser.add_argument("--baseline-tree", required=True)
	diff_parser.add_argument("--changed-files-file", required=True)
	sub.add_parser("stamp")
	sub.add_parser("hook")
	args = parser.parse_args(argv)
	if args.command == "hook":
		try:
			raw = sys.stdin.read()
		except (OSError, ValueError):
			raw = ""
		return cmd_hook(raw)
	if args.command == "stamp":
		return cmd_stamp(Path.cwd())
	if args.command == "assertion-diff":
		changed = list(parse_changes(Path(args.changed_files_file).read_text(encoding="utf-8").splitlines()))
		print(json.dumps(assertion_diff(Path(args.repo_root).resolve(), Path(args.baseline_tree), changed), indent=2))
		return 0
	selection = _selection_from_args(args)
	if args.command == "select":
		text = json.dumps(selection, indent=2)
		if args.out:
			Path(args.out).write_text(text + "\n", encoding="utf-8")
		else:
			print(text)
		return 0
	timeout = args.timeout_minutes if args.timeout_minutes and 0 < args.timeout_minutes <= 360 else None
	shards = args.shards if args.shards and 0 < args.shards <= 64 else None
	baseline = Path(args.baseline_tree) if args.baseline_tree else None
	result = run_gate(Path(args.repo_root), selection, baseline, timeout, shards)
	text = json.dumps(result, indent=2)
	if args.out:
		Path(args.out).write_text(text + "\n", encoding="utf-8")
	else:
		print(text)
	if args.summary:
		Path(args.summary).write_text(write_summary(result), encoding="utf-8")
	return 0


if __name__ == "__main__":
	sys.exit(main())
