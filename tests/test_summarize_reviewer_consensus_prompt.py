#!/usr/bin/env python3
"""The consensus summariser prompt must keep the model off its tools.

Runs 36518137244, 36545663590, 36573586774, 36598991686, 36656409877 and
36666750539 failed the same way: the prompt told the summariser that the
"full untruncated per-reviewer outputs remain on disk" under
PREVIOUS_REVIEWS_DIR (a /tmp path), so the model tried to Read those files
instead of summarising the inlined inputs. OpenCode's reviewer config has no
external_directory rule, so every read was auto-rejected, the session ended
with no text, and all 10 attempts produced empty stdout.

These tests run the real script with a stub OpenCode helper that captures
the prompt on stdin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SUMMARISER_SCRIPT = REPO_ROOT / "scripts" / "summarize_reviewer_consensus.sh"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"

STUB_HELPERS = """\
opencode_emit_failure_alert() { return 0; }
opencode_require_bootstrap() { return 0; }
opencode_strip_ansi() { cat; }
opencode_run_cmd() {
	cat > "${SUMMARISER_TEST_PROMPT_CAPTURE}"
	printf '=== CONSENSUS FINDINGS ===\\n(No findings reported.)\\n=== END CONSENSUS FINDINGS ===\\n'
}
"""


def _run_summariser(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], str, Path]:
	reviews_dir = tmp_path / "previous_reviews"
	runtime_dir = tmp_path / "runtime"
	support_dir = tmp_path / "support"
	workspace = tmp_path / "workspace"
	for directory in (reviews_dir, runtime_dir, support_dir, workspace):
		directory.mkdir()

	(reviews_dir / "review_z-ai_glm-5_2.txt").write_text(
		"CORRECTNESS & LOGIC\nFile: scripts/foo.sh\nLine or code reference: 12\n",
		encoding="utf-8",
	)
	# The shapes that tempted the model to open the on-disk copies: a
	# narration-only output and a failure notice.
	(reviews_dir / "review_deepseek_deepseek-v4-pro.txt").write_text(
		"Reading the runtime context to verify this is a docs-only commit.\n",
		encoding="utf-8",
	)
	(reviews_dir / "review_minimax_minimax-m3.txt").write_text(
		"Reviewer minimax/minimax-m3 failed after non-retryable error on attempt 1.\n",
		encoding="utf-8",
	)

	helpers = support_dir / "opencode_helpers.sh"
	helpers.write_text(STUB_HELPERS, encoding="utf-8")
	writer = support_dir / "write_opencode_config.sh"
	writer.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

	capture = tmp_path / "prompt.txt"
	output = tmp_path / "consensus.txt"
	env = {
		"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
		"HOME": str(tmp_path),
		"PREVIOUS_REVIEWS_DIR": str(reviews_dir),
		"RUNTIME_DIR": str(runtime_dir),
		"SUPPORT_SCRIPTS_DIR": str(support_dir),
		"OPENCODE_HELPERS_PATH": str(helpers),
		"OPENCODE_CONFIG_WRITER_PATH": str(writer),
		"RUNNER_TEMP": str(tmp_path),
		"GITHUB_WORKSPACE": str(workspace),
		"SUMMARISER_TEST_PROMPT_CAPTURE": str(capture),
	}
	result = subprocess.run(
		["bash", str(SUMMARISER_SCRIPT), "--prefix", "review", "--output", str(output)],
		env=env,
		capture_output=True,
		text=True,
		timeout=60,
	)
	prompt = capture.read_text(encoding="utf-8") if capture.exists() else ""
	return result, prompt, reviews_dir


def test_summariser_prompt_does_not_point_at_on_disk_reviews(tmp_path: Path) -> None:
	result, prompt, reviews_dir = _run_summariser(tmp_path)
	assert result.returncode == 0, result.stderr
	assert prompt, "stub OpenCode never received the prompt"
	assert str(reviews_dir) not in prompt
	assert "remain on disk" not in prompt
	assert "may open those files" not in prompt


def test_summariser_prompt_forbids_tool_calls(tmp_path: Path) -> None:
	result, prompt, _ = _run_summariser(tmp_path)
	assert result.returncode == 0, result.stderr
	assert "Do NOT call any tool" in prompt
	assert "do not try to open reviewer outputs on disk" in prompt
	assert "summarise exactly what it" in prompt


def test_summariser_prompt_still_inlines_every_input(tmp_path: Path) -> None:
	result, prompt, _ = _run_summariser(tmp_path)
	assert result.returncode == 0, result.stderr
	for slug in ("z-ai_glm-5_2", "deepseek_deepseek-v4-pro", "minimax_minimax-m3"):
		assert f"--- BEGIN REVIEW OUTPUT FROM {slug} ---" in prompt
		assert f"--- END REVIEW OUTPUT FROM {slug} ---" in prompt
	assert "File: scripts/foo.sh" in prompt
	assert (tmp_path / "consensus.txt").read_text(encoding="utf-8").startswith("=== CONSENSUS FINDINGS ===")


def _telegram_failure_step() -> str:
	text = REVIEW_WORKFLOW.read_text(encoding="utf-8")
	start = text.index("      - name: Telegram failure\n")
	end = text.index("\n      - name: ", start + 1)
	return text[start:end]


def test_telegram_failure_names_branch_when_no_pr() -> None:
	step = _telegram_failure_step()
	assert 'if [ -n "${PR_NUMBER:-}" ]; then' in step
	assert 'MSG+="PR: ${PR_URL}"' in step
	assert 'MSG+="Branch: ${TARGET_BRANCH:-${HEAD_REF_OVERRIDE_INPUT:-unknown}} (no PR)"' in step


def _extract_shell_function(path: Path, name: str) -> str:
	lines = path.read_text(encoding="utf-8").splitlines()
	start = lines.index(f"{name}() {{")
	end = lines.index("}", start)
	return "\n".join(lines[start:end + 1]) + "\n"


def test_pass2_cross_pollination_header_does_not_point_at_on_disk_reviews(tmp_path: Path) -> None:
	# Pass-2 reviewers use the same reviewer-role OpenCode config, so a read of
	# the pass-1 files under PREVIOUS_REVIEWS_DIR (/tmp) is rejected the same way.
	reviewers_script = REPO_ROOT / "scripts" / "review_run_reviewers.sh"
	function_src = _extract_shell_function(reviewers_script, "build_cross_pollination_summary")
	reviews_dir = tmp_path / "previous_reviews"
	runtime_dir = tmp_path / "runtime"
	reviews_dir.mkdir()
	runtime_dir.mkdir()
	ledger = tmp_path / "ledger.txt"
	ledger.write_text("=== CONSENSUS FINDINGS ===\n(No findings reported.)\n=== END CONSENSUS FINDINGS ===\n", encoding="utf-8")
	result = subprocess.run(
		["bash", "-c", function_src + 'cat "$(build_cross_pollination_summary "$1")"', "harness", str(ledger)],
		env={
			"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
			"RUNTIME_DIR": str(runtime_dir),
			"PREVIOUS_REVIEWS_DIR": str(reviews_dir),
		},
		capture_output=True,
		text=True,
		timeout=30,
	)
	assert result.returncode == 0, result.stderr
	summary = result.stdout
	assert str(reviews_dir) not in summary
	assert "remain on disk" not in summary
	assert "Do not try to open the raw" in summary
	assert "=== CONSENSUS FINDINGS ===" in summary
	assert summary.rstrip().endswith("=== END CROSS-POLLINATION SUMMARY ===")
