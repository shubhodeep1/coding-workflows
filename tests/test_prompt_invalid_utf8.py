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
3. (#9949) Persona sources and prompt contracts are trusted config, so they
   stay strict, but a bad byte now raises `PromptLoadError` /
   `ContractLoadError` naming the path instead of a traceback.
   `render_prompt.sh`'s placeholder scan reads with `errors="replace"`, so a
   trusted template with one bad byte still renders its env placeholders.
   The fallback `_embed_input_file` in `scripts/review_rb_judge.sh` steps its
   cut back to a character boundary (at most 3 bytes) and counts the bytes
   actually shown.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
GH_HELPERS_SH = REPO_ROOT / "scripts" / "gh_helpers.sh"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
REVIEW_RB_JUDGE_SH = REPO_ROOT / "scripts" / "review_rb_judge.sh"


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


def _load_render_prompt_module():
	spec = importlib.util.spec_from_file_location("render_prompt_invalid_utf8_probe", RENDER_PROMPT_PY)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	# @dataclass resolves annotations through sys.modules, so register first.
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def test_persona_invalid_utf8_raises_prompt_load_error() -> None:
	render_prompt = _load_render_prompt_module()
	with tempfile.TemporaryDirectory() as tmp:
		prompts_dir = Path(tmp) / "prompts"
		prompts_dir.mkdir()
		persona_file = prompts_dir / "_prelude_role_persona.txt"
		persona_file.write_bytes(b'{"mode-implement": "caf\xff"}\n')
		prompt_file = prompts_dir / "mode-implement.txt"
		prompt_file.write_text("Header\n", encoding="utf-8")
		try:
			render_prompt.load_phase_c_persona_prefixes(prompt_file)
		except render_prompt.PromptLoadError as exc:
			message = str(exc)
		else:
			raise AssertionError("expected PromptLoadError for a non-UTF-8 persona source")
	assert message.startswith("Unable to decode persona source "), message
	assert str(persona_file.resolve()) in message, message
	assert "caf" not in message, message


def test_contract_invalid_utf8_raises_contract_load_error() -> None:
	render_prompt = _load_render_prompt_module()
	with tempfile.TemporaryDirectory() as tmp:
		contract_file = Path(tmp) / "mode-utf8probe.yml"
		contract_file.write_bytes(b"required_vars:\n  - SECRET_\xffNAME\n")
		try:
			render_prompt._load_contract_payload(contract_file)
		except render_prompt.ContractLoadError as exc:
			message = str(exc)
		else:
			raise AssertionError("expected ContractLoadError for a non-UTF-8 contract")
	assert message.startswith("Unable to decode contract "), message
	assert str(contract_file) in message, message
	assert "required_vars" not in message, message


def _rb_judge_fallback_embedder_source() -> str:
	text = REVIEW_RB_JUDGE_SH.read_text(encoding="utf-8")
	start = text.index("if ! command -v _embed_input_file >/dev/null 2>&1; then\n")
	end = text.index("\nfi\n", text.index("  _embed_input_file() {\n", start)) + len("\nfi\n")
	return text[start:end]


def _run_rb_judge_fallback_embed(path: Path, cap: int, tmp_dir: str) -> tuple[subprocess.CompletedProcess[bytes], str]:
	env = _base_env()
	env["TMPDIR"] = tmp_dir
	state_out = Path(tmp_dir) / "budget_state_after.txt"
	script = (
		"set -euo pipefail\n"
		+ _rb_judge_fallback_embedder_source()
		+ "_init_prompt_budget\n"
		+ '_embed_input_file "$1" "$2" head\n'
		+ 'cat "$(_prompt_budget_state_file)" > "$3"\n'
		+ "_cleanup_prompt_budget\n"
	)
	proc = subprocess.run(
		["bash", "-c", script, "rb_embed", str(path), str(cap), str(state_out)],
		env=env,
		capture_output=True,
		timeout=60,
	)
	state = state_out.read_text(encoding="utf-8").strip() if state_out.exists() else ""
	return proc, state


def test_rb_judge_fallback_embed_reports_trimmed_count() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		input_file = Path(tmp) / "input.txt"
		# "abc" + U+20AC (e2 82 ac) + "def"; a 5-byte cap lands inside the euro sign.
		input_file.write_bytes("abc€def".encode("utf-8"))
		proc, state = _run_rb_judge_fallback_embed(input_file, 5, tmp)
		leftover = sorted(p.name for p in Path(tmp).glob("_embed_trim_count.*"))
	assert proc.returncode == 0, proc.stderr
	stdout = proc.stdout.decode("utf-8")  # strict: must be valid UTF-8
	assert stdout.startswith("abc\n"), stdout
	assert "first 3 bytes shown above" in stdout, stdout
	assert "fallback embedder" in stdout, stdout
	assert state == "3", state
	assert leftover == [], leftover


def test_rb_judge_fallback_embed_bounds_walk_back_on_malformed_input() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		input_file = Path(tmp) / "input.txt"
		# A run of continuation bytes has no character boundary within 3 bytes
		# of the cut; an unbounded walk-back emptied the prefix.
		input_file.write_bytes(b"\x80" * 10)
		proc, state = _run_rb_judge_fallback_embed(input_file, 5, tmp)
	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.startswith(b"\x80" * 5 + b"\n"), proc.stdout
	assert b"first 5 bytes shown above" in proc.stdout, proc.stdout
	assert state == "5", state


def test_render_prompt_sh_trusted_template_invalid_utf8_keeps_env_placeholders() -> None:
	# Built by concatenation so this source never carries a contiguous braced
	# token that a renderer could fill from the environment (#9465).
	probe_token = "{{" + "RENDER_PROMPT_TEST_PLAIN_PROBE" + "}}"
	with tempfile.TemporaryDirectory() as tmp:
		prompts_dir = Path(tmp) / "prompts"
		prompts_dir.mkdir()
		prompt_file = prompts_dir / "mode-utf8probe.txt"
		prompt_file.write_bytes(b"Header \xff\n" + probe_token.encode("ascii") + b"\nFooter\n")
		env = _base_env()
		env["PROMPT_PRELUDE_REFACTOR_ENABLED"] = "false"
		# Trusted template render: neither untrusted-body flag is set.
		env.pop("RENDER_PROMPT_SKIP_SYNTAX_VALIDATION", None)
		env.pop("RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED", None)
		env["RENDER_PROMPT_TEST_PLAIN_PROBE"] = "plain_probe_value"
		proc = subprocess.run(
			["bash", str(RENDER_PROMPT_SH), str(prompt_file)],
			cwd=tmp,
			env=env,
			capture_output=True,
			timeout=60,
		)
	stdout = proc.stdout.decode("utf-8")
	stderr = proc.stderr.decode("utf-8", errors="replace")
	assert proc.returncode == 0, stderr
	assert "Traceback" not in stderr, stderr
	assert [line for line in stderr.splitlines() if line.startswith("ERROR:")] == [], stderr
	warning_lines = [line for line in stderr.splitlines() if line.startswith("WARNING:")]
	assert len(warning_lines) == 1, stderr
	assert "invalid UTF-8" in warning_lines[0], stderr
	assert "plain_probe_value" in stdout, stdout
	assert probe_token not in stdout, stdout
	assert "\ufffd" in stdout, stdout
	assert "Header" in stdout and "Footer" in stdout, stdout


def main() -> int:
	test_render_prompt_cli_invalid_utf8_exits_zero()
	test_render_prompt_cli_valid_utf8_has_no_warning()
	test_embed_input_file_head_truncation_mid_char_is_valid_utf8()
	test_embed_input_file_head_truncation_four_byte_char_is_valid_utf8()
	test_embed_input_file_ascii_head_truncation_is_unchanged()
	test_persona_invalid_utf8_raises_prompt_load_error()
	test_contract_invalid_utf8_raises_contract_load_error()
	test_rb_judge_fallback_embed_reports_trimmed_count()
	test_rb_judge_fallback_embed_bounds_walk_back_on_malformed_input()
	test_render_prompt_sh_trusted_template_invalid_utf8_keeps_env_placeholders()
	print("OK: invalid UTF-8 prompt rendering and embed truncation assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
