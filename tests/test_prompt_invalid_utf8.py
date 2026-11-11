#!/usr/bin/env python3
"""Invalid UTF-8 in prompt content must not crash prompt rendering.

Background (PR #9180 ``reviewers_failed``, heal issue #9261): every
reviewer crashed in ``render_prompt.py`` with an uncaught
``UnicodeDecodeError``. ``load_prompt`` read the assembled reviewer body
with a strict UTF-8 decode, and ``_embed_input_file`` in
``scripts/gh_helpers.sh`` cut embedded files with a plain ``head -c`` byte
count that can split a multi-byte character. ``load_prompt`` now replaces
undecodable bytes with U+FFFD, and ``_embed_input_file`` drops a trailing
incomplete UTF-8 sequence after a truncation while keeping the marker and
budget accounting exact.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
RENDER_PROMPT_PY = SCRIPTS_DIR / "render_prompt.py"
RENDER_PROMPT_SH = SCRIPTS_DIR / "render_prompt.sh"
GH_HELPERS = SCRIPTS_DIR / "gh_helpers.sh"

INVALID_BODY = b"Reviewer body \xff\xfe and cut dash \xe2\x80\n"
EM_DASH = "—".encode("utf-8")
EMOJI = "\U0001F600".encode("utf-8")


def _env(tmp_path: Path) -> dict[str, str]:
	env = dict(os.environ)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["TMPDIR"] = str(tmp_path)
	return env


def _write_body(tmp_path: Path) -> Path:
	body = tmp_path / "body.txt"
	body.write_bytes(INVALID_BODY)
	return body


def test_render_prompt_cli_invalid_utf8_exits_zero(tmp_path: Path) -> None:
	body = _write_body(tmp_path)
	result = subprocess.run(
		[
			sys.executable,
			str(RENDER_PROMPT_PY),
			"--input-already-assembled",
			"--skip-syntax-validation",
			str(body),
		],
		capture_output=True,
		cwd=str(tmp_path),
		env=_env(tmp_path),
		check=False,
	)
	stderr = result.stderr.decode("utf-8", errors="replace")
	assert result.returncode == 0, stderr
	assert "Traceback" not in stderr
	stdout = result.stdout.decode("utf-8")
	assert "�" in stdout
	assert "Reviewer body" in stdout


def test_render_prompt_sh_invalid_utf8_exits_zero(tmp_path: Path) -> None:
	if not RENDER_PROMPT_SH.is_file():
		pytest.skip("scripts/render_prompt.sh not present")
	body = tmp_path / "body.txt"
	# An env-derived placeholder next to the invalid bytes: the wrapper's
	# collect_prompt_placeholders helper must still discover it.
	body.write_bytes(INVALID_BODY + b"Probe: {{UTF8_HEAL_PROBE_VALUE}}\n")
	env = _env(tmp_path)
	env["RENDER_PROMPT_INPUT_ALREADY_ASSEMBLED"] = "1"
	env["RENDER_PROMPT_SKIP_SYNTAX_VALIDATION"] = "1"
	env["UTF8_HEAL_PROBE_VALUE"] = "probe-substituted"
	result = subprocess.run(
		["bash", str(RENDER_PROMPT_SH), str(body)],
		capture_output=True,
		cwd=str(tmp_path),
		env=env,
		check=False,
	)
	stderr = result.stderr.decode("utf-8", errors="replace")
	stdout = result.stdout.decode("utf-8")
	assert result.returncode == 0, stderr
	assert "Traceback" not in stderr
	assert "Traceback" not in stdout
	assert "�" in stdout
	assert "Probe: probe-substituted" in stdout


def _import_render_prompt():
	if str(SCRIPTS_DIR) not in sys.path:
		sys.path.insert(0, str(SCRIPTS_DIR))
	import render_prompt  # noqa: E402

	return render_prompt


def test_load_prompt_replaces_invalid_bytes(tmp_path: Path) -> None:
	render_prompt = _import_render_prompt()
	body = _write_body(tmp_path)
	text = render_prompt.load_prompt(body)
	assert "�" in text
	assert text.startswith("Reviewer body ")


def test_contract_invalid_utf8_raises_contract_load_error(tmp_path: Path) -> None:
	render_prompt = _import_render_prompt()
	contract = tmp_path / "mode-x.yml"
	contract.write_bytes(b"required_vars: []\n# \xff\n")
	with pytest.raises(render_prompt.ContractLoadError) as excinfo:
		render_prompt.load_contract(contract, "mode-x")
	assert str(contract) in str(excinfo.value)


def _embed(tmp_path: Path, content: bytes, cap: int, mode: str = "head") -> tuple[bytes, str]:
	src = tmp_path / "input.bin"
	src.write_bytes(content)
	script = (
		f'source "{GH_HELPERS}"\n'
		"_init_prompt_budget\n"
		'_embed_input_file "$1" "$2" "$3"\n'
		'cat "$(_prompt_budget_state_file)" >&2\n'
	)
	result = subprocess.run(
		["bash", "-c", script, "embed", str(src), str(cap), mode],
		capture_output=True,
		cwd=str(tmp_path),
		env=_env(tmp_path),
		check=False,
	)
	assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
	budget_lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
	return result.stdout, budget_lines[-1].strip() if budget_lines else ""


def _shown_bytes(stdout_text: str) -> int:
	match = re.search(r"first (\d+) bytes shown", stdout_text)
	assert match is not None, stdout_text
	return int(match.group(1))


def test_embed_input_file_head_truncation_mid_char_is_valid_utf8(tmp_path: Path) -> None:
	content = b"abcdefgh" + EM_DASH * 20
	stdout, budget = _embed(tmp_path, content, 10)
	text = stdout.decode("utf-8")
	assert text.startswith("abcdefgh\n[... TRUNCATED")
	assert _shown_bytes(text) == 8
	assert budget == "8"


def test_embed_input_file_diff_mode_no_boundary_mid_char_is_valid_utf8(tmp_path: Path) -> None:
	content = b"diff --git a/x b/x\n+" + EM_DASH * 40
	# Cap lands one byte into an em-dash; no `^diff --git` past offset 0.
	cap = len(b"diff --git a/x b/x\n+") + 3 * 5 + 1
	stdout, budget = _embed(tmp_path, content, cap, mode="diff")
	text = stdout.decode("utf-8")
	assert _shown_bytes(text) == cap - 1
	assert budget == str(cap - 1)
	assert "mode=diff" in text


@pytest.mark.parametrize("split", [1, 2, 3])
def test_embed_input_file_four_byte_char_split_is_trimmed(tmp_path: Path, split: int) -> None:
	content = b"ab" + EMOJI + b"cd" * 20
	stdout, budget = _embed(tmp_path, content, 2 + split)
	text = stdout.decode("utf-8")
	assert text.startswith("ab\n[... TRUNCATED")
	assert _shown_bytes(text) == 2
	assert budget == "2"


def test_embed_input_file_aligned_cap_is_not_trimmed(tmp_path: Path) -> None:
	content = b"ab" + EMOJI + b"cd" * 20
	stdout, budget = _embed(tmp_path, content, 6)
	text = stdout.decode("utf-8")
	assert text.startswith("ab\U0001F600\n[... TRUNCATED")
	assert _shown_bytes(text) == 6
	assert budget == "6"


def test_embed_input_file_ascii_cut_unchanged(tmp_path: Path) -> None:
	content = b"0123456789" * 10
	stdout, budget = _embed(tmp_path, content, 25)
	text = stdout.decode("utf-8")
	assert text.startswith("0123456789012345678901234\n[... TRUNCATED")
	assert _shown_bytes(text) == 25
	assert budget == "25"
