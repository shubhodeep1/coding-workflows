#!/usr/bin/env python3
"""Invalid UTF-8 must not break prompt rendering or prompt-input embedding (#9261).

1. `load_prompt` in `scripts/render_prompt.py` used to read prompt files
   strictly and catch only `OSError`, so one invalid byte in a prompt body
   (which can carry PR- or issue-derived text) aborted the render with an
   uncaught `UnicodeDecodeError` traceback. It now replaces the bad bytes
   with U+FFFD, warns on stderr, and exits 0.
2. `_embed_input_file` in `scripts/gh_helpers.sh` truncated with `head -c`,
   which could cut a multi-byte UTF-8 character in half and leave invalid
   UTF-8 in the prompt that Codex's strict stdin reader rejects. The head
   cut now steps back to a character boundary.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
GH_HELPERS_SH = REPO_ROOT / "scripts" / "gh_helpers.sh"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env.pop("UNATTENDED_IDENTITY_REINJECT_ENABLED", None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PYTHONIOENCODING"] = "utf-8"
	env["PROMPT_PERSONA_PREFIX_ENABLED"] = "false"
	return env


def test_render_prompt_cli_invalid_utf8_exits_zero() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		prompts_dir = Path(tmp) / "prompts"
		prompts_dir.mkdir()
		prompt_file = prompts_dir / "mode-utf8probe.txt"
		prompt_file.write_bytes(b"hello \xff world\n")
		proc = subprocess.run(
			[
				sys.executable,
				str(RENDER_PROMPT_PY),
				str(prompt_file),
				"--input-already-assembled",
				"--skip-syntax-validation",
			],
			cwd=tmp,
			env=_base_env(),
			capture_output=True,
			timeout=60,
		)
	stdout = proc.stdout.decode("utf-8")
	stderr = proc.stderr.decode("utf-8", errors="replace")
	assert proc.returncode == 0, f"render_prompt.py must exit 0 on invalid UTF-8; stderr={stderr!r}"
	assert "Traceback" not in stderr, stderr
	assert "�" in stdout, stdout
	assert stdout.startswith("hello "), stdout
	assert stdout.rstrip("\n").endswith(" world"), stdout
	assert "invalid UTF-8" in stderr, stderr
	assert "at byte 6" in stderr, stderr


def test_render_prompt_cli_valid_utf8_has_no_warning() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		prompts_dir = Path(tmp) / "prompts"
		prompts_dir.mkdir()
		prompt_file = prompts_dir / "mode-utf8probe.txt"
		prompt_file.write_bytes("hello € world\n".encode("utf-8"))
		proc = subprocess.run(
			[
				sys.executable,
				str(RENDER_PROMPT_PY),
				str(prompt_file),
				"--input-already-assembled",
				"--skip-syntax-validation",
			],
			cwd=tmp,
			env=_base_env(),
			capture_output=True,
			timeout=60,
		)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout == "hello € world\n".encode("utf-8")
	assert b"invalid UTF-8" not in proc.stderr


def _run_embed(path: Path, cap: int, mode: str, tmp_dir: str) -> subprocess.CompletedProcess[bytes]:
	# TMPDIR isolates the per-process `_prompt_budget_state_file` so the run
	# never shares a running total with another process.
	env = _base_env()
	env["TMPDIR"] = tmp_dir
	script = (
		'set -euo pipefail\n'
		'source "$1"\n'
		'_init_prompt_budget\n'
		'_embed_input_file "$2" "$3" "$4"\n'
		'_cleanup_prompt_budget\n'
	)
	return subprocess.run(
		["bash", "-c", script, "embed", str(GH_HELPERS_SH), str(path), str(cap), mode],
		env=env,
		capture_output=True,
		timeout=60,
	)


def test_embed_input_file_head_truncation_mid_char_is_valid_utf8() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		input_file = Path(tmp) / "input.txt"
		# "abc" + U+20AC (3 bytes: e2 82 ac) + "def"; a 5-byte cap lands
		# inside the euro sign.
		input_file.write_bytes("abc€def".encode("utf-8"))
		proc = _run_embed(input_file, 5, "head", tmp)
	assert proc.returncode == 0, proc.stderr
	stdout = proc.stdout.decode("utf-8")  # strict: must be valid UTF-8
	assert stdout.startswith("abc\n"), stdout
	assert "TRUNCATED" in stdout, stdout
	assert "first 3 bytes" in stdout, stdout


def test_embed_input_file_head_truncation_four_byte_char_is_valid_utf8() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		input_file = Path(tmp) / "input.txt"
		# "ab" + U+1F600 (4 bytes) + "z"; caps 3, 4 and 5 all land inside it.
		input_file.write_bytes("ab\U0001F600z".encode("utf-8"))
		for cap in (3, 4, 5):
			proc = _run_embed(input_file, cap, "head", tmp)
			assert proc.returncode == 0, proc.stderr
			stdout = proc.stdout.decode("utf-8")
			assert stdout.startswith("ab\n"), (cap, stdout)
			assert "first 2 bytes" in stdout, (cap, stdout)
		proc = _run_embed(input_file, 6, "head", tmp)
	assert proc.returncode == 0, proc.stderr
	stdout = proc.stdout.decode("utf-8")
	assert stdout.startswith("ab\U0001F600\n"), stdout
	assert "first 6 bytes" in stdout, stdout


def test_embed_input_file_ascii_head_truncation_is_unchanged() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		input_file = Path(tmp) / "input.txt"
		input_file.write_bytes(b"abcdefghij")
		proc = _run_embed(input_file, 4, "head", tmp)
	assert proc.returncode == 0, proc.stderr
	stdout = proc.stdout.decode("utf-8")
	assert stdout.startswith("abcd\n"), stdout
	assert "first 4 bytes" in stdout, stdout


def main() -> int:
	test_render_prompt_cli_invalid_utf8_exits_zero()
	test_render_prompt_cli_valid_utf8_has_no_warning()
	test_embed_input_file_head_truncation_mid_char_is_valid_utf8()
	test_embed_input_file_head_truncation_four_byte_char_is_valid_utf8()
	test_embed_input_file_ascii_head_truncation_is_unchanged()
	print("OK: invalid UTF-8 prompt rendering and embed truncation assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
