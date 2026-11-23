#!/usr/bin/env python3
"""Contract tests for the find_tracking tracking-issue list limit (BUG-003).

`.github/workflows/orchestrate_poll.yml` step `find_tracking` used to list open
`ai:orchestrator-tracking` issues with a hard-coded `--limit 20`, silently
dropping the oldest projects once more than 20 were open (the failure in
docs/postmortems/2026-05-18-project-2734-stall.md). The limit is now the
validated `ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT` (default 100, 1-1000), and a
listing that reaches the limit emits `ORCH_TRACKING_ISSUE_LIST_TRUNCATED`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
LIMIT_VAR = "ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT"
INVALID_WARNING = "::warning::ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT is invalid"
TRUNCATED_PREFIX = "ORCH_TRACKING_ISSUE_LIST_TRUNCATED"


def _find_tracking_step() -> dict:
	"""Return the find_tracking step's `env` map and `run` body.

	Parsed with the standard library on purpose: the pre-push gate container
	has no PyYAML, and the step uses a plain `env:` map plus a `run: |` literal
	block (clip chomping), which is all this extracts.
	"""
	lines = ORCHESTRATE_POLL_WF.read_text(encoding="utf-8").splitlines()
	id_index = next(i for i, line in enumerate(lines) if line.strip() == "id: find_tracking")
	key_indent = len(lines[id_index]) - len(lines[id_index].lstrip(" "))
	start = id_index
	while not lines[start].lstrip(" ").startswith("- "):
		start -= 1
	end = id_index + 1
	while end < len(lines):
		line = lines[end]
		if line.strip() and len(line) - len(line.lstrip(" ")) < key_indent:
			break
		end += 1
	body = lines[start:end]
	step: dict = {"env": {}}
	i = 0
	while i < len(body):
		line = body[i]
		stripped = line.strip()
		indent = len(line) - len(line.lstrip(" "))
		if indent == key_indent and stripped == "env:":
			i += 1
			while i < len(body) and (not body[i].strip() or len(body[i]) - len(body[i].lstrip(" ")) > key_indent):
				if body[i].strip():
					key, value = body[i].strip().split(":", 1)
					step["env"][key.strip()] = value.strip()
				i += 1
			continue
		if indent == key_indent and stripped == "run: |":
			i += 1
			block: list[str] = []
			while i < len(body) and (not body[i].strip() or len(body[i]) - len(body[i].lstrip(" ")) > key_indent):
				block.append(body[i])
				i += 1
			block_indent = min(len(b) - len(b.lstrip(" ")) for b in block if b.strip())
			text_lines = [b[block_indent:] if b.strip() else "" for b in block]
			while text_lines and not text_lines[-1]:
				text_lines.pop()
			step["run"] = "\n".join(text_lines) + "\n"
			continue
		i += 1
	assert "run" in step, "find_tracking step has no run: | block"
	return step


def _issues(count: int) -> list[dict]:
	return [{"number": 100 + i, "title": f"Project {i}", "labels": []} for i in range(count)]


def _run_step(
	tmp_path: Path,
	limit_env: str | None,
	tracking: list[dict] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[list[str]], Path]:
	"""Run the find_tracking step body against a fake gh."""
	script = _find_tracking_step()["run"].replace("${{ github.repository }}", "owner/repo")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir()
	calls_file = tmp_path / "gh_calls.jsonl"
	gh = bin_dir / "gh"
	gh.write_text(
		"#!/usr/bin/env python3\n"
		"import json, os, sys\n"
		"args = sys.argv[1:]\n"
		"with open(os.environ['FAKE_GH_CALLS'], 'a', encoding='utf-8') as fh:\n"
		"    fh.write(json.dumps(args) + '\\n')\n"
		"if args[:2] == ['issue', 'list']:\n"
		"    sys.stdout.write(os.environ['FAKE_GH_TRACKING'])\n"
		"    sys.exit(0)\n"
		"sys.exit(9)\n",
		encoding="utf-8",
	)
	gh.chmod(0o755)
	output_file = tmp_path / "github_output"
	output_file.write_text("", encoding="utf-8")
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"RUNTIME_DIR": str(runtime_dir),
		"GITHUB_OUTPUT": str(output_file),
		"GITHUB_ENV": str(tmp_path / "github_env"),
		"GITHUB_REPOSITORY": "owner/repo",
		# Keep the standalone probe out of these cases; its fake gh call exits 9.
		"ENABLE_STANDALONE_STALL_RECOVERY": "false",
		"FAKE_GH_CALLS": str(calls_file),
		"FAKE_GH_TRACKING": json.dumps(tracking if tracking is not None else []),
	}
	for inherited in ("GH_TOKEN", "GITHUB_TOKEN", "GH_PAT", "BASH_ENV", "ENV", "WORKSPACE_PATH", LIMIT_VAR):
		env.pop(inherited, None)
	if limit_env is not None:
		env[LIMIT_VAR] = limit_env
	# No scripts/gh_helpers.sh in tmp_path, so the step's gh_retry fallback runs.
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False)
	outputs = dict(line.split("=", 1) for line in output_file.read_text(encoding="utf-8").splitlines() if "=" in line)
	calls = [json.loads(line) for line in calls_file.read_text(encoding="utf-8").splitlines()] if calls_file.exists() else []
	return result, outputs, calls, runtime_dir


def _issue_list_limit(calls: list[list[str]]) -> str:
	issue_lists = [call for call in calls if call[:2] == ["issue", "list"]]
	assert len(issue_lists) == 1, calls
	call = issue_lists[0]
	assert "ai:orchestrator-tracking" in call
	return call[call.index("--limit") + 1]


def test_find_tracking_env_declares_limit_with_default() -> None:
	step = _find_tracking_step()
	assert step["env"][LIMIT_VAR] == "${{ vars.ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT || '100' }}"


def test_find_tracking_run_body_uses_validated_limit() -> None:
	run = _find_tracking_step()["run"]
	assert "--limit 20" not in run
	assert '--limit "${tracking_issue_list_limit}"' in run
	assert '--label "ai:orchestrator-tracking"' in run
	assert "${ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT:-100}" in run
	assert "^[1-9][0-9]*$" in run
	assert "1000" in run
	assert "tracking_issue_list_limit=100" in run
	assert f"{TRUNCATED_PREFIX} limit=" in run
	assert not re.search(r"\$\{\{[^}]*ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT", run)
	# Unchanged parts of the step.
	assert "--json number,title,labels" in run
	assert "set -euo pipefail" in run
	assert '"${RUNTIME_DIR}/tracking_issues.json"' in run


VALID_CASES = (("250", "250"), ("1", "1"), ("1000", "1000"), (None, "100"), ("", "100"))
INVALID_CASES = ("0", "abc", "1001", "007", "-5", "99999999999999999999999", "x::set-output name=a::b")


@pytest.mark.parametrize(("limit_env", "expected"), VALID_CASES)
def test_find_tracking_valid_limits(tmp_path: Path, limit_env: str | None, expected: str) -> None:
	result, outputs, calls, _ = _run_step(tmp_path, limit_env)
	assert result.returncode == 0, result.stderr
	assert _issue_list_limit(calls) == expected
	assert INVALID_WARNING not in result.stdout
	assert outputs["has_tracking"] == "false"


@pytest.mark.parametrize("limit_env", INVALID_CASES)
def test_find_tracking_invalid_limits_fall_back_to_100(tmp_path: Path, limit_env: str) -> None:
	result, _, calls, _ = _run_step(tmp_path, limit_env)
	assert result.returncode == 0, result.stderr
	assert _issue_list_limit(calls) == "100"
	assert "integer expression expected" not in result.stderr
	warnings = [line for line in result.stdout.splitlines() if line.startswith(INVALID_WARNING)]
	assert len(warnings) == 1, result.stdout
	shown = re.search(r"configured='([^']*)'", warnings[0])
	assert shown is not None
	assert len(shown.group(1)) <= 32
	assert re.fullmatch(r"[A-Za-z0-9_-]*", shown.group(1))
	assert "::set-output" not in result.stdout


@pytest.mark.parametrize(("limit_env", "truncated"), (("2", True), ("3", False)))
def test_find_tracking_truncation_warning(tmp_path: Path, limit_env: str, truncated: bool) -> None:
	tracking = _issues(2)
	result, outputs, calls, runtime_dir = _run_step(tmp_path, limit_env, tracking)
	assert result.returncode == 0, result.stderr
	assert _issue_list_limit(calls) == limit_env
	assert outputs["has_tracking"] == "true"
	assert "Found 2 active tracking issue(s)" in result.stdout
	assert json.loads((runtime_dir / "tracking_issues.json").read_text(encoding="utf-8")) == tracking
	line = f"::warning::{TRUNCATED_PREFIX} limit=2 count=2"
	if truncated:
		assert line in result.stdout
	else:
		assert TRUNCATED_PREFIX not in result.stdout


def test_find_tracking_empty_list_not_truncated(tmp_path: Path) -> None:
	result, outputs, calls, _ = _run_step(tmp_path, None, [])
	assert result.returncode == 0, result.stderr
	assert _issue_list_limit(calls) == "100"
	assert TRUNCATED_PREFIX not in result.stdout
	assert outputs["has_tracking"] == "false"


def main() -> int:
	# CI runs this file directly with python3, so run every case here too.
	import tempfile

	def _with_tmp(fn, *args) -> None:
		with tempfile.TemporaryDirectory() as tmp:
			fn(Path(tmp), *args)

	test_find_tracking_env_declares_limit_with_default()
	test_find_tracking_run_body_uses_validated_limit()
	for limit_env, expected in VALID_CASES:
		_with_tmp(test_find_tracking_valid_limits, limit_env, expected)
	for limit_env in INVALID_CASES:
		_with_tmp(test_find_tracking_invalid_limits_fall_back_to_100, limit_env)
	for limit_env, truncated in (("2", True), ("3", False)):
		_with_tmp(test_find_tracking_truncation_warning, limit_env, truncated)
	_with_tmp(test_find_tracking_empty_list_not_truncated)
	print("test_orchestrate_poll_tracking_issue_list_limit: OK")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
