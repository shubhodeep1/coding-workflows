#!/usr/bin/env python3
"""Deterministic lint checks for rendered validation harness output.

Rule IDs:
- service-tool-scope
- shell-parity
- canary-ordering
- test-script-prologue
- healthcheck-test-strings
- init-true-required
- graceful-shutdown-hooks
- bounded-tail-capture
- external-tool-dependencies
- import-audit-safety

Escape hatch syntax:
# validation-lint: allow <rule-id> <reason>
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
	import yaml
except ModuleNotFoundError:
	yaml = None


TOKEN_CHARS = r"A-Za-z0-9._+-"
ALLOW_COMMENT_RE = re.compile(
	r"#\s*validation-lint:\s*allow\s+(?P<rule>[a-z0-9][a-z0-9_-]*)\s+(?P<reason>.+?)\s*$"
)
SHELL_LC_RE = re.compile(r"(^|[\"'\s])(?:/bin/)?(?:bash|sh)\s+-lc\b")
SHELL_BASH_C_RE = re.compile(r"(^|[\"'\s])(?:/bin/)?bash\s+-c\b")
TEST_SCRIPT_FILENAME_RE = re.compile(r"^(?P<prefix>\d{2})_.+\.sh$")
SET_EUO_PIPEFAIL_RE = re.compile(r"^\s*set\s+-euo\s+pipefail(?:\s|$)")
HEALTHCHECK_BLOCK_RE = re.compile(r"^\s*healthcheck\s*:\s*$")
HEALTHCHECK_TEST_KEY_RE = re.compile(r"^\s*test\s*:\s*(?P<value>.*)$")
YAML_LIST_ITEM_RE = re.compile(r"^\s*-\s*(?P<value>.*)$")

REQUIRED_BASH_SHEBANG = "#!/usr/bin/env bash"
REQUIRED_BASH_SHEBANG_RE = re.compile(r"^\s*#!/usr/bin/env bash(?:\s+#.*)?\s*$")

SERVICE_TOOL_DENYLIST = (
	"mongosh",
	"mongo",
	"psql",
	"redis-cli",
	"mysql",
	"mysqladmin",
	"kafkacat",
	"kcat",
)

TOOL_INSTALL_ALIASES: dict[str, tuple[str, ...]] = {
	"mongosh": ("mongosh", "mongodb-mongosh"),
	"mongo": ("mongo", "mongodb-clients", "mongo-tools"),
	"psql": ("psql", "postgresql-client"),
	"redis-cli": ("redis-cli", "redis-tools"),
	"mysql": ("mysql", "mysql-client", "default-mysql-client", "mariadb-client"),
	"mysqladmin": ("mysqladmin", "mysql-client", "default-mysql-client", "mariadb-client"),
	"kafkacat": ("kafkacat", "kcat"),
	"kcat": ("kcat", "kafkacat"),
	"forge": ("forge", "foundryup", "foundry.paradigm.xyz"),
	"cast": ("cast", "foundryup", "foundry.paradigm.xyz"),
	"hardhat": ("hardhat", "npm ci", "npm install"),
}

EXTERNAL_APP_TOOLS = ("forge", "cast", "hardhat")


@dataclass(frozen=True)
class Finding:
	path: Path
	line: int
	rule_id: str
	message: str
	hint: str


Checker = Callable[["LintContext"], list[Finding]]


@dataclass(frozen=True)
class Rule:
	rule_id: str
	description: str
	checker: Checker
	allow_escape: bool = True


class LintContext:
	def __init__(self, root: Path) -> None:
		self.root = root
		self._line_cache: dict[Path, list[str] | None] = {}
		self._escape_cache: dict[Path, dict[int, set[str]]] = {}
		self._dockerfile_text_cache: str | None = None

	def resolve(self, relative_path: str) -> Path:
		return self.root / relative_path

	def read_lines(self, path: Path) -> list[str] | None:
		if path in self._line_cache:
			return self._line_cache[path]
		if not path.exists() or not path.is_file():
			self._line_cache[path] = None
			return None
		try:
			if path.stat().st_size > 1_000_000:
				self._line_cache[path] = None
				return None
			self._line_cache[path] = path.read_text(encoding="utf-8").splitlines()
		except (OSError, UnicodeDecodeError):
			self._line_cache[path] = None
		return self._line_cache[path]

	def read_text(self, path: Path) -> str | None:
		lines = self.read_lines(path)
		if lines is None:
			return None
		return "\n".join(lines)

	def display_path(self, path: Path) -> str:
		try:
			return path.relative_to(Path.cwd()).as_posix()
		except ValueError:
			return path.as_posix()

	def find_line(self, path: Path, pattern: str | re.Pattern[str]) -> int:
		lines = self.read_lines(path)
		if lines is None:
			return 1
		regex = re.compile(pattern) if isinstance(pattern, str) else pattern
		for idx, line in enumerate(lines, start=1):
			if regex.search(line):
				return idx
		return 1

	def is_escaped(self, path: Path, line: int, rule_id: str) -> bool:
		escape_map = self._get_escape_map(path)
		allowed = escape_map.get(line, set())
		return rule_id in allowed

	def dockerfile_text(self) -> str:
		if self._dockerfile_text_cache is not None:
			return self._dockerfile_text_cache
		dockerfile_path = self.resolve("Dockerfile.app")
		lines = self.read_lines(dockerfile_path)
		if lines is None:
			self._dockerfile_text_cache = ""
			return self._dockerfile_text_cache

		sanitized: list[str] = []
		for raw in lines:
			if raw.lstrip().startswith("#"):
				continue
			no_comment = _strip_inline_comment(raw).strip()
			if not no_comment:
				continue
			sanitized.append(no_comment.rstrip("\\").strip().lower())

		self._dockerfile_text_cache = " ".join(sanitized)
		return self._dockerfile_text_cache

	def _get_escape_map(self, path: Path) -> dict[int, set[str]]:
		if path in self._escape_cache:
			return self._escape_cache[path]

		lines = self.read_lines(path)
		if lines is None:
			self._escape_cache[path] = {}
			return self._escape_cache[path]

		pending_rules: list[str] = []
		escapes: dict[int, set[str]] = {}

		for idx, line in enumerate(lines, start=1):
			for match in ALLOW_COMMENT_RE.finditer(line):
				rule = match.group("rule").strip()
				reason = match.group("reason").strip()
				if not reason:
					continue
				prefix = line[: match.start()]
				if prefix.strip():
					escapes.setdefault(idx, set()).add(rule)
				else:
					pending_rules.append(rule)

			stripped = line.strip()
			if not stripped:
				continue
			if stripped.startswith("#"):
				continue

			if pending_rules:
				for rule in pending_rules:
					escapes.setdefault(idx, set()).add(rule)
				pending_rules = []

		self._escape_cache[path] = escapes
		return escapes


def _strip_inline_comment(line: str) -> str:
	in_single = False
	in_double = False
	escaped = False
	result: list[str] = []

	for ch in line:
		if escaped:
			result.append(ch)
			escaped = False
			continue

		if ch == "\\" and not in_single:
			result.append(ch)
			escaped = True
			continue

		if ch == "'" and not in_double:
			in_single = not in_single
			result.append(ch)
			continue

		if ch == '"' and not in_single:
			in_double = not in_double
			result.append(ch)
			continue

		if ch == "#" and not in_single and not in_double:
			break

		result.append(ch)

	return "".join(result)


_tool_pattern_cache: dict[tuple[str, ...], re.Pattern[str]] = {}


def _get_tool_pattern(aliases: tuple[str, ...]) -> re.Pattern[str]:
	key = tuple(sorted(set(alias.lower() for alias in aliases)))
	if key in _tool_pattern_cache:
		return _tool_pattern_cache[key]
	alternatives = "|".join(re.escape(item) for item in key)
	pattern = re.compile(rf"(?<![{TOKEN_CHARS}])(?:{alternatives})(?![{TOKEN_CHARS}])")
	_tool_pattern_cache[key] = pattern
	return pattern


def _dockerfile_has_tool(context: LintContext, tool: str) -> bool:
	dockerfile_text = context.dockerfile_text()
	if not dockerfile_text:
		return False
	aliases = TOOL_INSTALL_ALIASES.get(tool, (tool,))
	return bool(_get_tool_pattern(aliases).search(dockerfile_text))


def _unwrap_canary_assignment(raw: str) -> str:
	value = raw.strip()
	wrappers = (
		re.compile(r'^"\$\{CANARY_TOOLS:-(?P<body>.*)\}"$'),
		re.compile(r"^'\$\{CANARY_TOOLS:-(?P<body>.*)\}'$"),
		re.compile(r"^\$\{CANARY_TOOLS:-(?P<body>.*)\}$"),
	)
	for wrapper in wrappers:
		match = wrapper.match(value)
		if match:
			return match.group("body").strip()

	if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
		return value[1:-1].strip()

	return value


def _parse_canary_array(lines: list[str], start_idx: int) -> tuple[int, list[str]]:
	start_line = start_idx + 1
	line = lines[start_idx]
	# Strip inline comments before matching
	stripped_line = _strip_inline_comment(line)
	oneline_match = re.match(r"^\s*CANARY_TOOLS\s*=\s*\((?P<body>.*)\)\s*$", stripped_line)
	if oneline_match:
		body = oneline_match.group("body").strip()
		if not body:
			return start_line, []
		try:
			return start_line, shlex.split(body)
		except ValueError:
			return start_line, [token for token in body.split() if token]

	tools: list[str] = []
	idx = start_idx + 1
	while idx < len(lines):
		current = lines[idx]
		# Strip inline comments before checking for closing parenthesis
		stripped_current = _strip_inline_comment(current).strip()
		if stripped_current == ")":
			break
		fragment = stripped_current  # Already stripped and trimmed
		if fragment:
			try:
				tools.extend(shlex.split(fragment))
			except ValueError:
				tools.extend(token for token in fragment.split() if token)
		idx += 1

	return start_line, tools


def _extract_canary_tools(context: LintContext) -> tuple[Path, int | None, list[str]]:
	canary_path = context.resolve("tests/00_canary.sh")
	lines = context.read_lines(canary_path)
	if lines is None:
		return canary_path, None, []

	for idx, line in enumerate(lines):
		if line.lstrip().startswith("#"):
			continue

		assign_match = re.match(r"^\s*CANARY_TOOLS\s*=\s*(?P<rhs>.+)$", line)
		if assign_match and "(" not in assign_match.group("rhs"):
			rhs = _strip_inline_comment(assign_match.group("rhs")).strip()
			unwrapped = _unwrap_canary_assignment(rhs)
			try:
				tools = shlex.split(unwrapped)
			except ValueError:
				tools = [token for token in unwrapped.split() if token]
			return canary_path, idx + 1, tools

		if re.match(r"^\s*CANARY_TOOLS\s*=\s*\(", line):
			start_line, tools = _parse_canary_array(lines, idx)
			return canary_path, start_line, tools

	return canary_path, None, []


def _check_service_tool_scope(context: LintContext) -> list[Finding]:
	canary_path, line_no, canary_tools = _extract_canary_tools(context)
	if line_no is None or not canary_tools:
		return []

	offenders: list[str] = []
	seen: set[str] = set()

	for tool in canary_tools:
		normalized = tool.strip().lower()
		if normalized not in SERVICE_TOOL_DENYLIST:
			continue
		if normalized in seen:
			continue
		seen.add(normalized)

		if not _dockerfile_has_tool(context, normalized):
			offenders.append(normalized)

	if not offenders:
		return []

	return [
		Finding(
			path=canary_path,
			line=line_no,
			rule_id="service-tool-scope",
			message=(
				"CANARY_TOOLS references service-side CLI(s) not installed in "
				f"Dockerfile.app: {', '.join(offenders)}"
			),
			hint=(
				"Remove service-side CLIs from CANARY_TOOLS or install matching binaries "
				"in validation/Dockerfile.app."
			),
		)
	]


def _iter_shell_paths(context: LintContext) -> list[Path]:
	paths: list[Path] = []
	compose_path = context.resolve("docker-compose.test.yml")
	if compose_path.exists():
		paths.append(compose_path)

	tests_dir = context.resolve("tests")
	if tests_dir.exists():
		paths.extend(sorted(tests_dir.rglob("*.sh")))

	lib_dir = context.resolve("_lib")
	if lib_dir.exists():
		paths.extend(sorted(lib_dir.rglob("*.sh")))

	return paths


def _check_shell_parity(context: LintContext) -> list[Finding]:
	findings: list[Finding] = []

	for path in _iter_shell_paths(context):
		lines = context.read_lines(path)
		if lines is None:
			continue
		for idx, line in enumerate(lines, start=1):
			if line.lstrip().startswith("#"):
				continue
			clean_line = _strip_inline_comment(line)
			if SHELL_LC_RE.search(clean_line):
				findings.append(
					Finding(
						path=path,
						line=idx,
						rule_id="shell-parity",
						message="Login shell form '-lc' is forbidden in validation harness commands.",
						hint="Use '/bin/sh -c' (or 'sh -c') for deterministic non-login shell behavior.",
					)
				)
				continue
			if SHELL_BASH_C_RE.search(clean_line):
				findings.append(
					Finding(
						path=path,
						line=idx,
						rule_id="shell-parity",
						message="Bash-specific '-c' invocation is forbidden for parity-sensitive checks.",
						hint="Replace bash invocations with '/bin/sh -c' in compose healthchecks and test scripts.",
					)
				)

	return findings


def _iter_top_level_test_scripts(context: LintContext) -> list[Path]:
	tests_dir = context.resolve("tests")
	if not tests_dir.exists() or not tests_dir.is_dir():
		return []
	return sorted(path for path in tests_dir.glob("*.sh") if path.is_file() or path.is_symlink())


def _is_explicitly_quoted_yaml_scalar(value: str) -> bool:
	stripped = value.strip()
	return len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'")


def _split_flow_style_yaml_items(value: str) -> list[str]:
	items: list[str] = []
	current: list[str] = []
	in_single = False
	in_double = False
	escaped = False
	for ch in value[1:-1]:
		if escaped:
			current.append(ch)
			escaped = False
			continue
		if ch == "\\" and in_double:
			current.append(ch)
			escaped = True
			continue
		if ch == "'" and not in_double:
			in_single = not in_single
			current.append(ch)
			continue
		if ch == '"' and not in_single:
			in_double = not in_double
			current.append(ch)
			continue
		if ch == "," and not in_single and not in_double:
			items.append("".join(current).strip())
			current = []
			continue
		current.append(ch)
	items.append("".join(current).strip())
	return [item for item in items if item]


# mode-validate-generate.txt: tests/00_canary.sh must be first and scripts must use NN_*.sh ordering.
def _check_canary_ordering(context: LintContext) -> list[Finding]:
	tests_dir = context.resolve("tests")
	scripts = _iter_top_level_test_scripts(context)

	if not tests_dir.exists() or not tests_dir.is_dir():
		return [
			Finding(
				path=tests_dir,
				line=1,
				rule_id="canary-ordering",
				message="tests directory is missing.",
				hint="Add tests with 00_canary.sh and zero-padded NN_*.sh scripts.",
			)
		]

	if not scripts:
		return [
			Finding(
				path=tests_dir,
				line=1,
				rule_id="canary-ordering",
				message="tests contains no top-level shell test scripts.",
				hint="Add 00_canary.sh and subsequent NN_*.sh scripts under tests/.",
			)
		]

	findings: list[Finding] = []
	canary_present = any(path.name == "00_canary.sh" for path in scripts)
	if not canary_present:
		findings.append(
			Finding(
				path=tests_dir,
				line=1,
				rule_id="canary-ordering",
				message="tests/00_canary.sh is required as the first test script.",
				hint="Add 00_canary.sh to gate infrastructure checks before app assertions.",
			)
		)

	if canary_present and scripts[0].name != "00_canary.sh":
		findings.append(
			Finding(
				path=scripts[0],
				line=1,
				rule_id="canary-ordering",
				message=f"{scripts[0].name} sorts before 00_canary.sh.",
				hint="Keep 00_canary.sh as the first top-level test script.",
			)
		)

	seen_prefixes: dict[int, str] = {}
	for script in scripts:
		match = TEST_SCRIPT_FILENAME_RE.match(script.name)
		if match is None:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="canary-ordering",
					message=f"Test script '{script.name}' must use zero-padded NN_*.sh naming.",
					hint="Rename script to NN_<name>.sh with a two-digit numeric prefix.",
				)
			)
			continue

		prefix = int(match.group("prefix"))
		if prefix == 0 and script.name != "00_canary.sh":
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="canary-ordering",
					message=f"Only 00_canary.sh may use the 00 prefix (found {script.name}).",
					hint="Reserve prefix 00 for canary and use 01+ for subsequent scripts.",
				)
			)
			continue

		if prefix in seen_prefixes and seen_prefixes[prefix] != script.name:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="canary-ordering",
					message=(
						f"Duplicate numeric prefix {prefix:02d} for scripts "
						f"'{seen_prefixes[prefix]}' and '{script.name}'."
					),
					hint="Use unique NN_ prefixes so script execution order stays deterministic.",
				)
			)
			continue
		seen_prefixes[prefix] = script.name

	return findings


# mode-validate-generate.txt: test scripts must include bash shebang, strict mode, and executable bit.
def _check_test_script_prologue(context: LintContext) -> list[Finding]:
	findings: list[Finding] = []

	for script in _iter_top_level_test_scripts(context):
		lines = context.read_lines(script)
		if lines is None:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="test-script-prologue",
					message=f"{script.name} could not be read for prologue validation.",
					hint="Ensure the script is a UTF-8 text file with standard shell prologue lines.",
				)
			)
			continue

		if not lines or REQUIRED_BASH_SHEBANG_RE.match(lines[0]) is None:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="test-script-prologue",
					message=f"{script.name} must start with '{REQUIRED_BASH_SHEBANG}'.",
					hint="Add '#!/usr/bin/env bash' as the first line of every validation test script.",
				)
			)

		has_strict_mode = any(
			SET_EUO_PIPEFAIL_RE.match(_strip_inline_comment(line))
			for line in lines
			if line.strip() and not line.lstrip().startswith("#")
		)
		if not has_strict_mode:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="test-script-prologue",
					message=f"{script.name} must include 'set -euo pipefail'.",
					hint="Enable strict shell mode near the top of each test script.",
				)
			)

		try:
			mode = script.stat().st_mode
		except OSError:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="test-script-prologue",
					message=f"{script.name} could not be stat'ed for executable-bit validation.",
					hint="Ensure the script metadata is readable before linting executable permissions.",
				)
			)
			continue
		if (mode & 0o111) == 0:
			findings.append(
				Finding(
					path=script,
					line=1,
					rule_id="test-script-prologue",
					message=f"{script.name} must be executable.",
					hint="Set executable permissions on generated .sh files (for example, chmod +x).",
				)
			)

	return findings


# mode-validate-generate.txt: healthcheck.test list entries must be explicitly quoted strings.
def _check_healthcheck_test_strings(context: LintContext) -> list[Finding]:
	compose_path = context.resolve("docker-compose.test.yml")
	compose_lines = context.read_lines(compose_path)
	if compose_lines is None:
		return []

	findings: list[Finding] = []
	in_healthcheck = False
	healthcheck_indent = -1
	in_test_list = False
	test_indent = -1

	for idx, line in enumerate(compose_lines, start=1):
		clean_line = _strip_inline_comment(line)
		if not clean_line.strip():
			continue

		indent = len(clean_line) - len(clean_line.lstrip(" "))
		item_match = YAML_LIST_ITEM_RE.match(clean_line)

		if in_test_list and (
			indent < test_indent
			or (indent == test_indent and item_match is None)
		):
			in_test_list = False

		if in_healthcheck and (
			indent < healthcheck_indent
			or (indent == healthcheck_indent and not HEALTHCHECK_BLOCK_RE.match(clean_line))
		):
			in_healthcheck = False
			in_test_list = False

		if HEALTHCHECK_BLOCK_RE.match(clean_line):
			in_healthcheck = True
			healthcheck_indent = indent
			in_test_list = False
			continue

		if not in_healthcheck:
			continue

		test_match = HEALTHCHECK_TEST_KEY_RE.match(clean_line)
		if test_match:
			value = test_match.group("value").strip()
			if not value:
				in_test_list = True
				test_indent = indent
				continue

			if value.startswith("[") and value.endswith("]"):
				entries = _split_flow_style_yaml_items(value)
				for entry in entries:
					if _is_explicitly_quoted_yaml_scalar(entry):
						continue
					findings.append(
						Finding(
							path=compose_path,
							line=idx,
							rule_id="healthcheck-test-strings",
							message=f"healthcheck.test entry '{entry}' must be explicitly quoted.",
							hint="Quote every healthcheck.test list entry, for example \"CMD-SHELL\".",
						)
					)
				continue

			if value.startswith("-"):
				entry = value[1:].strip()
				if entry and not _is_explicitly_quoted_yaml_scalar(entry):
					findings.append(
						Finding(
							path=compose_path,
							line=idx,
							rule_id="healthcheck-test-strings",
							message=f"healthcheck.test entry '{entry}' must be explicitly quoted.",
							hint="Quote every healthcheck.test list entry, for example \"CMD-SHELL\".",
						)
					)
				in_test_list = True
				test_indent = indent
				continue

			findings.append(
				Finding(
					path=compose_path,
					line=idx,
					rule_id="healthcheck-test-strings",
					message="healthcheck.test must be a list of explicitly quoted strings.",
					hint="Use list form and quote each entry (for example \"CMD-SHELL\").",
				)
			)
			continue

		if not in_test_list:
			continue

		if item_match is None:
			continue

		entry = item_match.group("value").strip()
		if not entry or _is_explicitly_quoted_yaml_scalar(entry):
			continue

		findings.append(
			Finding(
				path=compose_path,
				line=idx,
				rule_id="healthcheck-test-strings",
				message=f"healthcheck.test entry '{entry}' must be explicitly quoted.",
				hint="Quote every healthcheck.test list entry, for example \"CMD-SHELL\".",
			)
		)

	return findings


def _find_service_line(compose_lines: list[str], service_name: str) -> int:
	key_pattern = re.compile(rf"^\s*{re.escape(service_name)}\s*:\s*(?:#.*)?$")
	for idx, line in enumerate(compose_lines, start=1):
		if key_pattern.match(line):
			return idx
	return 1


def _check_init_true_required(context: LintContext) -> list[Finding]:
	compose_path = context.resolve("docker-compose.test.yml")
	compose_lines = context.read_lines(compose_path)
	if compose_lines is None:
		return [
			Finding(
				path=compose_path,
				line=1,
				rule_id="init-true-required",
				message="validation/docker-compose.test.yml is missing.",
				hint="Add docker-compose.test.yml with init: true on long-running services.",
			)
		]

	if yaml is None:
		return [
			Finding(
				path=compose_path,
				line=1,
				rule_id="init-true-required",
				message="PyYAML is required to lint docker-compose.test.yml.",
				hint="Install pyyaml in the lint environment.",
			)
		]

	try:
		compose_doc = yaml.safe_load("\n".join(compose_lines))
	except Exception as exc:  # pragma: no cover - parser internals vary by version
		line_no = 1
		mark = getattr(exc, "problem_mark", None)
		if mark is not None and hasattr(mark, "line"):
			line_no = int(mark.line) + 1
		return [
			Finding(
				path=compose_path,
				line=line_no,
				rule_id="init-true-required",
				message=f"docker-compose.test.yml could not be parsed: {exc}",
				hint="Fix YAML syntax before running validation lint.",
			)
		]

	if not isinstance(compose_doc, dict):
		return [
			Finding(
				path=compose_path,
				line=1,
				rule_id="init-true-required",
				message="docker-compose.test.yml must be a mapping with a services key.",
				hint="Declare services under a top-level 'services:' mapping.",
			)
		]

	services = compose_doc.get("services")
	if not isinstance(services, dict) or not services:
		return [
			Finding(
				path=compose_path,
				line=1,
				rule_id="init-true-required",
				message="docker-compose.test.yml has no services mapping to validate.",
				hint="Add one or more long-running services under services:.",
			)
		]

	findings: list[Finding] = []
	for service_name, service_def in services.items():
		if not isinstance(service_def, dict):
			continue
		init_value = service_def.get("init")
		if init_value is True:
			continue
		line_no = _find_service_line(compose_lines, str(service_name))
		if "init" not in service_def:
			message = f"Service '{service_name}' is missing init: true."
		else:
			message = f"Service '{service_name}' sets init to {init_value!r}; expected true."
		findings.append(
			Finding(
				path=compose_path,
				line=line_no,
				rule_id="init-true-required",
				message=message,
				hint="Set init: true under each long-running service in validation/docker-compose.test.yml.",
			)
		)

	return findings


def _line_with_token(lines: list[str], token: str) -> int:
	for idx, line in enumerate(lines, start=1):
		if token in line:
			return idx
	return 1


def _check_graceful_shutdown_hooks(context: LintContext) -> list[Finding]:
	findings: list[Finding] = []

	py_test = context.resolve("tests/30_graceful_shutdown.sh")
	py_helper = context.resolve("tests/_lib/graceful_shutdown.py")
	node_test = context.resolve("tests/30_hardhat_test.sh")
	node_helper = context.resolve("_lib/graceful_shutdown.sh")

	has_python_hooks = py_test.exists()
	has_node_hooks = node_test.exists()

	if not has_python_hooks and not has_node_hooks:
		findings.append(
			Finding(
				path=context.resolve("tests"),
				line=1,
				rule_id="graceful-shutdown-hooks",
				message="No graceful shutdown test hook found (expected 30_graceful_shutdown.sh or 30_hardhat_test.sh).",
				hint="Add a graceful shutdown test script and helper wiring.",
			)
		)
		return findings

	if has_python_hooks:
		py_test_lines = context.read_lines(py_test) or []
		required_tokens = (
			"_lib/graceful_shutdown.py",
			"--timeout-seconds",
			"--poll-seconds",
			"--log-tail-lines",
		)
		for token in required_tokens:
			if any(token in line for line in py_test_lines):
				continue
			findings.append(
				Finding(
					path=py_test,
					line=1,
					rule_id="graceful-shutdown-hooks",
					message=f"tests/30_graceful_shutdown.sh is missing required token '{token}'.",
					hint="Invoke tests/_lib/graceful_shutdown.py with timeout, poll, and bounded-tail flags.",
				)
			)

		if not py_helper.exists():
			findings.append(
				Finding(
					path=py_helper,
					line=1,
					rule_id="graceful-shutdown-hooks",
					message="Graceful shutdown helper tests/_lib/graceful_shutdown.py is missing.",
					hint="Add tests/_lib/graceful_shutdown.py and call it from tests/30_graceful_shutdown.sh.",
				)
			)

	if has_node_hooks:
		node_test_lines = context.read_lines(node_test) or []
		node_tokens = ("_lib/graceful_shutdown.sh", "graceful_shutdown")
		for token in node_tokens:
			if any(token in line for line in node_test_lines):
				continue
			findings.append(
				Finding(
					path=node_test,
					line=1,
					rule_id="graceful-shutdown-hooks",
					message=f"tests/30_hardhat_test.sh is missing required token '{token}'.",
					hint="Source _lib/graceful_shutdown.sh and call graceful_shutdown in watchdog paths.",
				)
			)

		if not node_helper.exists():
			findings.append(
				Finding(
					path=node_helper,
					line=1,
					rule_id="graceful-shutdown-hooks",
					message="Graceful shutdown helper _lib/graceful_shutdown.sh is missing.",
					hint="Add _lib/graceful_shutdown.sh and source it from tests/30_hardhat_test.sh.",
				)
			)

	return findings


def _check_bounded_tail_capture(context: LintContext) -> list[Finding]:
	findings: list[Finding] = []

	py_test = context.resolve("tests/30_graceful_shutdown.sh")
	py_helper = context.resolve("tests/_lib/graceful_shutdown.py")
	node_test = context.resolve("tests/30_hardhat_test.sh")
	node_helper = context.resolve("_lib/graceful_shutdown.sh")

	if py_helper.exists():
		py_helper_text = context.read_text(py_helper) or ""
		if "bounded_compose_logs_tail" not in py_helper_text:
			findings.append(
				Finding(
					path=py_helper,
					line=1,
					rule_id="bounded-tail-capture",
					message="graceful_shutdown.py is missing bounded_compose_logs_tail helper.",
					hint="Capture bounded compose log tails on timeout paths.",
				)
			)
		if "log_tail" not in py_helper_text:
			findings.append(
				Finding(
					path=py_helper,
					line=1,
					rule_id="bounded-tail-capture",
					message="graceful_shutdown.py does not emit bounded log tail payloads.",
					hint="Include a bounded log_tail field in timeout diagnostics.",
				)
			)

		py_test_text = context.read_text(py_test) or ""
		if py_test.exists() and "--log-tail-lines" not in py_test_text:
			findings.append(
				Finding(
					path=py_test,
					line=1,
					rule_id="bounded-tail-capture",
					message="30_graceful_shutdown.sh does not forward --log-tail-lines to helper.",
					hint="Pass --log-tail-lines so timeout diagnostics remain bounded.",
				)
			)

	if node_helper.exists():
		node_helper_text = context.read_text(node_helper) or ""
		if not re.search(r"\btail\s+-n\b", node_helper_text):
			findings.append(
				Finding(
					path=node_helper,
					line=1,
					rule_id="bounded-tail-capture",
					message="_lib/graceful_shutdown.sh is missing bounded 'tail -n' capture.",
					hint="Emit bounded tail output when graceful shutdown times out.",
				)
			)

	if node_test.exists():
		node_test_text = context.read_text(node_test) or ""
		if not re.search(r"\btail\s+-n\b", node_test_text):
			findings.append(
				Finding(
					path=node_test,
					line=1,
					rule_id="bounded-tail-capture",
					message="tests/30_hardhat_test.sh is missing bounded 'tail -n' capture on failure.",
					hint="Log only a bounded tail on failures to keep diagnostics deterministic.",
				)
			)

	return findings


def _check_external_tool_dependencies(context: LintContext) -> list[Finding]:
	findings: list[Finding] = []

	canary_path, canary_line, canary_tools = _extract_canary_tools(context)
	required_tools: list[tuple[str, Path, int]] = []

	if canary_line is not None:
		for tool in canary_tools:
			tool_norm = tool.strip().lower()
			if tool_norm not in EXTERNAL_APP_TOOLS or tool_norm == "hardhat":
				continue
			required_tools.append((tool, canary_path, canary_line))

	hardhat_test = context.resolve("tests/30_hardhat_test.sh")
	hardhat_lines = context.read_lines(hardhat_test)
	if hardhat_lines is not None:
		for idx, line in enumerate(hardhat_lines, start=1):
			if line.lstrip().startswith("#"):
				continue
			clean_line = _strip_inline_comment(line)
			if re.search(r"(?<![A-Za-z0-9._+-])hardhat(?![A-Za-z0-9._+-])", clean_line, re.IGNORECASE):
				required_tools.append(("hardhat", hardhat_test, idx))
				break

	deduped: list[tuple[str, Path, int]] = []
	seen: set[tuple[str, str, int]] = set()
	for tool, path, line_no in required_tools:
		key = (tool, path.as_posix(), line_no)
		if key in seen:
			continue
		seen.add(key)
		deduped.append((tool, path, line_no))

	for tool, path, line_no in deduped:
		if _dockerfile_has_tool(context, tool.strip().lower()):
			continue
		findings.append(
			Finding(
				path=path,
				line=line_no,
				rule_id="external-tool-dependencies",
				message=f"Tool '{tool}' is referenced by validation tests but Dockerfile.app has no install evidence.",
				hint="Install the tool in validation/Dockerfile.app or remove it from custom validation hooks.",
			)
		)

	return findings


def _check_import_audit_safety(context: LintContext) -> list[Finding]:
	findings: list[Finding] = []

	runner_path = context.resolve("tests/20_import_audit.sh")
	runner_lines = context.read_lines(runner_path)
	if runner_lines is None:
		findings.append(
			Finding(
				path=runner_path,
				line=1,
				rule_id="import-audit-safety",
				message="tests/20_import_audit.sh is missing.",
				hint="Add an import-audit wrapper script that executes _lib/import_audit.py.",
			)
		)
	else:
		runner_text = "\n".join(runner_lines)
		if "_lib/import_audit.py" not in runner_text:
			findings.append(
				Finding(
					path=runner_path,
					line=1,
					rule_id="import-audit-safety",
					message="tests/20_import_audit.sh does not invoke _lib/import_audit.py.",
					hint="Call python3 tests/_lib/import_audit.py from tests/20_import_audit.sh.",
				)
			)
		if not any(re.search(r"\bpython3\b", line) for line in runner_lines if not line.lstrip().startswith("#")):
			findings.append(
				Finding(
					path=runner_path,
					line=1,
					rule_id="import-audit-safety",
					message="tests/20_import_audit.sh must invoke import audit through python3.",
					hint="Run the import audit helper via python3 to keep interpreter selection explicit.",
				)
			)

	audit_path = context.resolve("tests/_lib/import_audit.py")
	audit_lines = context.read_lines(audit_path)
	if audit_lines is None:
		findings.append(
			Finding(
				path=audit_path,
				line=1,
				rule_id="import-audit-safety",
				message="tests/_lib/import_audit.py is missing.",
				hint="Add import_audit.py with subprocess-isolated import checks.",
			)
		)
		return findings

	audit_text = "\n".join(audit_lines)

	required_snippets = (
		"import subprocess",
		"subprocess.run(",
		"sys.executable",
		"importlib.import_module",
	)
	for snippet in required_snippets:
		if snippet in audit_text:
			continue
		findings.append(
			Finding(
				path=audit_path,
				line=1,
				rule_id="import-audit-safety",
				message=f"import_audit.py is missing required safety snippet: {snippet!r}.",
				hint="Keep import checks subprocess-isolated using sys.executable and importlib.import_module.",
			)
		)

	forbidden_patterns = (
		(re.compile(r"\beval\s*\("), "Use of eval() is forbidden in import audit."),
		(re.compile(r"\bexec\s*\("), "Use of exec() is forbidden in import audit."),
		(
			re.compile(r"\bimportlib\.util\.spec_from_file_location\b"),
			"Dynamic module loading via importlib.util.spec_from_file_location is forbidden.",
		),
		(
			re.compile(r"\bSourceFileLoader\b"),
			"Dynamic module loading via SourceFileLoader is forbidden.",
		),
		(
			re.compile(r"^\s*(?:from\s+dependency_auditing\s+import|import\s+dependency_auditing)\b", re.MULTILINE),
			"Direct dependency_auditing import is forbidden; import checks must run in subprocess isolation.",
		),
	)

	for pattern, message in forbidden_patterns:
		line_no = context.find_line(audit_path, pattern)
		if line_no == 1 and not pattern.search(audit_text):
			continue
		if pattern.search(audit_text):
			findings.append(
				Finding(
					path=audit_path,
					line=line_no,
					rule_id="import-audit-safety",
					message=message,
					hint="Limit import_audit.py to controlled subprocess import probes.",
				)
			)

	return findings


RULES: tuple[Rule, ...] = (
	Rule(
		rule_id="service-tool-scope",
		description="Service-side CLIs must not appear in app CANARY_TOOLS without Dockerfile install evidence.",
		checker=_check_service_tool_scope,
	),
	Rule(
		rule_id="shell-parity",
		description="Shell invocations must use /bin/sh -c parity form; login shells are forbidden.",
		checker=_check_shell_parity,
	),
	Rule(
		rule_id="canary-ordering",
		description="Top-level test scripts must keep 00_canary.sh first and use deterministic NN_*.sh naming.",
		checker=_check_canary_ordering,
	),
	Rule(
		rule_id="test-script-prologue",
		description="Top-level test scripts must include bash shebang, strict mode, and executable permissions.",
		checker=_check_test_script_prologue,
	),
	Rule(
		rule_id="healthcheck-test-strings",
		description="Compose healthcheck.test entries must be explicitly quoted YAML strings.",
		checker=_check_healthcheck_test_strings,
	),
	Rule(
		rule_id="init-true-required",
		description="Long-running compose services must explicitly set init: true.",
		checker=_check_init_true_required,
	),
	Rule(
		rule_id="graceful-shutdown-hooks",
		description="Validation harness must include graceful shutdown helper wiring.",
		checker=_check_graceful_shutdown_hooks,
	),
	Rule(
		rule_id="bounded-tail-capture",
		description="Failure hooks must capture bounded stdout/stderr tails.",
		checker=_check_bounded_tail_capture,
	),
	Rule(
		rule_id="external-tool-dependencies",
		description="External tools referenced by tests must be installed in Dockerfile.app.",
		checker=_check_external_tool_dependencies,
	),
	Rule(
		rule_id="import-audit-safety",
		description="Import audit checks must stay subprocess-isolated and avoid unsafe dynamic loading.",
		checker=_check_import_audit_safety,
	),
)

RULE_MAP: dict[str, Rule] = {rule.rule_id: rule for rule in RULES}


def _run_rules(context: LintContext, selected_rule_ids: list[str] | None) -> list[Finding]:
	active_rules = RULES if selected_rule_ids is None else tuple(RULE_MAP[rule_id] for rule_id in selected_rule_ids)

	findings: list[Finding] = []
	for rule in active_rules:
		for finding in rule.checker(context):
			if rule.allow_escape and context.is_escaped(finding.path, finding.line, finding.rule_id):
				continue
			findings.append(finding)

	findings.sort(key=lambda item: (item.path.as_posix(), item.line, item.rule_id, item.message))
	return findings


def _render_text(findings: list[Finding], context: LintContext) -> str:
	if not findings:
		return "validation-lint: OK"

	rendered: list[str] = []
	for finding in findings:
		rendered.append(
			f"{context.display_path(finding.path)}:{finding.line}: "
			f"[{finding.rule_id}] {finding.message} Hint: {finding.hint}"
		)
	rendered.append(f"validation-lint: {len(findings)} violation(s)")
	return "\n".join(rendered)


def _render_json(findings: list[Finding], context: LintContext) -> str:
	payload = [
		{
			"path": context.display_path(finding.path),
			"line": finding.line,
			"rule_id": finding.rule_id,
			"message": finding.message,
			"hint": finding.hint,
		}
		for finding in findings
	]
	return json.dumps(payload, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Lint rendered validation harness files.")
	parser.add_argument("validation_root", help="Root directory containing rendered validation harness files")
	parser.add_argument(
		"--rules",
		default="",
		help="Comma-separated rule IDs to run (default: run all rules)",
	)
	parser.add_argument(
		"--format",
		choices=("text", "json"),
		default="text",
		help="Diagnostic output format",
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)

	validation_root = Path(args.validation_root)
	if not validation_root.exists() or not validation_root.is_dir():
		print(
			f"validation-lint: path is missing or not a directory: {validation_root}",
			file=sys.stderr,
		)
		return 2

	selected_rule_ids: list[str] | None = None
	if args.rules.strip():
		selected_rule_ids = [item.strip() for item in args.rules.split(",") if item.strip()]
		unknown = sorted({rule_id for rule_id in selected_rule_ids if rule_id not in RULE_MAP})
		if unknown:
			print(
				"validation-lint: unknown rule id(s): " + ", ".join(unknown),
				file=sys.stderr,
			)
			return 2

	context = LintContext(validation_root)
	findings = _run_rules(context, selected_rule_ids)

	if args.format == "json":
		print(_render_json(findings, context))
	else:
		print(_render_text(findings, context))

	return 1 if findings else 0


if __name__ == "__main__":
	raise SystemExit(main())
