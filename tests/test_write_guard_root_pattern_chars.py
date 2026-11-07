#!/usr/bin/env python3
"""Regression tests for SHELL-001: write_guard_check must strip the repo
root from the logged config= path literally, never as a glob pattern.

Hermetic: every file lives under a temporary directory; nothing is written
into the repository or its .git directory.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WRITE_GUARD_SCRIPT = REPO_ROOT / "scripts" / "write_guard.sh"
CONFIG_REL = ".github/ai/write_guards.v1.json"

GIT_ENV_VARS_TO_DROP = (
	"GIT_DIR",
	"GIT_WORK_TREE",
	"GIT_INDEX_FILE",
	"GIT_COMMON_DIR",
	"GIT_OBJECT_DIRECTORY",
	"GIT_ALTERNATE_OBJECT_DIRECTORIES",
	"SUPPORT_ROOT_DIR",
	"WRITE_GUARDS_ENABLED",
)


class WriteGuardRootPatternCharsTest(unittest.TestCase):
	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.temp_dir = pathlib.Path(os.path.realpath(self._tmp.name))

	def tearDown(self):
		self._tmp.cleanup()

	def _base_env(self):
		env = dict(os.environ)
		for name in GIT_ENV_VARS_TO_DROP:
			env.pop(name, None)
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		env["TMPDIR"] = str(self.temp_dir)
		env["GIT_CEILING_DIRECTORIES"] = str(self.temp_dir.parent)
		return env

	def _make_root(self, name):
		root = self.temp_dir / name
		(root / "scripts").mkdir(parents=True)
		shutil.copy2(WRITE_GUARD_SCRIPT, root / "scripts" / "write_guard.sh")
		subprocess.run(
			["git", "init", "-q"],
			cwd=str(root),
			env=self._base_env(),
			check=True,
			capture_output=True,
			timeout=60,
		)
		return root

	@staticmethod
	def _write_invalid_config(base_dir):
		config_path = base_dir / CONFIG_REL
		config_path.parent.mkdir(parents=True, exist_ok=True)
		config_path.write_text("{invalid json\n", encoding="utf-8")
		return config_path

	def _run_check(self, root, extra_env=None):
		staged_list = self.temp_dir / "staged-files.txt"
		staged_list.write_text(".github/workflows/test.yml\n", encoding="utf-8")
		env = self._base_env()
		if extra_env:
			env.update(extra_env)
		return subprocess.run(
			[
				"bash",
				"-c",
				'source scripts/write_guard.sh; write_guard_check review_editor "$1"',
				"_",
				str(staged_list),
			],
			cwd=str(root),
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)

	def _config_value(self, result):
		for line in result.stdout.splitlines():
			if line.startswith("WRITE_GUARD_CONFIG_ERROR:"):
				self.assertIn(" config=", line)
				self.assertIn(" detail=", line)
				return line.split(" config=", 1)[1].split(" detail=", 1)[0]
		self.fail(
			"no WRITE_GUARD_CONFIG_ERROR line; stdout={!r} stderr={!r}".format(
				result.stdout, result.stderr
			)
		)

	@staticmethod
	def _printf_q(value):
		return subprocess.run(
			["bash", "-c", 'printf %q "$1"', "_", value],
			capture_output=True,
			text=True,
			check=True,
			timeout=30,
		).stdout

	def test_root_with_glob_chars_logs_repo_relative_config(self):
		root = self._make_root("root[ab]*?")
		self._write_invalid_config(root)
		result = self._run_check(root)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._config_value(result), CONFIG_REL)

	def test_sibling_matching_root_only_as_glob_is_not_stripped(self):
		root = self._make_root("root[ab]*?")
		sibling = self.temp_dir / "rootaXY"
		sibling_config = self._write_invalid_config(sibling)
		result = self._run_check(root, {"SUPPORT_ROOT_DIR": str(sibling)})
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		config_value = self._config_value(result)
		self.assertNotEqual(config_value, CONFIG_REL)
		self.assertEqual(config_value, self._printf_q(str(sibling_config)))

	def test_plain_root_still_stripped(self):
		root = self._make_root("plainroot")
		self._write_invalid_config(root)
		result = self._run_check(root)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertEqual(self._config_value(result), CONFIG_REL)


if __name__ == "__main__":
	unittest.main()
