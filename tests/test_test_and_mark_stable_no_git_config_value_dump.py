#!/usr/bin/env python3
"""Keep checkout credentials out of the release-gate Git diagnostic logs."""

from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
CONFIG_LIST = re.compile(r"(?:(?<=-c ')|\$\(|(?<![\w\"'\\(]))git\s+config\b[^\n]*?(?:--(?:list|get-regexp)\b|(?<!\S)-l\b)")


def _config_list_lines(text: str) -> list[str]:
	return re.sub(r"\\\r?\n[ \t]*", " ", text).splitlines()


def _config_dumps_values(line: str) -> bool:
	if line.lstrip().startswith("#"):
		return False
	for config_match in CONFIG_LIST.finditer(line):
		config_lexer = shlex.shlex(line[config_match.start():], posix=True, punctuation_chars=";&|()")
		config_lexer.whitespace_split = True
		try:
			for config_arg in config_lexer:
				if config_arg == "--name-only":
					break
				if config_arg in (";", "&&", "||", "|", "&", ")"):
					return True
			else:
				return True
		except ValueError:
			return True
	return False


def _job_block(text: str, job_id: str) -> str:
	match = re.search(rf"^  {re.escape(job_id)}:\s*$", text, re.MULTILINE)
	assert match, f"Missing job: {job_id}"
	next_job = re.search(r"^  [a-z0-9-]+:\s*$", text[match.end():], re.MULTILINE)
	return text[match.start():match.end() + next_job.start() if next_job else len(text)]


def _diag_run_body(text: str) -> str:
	match = re.search(r"^      - &git-checkout-diag\n(?:        .*\n)*?        run: \|\n", text, re.MULTILINE)
	assert match, "Missing git-checkout-diag run body"
	body_lines = []
	for line in text[match.end():].splitlines(keepends=True):
		if line.strip() and not line.startswith("          "):
			break
		body_lines.append(line)
	assert body_lines, "Empty git-checkout-diag run body"
	return textwrap.dedent("".join(body_lines))


def test_no_workflow_dumps_git_config_values() -> None:
	files = sorted([
		*(REPO_ROOT / ".github" / "workflows").glob("*.yml"),
		*(REPO_ROOT / "workflow-templates").glob("*.yml"),
	])
	assert files, "No workflows to check"
	for path in files:
		for line_number, line in enumerate(_config_list_lines(path.read_text(encoding="utf-8")), 1):
			assert not _config_dumps_values(line), f"{path.relative_to(REPO_ROOT)}:{line_number} dumps Git config values"


def test_config_list_matches_shell_continuation() -> None:
	continued_command = "git config \\\n    --list --show-origin"
	assert any(_config_dumps_values(line) for line in _config_list_lines(continued_command))


def test_config_get_regexp_requires_name_only() -> None:
	unsafe_command = "EXTRAHEADER_KEYS_STDOUT=$(git config --local --get-regexp 'http\\..*\\.extraheader')"
	assert _config_dumps_values(unsafe_command)
	assert not CONFIG_LIST.search('echo "git config --get-regexp failed"')


def test_config_scanner_handles_wrappers_and_comments() -> None:
	assert _config_dumps_values("if git config --list --show-origin; then true; fi")
	assert _config_dumps_values("bash -c 'cd /tmp && git config --list --show-origin'")
	assert _config_dumps_values("sh -c 'git config --list'")
	assert _config_dumps_values("git config --list --show-origin # --name-only")
	assert _config_dumps_values("git config --list; echo --name-only")
	assert not _config_dumps_values("git config --list --name-only --show-origin # diagnostic")
	assert not _config_dumps_values("# git config --list --show-origin")
	assert not _config_dumps_values('echo "unset_rc=skipped (git config --get-regexp failed)"')
	assert _config_dumps_values('echo "$(git config --list)"')
	assert not _config_dumps_values("AUTHOR=$(git config --get user.name)")


def test_release_job_does_not_run_checkout_diagnostic() -> None:
	block = _job_block(WORKFLOW.read_text(encoding="utf-8"), "release")
	assert "*git-checkout-diag" not in block
	assert not any(_config_dumps_values(line) for line in _config_list_lines(block))


def test_diag_anchor_prints_keys_only() -> None:
	body = _diag_run_body(WORKFLOW.read_text(encoding="utf-8"))
	list_lines = [line for line in _config_list_lines(body) if CONFIG_LIST.search(line)]
	assert list_lines and all(not _config_dumps_values(line) for line in list_lines)


def test_diag_body_does_not_leak_extraheader_value() -> None:
	body = _diag_run_body(WORKFLOW.read_text(encoding="utf-8"))
	with tempfile.TemporaryDirectory() as temp_dir:
		env = os.environ.copy()
		for key in (
			"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
			"GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_PARAMETERS",
			"BASH_ENV", "ENV",
		):
			env.pop(key, None)
		env["HOME"] = temp_dir
		env["GIT_CONFIG_NOSYSTEM"] = "1"
		subprocess.run(["git", "init", "-q"], cwd=temp_dir, env=env, check=True)
		header_value = base64.b64encode(b"x-access-token:example-credential").decode("ascii")
		remote_value = "sentinel-remote-value-6531"
		subprocess.run(
			["git", "config", "--local", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {header_value}"],
			cwd=temp_dir, env=env, check=True,
		)
		subprocess.run(
			["git", "config", "--local", "remote.origin.url", f"https://x-access-token:{remote_value}@github.com/o/r"],
			cwd=temp_dir, env=env, check=True,
		)
		result = subprocess.run(["bash", "-c", body], cwd=temp_dir, env=env, capture_output=True, text=True, check=False)
		output = result.stdout + result.stderr
		assert result.returncode == 0, f"Diagnostic failed (exit {result.returncode})"
		assert header_value not in output and remote_value not in output, "Git config value leaked"
		assert "http.https://github.com/.extraheader" in output, "Git config key was not reported"


def main() -> int:
	test_functions = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
	for func in test_functions:
		func()
	print(f"OK: {len(test_functions)} release diagnostic credential dump checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
