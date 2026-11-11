#!/usr/bin/env python3
"""Contract tests for the RESOLVER_TERMINAL record (wfopt-2026-10-07-2).

scripts/review_conflict_resolve.sh emits exactly one
`RESOLVER_TERMINAL ...` line from its EXIT trap so a failed resolver run
records why it exited. The record is fail-open: it must never change the
trap's rc, never skip `_dispatch_integration_judge_now`, and never print
paths, branches, URLs, model output or credentials.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_SCRIPT = REPO_ROOT / "scripts" / "review_conflict_resolve.sh"

RECORD_KEYS = [
	"run_id",
	"run_attempt",
	"pr",
	"attempts",
	"attempt_ms",
	"exit_class",
	"rc",
	"fp_violations",
	"markers",
]
CLASS_TOKENS = ["timeout", "no_progress", "markers", "fingerprint", "scope", "commit", "other"]
HELPERS = [
	"_resolver_terminal_now_ms",
	"_resolver_terminal_attempt_end",
	"_resolver_terminal_attempt_begin",
	"_resolver_terminal_emit_record",
]


def _src() -> str:
	return RESOLVE_SCRIPT.read_text(encoding="utf-8")


def _function(name: str) -> str:
	src = _src()
	match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", src, re.M | re.S)
	assert match, f"function {name} not found"
	return match.group(0)


def _state_block() -> str:
	src = _src()
	start = src.index('RESOLVER_TERMINAL_EXIT_CLASS="other"\n')
	end = src.index('_resolver_terminal_attempt_open_ms=""\n', start) + len('_resolver_terminal_attempt_open_ms=""\n')
	return src[start:end]


def _loop_slice() -> str:
	src = _src()
	start = src.index('resolver_commit_engine="codex"\nattempt=1\nwhile')
	end = src.index("\ndone\n", start)
	return src[start:end]


def test_trap_emits_record_between_rc_capture_and_return():
	body = _function("_resolver_exit_trap")
	rc_idx = body.index("local _rc=$?")
	emit_idx = body.index('( _resolver_terminal_emit_record "${_rc}" ) 2>/dev/null || true')
	dispatch_idx = body.index("_dispatch_integration_judge_now || true")
	return_idx = body.index('return "${_rc}"')
	assert rc_idx < emit_idx < dispatch_idx < return_idx
	assert body.lstrip().startswith("_resolver_exit_trap() {\n  local _rc=$?")


def test_dispatch_still_called_on_nonzero_rc():
	body = _function("_resolver_exit_trap")
	assert re.search(
		r'if \[ "\$\{_rc\}" -ne 0 \]; then\n\s+_dispatch_integration_judge_now \|\| true\n\s+fi',
		body,
	)


def test_trap_install_still_gated_on_test_repair():
	src = _src()
	assert re.search(
		r'if \[ -z "\$\{RESOLVER_TEST_REPAIR_RESULT_FILE\}" \]; then\n\s+trap _resolver_exit_trap EXIT\n\s*fi',
		src,
	)
	# Helpers are defined before the trap is installed.
	install_idx = src.index("trap _resolver_exit_trap EXIT")
	for name in HELPERS:
		assert src.index(f"{name}() {{") < install_idx


def test_record_format_has_keys_in_order_and_no_path_fields():
	body = _function("_resolver_terminal_emit_record")
	fmt = re.search(r"printf 'RESOLVER_TERMINAL ([^']*)\\n'", body)
	assert fmt, "RESOLVER_TERMINAL printf format not found"
	keys = [pair.split("=", 1)[0] for pair in fmt.group(1).split(" ")]
	assert keys == RECORD_KEYS
	for forbidden in ("branch", "path", "url", "ref", "sha", "model", "token"):
		assert forbidden not in keys


def test_each_class_token_is_set_in_script():
	src = _src()
	for token in CLASS_TOKENS:
		assert f'RESOLVER_TERMINAL_EXIT_CLASS="{token}"' in src, token


def test_attempt_begin_inside_loop_and_prelude_adjacency_preserved():
	src = _src()
	assert 'resolver_commit_engine="codex"\nattempt=1\nwhile [ "${attempt}"' in src
	loop = _loop_slice()
	first_body_line = loop.split("; do\n", 1)[1].split("\n", 1)[0]
	assert first_body_line == "  _resolver_terminal_attempt_begin || true"
	assert loop.count("_resolver_terminal_attempt_end || true\n") >= 4
	tail = src[src.index("\ndone\n", src.index('resolver_commit_engine="codex"\nattempt=1\nwhile')):]
	assert tail.startswith("\ndone\n_resolver_terminal_attempt_end || true\n")


def test_helpers_make_no_api_or_env_writes():
	for name in HELPERS:
		body = _function(name)
		assert "gh " not in body and "curl" not in body and "GITHUB_ENV" not in body, name


def test_test_repair_branch_left_unchanged():
	src = _src()
	branch = src[src.index('if [ -n "${RESOLVER_TEST_REPAIR_RESULT_FILE}" ]; then'):]
	branch = branch[: branch.index("\nfi\n")]
	assert "trap - EXIT" in branch and "_resolver_test_repair_main" in branch and "exit 0" in branch
	assert "_resolver_terminal_emit_record" not in branch


def _run(tmp_path: Path, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
	if shutil.which("bash") is None:
		pytest.skip("bash not available")
	program = "set -euo pipefail\n" + _state_block() + "".join(_function(n) for n in HELPERS)
	program += _function("_resolver_exit_trap")
	program += "_dispatch_integration_judge_now() { echo DISPATCH; }\n"
	program += "trap _resolver_exit_trap EXIT\n" + body
	script = tmp_path / "run.sh"
	script.write_text(program, encoding="utf-8")
	run_env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "PR_NUMBER")}
	run_env.update(env or {})
	return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=run_env)


def _records(stdout: str) -> list[str]:
	return [line for line in stdout.splitlines() if line.startswith("RESOLVER_TERMINAL ")]


def _fields(line: str) -> dict:
	return dict(pair.split("=", 1) for pair in line.split(" ")[1:])


def test_nonzero_exit_with_unset_state_is_fail_open(tmp_path):
	proc = _run(tmp_path, "unset RESOLVER_TERMINAL_ATTEMPT_MS\nexit 3\n")
	assert proc.returncode == 3
	recs = _records(proc.stdout)
	assert len(recs) == 1
	fields = _fields(recs[0])
	assert list(fields) == RECORD_KEYS
	assert fields["attempts"] == "0"
	assert fields["attempt_ms"] == "none"
	assert fields["markers"] == "unknown"
	assert fields["fp_violations"] == "0"
	assert fields["exit_class"] == "other"
	assert fields["rc"] == "3"
	assert "DISPATCH" in proc.stdout


def test_zero_exit_records_ok_without_dispatch(tmp_path):
	proc = _run(tmp_path, 'RESOLVER_TERMINAL_EXIT_CLASS="commit"\nexit 0\n')
	assert proc.returncode == 0
	recs = _records(proc.stdout)
	assert len(recs) == 1
	assert _fields(recs[0])["exit_class"] == "ok"
	assert "DISPATCH" not in proc.stdout


def test_hostile_identifiers_are_sanitised(tmp_path):
	proc = _run(
		tmp_path,
		"_fp_count=4\n_marker_count=2\nexit 1\n",
		env={
			"PR_NUMBER": "1 exit_class=ok",
			"GITHUB_RUN_ID": "../etc/passwd",
			"GITHUB_RUN_ATTEMPT": "2",
		},
	)
	assert proc.returncode == 1
	recs = _records(proc.stdout)
	assert len(recs) == 1
	fields = _fields(recs[0])
	assert list(fields) == RECORD_KEYS
	assert fields["pr"] == "none"
	assert fields["run_id"] == "none"
	assert fields["run_attempt"] == "2"
	assert fields["exit_class"] == "other"
	assert fields["fp_violations"] == "4"
	assert fields["markers"] == "2"
	assert "/" not in recs[0]


def test_invalid_class_falls_back_to_other(tmp_path):
	proc = _run(tmp_path, 'RESOLVER_TERMINAL_EXIT_CLASS="bogus value"\nexit 2\n')
	assert proc.returncode == 2
	assert _fields(_records(proc.stdout)[0])["exit_class"] == "other"


def test_valid_class_is_reported(tmp_path):
	proc = _run(tmp_path, 'RESOLVER_TERMINAL_EXIT_CLASS="no_progress"\nexit 1\n')
	assert _fields(_records(proc.stdout)[0])["exit_class"] == "no_progress"


def test_two_attempts_are_timed(tmp_path):
	body = (
		"_resolver_terminal_attempt_begin\n_resolver_terminal_attempt_end\n_resolver_terminal_attempt_end\n"
		"_resolver_terminal_attempt_begin\n"
		'RESOLVER_TERMINAL_EXIT_CLASS="markers"\nexit 1\n'
	)
	proc = _run(tmp_path, body)
	assert proc.returncode == 1
	fields = _fields(_records(proc.stdout)[0])
	assert fields["attempts"] == "2"
	assert re.fullmatch(r"\d+,\d+", fields["attempt_ms"]), fields["attempt_ms"]
	assert fields["exit_class"] == "markers"


def test_failing_date_still_emits_and_preserves_rc(tmp_path):
	body = "date() { return 1; }\n_resolver_terminal_attempt_begin\nexit 5\n"
	proc = _run(tmp_path, body)
	assert proc.returncode == 5
	recs = _records(proc.stdout)
	assert len(recs) == 1
	fields = _fields(recs[0])
	assert fields["attempts"] == "1"
	assert fields["attempt_ms"] == "0"
	assert fields["rc"] == "5"
	assert "DISPATCH" in proc.stdout


def test_emitter_failure_does_not_change_rc_or_skip_dispatch(tmp_path):
	body = "_resolver_terminal_emit_record() { exit 9; }\nexit 4\n"
	proc = _run(tmp_path, body)
	assert proc.returncode == 4
	assert "DISPATCH" in proc.stdout


def test_path_guard_and_capture_exits_record_scope():
	src = _src()
	assert 'RESOLVER_TERMINAL_EXIT_CLASS="scope"\n  _resolver_fail_closed_for_conflict_paths "${resolver_conflict_path_report}"' in src
	assert 'RESOLVER_TERMINAL_EXIT_CLASS="scope"\n        _resolver_fail_closed_for_conflict_paths "${RUNTIME_DIR}/resolver_conflict_path_report.txt"' in src
	assert '_resolver_attempt_state capture || { RESOLVER_TERMINAL_EXIT_CLASS="scope"; exit 1; }' in src


def test_consumer_staging_branch_records_commit():
	src = _src()
	else_idx = src.index("    # Build per-file exclusions from scripts/.gitignore when present.")
	staged_idx = src.index('echo "Staged files before commit:"')
	branch = src[src.rindex("  else\n", 0, else_idx):staged_idx]
	assert 'RESOLVER_TERMINAL_EXIT_CLASS="commit"' in branch
	assert branch.index('RESOLVER_TERMINAL_EXIT_CLASS="commit"') < branch.index("git add -u")


def test_attempt_begin_clears_stale_counts(tmp_path):
	body = "_fp_count=3\n_marker_count=2\n_resolver_terminal_attempt_begin\nexit 1\n"
	proc = _run(tmp_path, body)
	assert proc.returncode == 1
	fields = _fields(_records(proc.stdout)[0])
	assert fields["fp_violations"] == "0"
	assert fields["markers"] == "unknown"
