#!/usr/bin/env python3
"""Contract tests for the extracted plan Codex runner."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
PRE_EXTRACTION_PROMPT_SHA256 = (
	"8975d0d700693d1125170ee5ba76c5193370992207056236f86f7c61b27d80f6"
)
PRE_EXTRACTION_PROMPT_BYTES = 13_107
PRE_EXTRACTION_PROMPT_LINES = 205


def _workflow_step(workflow_text: str, step_name: str) -> str:
	marker = f"      - name: {step_name}\n"
	start = workflow_text.find(marker)
	assert start != -1, f"missing workflow step: {step_name}"
	end = workflow_text.find("\n      - name:", start + len(marker))
	return workflow_text[start:] if end == -1 else workflow_text[start:end]


def _inline_prompt(runner_text: str) -> str:
	marker = 'cat > "${PROMPT_TEMPLATE_FILE}" <<\'EOF\'\n'
	start = runner_text.find(marker)
	assert start != -1, "plan runner no longer writes mode-plan-inline.txt"
	start += len(marker)
	end = runner_text.find("\nEOF\n", start)
	assert end != -1, "plan runner prompt heredoc is unterminated"
	return runner_text[start:end] + "\n"


def test_workflow_stages_and_invokes_extracted_runner() -> None:
	workflow_text = PLAN_WORKFLOW.read_text(encoding="utf-8")
	step = _workflow_step(workflow_text, "Run Codex planning")

	assert "for f in gh_helpers.sh run_plan_codex.sh render_prompt.sh" in workflow_text
	assert "if: env.SKIP_PLAN != 'true'" in step
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in step
	assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in step
	assert "TOOL_CALL_BUDGET: ${{ vars.TOOL_CALL_BUDGET_PLAN || '40' }}" in step
	assert step.split("        run: |\n", 1)[1] == (
		"          bash scripts/run_plan_codex.sh\n"
	)
	assert len(step.encode("utf-8")) < 2_000


def test_extracted_prompt_matches_pre_extraction_bytes() -> None:
	runner_text = PLAN_RUNNER.read_text(encoding="utf-8")
	prompt_bytes = _inline_prompt(runner_text).encode("utf-8")

	assert len(prompt_bytes) == PRE_EXTRACTION_PROMPT_BYTES
	assert len(prompt_bytes.splitlines()) == PRE_EXTRACTION_PROMPT_LINES
	assert hashlib.sha256(prompt_bytes).hexdigest() == PRE_EXTRACTION_PROMPT_SHA256
	assert "${{" not in runner_text
	assert runner_text.count("repos/${GITHUB_REPOSITORY}/issues/comments/") == 2


def _write_executable(path: Path, content: str) -> None:
	path.write_text(content, encoding="utf-8")
	path.chmod(0o755)


def _run_runner(
	scenario: str,
) -> tuple[
	subprocess.CompletedProcess[str], Path, tempfile.TemporaryDirectory[str]
]:
	temporary_directory = tempfile.TemporaryDirectory(prefix="plan-codex-runner-")
	root = Path(temporary_directory.name)
	runtime_dir = root / "runtime"
	scripts_dir = root / "scripts"
	prompts_dir = root / "prompts"
	mock_bin_dir = root / "mock-bin"
	for directory in (runtime_dir, scripts_dir, prompts_dir, mock_bin_dir):
		directory.mkdir(parents=True)

	(scripts_dir / PLAN_RUNNER.name).write_text(
		PLAN_RUNNER.read_text(encoding="utf-8"), encoding="utf-8"
	)
	_write_executable(
		scripts_dir / "render_prompt.sh",
		"""#!/usr/bin/env bash
set -euo pipefail
python3 - "$1" <<'PY'
import os
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
rendered = re.sub(
    r"{{([A-Z][A-Z0-9_]*)}}",
    lambda match: os.environ.get(match.group(1), match.group(0)),
    text,
)
sys.stdout.write(rendered)
PY
""",
	)
	_write_executable(
		mock_bin_dir / "gh",
		"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${MOCK_LOG_DIR}/gh.log"
""",
	)
	_write_executable(
		mock_bin_dir / "sleep",
		"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$1" >> "${MOCK_LOG_DIR}/sleep.log"
""",
	)
	_write_executable(
		mock_bin_dir / "codex",
		"""#!/usr/bin/env bash
set -euo pipefail
attempt_file="${MOCK_LOG_DIR}/attempt-count"
attempt=0
if [ -f "${attempt_file}" ]; then
  attempt="$(cat "${attempt_file}")"
fi
attempt=$((attempt + 1))
printf '%s\n' "${attempt}" > "${attempt_file}"
printf '%s\n' "$*" >> "${MOCK_LOG_DIR}/codex-args.log"
cat > "${MOCK_LOG_DIR}/prompt-${attempt}.txt"
case "${MOCK_CODEX_SCENARIO}" in
  success)
    printf 'primary plan output\n'
    ;;
  retry_then_fallback)
    if [ "${attempt}" -eq 1 ]; then
      exit 7
    elif [ "${attempt}" -eq 2 ]; then
      printf '   \n'
    else
      printf 'fallback plan output\n'
    fi
    ;;
  exhaust)
    exit 9
    ;;
  *)
    exit 64
    ;;
esac
""",
	)

	(root / "pre_assembled_static.txt").write_text(
		"STATIC CONTEXT\n", encoding="utf-8"
	)
	(prompts_dir / "header.txt").write_text(
		"{{REPO_LEARNINGS}}", encoding="utf-8"
	)
	(runtime_dir / "repo_learnings.txt").write_text(
		"REPOSITORY LEARNINGS\n", encoding="utf-8"
	)
	(runtime_dir / "memory_context.txt").write_text(
		"MEMORY CONTEXT\n", encoding="utf-8"
	)
	(runtime_dir / "planning_context.txt").write_text(
		"DYNAMIC PLANNING CONTEXT\n", encoding="utf-8"
	)

	environment = os.environ.copy()
	environment.update(
		{
			"CODEX_OUTPUT_FILE": str(runtime_dir / "codex_output.txt"),
			"CODEX_PROMPT_FILE": str(runtime_dir / "codex_prompt.txt"),
			"GITHUB_REPOSITORY": "example/repository",
			"MOCK_CODEX_SCENARIO": scenario,
			"MOCK_LOG_DIR": str(runtime_dir),
			"MODEL_EDITOR": "primary/model",
			"MODEL_EDITOR_FALLBACK": "fallback/model",
			"PATH": f"{mock_bin_dir}{os.pathsep}{environment['PATH']}",
			"PLAN_DIAGRAMS_OPTIONAL": "true",
			"PLAN_PROGRESS_COMMENT_ID": "12345",
			"PLAN_REUSE_AUDIT_REQUIRED": "true",
			"PLAN_SCOPE_MODE_REQUIRED": "true",
			"PLANNING_CONTEXT_FILE": str(runtime_dir / "planning_context.txt"),
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUNTIME_DIR": str(runtime_dir),
			"TOOL_CALL_BUDGET": "40",
		}
	)
	for key in ("BASH_ENV", "ENV"):
		environment.pop(key, None)

	result = subprocess.run(
		["bash", "scripts/run_plan_codex.sh"],
		cwd=root,
		env=environment,
		text=True,
		capture_output=True,
		check=False,
	)
	return result, root, temporary_directory


def _read_lines(path: Path) -> list[str]:
	return path.read_text(encoding="utf-8").splitlines()


def test_primary_success_preserves_prompt_order_and_outputs() -> None:
	result, root, _temporary_directory = _run_runner("success")
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "Codex planning succeeded on attempt 1 (1 lines of output)." in result.stdout
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == (
		"primary plan output\n"
	)
	assert _read_lines(runtime_dir / "codex-args.log") == [
		"--ask-for-approval never -c model_verbosity=low "
		"-c include_apply_patch_tool=true exec --skip-git-repo-check "
		"--model primary/model --sandbox danger-full-access"
	]
	assert (runtime_dir / "codex_log.txt").is_file()
	assert _read_lines(runtime_dir / "gh.log") == [
		"api repos/example/repository/issues/comments/12345 -X PATCH "
		"-f body=<!-- ai:plan-progress -->⏳ Planning in progress — invoking "
		"model (primary/model)…"
	]
	prompt_text = (runtime_dir / "codex_prompt.txt").read_text(encoding="utf-8")
	ordered_sections = [
		"STATIC CONTEXT",
		"TOOL_CALL_BUDGET: 40",
		"=== PLANNING TASK ===",
		"REPOSITORY LEARNINGS",
		"=== AI MEMORY CONTEXT ===",
		"MEMORY CONTEXT",
		"=== PLANNING CONTEXT ===",
		"DYNAMIC PLANNING CONTEXT",
	]
	positions = [prompt_text.index(section) for section in ordered_sections]
	assert positions == sorted(positions)


def test_retries_backoff_comments_and_final_fallback_are_preserved() -> None:
	result, root, _temporary_directory = _run_runner("retry_then_fallback")
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "Codex exited with code 7 on attempt 1." in result.stdout
	assert "Codex returned empty output on attempt 2." in result.stdout
	assert "Retrying in 10s..." in result.stdout
	assert "Retrying in 20s..." in result.stdout
	assert (
		"Final attempt: switching editor model to fallback fallback/model "
		"(primary primary/model capacity-limited)."
	) in result.stdout
	assert _read_lines(runtime_dir / "sleep.log") == ["10", "20"]
	assert [
		line.split("--model ", 1)[1].split(" ", 1)[0]
		for line in _read_lines(runtime_dir / "codex-args.log")
	] == ["primary/model", "primary/model", "fallback/model"]
	assert _read_lines(runtime_dir / "gh.log") == [
		"api repos/example/repository/issues/comments/12345 -X PATCH "
		"-f body=<!-- ai:plan-progress -->⏳ Planning in progress — invoking "
		"model (primary/model)…",
		"api repos/example/repository/issues/comments/12345 -X PATCH "
		"-f body=<!-- ai:plan-progress -->⏳ Planning in progress — model attempt "
		"1 failed, retrying (1/3)…",
		"api repos/example/repository/issues/comments/12345 -X PATCH "
		"-f body=<!-- ai:plan-progress -->⏳ Planning in progress — model attempt "
		"2 failed, retrying (2/3)…",
	]
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == (
		"fallback plan output\n"
	)


def test_retry_exhaustion_preserves_failure_exit() -> None:
	result, root, _temporary_directory = _run_runner("exhaust")
	runtime_dir = root / "runtime"

	assert result.returncode == 1
	assert "::error::Codex planning failed after 3 attempts." in result.stdout
	assert _read_lines(runtime_dir / "sleep.log") == ["10", "20"]
	assert len(_read_lines(runtime_dir / "codex-args.log")) == 3
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == ""


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
