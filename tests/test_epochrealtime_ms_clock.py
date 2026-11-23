#!/usr/bin/env python3
"""Millisecond clocks read bash's EPOCHREALTIME before `date +%s%3N` (issue #9252).

On ubuntu-26.04 runners (Rust uutils coreutils) `date +%s%3N` sometimes
returned a reading one digit short, which printed negative or wild ``*_ms``
values. PR #9240 fixed ``_orch_poll_cycle_now_ms``; these cases pin the same
fallback chain in every other now-ms function:

1. EPOCHREALTIME, when it looks like ``<sec>.<6 digits>``;
2. ``date +%s%3N``, only when it has 13 or more digits;
3. ``date +%s`` (``date -u +%s`` in the merge train) times 1000.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

# (script, function name, brace on the same line as the name)
CLOCKS = [
	("review_conflict_resolve.sh", "_resolver_terminal_now_ms", True),
	("review_merge_train.sh", "_mt_now_ms", False),
	("review_autofix_step_checkout_pr_head_branch.sh", "review_head_checkout_now_ms", False),
	("semble_helpers.sh", "_semble_now_ms", False),
	("build_semble_wrapper.sh", "semble_build_now_ms", False),
	("install_semble.sh", "semble_install_now_ms", False),
]

FAKE_DATE = """#!/usr/bin/env bash
for arg in "$@"; do
	case "${arg}" in
		+%s%3N) printf '%s\\n' "${FAKE_DATE_MS:-170000000000}"; exit 0 ;;
		+%s) printf '1700000000\\n'; exit 0 ;;
	esac
done
exit 1
"""


def _function(script: str, name: str, same_line: bool) -> str:
	src = (SCRIPTS / script).read_text(encoding="utf-8")
	opener = r"\(\) \{\n" if same_line else r"\(\)\n\{\n"
	match = re.search(rf"^{re.escape(name)}{opener}.*?^\}}\n", src, re.M | re.S)
	assert match, f"function {name} not found in {script}"
	return match.group(0)


def _run_clock(tmp_path: Path, script: str, name: str, same_line: bool, prelude: str, fake_ms: str | None = None) -> str:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	fake_date = bin_dir / "date"
	fake_date.write_text(FAKE_DATE, encoding="utf-8")
	fake_date.chmod(0o755)
	env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
	if fake_ms is not None:
		env["FAKE_DATE_MS"] = fake_ms
	body = "set -euo pipefail\n" + _function(script, name, same_line) + prelude + f"\n{name}\n"
	result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False, env=env)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


@pytest.mark.parametrize(("script", "name", "same_line"), CLOCKS)
def test_epochrealtime_wins_over_a_short_date_reading(tmp_path, script, name, same_line):
	before = time.time() * 1000
	out = _run_clock(tmp_path, script, name, same_line, "")
	after = time.time() * 1000
	assert re.fullmatch(r"[0-9]{13,}", out), out
	assert before - 5000 <= int(out) <= after + 5000, (out, before, after)


@pytest.mark.parametrize(("script", "name", "same_line"), CLOCKS)
@pytest.mark.parametrize("prelude", ["unset EPOCHREALTIME", 'unset EPOCHREALTIME\nEPOCHREALTIME=""', 'unset EPOCHREALTIME\nEPOCHREALTIME="garbage"'])
def test_valid_date_ms_reading_is_used_without_epochrealtime(tmp_path, script, name, same_line, prelude):
	out = _run_clock(tmp_path, script, name, same_line, "\n" + prelude, fake_ms="1700000000123")
	assert out == "1700000000123"


@pytest.mark.parametrize(("script", "name", "same_line"), CLOCKS)
def test_short_date_ms_reading_falls_back_to_whole_seconds(tmp_path, script, name, same_line):
	out = _run_clock(tmp_path, script, name, same_line, "\nunset EPOCHREALTIME")
	assert out == "1700000000000"


@pytest.mark.parametrize(
	("script", "name"),
	[
		("semble_helpers.sh", "_semble_now_ms"),
		("build_semble_wrapper.sh", "semble_build_now_ms"),
		("install_semble.sh", "semble_install_now_ms"),
	],
)
def test_semble_scripts_read_date_ms_only_inside_their_clock(script, name):
	src = (SCRIPTS / script).read_text(encoding="utf-8")
	rest = src.replace(_function(script, name, False), "")
	assert "date +%s%3N" not in rest


def test_semble_start_readings_use_the_new_clocks():
	build = (SCRIPTS / "build_semble_wrapper.sh").read_text(encoding="utf-8")
	install = (SCRIPTS / "install_semble.sh").read_text(encoding="utf-8")
	helpers = (SCRIPTS / "semble_helpers.sh").read_text(encoding="utf-8")
	assert 'SEMBLE_BUILD_START_MS="$(semble_build_now_ms)"' in build
	assert 'index_ms="$(( $(semble_build_now_ms) - SEMBLE_BUILD_START_MS ))"' in build
	assert '[ "${index_ms}" -ge 0 ] || index_ms=0' in build
	assert 'SEMBLE_INSTALL_START_MS="$(semble_install_now_ms)"' in install
	assert install.count('[ "${semble_install_elapsed_ms}" -ge 0 ] || semble_install_elapsed_ms=0') == 2
	assert install.count('write_github_env "SEMBLE_INSTALL_MS" "${semble_install_elapsed_ms}"') == 2
	assert helpers.count('start_ms="$(_semble_now_ms)"') == 3
	assert 'end_ms="$(_semble_now_ms)"' in helpers


def test_install_semble_ms_never_goes_negative(tmp_path):
	# A start reading in the future (a wall-clock step back) must clamp to 0.
	src = (SCRIPTS / "install_semble.sh").read_text(encoding="utf-8")
	mark = re.search(r"^mark_unavailable\(\)\n\{\n.*?^\}\n", src, re.M | re.S)
	assert mark
	env_file = tmp_path / "github_env"
	body = (
		"set -euo pipefail\n"
		+ _function("install_semble.sh", "semble_install_now_ms", False)
		+ "write_github_env() { printf '%s=%s\\n' \"$1\" \"$2\" >> \"${GITHUB_ENV}\"; }\n"
		+ "log() { :; }\n"
		+ "SEMBLE_VERSION=0.1.3\nSEMBLE_INSTALL_START_MS=99999999999999\n"
		+ mark.group(0)
		+ "mark_unavailable\n"
	)
	result = subprocess.run(
		["bash", "-c", body],
		capture_output=True,
		text=True,
		check=False,
		env={"PATH": "/usr/bin:/bin", "GITHUB_ENV": str(env_file)},
	)
	assert result.returncode == 0, result.stderr
	assert "SEMBLE_INSTALL_MS=0\n" in env_file.read_text(encoding="utf-8")
