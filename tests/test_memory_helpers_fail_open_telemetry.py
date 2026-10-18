"""Fail-open telemetry for memory_record_run_event / memory_record_candidate.

The two shell wrappers in scripts/memory_helpers.sh used to emit only
``{"op":...,"ok":false,"fail_open":true,"source":"shell"}`` on failure. They
now append ``exit_code``, ``duration_ms`` and a fixed ``stderr_category``
token, never raw stderr text, and still return 0.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPERS = REPO_ROOT / "scripts" / "memory_helpers.sh"
TELEMETRY_PREFIX = "AI_MEMORY_TELEMETRY: "

FUNCTIONS = {
	"memory_record_run_event": "record-run-event",
	"memory_record_candidate": "record-candidate",
}

STUB = textwrap.dedent(
	"""\
	import os
	import sys

	sys.stderr.write(os.environ.get("STUB_STDERR", ""))
	sys.stdout.write(os.environ.get("STUB_STDOUT", ""))
	sys.exit(int(os.environ.get("STUB_EXIT", "0")))
	"""
)


@pytest.fixture
def stub_dir(tmp_path):
	directory = tmp_path / "stub"
	directory.mkdir()
	(directory / "ai_memory.py").write_text(STUB, encoding="utf-8")
	return directory


def _run(tmp_path, stub_dir, fn, *, stderr="", stdout="", exit_code=0, enabled="true", path_prefix=None, prelude=""):
	tmpdir = tmp_path / "tmp"
	tmpdir.mkdir(exist_ok=True)
	env = dict(os.environ)
	env.update(
		{
			"AI_MEMORY_ENABLED": enabled,
			"PYTHONDONTWRITEBYTECODE": "1",
			"TMPDIR": str(tmpdir),
			"STUB_STDERR": stderr,
			"STUB_STDOUT": stdout,
			"STUB_EXIT": str(exit_code),
		}
	)
	if path_prefix is not None:
		env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
	script = (
		"set -euo pipefail\n"
		f"{prelude}"
		f'source "{HELPERS}"\n'
		f'MEMORY_SCRIPTS_DIR="{stub_dir}"\n'
		f"{fn} --x y\n"
		'echo "RC=$?"\n'
	)
	proc = subprocess.run(
		["bash", "-c", script],
		capture_output=True,
		text=True,
		env=env,
		cwd=str(tmp_path),
		check=False,
	)
	telemetry = [
		line[len(TELEMETRY_PREFIX):]
		for line in (proc.stdout + "\n" + proc.stderr).splitlines()
		if line.startswith(TELEMETRY_PREFIX)
	]
	return proc, telemetry, tmpdir


def _failure_lines(telemetry):
	return [raw for raw in telemetry if '"ok":false' in raw]


@pytest.mark.parametrize("fn", sorted(FUNCTIONS))
@pytest.mark.parametrize(
	("stderr", "category"),
	[
		(" ! [rejected]        ai-memory -> ai-memory (non-fast-forward)\n", "push_rejected"),
		("remote: Permission denied. The requested URL returned error: 403\n", "auth"),
		("fatal: Authentication failed for repo\n", "auth"),
		("fatal: Could not resolve host: github.com\n", "network"),
		("MemoryValidationError: bad\n", "validation"),
		("boom\n", "other"),
		("value 14030 unrelated\n", "other"),
		("HTTP 403: Resource not accessible by integration\n", "auth"),
		("403 Forbidden\n", "auth"),
		('Traceback (most recent call last):\n  File "ai_memory.py", line 403, in main\nKeyError: x\n', "other"),
		("", "none"),
		("error: failed to push some refs\nreturned error: 403\n", "push_rejected"),
	],
)
def test_failure_telemetry_fields(tmp_path, stub_dir, fn, stderr, category):
	op = FUNCTIONS[fn]
	proc, telemetry, tmpdir = _run(tmp_path, stub_dir, fn, stderr=stderr, exit_code=3)

	assert proc.returncode == 0, proc.stderr
	assert "RC=0" in proc.stdout
	failures = _failure_lines(telemetry)
	assert len(failures) == 1, telemetry
	raw = failures[0]
	assert raw.startswith(f'{{"op":"{op}","ok":false,"fail_open":true,"source":"shell",')
	data = json.loads(raw)
	assert data["op"] == op
	assert data["ok"] is False
	assert data["fail_open"] is True
	assert data["source"] == "shell"
	assert data["exit_code"] == 3
	assert isinstance(data["duration_ms"], int) and data["duration_ms"] >= 0
	assert data["stderr_category"] == category
	if stderr:
		assert stderr.strip().splitlines()[0] in proc.stderr
	assert f"::warning::memory {op} failed (fail-open) exit_code=3" in proc.stderr
	assert list(tmpdir.iterdir()) == []


@pytest.mark.parametrize("fn", sorted(FUNCTIONS))
def test_secrets_never_reach_telemetry(tmp_path, stub_dir, fn):
	stderr = "fatal: unable to access 'https://x:tok@github.com/o/r.git': ghp_FAKEtoken1234567890\n"
	proc, telemetry, _ = _run(tmp_path, stub_dir, fn, stderr=stderr, exit_code=1)

	assert proc.returncode == 0
	assert "ghp_FAKEtoken1234567890" in proc.stderr
	assert "https://x:tok@github.com" in proc.stderr
	assert telemetry
	for raw in telemetry:
		assert "ghp_FAKEtoken1234567890" not in raw
		assert "x:tok@" not in raw
	assert json.loads(_failure_lines(telemetry)[0])["stderr_category"] == "other"


@pytest.mark.parametrize("fn", sorted(FUNCTIONS))
def test_success_path_forwards_output_without_failure_telemetry(tmp_path, stub_dir, fn):
	proc, telemetry, tmpdir = _run(
		tmp_path,
		stub_dir,
		fn,
		stderr="AI_MEMORY_TELEMETRY: {\"op\":\"x\",\"ok\":true}\nsome log\n",
		stdout='{"ok": true}\n',
		exit_code=0,
	)

	assert proc.returncode == 0
	assert proc.stdout.startswith('{"ok": true}\n')
	assert "RC=0" in proc.stdout
	assert "some log" in proc.stderr
	assert _failure_lines(telemetry) == []
	assert "::warning::" not in proc.stderr
	assert list(tmpdir.iterdir()) == []


@pytest.mark.parametrize("fn", sorted(FUNCTIONS))
def test_mktemp_failure_reports_unavailable(tmp_path, stub_dir, fn):
	# A shell function shadows mktemp. A stub executable on PATH would be
	# skipped when the temp directory is mounted noexec.
	proc, telemetry, _ = _run(
		tmp_path, stub_dir, fn, stderr="fatal: Could not resolve host\n", exit_code=2, prelude="mktemp() { return 1; }\n"
	)

	assert proc.returncode == 0
	assert "RC=0" in proc.stdout
	assert "Could not resolve host" in proc.stderr
	data = json.loads(_failure_lines(telemetry)[0])
	assert data["stderr_category"] == "unavailable"
	assert data["exit_code"] == 2


@pytest.mark.parametrize("fn", sorted(FUNCTIONS))
def test_disabled_path_unchanged(tmp_path, stub_dir, fn):
	op = FUNCTIONS[fn]
	proc, telemetry, _ = _run(tmp_path, stub_dir, fn, stderr="boom\n", exit_code=5, enabled="false")

	assert proc.returncode == 0
	assert telemetry == [f'{{"op":"{op}","ok":true,"enabled":false,"source":"shell"}}']
	assert "boom" not in proc.stderr
