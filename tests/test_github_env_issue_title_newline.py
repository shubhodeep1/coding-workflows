#!/usr/bin/env python3
"""The issue title never splits into extra GITHUB_ENV assignments (SEC-002).

clarify.yml ("Fetch issue metadata") and orchestrate_clarify_respond.yml
("Fetch issue and tracking context") write the API-sourced issue title to
$GITHUB_ENV in the single-line NAME=value form. The title is
attacker-influenced, so a CR or LF in it would turn the text after the line
break into another environment assignment for every later step. Both steps
replace CR/LF with spaces before the write and emit
`::warning::ISSUE_TITLE_NEWLINE_STRIPPED issue=<int>` without the title text.

CI runs these cases through tests/test_clarify_respond_standalone.py, which
collects them (ci.yml was outside the file grant of the issue that added them).
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

try:
	import yaml
except ModuleNotFoundError:
	# The credential-free pre-push gate image has no PyYAML; _step_run then
	# reads the step's `run: |` block from the workflow text instead.
	yaml = None

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

ISSUE_TITLE_NEWLINE_CALLSITES = (
	("clarify.yml", "Fetch issue metadata"),
	("orchestrate_clarify_respond.yml", "Fetch issue and tracking context"),
)
ISSUE_TITLE_NEWLINE_IDS = [workflow for workflow, _ in ISSUE_TITLE_NEWLINE_CALLSITES]
ISSUE_TITLE_NEWLINE_TR = "tr '\\r\\n' '  '"
ISSUE_TITLE_NEWLINE_WRITE = "ISSUE_TITLE=${ISSUE_TITLE}"
ISSUE_TITLE_NEWLINE_WARNING = "::warning::ISSUE_TITLE_NEWLINE_STRIPPED issue="
ISSUE_TITLE_NEWLINE_EVIL = "Real title\nEVIL=1\r\nEVIL2=2"
ISSUE_TITLE_NEWLINE_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _indent_of(line: str) -> int:
	return len(line) - len(line.lstrip(" "))


def _step_runs_from_text(text: str, step_name: str) -> list[str]:
	lines = text.splitlines()
	runs: list[str] = []
	for i, line in enumerate(lines):
		if line.strip() != f"- name: {step_name}":
			continue
		item_indent = _indent_of(line)
		key_indent = item_indent + 2
		j = i + 1
		while j < len(lines) and (not lines[j].strip() or _indent_of(lines[j]) > item_indent):
			if _indent_of(lines[j]) == key_indent and lines[j].strip().startswith("run: |"):
				body: list[str] = []
				k = j + 1
				while k < len(lines) and (not lines[k].strip() or _indent_of(lines[k]) > key_indent):
					body.append(lines[k])
					k += 1
				while body and not body[-1].strip():
					body.pop()
				runs.append(textwrap.dedent("\n".join(body)) + "\n")
				break
			j += 1
		else:
			runs.append("")
	return runs


def _step_run(workflow: str, step_name: str) -> str:
	text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
	if yaml is None:
		runs = _step_runs_from_text(text, step_name)
	else:
		data = yaml.safe_load(text)
		runs = [
			step.get("run", "")
			for job in (data.get("jobs") or {}).values()
			for step in (job.get("steps") or [])
			if step.get("name") == step_name
		]
	assert len(runs) == 1, f"{workflow}: expected exactly one step named {step_name!r}, found {len(runs)}"
	return runs[0]


def _substitute(script: str) -> str:
	script = script.replace("${{ github.repository }}", "owner/repo")
	assert "${{" not in script, "unexpected GitHub expression left in the extracted snippet"
	return script


def _title_snippet(workflow: str, step_name: str) -> str:
	lines = _step_run(workflow, step_name).splitlines()
	start = next(i for i, line in enumerate(lines) if line.strip().startswith('ISSUE_TITLE="$('))
	end = next(i for i in range(start, len(lines)) if ISSUE_TITLE_NEWLINE_WRITE in lines[i])
	if workflow == "clarify.yml":
		end = next(i for i in range(end, len(lines)) if lines[i].strip() == '} >> "$GITHUB_ENV"')
	return _substitute("\n".join(lines[start : end + 1]))


def _run_snippet(workflow: str, step_name: str, title: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
	payload = json.dumps({"number": 6262, "html_url": "https://github.com/owner/repo/issues/6262", "title": title, "body": "body"})
	meta_file = tmp_path / "issue_meta.json"
	meta_file.write_text(payload, encoding="utf-8")
	env_file = tmp_path / "github_env"
	env_file.write_text("", encoding="utf-8")
	runtime_dir = tmp_path / "runtime"
	runtime_dir.mkdir()
	env = {
		"PATH": "/usr/local/bin:/usr/bin:/bin",
		"GITHUB_ENV": str(env_file),
		"RUNTIME_DIR": str(runtime_dir),
		"ISSUE_META": payload,
		"ISSUE_META_FILE": str(meta_file),
		"ISSUE_NUMBER": "6262",
		"ISSUE_NUMBER_JSON": "6262",
		"ISSUE_URL_JSON": "https://github.com/owner/repo/issues/6262",
	}
	script = "set -euo pipefail\n" + _title_snippet(workflow, step_name) + "\n"
	result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	return result, env_file.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(("workflow", "step_name"), ISSUE_TITLE_NEWLINE_CALLSITES, ids=ISSUE_TITLE_NEWLINE_IDS)
def test_issue_title_newline_sanitized_before_github_env_write(workflow: str, step_name: str) -> None:
	run = _step_run(workflow, step_name)
	assert ISSUE_TITLE_NEWLINE_TR in run
	assert run.index(ISSUE_TITLE_NEWLINE_TR) < run.index(ISSUE_TITLE_NEWLINE_WRITE)
	assert ISSUE_TITLE_NEWLINE_WARNING in run
	# The single-line write and the variable name are unchanged.
	assert f'echo "{ISSUE_TITLE_NEWLINE_WRITE}"' in run
	assert "ISSUE_TITLE<<" not in run


@pytest.mark.parametrize(("workflow", "step_name"), ISSUE_TITLE_NEWLINE_CALLSITES, ids=ISSUE_TITLE_NEWLINE_IDS)
def test_issue_title_newline_warning_never_echoes_title(workflow: str, step_name: str) -> None:
	warning_lines = [line for line in _step_run(workflow, step_name).splitlines() if "ISSUE_TITLE_NEWLINE_STRIPPED" in line]
	assert warning_lines
	for line in warning_lines:
		assert "ISSUE_TITLE}" not in line
		assert "ISSUE_TITLE_SINGLE_LINE}" not in line


@pytest.mark.parametrize(("workflow", "step_name"), ISSUE_TITLE_NEWLINE_CALLSITES, ids=ISSUE_TITLE_NEWLINE_IDS)
def test_issue_title_newline_injection_is_neutralized(workflow: str, step_name: str, tmp_path: Path) -> None:
	result, env_lines = _run_snippet(workflow, step_name, ISSUE_TITLE_NEWLINE_EVIL, tmp_path)
	assert env_lines
	assert not any(line.startswith("EVIL") for line in env_lines), env_lines
	assert all(ISSUE_TITLE_NEWLINE_ENV_LINE.match(line) for line in env_lines), env_lines
	assert [line for line in env_lines if line.startswith("ISSUE_TITLE=")] == ["ISSUE_TITLE=Real title EVIL=1  EVIL2=2"]
	assert "::warning::ISSUE_TITLE_NEWLINE_STRIPPED issue=6262" in result.stdout
	assert "EVIL" not in result.stdout
	assert "Real title" not in result.stdout


@pytest.mark.parametrize(("workflow", "step_name"), ISSUE_TITLE_NEWLINE_CALLSITES, ids=ISSUE_TITLE_NEWLINE_IDS)
def test_issue_title_newline_clean_title_unchanged(workflow: str, step_name: str, tmp_path: Path) -> None:
	result, env_lines = _run_snippet(workflow, step_name, "Plain title", tmp_path)
	assert "ISSUE_TITLE_NEWLINE_STRIPPED" not in result.stdout
	assert [line for line in env_lines if line.startswith("ISSUE_TITLE=")] == ["ISSUE_TITLE=Plain title"]
