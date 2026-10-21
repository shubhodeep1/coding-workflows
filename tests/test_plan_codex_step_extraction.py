#!/usr/bin/env python3
"""Contract tests for the extracted plan Codex runner."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from runpy import run_path


REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_isolation_fakes import enable_fake_isolation  # noqa: E402
# Issue #7657 added the 11-line item-12 `## Deferred scope` rule to the
# inline prompt (230 -> 241 lines); the pins below include it.
# Issue #7724 added the 4-line "notes must not name another path" rule to
# item 1 `## Files likely to change` (241 -> 245 lines).
PRE_EXTRACTION_PROMPT_SHA256 = (
	"e2e2e8e6ca492208ea4f7ac4a507b91860f30907eeeca7c2a12dd83f7581ab7b"
)
PRE_EXTRACTION_PROMPT_BYTES = 15_938
PRE_EXTRACTION_PROMPT_LINES = 245


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
	stage_step = _workflow_step(workflow_text, "Stage workflow support files")

	assert "for f in gh_helpers.sh run_plan_codex.sh render_prompt.sh" in workflow_text
	assert 'install -m 0644 "${src}" scripts/claude_settings.json.tmpl' in workflow_text
	assert (REPO_ROOT / "scripts" / "claude_settings.json.tmpl").is_file()
	assert "if: env.SKIP_PLAN != 'true'" in step
	assert "GH_TOKEN: ${{ secrets.GH_PAT }}" in step
	assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in step
	assert "TOOL_CALL_BUDGET: ${{ vars.TOOL_CALL_BUDGET_PLAN || '40' }}" in step
	assert 'TOOL_CALL_BUDGET="${TOOL_CALL_BUDGET:-40}"' in PLAN_RUNNER.read_text(encoding="utf-8")
	# Heal issues (#6463) collect verified evidence and switch the runner to
	# the isolated planner before it starts; ordinary issues run it unchanged.
	assert step.split("        run: |\n", 1)[1] == (
		'          if ! heal_route="$(python3 scripts/workflow_failure_heal.py heal-route --issue-json "${ISSUE_META_FILE}")"; then\n'
		"            echo '::error::Heal route classification failed' >&2\n"
		'            exit 1\n'
		'          fi\n'
		'          if [ "${heal_route}" = true ]; then\n'
		"            echo 'HEAL_ROUTE=true' >> \"${GITHUB_ENV}\"\n"
		'            HEAL_ROUTE=true HEAL_EVIDENCE_FILE="${RUNTIME_DIR}/heal_evidence.md" bash scripts/workflow_failure_heal_evidence.sh\n'
		'            export HEAL_ROUTE=true HEAL_EVIDENCE_FILE="${RUNTIME_DIR}/heal_evidence.md"\n'
		"          fi\n"
		"          bash scripts/run_plan_codex.sh\n"
	)
	assert len(step.encode("utf-8")) < 2_000


def test_workflow_reference_checker_accepts_claude_settings_template() -> None:
	checker_ns = run_path(str(REPO_ROOT / "scripts" / "check_workflow_script_refs.py"))
	extract_refs = checker_ns["extract_refs"]
	for reference in (
		"scripts/claude_settings.json.tmpl",
		"${SUPPORT_SCRIPTS_DIR}/claude_settings.json.tmpl",
		"for f in claude_settings.json.tmpl; do install scripts/${f}; done",
	):
		assert extract_refs(reference) == {"claude_settings.json.tmpl"}
	result = subprocess.run(
		["python3", "scripts/check_workflow_script_refs.py"],
		cwd=REPO_ROOT, capture_output=True, text=True, check=False,
	)
	assert result.returncode == 0, result.stderr


def test_extracted_prompt_matches_pre_extraction_bytes() -> None:
	runner_text = PLAN_RUNNER.read_text(encoding="utf-8")
	prompt_bytes = _inline_prompt(runner_text).encode("utf-8")

	assert len(prompt_bytes) == PRE_EXTRACTION_PROMPT_BYTES
	assert len(prompt_bytes.splitlines()) == PRE_EXTRACTION_PROMPT_LINES
	assert hashlib.sha256(prompt_bytes).hexdigest() == PRE_EXTRACTION_PROMPT_SHA256
	assert "${{" not in runner_text
	assert runner_text.count("repos/${GITHUB_REPOSITORY}/issues/comments/") == 2


def test_plan_prompts_keep_paths_out_of_files_section_notes() -> None:
	# Issue #7724: a note "(one added `scripts/` bullet)" in the Files section
	# made the plan approval gate hold on the bare directory `scripts/`.
	sys.path.insert(0, str(REPO_ROOT / "scripts"))
	import files_touched_scope_guard as guard  # noqa: E402

	rule = "the note must not name another path\n   or a directory such as `scripts/`"
	for prompt in (PLAN_RUNNER, REPO_ROOT / "prompts" / "mode-plan.txt",
			REPO_ROOT / "prompts" / "_templates" / "mode-plan.txt"):
		assert rule in prompt.read_text(encoding="utf-8"), prompt
	held = "## Files likely to change\n- `docs/INVENTORY.md` (one added `scripts/` bullet)\n\n## Next\n"
	compliant = "## Files likely to change\n- `docs/INVENTORY.md` (new)\n- `scripts/x.sh` (new)\n\n## Next\n"
	assert "scripts/" in guard.extract_plan_automation_paths(held)
	assert guard.extract_plan_automation_paths(compliant) == ["scripts/x.sh"]


def _write_executable(path: Path, content: str) -> None:
	path.write_text(content, encoding="utf-8")
	path.chmod(0o755)


def _run_runner(
	scenario: str,
	engine: str = "",
	claude_scenario: str = "",
	planning_ref_mismatch: str | None = None,
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
  ref_mismatch_then_plan)
    if [ "${attempt}" -eq 1 ]; then
      printf 'Checked git: branch isolated.\nBLOCKED: integration branch mismatch\n'
    else
      printf 'plan after retry\n'
    fi
    ;;
  ref_mismatch_always)
    printf 'BLOCKED: Integration branch mismatch.\n'
    ;;
  ref_mismatch_embellished_then_plan)
    if [ "${attempt}" -eq 1 ]; then
      printf 'BLOCKED: integration branch mismatch (workspace is on isolated, expected orchestrator/project-7126)\n'
    else
      printf 'plan after retry\n'
    fi
    ;;
  ref_mismatch_other_reason)
    printf 'BLOCKED: integration branch mismatches are not the issue here\n'
    ;;
  ref_mismatch_fenced)
    printf '```text\nBLOCKED: integration branch mismatch\n```\nfenced example plan\n'
    ;;
  *)
    exit 64
    ;;
esac
""",
	)

	if engine:
		# A stand-in for scripts/ai_engine.sh: claude_run logs its arguments
		# and succeeds, crashes, or reports Claude unavailable (exit 75).
		(scripts_dir / "ai_engine.sh").write_text(
			"""claude_run()
{
	printf '%s|%s|%s|%s|%s|%s\\n' "$1" "$2" "$3" "$4" "${AI_ENGINE_MODEL_HINT:-}" "${AI_ENGINE_EFFORT_HINT:-}" >> "${MOCK_LOG_DIR}/claude-run.log"
	case "${MOCK_CLAUDE_SCENARIO}" in
		success) printf 'claude plan output\\n' > "$3" ;;
		unavailable) echo "AI_ENGINE_FALLBACK role=$1 reason=no_credential" >&2; return 75 ;;
		*) return 1 ;;
	esac
}
""",
			encoding="utf-8",
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
			"MODEL_REASONING_EFFORT": "high",
			"MOCK_CLAUDE_SCENARIO": claude_scenario,
		}
	)
	environment.pop("PLAN_ENGINE", None)
	environment.pop("PLANNING_REF_MISMATCH", None)
	if planning_ref_mismatch is not None:
		environment["PLANNING_REF_MISMATCH"] = planning_ref_mismatch
	if engine:
		environment["PLAN_ENGINE"] = engine
	for key in ("BASH_ENV", "ENV"):
		environment.pop(key, None)
	# The planner runs through scripts/codex_isolated_exec.sh; the fake docker
	# runs the mock codex in the fake container.
	environment = enable_fake_isolation(mock_bin_dir, scripts_dir, environment)

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


def test_ref_mismatch_block_contradicting_the_workflow_is_retried() -> None:
	# Issue #7172: the planner checked git inside its sandbox (a snapshot on
	# branch `isolated`) and answered BLOCKED although the workflow computed
	# PLANNING_REF_MISMATCH=false. The runner retries with a note instead of
	# handing a false block to plan.yml.
	result, root, _temporary_directory = _run_runner(
		"ref_mismatch_then_plan", planning_ref_mismatch="false"
	)
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "PLAN_REF_MISMATCH_RETRY attempt=1 outcome=retry planning_ref_mismatch=false" in result.stdout
	assert "Codex planning succeeded on attempt 2 (1 lines of output)." in result.stdout
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == "plan after retry\n"
	assert _read_lines(runtime_dir / "sleep.log") == ["10"]
	assert "=== RETRY NOTE ===" not in (runtime_dir / "prompt-1.txt").read_text(encoding="utf-8")
	retry_prompt = (runtime_dir / "prompt-2.txt").read_text(encoding="utf-8")
	assert retry_prompt.count("=== RETRY NOTE ===") == 1
	assert "Integration branch mismatch: false" in retry_prompt
	# The note also carries the line above; this pins the original context.
	assert "DYNAMIC PLANNING CONTEXT" in retry_prompt


def test_ref_mismatch_block_is_kept_after_the_last_attempt() -> None:
	result, root, _temporary_directory = _run_runner(
		"ref_mismatch_always", planning_ref_mismatch="false"
	)
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "outcome=retry" in result.stdout
	assert "PLAN_REF_MISMATCH_RETRY attempt=3 outcome=kept planning_ref_mismatch=false" in result.stdout
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == (
		"BLOCKED: Integration branch mismatch.\n"
	)
	assert len(_read_lines(runtime_dir / "codex-args.log")) == 3
	# The note is added once, not once per retry.
	assert (runtime_dir / "prompt-3.txt").read_text(encoding="utf-8").count("=== RETRY NOTE ===") == 1


def test_ref_mismatch_block_stands_when_the_workflow_found_a_mismatch() -> None:
	for value in ("true", "unknown", None):
		result, root, _temporary_directory = _run_runner(
			"ref_mismatch_then_plan", planning_ref_mismatch=value
		)
		runtime_dir = root / "runtime"

		assert result.returncode == 0, (value, result.stderr)
		assert "PLAN_REF_MISMATCH_RETRY" not in result.stdout, value
		assert "Codex planning succeeded on attempt 1" in result.stdout, value
		assert "BLOCKED: integration branch mismatch" in (runtime_dir / "codex_output.txt").read_text(encoding="utf-8")


def test_embellished_ref_mismatch_block_is_retried() -> None:
	result, root, _temporary_directory = _run_runner(
		"ref_mismatch_embellished_then_plan", planning_ref_mismatch="false"
	)
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "PLAN_REF_MISMATCH_RETRY attempt=1 outcome=retry planning_ref_mismatch=false" in result.stdout
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == "plan after retry\n"


def test_longer_word_after_the_phrase_is_not_retried() -> None:
	result, _root, _temporary_directory = _run_runner(
		"ref_mismatch_other_reason", planning_ref_mismatch="false"
	)

	assert result.returncode == 0, result.stderr
	assert "PLAN_REF_MISMATCH_RETRY" not in result.stdout
	assert "Codex planning succeeded on attempt 1" in result.stdout


def test_fenced_ref_mismatch_example_is_not_retried() -> None:
	result, root, _temporary_directory = _run_runner(
		"ref_mismatch_fenced", planning_ref_mismatch="false"
	)

	assert result.returncode == 0, result.stderr
	assert "PLAN_REF_MISMATCH_RETRY" not in result.stdout
	assert "Codex planning succeeded on attempt 1" in result.stdout


CODEX_ARGS = (
	"--ask-for-approval never -c model_verbosity=low "
	"-c include_apply_patch_tool=true exec --skip-git-repo-check "
	"--model primary/model --sandbox danger-full-access"
)


def test_claude_engine_runs_claude_run_and_not_codex() -> None:
	# Phase 5a: PLAN on Claude runs claude_run in the checkout with the role's
	# codex model and effort as hints; codex is not called.
	result, root, _temporary_directory = _run_runner("exhaust", engine="claude", claude_scenario="success")
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "Claude planning attempt 1/3..." in result.stdout
	assert "Codex planning" not in result.stdout
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == "claude plan output\n"
	assert _read_lines(runtime_dir / "claude-run.log") == [
		f"PLAN|{runtime_dir / 'codex_prompt.txt'}|{runtime_dir / 'codex_output.txt'}|{root}|primary/model|high"
	]
	assert not (runtime_dir / "codex-args.log").exists()


def test_claude_unavailable_falls_back_to_the_unchanged_codex_call() -> None:
	# Plan D1 and G4: exit 75 runs the exact pre-cutover codex command, in the
	# same attempt, and the remaining attempts stay on codex.
	result, root, _temporary_directory = _run_runner("success", engine="claude", claude_scenario="unavailable")
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert "AI_ENGINE_FALLBACK role=PLAN reason=no_credential" in result.stderr
	# The attempt starts on Claude; once it falls back the log names Codex.
	assert "Claude planning attempt 1/3..." in result.stdout
	assert "Codex planning succeeded on attempt 1 (1 lines of output)." in result.stdout
	assert len(_read_lines(runtime_dir / "claude-run.log")) == 1
	assert _read_lines(runtime_dir / "codex-args.log") == [CODEX_ARGS]
	assert (runtime_dir / "codex_output.txt").read_text(encoding="utf-8") == "primary plan output\n"
	assert not (runtime_dir / "sleep.log").exists()


def test_a_claude_crash_retries_on_claude_and_never_reaches_codex() -> None:
	result, root, _temporary_directory = _run_runner("success", engine="claude", claude_scenario="crash")
	runtime_dir = root / "runtime"

	assert result.returncode == 1
	assert "Claude exited with code 1 on attempt 1." in result.stdout
	assert len(_read_lines(runtime_dir / "claude-run.log")) == 3
	assert not (runtime_dir / "codex-args.log").exists()


def test_codex_engine_runs_the_unchanged_codex_call() -> None:
	result, root, _temporary_directory = _run_runner("success", engine="codex")
	runtime_dir = root / "runtime"

	assert result.returncode == 0, result.stderr
	assert _read_lines(runtime_dir / "codex-args.log") == [CODEX_ARGS]
	assert not (runtime_dir / "claude-run.log").exists()
	runner = PLAN_RUNNER.read_text(encoding="utf-8")
	# The codex arguments are unchanged; the call runs in the isolated container.
	assert (
		'bash "${CODEX_ISOLATED_EXEC}" run --mode read-only -- --ask-for-approval never -c model_verbosity=low -c include_apply_patch_tool=true '
		'exec --skip-git-repo-check --model "${attempt_model}" --sandbox danger-full-access < "${CODEX_PROMPT_FILE}" > "${CODEX_OUTPUT_FILE}" '
		'2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2) || plan_rc=$?'
	) in runner


POOL_TOKEN_ACTION_DIR = REPO_ROOT / ".github" / "actions" / "claude-pool-token"
PIN_STEP_NAME = "Pin claude-pool-token action module type"
PIN_STEP_IF = "env.SKIP_PLAN != 'true' && env.CHECK_TRIAGE_BRANCH_GUARD_BLOCKED != 'true'"
STAGED_ACTION_REL = Path(".codex-workflow-src") / ".github" / "actions" / "claude-pool-token"
CLARIFY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
# Issue #7516: every clarify step after "Resolve integration ref" also skips on
# the label_missing hold.
CLARIFY_PIN_STEP_IF = "steps.refctx.outputs.label_missing_hold != 'true' && (steps.ai_engine.outputs.engine == 'claude')"


def _step_run_body(step: str) -> str:
	body = step.split("        run: |\n", 1)[1]
	return "".join(
		line[10:] if line.startswith("          ") else line.lstrip(" ")
		for line in body.splitlines(keepends=True)
	)


def _pin_step_body() -> str:
	return _step_run_body(_workflow_step(PLAN_WORKFLOW.read_text(encoding="utf-8"), PIN_STEP_NAME))


def _pool_token_action_files() -> tuple[str, str]:
	text = (POOL_TOKEN_ACTION_DIR / "action.yml").read_text(encoding="utf-8")
	try:
		import yaml  # type: ignore[import-untyped]

		runs = yaml.safe_load(text)["runs"]
		return str(runs["main"]), str(runs["post"])
	except ImportError:
		main_match = re.search(r"^\s+main:\s*(\S+)\s*$", text, re.MULTILINE)
		post_match = re.search(r"^\s+post:\s*(\S+)\s*$", text, re.MULTILINE)
		assert main_match and post_match
		return main_match.group(1), post_match.group(1)


def _clean_env(**extra: str) -> dict[str, str]:
	environment = {
		key: value
		for key, value in os.environ.items()
		if not key.startswith("GIT_") and key not in ("BASH_ENV", "ENV", "NODE_OPTIONS")
	}
	environment.update(extra)
	return environment


def _run_pin(root: Path, body: str | None = None) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", "-c", _pin_step_body() if body is None else body],
		cwd=root, env=_clean_env(), capture_output=True, text=True, check=False,
	)


def _stage_action(root: Path, with_pin: bool) -> Path:
	action_dir = root / STAGED_ACTION_REL
	action_dir.mkdir(parents=True)
	main_file, post_file = _pool_token_action_files()
	for name in ("action.yml", main_file, post_file):
		shutil.copyfile(POOL_TOKEN_ACTION_DIR / name, action_dir / name)
	scripts_dir = root / ".codex-workflow-src" / "scripts"
	scripts_dir.mkdir(parents=True)
	_write_executable(scripts_dir / "claude_pool_token.sh", "#!/usr/bin/env bash\nexit 1\n")
	(root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
	if with_pin:
		result = _run_pin(root)
		assert result.returncode == 0, result.stderr
		assert "CLAUDE_POOL_MODULE_PIN outcome=written" in result.stdout
	return action_dir


def _run_node(action_dir: Path, script: str, root: Path, output: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["node", script],
		cwd=action_dir,
		env=_clean_env(
			GITHUB_OUTPUT=str(output),
			GITHUB_WORKSPACE=str(root),
			RUNNER_TEMP=str(root / "runner-temp"),
			INPUT_CONFIG_PATH="",
			INPUT_PROBE="false",
		),
		capture_output=True, text=True, check=False, timeout=60,
	)


def test_pool_token_action_pin_step_wiring() -> None:
	workflow_text = PLAN_WORKFLOW.read_text(encoding="utf-8")
	step = _workflow_step(workflow_text, PIN_STEP_NAME)
	assert f"        if: {PIN_STEP_IF}\n" in step
	assert "        continue-on-error: true\n" in step
	body = _pin_step_body()
	assert "${{" not in body
	assert "set -C" in body
	assert '".codex-workflow-src/.github/actions/claude-pool-token"' in body
	order = [
		workflow_text.index("      - name: Check-triage target-branch guard\n"),
		workflow_text.index(f"      - name: {PIN_STEP_NAME}\n"),
		workflow_text.index("      - name: Capture pre-Codex plan write-guard snapshot\n"),
		workflow_text.index("      - name: Resolve Claude credential\n"),
	]
	assert order == sorted(order)
	credential = _workflow_step(workflow_text, "Resolve Claude credential")
	assert "uses: ./.codex-workflow-src/.github/actions/claude-pool-token\n" in credential
	assert "        continue-on-error: true\n" in credential
	assert "steps.ai_engine.outputs.engine == 'claude'" in credential


def test_pool_token_action_loads_under_esm_parent_after_pin() -> None:
	if shutil.which("node") is None:
		print("skip: node is not on PATH")
		return
	with tempfile.TemporaryDirectory(prefix="pool-token-esm-") as tmp:
		root = Path(tmp)
		action_dir = _stage_action(root, with_pin=True)
		output = root / "github_output"
		output.write_text("", encoding="utf-8")
		main_file, post_file = _pool_token_action_files()
		for script in (main_file, post_file):
			result = _run_node(action_dir, script, root, output)
			assert result.returncode == 0, (script, result.stderr)
			assert "ReferenceError" not in result.stderr, (script, result.stderr)
			assert "require is not defined" not in result.stderr, (script, result.stderr)
		written = output.read_text(encoding="utf-8")
		assert "available=false" in written
		assert "reason=script_failed" in written
		assert (action_dir / "package.json").read_text(encoding="utf-8") == '{"type":"commonjs"}\n'


def test_pool_token_action_crashes_under_esm_parent_without_pin() -> None:
	# Reproduces the reported failure (digital_pa#145 at 7feb4ef553b2): with
	# the consumer's "type": "module" package.json and no pin, main.js never
	# reaches its own available=false fallback.
	main_file, _post_file = _pool_token_action_files()
	if shutil.which("node") is None or not main_file.endswith(".js"):
		print("skip: node missing or the action no longer ships .js entry points")
		return
	# _stage_action copies only action.yml and the entry points, so the
	# staged copy never has the action's own package.json.
	with tempfile.TemporaryDirectory(prefix="pool-token-esm-") as tmp:
		root = Path(tmp)
		action_dir = _stage_action(root, with_pin=False)
		output = root / "github_output"
		output.write_text("", encoding="utf-8")
		result = _run_node(action_dir, main_file, root, output)
		assert result.returncode != 0
		assert "require is not defined" in result.stderr
		assert "available=" not in output.read_text(encoding="utf-8")


def test_pool_token_action_pin_step_keeps_existing_package_json() -> None:
	with tempfile.TemporaryDirectory(prefix="pool-token-esm-") as tmp:
		root = Path(tmp)
		action_dir = _stage_action(root, with_pin=False)
		pkg = action_dir / "package.json"
		pkg.write_text('{"type":"commonjs","name":"upstream"}\n', encoding="utf-8")
		result = _run_pin(root)
		assert result.returncode == 0, result.stderr
		assert "CLAUDE_POOL_MODULE_PIN outcome=skip reason=present" in result.stdout
		assert pkg.read_text(encoding="utf-8") == '{"type":"commonjs","name":"upstream"}\n'

		pkg.unlink()
		dangling = root / "elsewhere.json"
		pkg.symlink_to(dangling)
		result = _run_pin(root)
		assert result.returncode == 0, result.stderr
		assert "reason=present" in result.stdout
		assert pkg.is_symlink() and not dangling.exists()

		target = root / "other-dir"
		target.mkdir()
		shutil.rmtree(action_dir)
		action_dir.symlink_to(target, target_is_directory=True)
		result = _run_pin(root)
		assert result.returncode == 0, result.stderr
		assert "reason=action_dir_unavailable" in result.stdout
		assert not (target / "package.json").exists()

		action_dir.unlink()
		result = _run_pin(root)
		assert result.returncode == 0, result.stderr
		assert "reason=action_dir_unavailable" in result.stdout


def test_pool_token_action_pin_does_not_change_write_guard_paths() -> None:
	if shutil.which("git") is None:
		print("skip: git is not on PATH")
		return
	snapshot_body = _step_run_body(
		_workflow_step(PLAN_WORKFLOW.read_text(encoding="utf-8"), "Capture pre-Codex plan write-guard snapshot")
	)
	with tempfile.TemporaryDirectory(prefix="pool-token-esm-") as tmp:
		root = Path(tmp) / "ws"
		root.mkdir()
		_stage_action(root, with_pin=False)
		for repo in (root, root / ".codex-workflow-src"):
			subprocess.run(["git", "init", "-q", str(repo)], env=_clean_env(), check=True)

		def _paths(name: str) -> list[str]:
			out = Path(tmp) / name
			subprocess.run(
				["bash", "-c", snapshot_body], cwd=root, check=True, capture_output=True,
				env=_clean_env(PRE_PLAN_WRITE_GUARD_PATHS_FILE=str(out)),
			)
			return [line.split("\t", 1)[0] for line in out.read_text(encoding="utf-8").splitlines()]

		before = _paths("before.tsv")
		pin = _run_pin(root)
		assert pin.returncode == 0, pin.stderr
		after = _paths("after.tsv")
		assert before == after
		assert ".codex-workflow-src/" in after


def _clarify_pin_step_body() -> str:
	return _step_run_body(_workflow_step(CLARIFY_WORKFLOW.read_text(encoding="utf-8"), PIN_STEP_NAME))


def test_clarify_pool_token_action_pin_step_wiring() -> None:
	# Heal #7476: clarify runs the same pin as plan (#7434) before the action.
	workflow_text = CLARIFY_WORKFLOW.read_text(encoding="utf-8")
	step = _workflow_step(workflow_text, PIN_STEP_NAME)
	assert f"        if: {CLARIFY_PIN_STEP_IF}\n" in step
	assert "        continue-on-error: true\n" in step
	assert "${{" not in _clarify_pin_step_body()
	order = [
		workflow_text.index("      - name: Resolve AI engine\n"),
		workflow_text.index("      - name: Install Claude Code CLI\n"),
		workflow_text.index(f"      - name: {PIN_STEP_NAME}\n"),
		workflow_text.index("      - name: Resolve Claude credential\n"),
	]
	assert order == sorted(order)
	assert workflow_text.count(f"      - name: {PIN_STEP_NAME}\n") == 1
	credential = _workflow_step(workflow_text, "Resolve Claude credential")
	assert "uses: ./.codex-workflow-src/.github/actions/claude-pool-token\n" in credential
	assert "        continue-on-error: true\n" in credential
	assert f"        if: {CLARIFY_PIN_STEP_IF}\n" in credential


def test_clarify_pool_token_action_pin_body_matches_plan() -> None:
	assert _clarify_pin_step_body() == _pin_step_body()


def test_clarify_pool_token_action_loads_under_esm_parent_after_pin() -> None:
	if shutil.which("node") is None:
		print("skip: node is not on PATH")
		return
	with tempfile.TemporaryDirectory(prefix="pool-token-esm-") as tmp:
		root = Path(tmp)
		action_dir = _stage_action(root, with_pin=False)
		pin = _run_pin(root, _clarify_pin_step_body())
		assert pin.returncode == 0, pin.stderr
		assert (
			"CLAUDE_POOL_MODULE_PIN outcome=written" in pin.stdout
			or "CLAUDE_POOL_MODULE_PIN outcome=skip reason=present" in pin.stdout
		), pin.stdout
		output = root / "github_output"
		output.write_text("", encoding="utf-8")
		main_file, post_file = _pool_token_action_files()
		for script in (main_file, post_file):
			result = _run_node(action_dir, script, root, output)
			assert result.returncode == 0, (script, result.stderr)
			assert "require is not defined" not in result.stderr, (script, result.stderr)
		assert "available=false" in output.read_text(encoding="utf-8")


def test_clarify_pool_token_action_pin_keeps_existing_package_json() -> None:
	body = _clarify_pin_step_body()
	with tempfile.TemporaryDirectory(prefix="pool-token-esm-") as tmp:
		root = Path(tmp)
		action_dir = _stage_action(root, with_pin=False)
		pkg = action_dir / "package.json"
		pkg.write_text('{"type":"commonjs","name":"upstream"}\n', encoding="utf-8")
		result = _run_pin(root, body)
		assert result.returncode == 0, result.stderr
		assert "CLAUDE_POOL_MODULE_PIN outcome=skip reason=present" in result.stdout
		assert pkg.read_text(encoding="utf-8") == '{"type":"commonjs","name":"upstream"}\n'

		target = root / "other-dir"
		target.mkdir()
		shutil.rmtree(action_dir)
		action_dir.symlink_to(target, target_is_directory=True)
		result = _run_pin(root, body)
		assert result.returncode == 0, result.stderr
		assert "reason=action_dir_unavailable" in result.stdout
		assert not (target / "package.json").exists()


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
