"""Contract tests for the REVIEWER_PROMPT_COMPONENTS log line (Refs #8845).

``scripts/review_run_reviewers.sh`` logs one log-only, fail-open line per
assembled reviewer prompt with the byte count of each input component, the
assembled total and a 12-hex sha256 prefix of the static context. It is
measurement only: it must never change the prompt bytes and must never log
any component content or path.
"""

import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "review_run_reviewers.sh"

LINE_RE = re.compile(
	r"^REVIEWER_PROMPT_COMPONENTS pass=(?P<pass>[A-Za-z0-9._-]+)"
	r" static_bytes=(?P<static>[0-9]+)"
	r" readme_bytes=(?P<readme>[0-9]+)"
	r" memory_bytes=(?P<memory>[0-9]+)"
	r" body_bytes=(?P<body>[0-9]+)"
	r" semble_bytes=(?P<semble>[0-9]+)"
	r" extra_bytes=(?P<extra>[0-9]+)"
	r" total_bytes=(?P<total>[0-9]+)"
	r" static_prefix_sha256=(?P<sha>[0-9a-f]{12}|unavailable)$"
)

SENTINELS = {
	"static": "STATIC_SENTINEL_qz81\nsecond static line\n",
	"readme": "README_SENTINEL_qz82\nreadme line two\n",
	"memory": "MEMORY_SENTINEL_qz83\n",
	"body": "BODY_SENTINEL_qz84\nbody line two\nbody line three\n",
	"semble": "SEMBLE_SENTINEL_qz85\n",
	"extra": "EXTRA_SENTINEL_qz86\nextra two\n",
}


def _script_text() -> str:
	return SCRIPT_PATH.read_text(encoding="utf-8")


def _extract_shell_function(text: str, name: str) -> str:
	start = text.find(f"{name}() {{\n")
	assert start != -1, f"function {name} not found"
	end = text.find("\n}\n", start)
	assert end != -1, f"function {name} has no closing brace"
	return text[start : end + 3]


def _clean_env() -> dict:
	env = {
		key: value
		for key, value in os.environ.items()
		if key not in {"BASH_ENV", "ENV"}
		and not key.startswith("REVIEWER_")
		and key not in {"RUNTIME_DIR", "MEMORY_CONTEXT_FILE", "TOOL_CALL_BUDGET_JUDGE"}
	}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PROMPT_ARTIFACT_PATH_HINT"] = "ARTIFACT_HINT"
	env["PROMPT_RUNTIME_CONTEXT_HINT"] = "RUNTIME_HINT"
	return env


def _run(
	tmp: Path,
	*,
	label: str = "pass1",
	files: tuple = ("static", "readme", "memory", "body", "semble", "extra"),
	assemble: bool = True,
) -> tuple:
	text = _script_text()
	functions = "\n".join(
		_extract_shell_function(text, name)
		for name in (
			"assemble_reviewer_prompt",
			"reviewer_prompt_component_bytes",
			"log_reviewer_prompt_components",
		)
	)
	runtime_dir = tmp / "runtime"
	runtime_dir.mkdir(exist_ok=True)
	paths = {
		"static": tmp / "static.txt",
		"readme": runtime_dir / "static_readme_trimmed.txt",
		"memory": tmp / "memory.txt",
		"body": tmp / "body.txt",
		"semble": tmp / "semble.txt",
		"extra": tmp / "extra.txt",
	}
	for key, path in paths.items():
		if key in files:
			path.write_text(SENTINELS[key], encoding="utf-8")
		elif path.exists():
			path.unlink()
	assembled = tmp / "assembled.txt"
	env = _clean_env()
	env["REVIEWER_STATIC_CONTEXT_PATH"] = str(paths["static"])
	if "readme" in files:
		env["RUNTIME_DIR"] = str(runtime_dir)
	if "memory" in files:
		env["MEMORY_CONTEXT_FILE"] = str(paths["memory"])
	if "semble" in files:
		env["REVIEWER_SEMBLE_CONTEXT_FILE"] = str(paths["semble"])
	extra_arg = str(paths["extra"]) if "extra" in files else ""
	script = (
		"set -euo pipefail\n"
		"append_reviewer_checklist_block() { echo CHECKLIST_STUB; }\n"
		f"{functions}\n"
		'MEMORY_CONTEXT_FILE="${MEMORY_CONTEXT_FILE:-}"\n'
		+ ('assemble_reviewer_prompt "$2" "$3" "$4"\n' if assemble else "")
		+ 'if ! log_reviewer_prompt_components "$1" "$2" "$3" "$4"; then\n'
		'  echo "::warning::REVIEWER_PROMPT_COMPONENTS could not be computed; continuing (log-only)."\n'
		"fi\n"
	)
	proc = subprocess.run(
		["bash", "-c", script, "x", label, str(assembled), str(paths["body"]), extra_arg],
		cwd=str(tmp),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)
	return proc, paths, assembled


class ReviewerPromptComponentsTest(unittest.TestCase):
	def _parse(self, proc) -> dict:
		self.assertEqual(proc.returncode, 0, proc.stderr)
		lines = proc.stdout.splitlines()
		self.assertEqual(len(lines), 1, proc.stdout)
		match = LINE_RE.match(lines[0])
		self.assertIsNotNone(match, lines[0])
		return match.groupdict()

	def test_counts_match_fixture_sizes_and_total_matches_assembled_file(self):
		with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
			proc, _paths, assembled = _run(Path(tmp))
			fields = self._parse(proc)
			for key in ("static", "readme", "memory", "body", "semble", "extra"):
				with self.subTest(component=key):
					self.assertEqual(int(fields[key]), len(SENTINELS[key].encode("utf-8")))
			self.assertEqual(int(fields["total"]), assembled.stat().st_size)
			self.assertEqual(fields["pass"], "pass1")
			self.assertIn("CHECKLIST_STUB", assembled.read_text(encoding="utf-8"))

	def test_missing_optional_components_count_zero(self):
		with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
			proc, _paths, assembled = _run(Path(tmp), files=("static", "body"))
			fields = self._parse(proc)
			for key in ("readme", "memory", "semble", "extra"):
				with self.subTest(component=key):
					self.assertEqual(fields[key], "0")
			self.assertEqual(int(fields["body"]), len(SENTINELS["body"].encode("utf-8")))
			self.assertEqual(int(fields["total"]), assembled.stat().st_size)

	def test_static_prefix_sha_is_stable_and_tracks_content(self):
		with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
			tmp_p = Path(tmp)
			first = self._parse(_run(tmp_p)[0])["sha"]
			second = self._parse(_run(tmp_p)[0])["sha"]
			self.assertRegex(first, r"^[0-9a-f]{12}$")
			self.assertEqual(first, second)
			expected = hashlib.sha256(SENTINELS["static"].encode("utf-8")).hexdigest()[:12]
			self.assertEqual(first, expected)
		original = SENTINELS["static"]
		try:
			SENTINELS["static"] = original + "changed\n"
			with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
				changed = self._parse(_run(Path(tmp))[0])["sha"]
		finally:
			SENTINELS["static"] = original
		self.assertNotEqual(changed, first)

	def test_missing_static_file_reports_unavailable(self):
		with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
			proc, _paths, _assembled = _run(
				Path(tmp),
				files=("body",),
				assemble=False,
			)
			fields = self._parse(proc)
			self.assertEqual(fields["sha"], "unavailable")
			self.assertEqual(fields["static"], "0")
			self.assertEqual(fields["total"], "0")

	def test_pass_label_is_whitelisted(self):
		with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
			proc, _paths, _assembled = _run(Path(tmp), label="bad label;$(x)")
			self.assertEqual(self._parse(proc)["pass"], "unknown")
			self.assertNotIn("bad label", proc.stdout + proc.stderr)

	def test_no_fixture_content_or_paths_are_logged(self):
		with tempfile.TemporaryDirectory(prefix="rpc-") as tmp:
			proc, _paths, _assembled = _run(Path(tmp))
			self._parse(proc)
			output = proc.stdout + proc.stderr
			for key, value in SENTINELS.items():
				with self.subTest(component=key):
					self.assertNotIn(value.splitlines()[0], output)
			self.assertNotIn(tmp, output)
			self.assertNotIn("::warning::", output)

	def test_call_sites_are_wired_after_each_assembly(self):
		text = _script_text()
		self.assertEqual(len(re.findall(r"^reviewer_prompt_component_bytes\(\) \{", text, re.M)), 1)
		self.assertEqual(len(re.findall(r"^log_reviewer_prompt_components\(\) \{", text, re.M)), 1)
		calls = re.findall(r'if ! log_reviewer_prompt_components "([^"]+)"', text)
		self.assertEqual(calls, ["default", "pass1", "pass2"])
		size_echo = text.index('echo "Reviewer prompt assembled size: ${reviewer_prompt_assembled_bytes} bytes')
		default_call = text.index('if ! log_reviewer_prompt_components "default" "${REVIEWER_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}"; then')
		self.assertLess(
			text.index('assemble_reviewer_prompt "${REVIEWER_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}"\n'),
			size_echo,
		)
		self.assertLess(size_echo, default_call)
		pass1_assemble = 'assemble_reviewer_prompt "${PASS1_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}"\n'
		pass1_call = 'if ! log_reviewer_prompt_components "pass1" "${PASS1_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}"; then'
		self.assertIn(pass1_assemble + "    " + pass1_call, text)
		pass2_assemble = 'assemble_reviewer_prompt "${PASS2_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}" "${CROSS_POLLINATION_FILE}"\n'
		pass2_call = 'if ! log_reviewer_prompt_components "pass2" "${PASS2_PROMPT_FILE}" "${REVIEWER_PROMPT_BODY_FILE}" "${CROSS_POLLINATION_FILE}"; then'
		self.assertIn(pass2_assemble + "  " + pass2_call, text)
		self.assertEqual(text.count("REVIEWER_PROMPT_COMPONENTS could not be computed"), 3)


if __name__ == "__main__":
	unittest.main()
