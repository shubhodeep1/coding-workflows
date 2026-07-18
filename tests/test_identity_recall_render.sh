#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
export PROMPT_PERSONA_PREFIX_ENABLED=false
export PREVIOUS_ATTEMPT_NUMBER=1
export MAX_ATTEMPTS=3
export PREVIOUS_ATTEMPT_FAILURE_KIND=timeout

python3 - "${REPO_ROOT}" <<'PY'
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(sys.argv[1])
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
MODE_PROMPTS = sorted((REPO_ROOT / "prompts").glob("mode-*.txt"))
ROLE_GOAL_RE = re.compile(r"^Role:\s*(?P<role>.+?)\s+Goal:\s*(?P<goal>.+?)\s*$")
IDENTITY_BLOCK_RE = re.compile(
	r"<identity-recall>\n"
	r"Phase: (?P<phase>.+?)\.\n"
	r"Role: (?P<role>.+?)\.\n"
	r"Mission: (?P<mission>.+?)\.\n"
	r"Re-emit this block at the top of your next message if you\n"
	r"have just performed a context compaction\.\n"
	r"</identity-recall>",
	re.DOTALL,
)


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env.pop("UNATTENDED_IDENTITY_REINJECT_ENABLED", None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env["PROMPT_PERSONA_PREFIX_ENABLED"] = "false"
	env["PREVIOUS_ATTEMPT_NUMBER"] = "1"
	env["MAX_ATTEMPTS"] = "3"
	env["PREVIOUS_ATTEMPT_FAILURE_KIND"] = "timeout"
	return env


def _run_render(
	prompt_path: Path,
	*,
	cwd: Path = REPO_ROOT,
	env_overrides: dict[str, str] | None = None,
	render_prompt_sh: Path = RENDER_PROMPT_SH,
) -> subprocess.CompletedProcess[str]:
	env = _base_env()
	if env_overrides:
		env.update(env_overrides)
	return subprocess.run(
		["bash", str(render_prompt_sh), str(prompt_path)],
		cwd=str(cwd),
		env=env,
		text=True,
		capture_output=True,
		timeout=60,
	)


def _opening_role_goal_end_offset(rendered_text: str) -> int | None:
	paragraph_lines: list[str] = []
	paragraph_end: int | None = None
	started = False
	in_compaction_rules = False
	offset = 0
	for raw_line in rendered_text.splitlines(keepends=True):
		line = raw_line.strip()
		if not started:
			if in_compaction_rules:
				if line == "</compaction-rules>":
					in_compaction_rules = False
				offset += len(raw_line)
				continue
			if not line or line.startswith("#") or (line.startswith("{%") and line.endswith("%}")):
				offset += len(raw_line)
				continue
			if line.startswith("<compaction-rules>"):
				if not line.endswith("</compaction-rules>"):
					in_compaction_rules = True
				offset += len(raw_line)
				continue
			started = True
		if not line:
			paragraph = " ".join(paragraph_lines)
			if paragraph_end is None or ROLE_GOAL_RE.match(paragraph) is None:
				return None
			return paragraph_end
		paragraph_lines.append(line)
		paragraph_end = offset + len(raw_line.rstrip("\r\n"))
		offset += len(raw_line)
	if not started or not paragraph_lines or paragraph_end is None:
		return None
	paragraph = " ".join(paragraph_lines)
	if ROLE_GOAL_RE.match(paragraph) is None:
		return None
	return paragraph_end


def _assert_identity_block(rendered_text: str, *, phase_name: str) -> re.Match[str]:
	match = IDENTITY_BLOCK_RE.search(rendered_text)
	assert match is not None, f"missing identity-recall block for {phase_name}"
	assert match.group("phase") == phase_name, match.groupdict()
	assert match.group("role"), match.groupdict()
	assert match.group("mission"), match.groupdict()
	return match


def _assert_identity_block_after_opening_role_goal(rendered_text: str, *, phase_name: str) -> re.Match[str]:
	match = _assert_identity_block(rendered_text, phase_name=phase_name)
	paragraph_end = _opening_role_goal_end_offset(rendered_text)
	assert paragraph_end is not None, f"missing opening role/goal paragraph for {phase_name}"
	assert rendered_text[paragraph_end:match.start()] == "\n\n", rendered_text[max(0, paragraph_end - 80):match.start() + 80]
	return match


def _copy(src: Path, dst: Path) -> None:
	dst.parent.mkdir(parents=True, exist_ok=True)
	shutil.copy2(src, dst)


def test_flag_off_is_byte_stable_for_mode_prompt_corpus() -> None:
	assert MODE_PROMPTS, "no prompts/mode-*.txt files found"
	for prompt_path in MODE_PROMPTS:
		unset_proc = _run_render(prompt_path)
		false_proc = _run_render(
			prompt_path,
			env_overrides={"UNATTENDED_IDENTITY_REINJECT_ENABLED": "false"},
		)
		assert unset_proc.returncode == 0, f"{prompt_path.name}: {unset_proc.stderr}"
		assert false_proc.returncode == 0, f"{prompt_path.name}: {false_proc.stderr}"
		assert unset_proc.stdout == false_proc.stdout, prompt_path.name
		assert unset_proc.stderr == false_proc.stderr, prompt_path.name


def test_flag_on_renders_identity_block_for_mode_prompt_corpus() -> None:
	for prompt_path in MODE_PROMPTS:
		identity_proc = _run_render(
			prompt_path,
			env_overrides={"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true"},
		)
		assert identity_proc.returncode == 0, f"{prompt_path.name}: {identity_proc.stderr}"
		assert identity_proc.stderr == "", f"{prompt_path.name}: {identity_proc.stderr}"
		assert len(list(IDENTITY_BLOCK_RE.finditer(identity_proc.stdout))) == 1, prompt_path.name
		_assert_identity_block_after_opening_role_goal(identity_proc.stdout, phase_name=prompt_path.stem)


def test_wrapped_goal_paragraph_is_captured_in_full() -> None:
	proc = _run_render(
		REPO_ROOT / "prompts" / "mode-check-failure-triage.txt",
		env_overrides={"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true"},
	)
	assert proc.returncode == 0, proc.stderr
	block = _assert_identity_block(proc.stdout, phase_name="mode-check-failure-triage")
	mission = block.group("mission")
	assert "autonomous AI pipeline (clarify -> plan -> implement -> review)" in mission, mission


def test_crlf_prompt_does_not_leave_carriage_return_before_remainder() -> None:
	with tempfile.TemporaryDirectory(prefix="identity_recall_crlf_") as td:
		prompt_path = Path(td) / "mode-crlf.txt"
		prompt_path.write_bytes(
			b"# tier: DEFAULT\r\nRole: crlf parser. Goal: preserve the remainder body.\r\n\r\nBody follows.\r\n"
		)
		proc = _run_render(
			prompt_path,
			env_overrides={"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true"},
		)
		assert proc.returncode == 0, proc.stderr
		_assert_identity_block_after_opening_role_goal(proc.stdout, phase_name="mode-crlf")
		assert "\n\n\r" not in proc.stdout, proc.stdout


def test_parse_failure_is_fail_open_for_synthetic_malformed_prompt() -> None:
	with tempfile.TemporaryDirectory(prefix="identity_recall_malformed_") as td:
		prompt_path = Path(td) / "mode-malformed.txt"
		prompt_path.write_text(
			"# tier: DEFAULT\nRole: malformed prompt without a goal line.\n\nBody.\n",
			encoding="utf-8",
		)
		baseline = _run_render(prompt_path)
		identity_proc = _run_render(
			prompt_path,
			env_overrides={"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true"},
		)
		assert baseline.returncode == 0, baseline.stderr
		assert identity_proc.returncode == 0, identity_proc.stderr
		assert identity_proc.stdout == baseline.stdout
		assert "<identity-recall>" not in identity_proc.stdout
		assert identity_proc.stderr.strip() == "IDENTITY_REINJECT_PARSE_FAIL: mode-malformed reason=metadata_extract_failed"


def test_identity_only_path_cleans_tmpdir_temp_files() -> None:
	with tempfile.TemporaryDirectory(prefix="identity_recall_tmpdir_") as td:
		tmpdir = Path(td)
		proc = _run_render(
			REPO_ROOT / "prompts" / "mode-plan.txt",
			env_overrides={
				"TMPDIR": str(tmpdir),
				"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true",
			},
		)
		assert proc.returncode == 0, proc.stderr
		assert list(tmpdir.iterdir()) == [], [path.name for path in tmpdir.iterdir()]


def test_inline_prompt_uses_canonical_mode_metadata() -> None:
	with tempfile.TemporaryDirectory(prefix="identity_recall_inline_") as td:
		root = Path(td)
		render_prompt_sh = root / "scripts" / "render_prompt.sh"
		_copy(REPO_ROOT / "scripts" / "render_prompt.sh", root / "scripts" / "render_prompt.sh")
		_copy(REPO_ROOT / "scripts" / "render_prompt.py", root / "scripts" / "render_prompt.py")
		render_prompt_sh.chmod(0o755)
		(root / "prompts").mkdir(parents=True, exist_ok=True)
		(root / "prompts" / "mode-plan.txt").write_text(
			"# tier: DEFAULT\nRole: synthetic inline mapping verifier. Goal: prove the canonical temp prompt is used.\n",
			encoding="utf-8",
		)
		_copy(REPO_ROOT / "prompts" / "_identity_recall.txt", root / "prompts" / "_identity_recall.txt")
		inline_path = root / "runtime" / "mode-plan-inline.txt"
		inline_path.parent.mkdir(parents=True, exist_ok=True)
		inline_path.write_text("Runtime wrapper prelude.\n\nBody follows.\n", encoding="utf-8")
		proc = _run_render(
			inline_path,
			cwd=root,
			env_overrides={"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true"},
			render_prompt_sh=render_prompt_sh,
		)
		assert proc.returncode == 0, proc.stderr
		block = _assert_identity_block(proc.stdout, phase_name="mode-plan")
		assert block.group("role") == "synthetic inline mapping verifier", block.groupdict()


def test_shared_prelude_path_injects_and_cleans_temp_files() -> None:
	with tempfile.TemporaryDirectory(prefix="identity_recall_prelude_") as td:
		root = Path(td)
		render_prompt_sh = root / "scripts" / "render_prompt.sh"
		for relative in (
			"scripts/render_prompt.sh",
			"scripts/render_prompt.py",
			"scripts/assemble_prompt.sh",
			"prompts/mode-plan.txt",
			"prompts/_identity_recall.txt",
			"prompts/_prelude_common.txt",
			"prompts/_prelude_clarify_and_plan.txt",
			"prompts/_prelude_output_contract.txt",
			"prompts/_templates/mode-plan.txt",
			"prompts/contracts/mode-plan.yml",
			"prompts/references/output-contract.txt",
		):
			_copy(REPO_ROOT / relative, root / relative)
		render_prompt_sh.chmod(0o755)
		(root / "scripts" / "assemble_prompt.sh").chmod(0o755)
		before = sorted(path.name for path in (root / "prompts").glob(".mode-plan.txt.*"))
		proc = _run_render(
			root / "prompts" / "mode-plan.txt",
			cwd=root,
			env_overrides={
				"UNATTENDED_IDENTITY_REINJECT_ENABLED": "true",
				"PROMPT_PRELUDE_REFACTOR_ENABLED": "true",
			},
			render_prompt_sh=render_prompt_sh,
		)
		after = sorted(path.name for path in (root / "prompts").glob(".mode-plan.txt.*"))
		assert proc.returncode == 0, proc.stderr
		assert before == after, after
		_assert_identity_block_after_opening_role_goal(proc.stdout, phase_name="mode-plan")


def main() -> int:
	test_flag_off_is_byte_stable_for_mode_prompt_corpus()
	test_flag_on_renders_identity_block_for_mode_prompt_corpus()
	test_wrapped_goal_paragraph_is_captured_in_full()
	test_crlf_prompt_does_not_leave_carriage_return_before_remainder()
	test_parse_failure_is_fail_open_for_synthetic_malformed_prompt()
	test_identity_only_path_cleans_tmpdir_temp_files()
	test_inline_prompt_uses_canonical_mode_metadata()
	test_shared_prelude_path_injects_and_cleans_temp_files()
	print("test_identity_recall_render.sh: PASS")
	return 0


raise SystemExit(main())
PY
