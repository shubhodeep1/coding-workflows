#!/usr/bin/env python3
"""Every in-container Python entry point is tested inside its sandbox image.

The sandbox containers run a few repository files with the image's own
`python3` (Debian bookworm, Python 3.11), not CI's pinned 3.12. The
`tests-sandbox-python` job of `.github/workflows/ci.yml` (#9233) runs the
tests that cover those files inside each image. This guard keeps that list
honest:

  1. every in-container entry point the launch scripts mount, install into
     `/support/` or run is in `SANDBOX_PYTHON_ENTRY_POINTS`;
  2. every registry entry still exists in a launch script, and its host file
     and covering tests exist (a stale entry fails);
  3. the job's family matrix equals the families the registry names, its
     `Sandbox-image Python tests` step asks this file for the test list, and
     the job passes the container no secret, network or root.

To add an entry point: add a row below, with the host file, the tests that
exercise it, and the image families (`clarify`, `review`,
`codex-isolated-codex`) whose containers run it.

CLI: `python3 tests/test_sandbox_python_coverage.py --print-tests <family>`
prints that family's covering test files, one per line (exit 2 on an unknown
family). Without arguments the unittest suite runs. Standard library only,
apart from PyYAML for the workflow checks.

Known gap: inline Python (`python3 -c`, `python3 - <<EOF` heredocs) inside a
launch script is not an entry point here.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SANDBOX_JOB_ID = "tests-sandbox-python"
SANDBOX_TEST_STEP_NAME = "Sandbox-image Python tests"
SANDBOX_FAMILIES = ("clarify", "review", "codex-isolated-codex")

SCANNED_LAUNCH_SCRIPTS = (
	"scripts/codex_isolated_exec.sh",
	"scripts/clarify_isolated_run.sh",
	"scripts/review_untrusted_sandbox.sh",
	"scripts/heal_isolated_implement.sh",
	"scripts/prepush_test_gate_run.sh",
)

# In-container files that are not Python and run no Python: the Codex config
# writer is plain shell (its own tests run on the host).
NON_PYTHON_ENTRY_POINTS = frozenset({"/support/write_codex_config.sh"})

_GUARD_TESTS = ["tests/test_gh_api_write_guard.py"]
_RELAY_TESTS = ["tests/test_claude_anthropic_relay.py"]
_BRIDGE_TESTS = ["tests/test_clarify_openrouter_broker_bridge.py"]
_PROXY_TESTS = ["tests/test_dependency_registry_proxy.py"]

# In-container path -> host file, covering tests, image families that run it.
SANDBOX_PYTHON_ENTRY_POINTS: dict[str, dict] = {
	# Heal editor's syntax validator (clarify image, heal_isolated_implement.sh).
	"/validator.sh": {
		"host": "scripts/validate_changed_files_syntax.sh",
		"tests": ["tests/test_validate_changed_files_syntax.py"],
		"families": ["clarify"],
	},
	"/support/dependency_registry_proxy.py": {
		"host": "scripts/dependency_registry_proxy.py",
		"tests": _PROXY_TESTS,
		"families": ["codex-isolated-codex", "review"],
	},
	# Claude relay bridge: clarify and review Claude roles.
	"/relay.py": {
		"host": "scripts/claude_anthropic_relay.py",
		"tests": _RELAY_TESTS,
		"families": ["clarify", "review"],
	},
	# codex-isolated-claude is the same bookworm base and Python as
	# codex-isolated-codex plus the Claude CLI, so its files run there (D4).
	"/support/claude_anthropic_relay.py": {
		"host": "scripts/claude_anthropic_relay.py",
		"tests": _RELAY_TESTS,
		"families": ["codex-isolated-codex"],
	},
	"/bridge.py": {
		"host": "scripts/clarify_openrouter_broker.py",
		"tests": _BRIDGE_TESTS,
		"families": ["clarify", "review"],
	},
	"/support/clarify_openrouter_broker.py": {
		"host": "scripts/clarify_openrouter_broker.py",
		"tests": _BRIDGE_TESTS,
		"families": ["codex-isolated-codex"],
	},
	"/gate/prepush_affected_tests.py": {
		"host": "workflow-templates/.claude/hooks/prepush_affected_tests.py",
		"tests": ["tests/test_prepush_affected_tests.py"],
		"families": ["codex-isolated-codex"],
	},
	# Claude PreToolUse hook (`scripts/claude_engine.py` GUARD_HOOK_RELATIVE).
	"/guard.py": {
		"host": ".claude/hooks/gh_api_write_guard.py",
		"tests": _GUARD_TESTS,
		"families": ["clarify", "review"],
	},
	"/support/guard.py": {
		"host": ".claude/hooks/gh_api_write_guard.py",
		"tests": _GUARD_TESTS,
		"families": ["codex-isolated-codex"],
	},
}

_ENTRY_SUFFIX = r"\.(?:py|sh)"
_MOUNT_RE = re.compile(r"\bsrc=(?P<src>[^,]+?),dst=(?P<dst>/[^,\"\s]+" + _ENTRY_SUFFIX + r")\b")
_INSTALL_RE = re.compile(
	r"\binstall\b[^\n]*\"(?P<src>[^\"]+)\"\s+\"\$\{root\}/support/(?P<name>[^\"/]+" + _ENTRY_SUFFIX + r")\""
)
_RUN_RE = re.compile(r"(?:\bpython3|(?:/bin/)?\bbash)\s+(?P<path>/[^\s\"';|&]+" + _ENTRY_SUFFIX + r")\b")
# Paths under the checked-out source are repository files, not mounted entry points.
_SOURCE_PREFIXES = ("/source/", "/baseline/", "/src/")


def _strip_comment(line: str) -> str:
	return "" if line.lstrip().startswith("#") else line


def scan_entry_points() -> dict[str, list[tuple[str, str]]]:
	"""Map each in-container entry point to [(launch script, host source text)]."""
	found: dict[str, list[tuple[str, str]]] = {}

	def record(path: str, script: str, src: str) -> None:
		if path.startswith(_SOURCE_PREFIXES):
			return
		found.setdefault(path, []).append((script, src))

	for script in SCANNED_LAUNCH_SCRIPTS:
		text = (REPO_ROOT / script).read_text(encoding="utf-8")
		for raw_line in text.splitlines():
			line = _strip_comment(raw_line)
			if not line:
				continue
			for match in _MOUNT_RE.finditer(line):
				record(match.group("dst"), script, match.group("src"))
			for match in _INSTALL_RE.finditer(line):
				record("/support/" + match.group("name"), script, match.group("src"))
			for match in _RUN_RE.finditer(line):
				record(match.group("path"), script, "")
	return found


def _host_basename(src: str) -> str | None:
	"""Basename of a mount/install source, or None when it is a bare variable."""
	cleaned = re.sub(r"^\$\(realpath\s+\"?|\"?\)$", "", src.strip()).strip('"')
	tail = cleaned.rsplit("/", 1)[-1]
	if not tail or tail.startswith("$"):
		return None
	return tail


# Mount/install sources that are a launch script's runtime argument, with the
# callers that pass it. ("variable", name): the caller passes "${name}" and
# assigns it from `_ai_engine_py support-file --name <file>`; ("names", file):
# the caller names that host file. Any other bare-variable source fails the
# host-file check instead of being skipped.
ARGUMENT_SOURCED_VARIABLES: dict[tuple[str, str], tuple[tuple[str, str, str], ...]] = {
	("scripts/codex_isolated_exec.sh", "claude_guard_hook"): (
		("scripts/ai_engine.sh", "variable", "guard_hook"),
	),
	("scripts/prepush_test_gate_run.sh", "module_real"): (
		("scripts/heal_reproduce.sh", "names", "prepush_affected_tests.py"),
		("scripts/implement_step_prepush_test_gate.sh", "names", "prepush_affected_tests.py"),
		("scripts/review_autofix_step_prepush_test_gate.sh", "names", "prepush_affected_tests.py"),
	),
}

_CLAUDE_ENGINE = REPO_ROOT / "scripts" / "claude_engine.py"


def _source_variable(src: str) -> str | None:
	"""Name of a bare `${name}` (or `$(realpath "${name}")`) source, else None."""
	cleaned = re.sub(r"^\$\(realpath\s+\"?|\"?\)$", "", src.strip()).strip('"')
	match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", cleaned)
	return match.group(1) if match else None


def _support_file_basename(name: str) -> str | None:
	"""Basename `scripts/claude_engine.py support-file --name <name>` returns."""
	text = _CLAUDE_ENGINE.read_text(encoding="utf-8")
	entry = re.search(r'^\t"' + re.escape(name) + r'":\s*([A-Z_][A-Z0-9_]*),', text, re.MULTILINE)
	if entry is None:
		return None
	constant = re.search(r"^" + entry.group(1) + r"\s*=\s*(.+)$", text, re.MULTILINE)
	parts = re.findall(r'"([^"]+)"', constant.group(1)) if constant else []
	return parts[-1] if parts else None


def _support_file_variable_basename(script: str, variable: str) -> str | None:
	text = (REPO_ROOT / script).read_text(encoding="utf-8")
	match = re.search(r"\b" + re.escape(variable) + r'="\$\(_ai_engine_py support-file --name ([A-Za-z0-9_-]+)\)"', text)
	return _support_file_basename(match.group(1)) if match else None


def resolve_host_basenames(script: str, src: str) -> tuple[list[str], str]:
	"""Host basenames a mount/install source resolves to, or ([], reason)."""
	basename = _host_basename(src)
	if basename is not None:
		return [basename], ""
	variable = _source_variable(src)
	if variable is None:
		return [], f"source {src!r} in {script} is neither a path nor a bare variable"
	resolved = _support_file_variable_basename(script, variable)
	if resolved is not None:
		return [resolved], ""
	callers = ARGUMENT_SOURCED_VARIABLES.get((script, variable))
	if not callers:
		return [], (
			f"${{{variable}}} in {script} is not assigned from `_ai_engine_py support-file` and is not listed "
			"in ARGUMENT_SOURCED_VARIABLES"
		)
	basenames: list[str] = []
	for caller, kind, value in callers:
		text = (REPO_ROOT / caller).read_text(encoding="utf-8")
		if kind == "variable":
			resolved = _support_file_variable_basename(caller, value) if f'"${{{value}}}"' in text else None
		else:
			resolved = value if value in text else None
		if resolved is None:
			return [], f"{caller} no longer passes a resolvable {kind} {value!r} for ${{{variable}}} in {script}"
		basenames.append(resolved)
	return basenames, ""


def covering_tests_for_family(family: str) -> list[str]:
	return sorted({test for entry in SANDBOX_PYTHON_ENTRY_POINTS.values() if family in entry["families"] for test in entry["tests"]})


def _ci_jobs() -> dict:
	import yaml

	return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _yaml_available() -> bool:
	try:
		import yaml  # noqa: F401
	except ImportError:
		return False
	return True


class RegistryCoverageTest(unittest.TestCase):
	def test_every_scanned_entry_point_is_registered(self) -> None:
		found = scan_entry_points()
		self.assertTrue(found, "the scanner found no entry point; the launch scripts or the patterns changed")
		for path, sites in sorted(found.items()):
			if path in NON_PYTHON_ENTRY_POINTS:
				continue
			with self.subTest(entry_point=path):
				self.assertIn(
					path,
					SANDBOX_PYTHON_ENTRY_POINTS,
					f"{path} runs inside a sandbox container ({sites[0][0]}) but no test covers it there; "
					"add it to SANDBOX_PYTHON_ENTRY_POINTS in tests/test_sandbox_python_coverage.py",
				)

	def test_scanned_host_file_matches_the_registry(self) -> None:
		found = scan_entry_points()
		for path, entry in sorted(SANDBOX_PYTHON_ENTRY_POINTS.items()):
			for script, src in found.get(path, []):
				if not src:
					# A `python3 /x.py` run site carries no host source.
					continue
				with self.subTest(entry_point=path, script=script):
					basenames, reason = resolve_host_basenames(script, src)
					self.assertTrue(basenames, reason)
					for basename in basenames:
						self.assertEqual(basename, Path(entry["host"]).name)

	def test_variable_sources_resolve_or_fail(self) -> None:
		self.assertEqual(
			resolve_host_basenames("scripts/clarify_isolated_run.sh", '$(realpath "${guard_hook}")'),
			(["gh_api_write_guard.py"], ""),
		)
		self.assertEqual(
			resolve_host_basenames("scripts/codex_isolated_exec.sh", "${claude_guard_hook}")[0],
			["gh_api_write_guard.py"],
		)
		basenames, reason = resolve_host_basenames("scripts/clarify_isolated_run.sh", "${unknown_source}")
		self.assertEqual(basenames, [])
		self.assertIn("ARGUMENT_SOURCED_VARIABLES", reason)

	def test_no_stale_registry_entries(self) -> None:
		found = scan_entry_points()
		for path in sorted(SANDBOX_PYTHON_ENTRY_POINTS):
			with self.subTest(entry_point=path):
				self.assertIn(path, found, f"{path} is registered but no launch script uses it any more")
		for path in sorted(NON_PYTHON_ENTRY_POINTS):
			with self.subTest(non_python=path):
				self.assertIn(path, found)

	def test_registry_files_exist_and_families_are_known(self) -> None:
		for path, entry in sorted(SANDBOX_PYTHON_ENTRY_POINTS.items()):
			with self.subTest(entry_point=path):
				self.assertTrue((REPO_ROOT / entry["host"]).is_file(), entry["host"])
				self.assertTrue(entry["tests"])
				for test in entry["tests"]:
					self.assertRegex(test, r"^tests/test_[A-Za-z0-9_]+\.py$")
					self.assertTrue((REPO_ROOT / test).is_file(), test)
				self.assertTrue(entry["families"])
				self.assertLessEqual(set(entry["families"]), set(SANDBOX_FAMILIES))

	def test_every_family_has_tests(self) -> None:
		for family in SANDBOX_FAMILIES:
			with self.subTest(family=family):
				self.assertTrue(covering_tests_for_family(family))

	def test_scanner_reads_mount_install_and_run_forms(self) -> None:
		found = scan_entry_points()
		self.assertIn("/validator.sh", found)
		self.assertIn("/support/guard.py", found)
		self.assertIn("/gate/prepush_affected_tests.py", found)
		self.assertNotIn("/source/scripts/write_codex_config.sh", found)


@unittest.skipUnless(_yaml_available(), "PyYAML is needed to read ci.yml; every CI job that runs this file installs it")
class SandboxJobWiringTest(unittest.TestCase):
	def test_job_matrix_matches_registry_families(self) -> None:
		job = _ci_jobs()[SANDBOX_JOB_ID]
		matrix_families = job["strategy"]["matrix"]["sandbox-family"]
		registry_families = {family for entry in SANDBOX_PYTHON_ENTRY_POINTS.values() for family in entry["families"]}
		self.assertEqual(sorted(matrix_families), sorted(registry_families))
		self.assertEqual(sorted(matrix_families), sorted(SANDBOX_FAMILIES))

	def _test_step(self) -> dict:
		job = _ci_jobs()[SANDBOX_JOB_ID]
		steps = [step for step in job["steps"] if step.get("name") == SANDBOX_TEST_STEP_NAME]
		self.assertEqual(len(steps), 1)
		return steps[0]

	def test_job_runs_the_registry_for_its_family(self) -> None:
		step = self._test_step()
		self.assertEqual(step["env"]["CI_SANDBOX_PYTHON_FAMILY"], "${{ matrix.sandbox-family }}")
		self.assertIn('tests/test_sandbox_python_coverage.py --print-tests "${family}"', step["run"])
		self.assertIn('"${family_tests[@]}"', step["run"])
		self.assertIn("scripts/sandbox_image.sh build --family", step["run"])
		self.assertIn("scripts/codex_isolated_exec.sh image", step["run"])

	def test_job_passes_no_secret_or_token(self) -> None:
		job = _ci_jobs()[SANDBOX_JOB_ID]
		self.assertEqual(job.get("permissions"), {"contents": "read"})
		blobs = [repr(job.get("env", {}))] + [repr(step.get("env", {})) + repr(step.get("with", {})) for step in job["steps"]]
		for blob in blobs:
			self.assertNotIn("secrets.", blob)
			self.assertNotIn("github.token", blob)
			self.assertNotIn("GITHUB_TOKEN", blob)
			self.assertNotIn("GH_TOKEN", blob)

	def test_test_container_gets_no_network_root_or_credential(self) -> None:
		run = self._test_step()["run"]
		for flag in ("--network none", "--user 65534:65534", "--cap-drop ALL", "--read-only", "dst=/src,readonly"):
			with self.subTest(flag=flag):
				self.assertIn(flag, run)
		self.assertNotRegex(run, r"-e\s+\"?(GH_TOKEN|GITHUB_TOKEN)")
		self.assertNotIn("--env-file", run)


def _main(argv: list[str]) -> int:
	if argv[:1] == ["--print-tests"]:
		if len(argv) != 2 or argv[1] not in SANDBOX_FAMILIES:
			print(f"usage: {Path(__file__).name} --print-tests {{{'|'.join(SANDBOX_FAMILIES)}}}", file=sys.stderr)
			return 2
		tests = covering_tests_for_family(argv[1])
		if not tests:
			print(f"no covering tests registered for family {argv[1]}", file=sys.stderr)
			return 2
		print("\n".join(tests))
		return 0
	unittest.main(argv=[sys.argv[0], *argv])
	return 0


if __name__ == "__main__":
	raise SystemExit(_main(sys.argv[1:]))
