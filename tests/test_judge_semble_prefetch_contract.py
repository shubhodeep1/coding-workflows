#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_WF = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
MODE_JUDGE = REPO_ROOT / "prompts" / "mode-judge.txt"
MODE_JUDGE_REVIEW_BLOCKED = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
MODE_JUDGE_STALL_RECOVERY = REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt"
MODE_ORCHESTRATE_POLL_JUDGE = REPO_ROOT / "prompts" / "mode-orchestrate-poll-judge.txt"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
    marker = f"- name: {step_name}"
    start = text.find(marker)
    assert start != -1, f"Missing workflow step: {step_name}"
    next_step = text.find("\n      - name:", start + len(marker))
    if next_step == -1:
        return text[start:]
    return text[start:next_step]


def _render_prompt(
    prompt_text: str,
    semble_prefetch: str | None,
    *,
    allow_workflow_edits: str | None = None,
    extra_env: dict[str, str | None] | None = None,
) -> str:
    with tempfile.TemporaryDirectory(prefix="judge_semble_render_") as td:
        tmpdir = Path(td)
        prompt_file = tmpdir / "prompt.txt"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        env = os.environ.copy()
        for key, value in (extra_env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if allow_workflow_edits is not None:
            env["ALLOW_WORKFLOW_EDITS"] = allow_workflow_edits
        if semble_prefetch is None:
            env.pop("SEMBLE_PREFETCH", None)
        else:
            env["SEMBLE_PREFETCH"] = semble_prefetch

        proc = subprocess.run(
            ["bash", str(RENDER_PROMPT), str(prompt_file)],
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 0, (
            f"render_prompt.sh failed with {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n\n"
            f"stderr:\n{proc.stderr}"
        )
        return proc.stdout


def test_render_prompt_replaces_semble_prefetch_and_existing_placeholder() -> None:
    render_prompt = _read(RENDER_PROMPT)
    assert '"{{WORKFLOW_EDIT_RESTRICTION}}")' in render_prompt
    assert '"{{SEMBLE_PREFETCH}}")' in render_prompt
    assert "Unresolved WORKFLOW_EDIT_RESTRICTION placeholder" in render_prompt
    assert "Unresolved SEMBLE_PREFETCH placeholder" in render_prompt

    rendered = _render_prompt(
        "Header\n{{SEMBLE_PREFETCH}}\n{{WORKFLOW_EDIT_RESTRICTION}}\nFooter\n",
        "=== SEMBLE: Judge Context ===\nchunk one\n=== END SEMBLE ===\n",
        allow_workflow_edits="true",
    )

    assert rendered == (
        "Header\n"
        "=== SEMBLE: Judge Context ===\n"
        "chunk one\n"
        "=== END SEMBLE ===\n"
        "- CI workflow edits under .github/workflows/ are permitted when required by the approved plan; keep changes inside the plan's stated file scope.\n"
        "Footer\n"
    )


def test_render_prompt_preserves_multiline_semble_prefetch_verbatim() -> None:
    rendered = _render_prompt(
        "Before\n{{SEMBLE_PREFETCH}}\nAfter\n",
        "=== SEMBLE: Judge Context ===\nchunk 1\n\nchunk 2\n=== END SEMBLE ===",
    )

    assert rendered == (
        "Before\n=== SEMBLE: Judge Context ===\nchunk 1\n\nchunk 2\n=== END SEMBLE ===\nAfter\n"
    )


def test_render_prompt_injects_semble_prefetch_with_surrounding_whitespace() -> None:
    rendered = _render_prompt(
        "Role: judge\n  {{SEMBLE_PREFETCH}}   \nFooter\n",
        "=== SEMBLE: Judge Context ===\nchunk",
    )
    assert "{{SEMBLE_PREFETCH}}" not in rendered
    assert "=== SEMBLE: Judge Context ===" in rendered
    assert "chunk" in rendered
    assert rendered.endswith("Footer\n")


def test_render_prompt_drops_semble_prefetch_placeholder_when_empty() -> None:
    rendered = _render_prompt("Before\n{{SEMBLE_PREFETCH}}\nAfter\n", None)

    assert "{{SEMBLE_PREFETCH}}" not in rendered
    assert rendered == "Before\n\nAfter\n"


def test_render_prompt_resolves_workflow_and_semble_placeholders_together() -> None:
	rendered = _render_prompt(
		"{{WORKFLOW_EDIT_RESTRICTION}}\n{{SEMBLE_PREFETCH}}\nFooter\n",
		"=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===",
		extra_env={"ALLOW_WORKFLOW_EDITS": "true"},
	)

	assert "{{WORKFLOW_EDIT_RESTRICTION}}" not in rendered
	assert "{{SEMBLE_PREFETCH}}" not in rendered
	workflow_line, remainder = rendered.split("\n", 1)
	assert workflow_line.startswith("- ")
	assert ".github/workflows/" in workflow_line
	assert remainder == (
		"=== SEMBLE: Judge Context ===\n"
		"chunk\n"
		"=== END SEMBLE ===\n"
		"Footer\n"
	)


def test_render_prompt_drops_semble_prefetch_placeholder_when_empty_string() -> None:
	rendered = _render_prompt("Before\n{{SEMBLE_PREFETCH}}\nAfter\n", "")

	assert "{{SEMBLE_PREFETCH}}" not in rendered
	assert rendered == "Before\n\nAfter\n"


def test_render_prompt_extra_env_none_unsets_inherited_workflow_flag() -> None:
    previous = os.environ.get("ALLOW_WORKFLOW_EDITS")
    os.environ["ALLOW_WORKFLOW_EDITS"] = "true"
    try:
        rendered = _render_prompt(
            "{{WORKFLOW_EDIT_RESTRICTION}}\n",
            "",
            extra_env={"ALLOW_WORKFLOW_EDITS": None},
        )
    finally:
        if previous is None:
            os.environ.pop("ALLOW_WORKFLOW_EDITS", None)
        else:
            os.environ["ALLOW_WORKFLOW_EDITS"] = previous

    assert rendered == "- Do not change CI workflows.\n"


def test_render_prompt_leaves_nonstandalone_semble_marker_text_unchanged() -> None:
    rendered = _render_prompt(
        "Before {{SEMBLE_PREFETCH}} After\n",
        "=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===",
    )

    assert rendered == "Before {{SEMBLE_PREFETCH}} After\n"

def test_render_prompt_preserves_workflow_edit_restriction_contract() -> None:
    for allow_workflow_edits, expected_line in [
        ("false", "- Do not change CI workflows."),
        (
            "true",
            "- CI workflow edits under .github/workflows/ are permitted when required by the approved plan; keep changes inside the plan's stated file scope.",
        ),
    ]:
        rendered = _render_prompt(
            "Role: judge\n{{WORKFLOW_EDIT_RESTRICTION}}\n{{SEMBLE_PREFETCH}}\nFooter\n",
            "=== SEMBLE: Judge Context ===\nchunk",
            allow_workflow_edits=allow_workflow_edits,
        )

        assert "{{WORKFLOW_EDIT_RESTRICTION}}" not in rendered
        assert expected_line in rendered
        assert "=== SEMBLE: Judge Context ===" in rendered
        assert rendered.endswith("Footer\n")


def test_orchestrate_poll_workflow_bootstraps_optional_semble_support_for_judges() -> None:
    workflow = _read(ORCHESTRATE_POLL_WF)
    workspace_block = _step_block(workflow, "Create runtime workspace")
    stage_block = _step_block(workflow, "Stage workflow support files")
    setup_block = _step_block(workflow, "setup-uv")
    install_block = _step_block(workflow, "Install semble")
    index_block = _step_block(workflow, "Build semble index")

    assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
    assert 'echo "SEMBLE_AVAILABLE=false"' in workspace_block
    assert 'echo "SEMBLE_BIN="' in workspace_block
    assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in workspace_block
    assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in workspace_block

    assert "for f in install_semble.sh semble_helpers.sh; do" in stage_block
    assert '_fetched_scripts+=("${f}")' in stage_block
    assert "Optional Semble support script ${f} is unavailable" in stage_block
    assert "legacy path remains active" in stage_block

    assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in setup_block
    assert "uses: astral-sh/setup-uv@v3" in setup_block

    assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in install_block
    assert 'echo "SEMBLE_AVAILABLE=false" >> "$GITHUB_ENV"' in install_block
    assert 'if [ ! -f scripts/install_semble.sh ]; then' in install_block
    assert 'scripts/install_semble.sh missing from staged workflow support files' in install_block
    assert 'if ! bash scripts/install_semble.sh; then' in install_block
    assert 'SEMBLE_BIN_PATH="$(command -v semble 2>/dev/null || true)"' in install_block
    assert 'echo "SEMBLE_BIN=${SEMBLE_BIN_PATH}" >> "$GITHUB_ENV"' in install_block

    assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in index_block
    assert 'semble_index_path="${SEMBLE_INDEX_PATH:-${RUNTIME_DIR}/.semble-index}"' in index_block
    assert 'echo "SEMBLE_INDEX_PATH=${semble_index_path}" >> "$GITHUB_ENV"' in index_block
    assert 'echo "SEMBLE_INDEX_AVAILABLE=false" >> "$GITHUB_ENV"' in index_block
    assert 'if [ "${SEMBLE_AVAILABLE:-false}" != "true" ]; then' in index_block
    assert 'if "${semble_bin}" index . --out "${semble_index_path}" > "${RUNTIME_DIR}/semble_index.log" 2>&1; then' in index_block
    assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in index_block

    assert workflow.find("- name: setup-uv") < workflow.find("- name: Process each tracking issue")
    assert workflow.find("- name: Install semble") < workflow.find("- name: Process each tracking issue")
    assert workflow.find("- name: Build semble index") < workflow.find("- name: Process each tracking issue")


def test_live_judge_templates_expose_semble_placeholder() -> None:
    for path in [MODE_JUDGE, MODE_JUDGE_REVIEW_BLOCKED, MODE_JUDGE_STALL_RECOVERY]:
        text = _read(path)
        assert "{{SEMBLE_PREFETCH}}" in text, f"missing Semble placeholder in {path.name}"
        assert text.count("{{SEMBLE_PREFETCH}}") == 1, f"expected one Semble placeholder in {path.name}"


def test_orchestrate_poll_process_wires_semble_prefetch_into_live_judges() -> None:
    text = _read(ORCHESTRATE_POLL_PROCESS)

    assert 'if [ -f "scripts/semble_helpers.sh" ]; then' in text
    assert 'SEMBLE_HELPERS_AVAILABLE="false"' in text
    assert 'JUDGE_SEMBLE_MAX_CHUNKS="4"' in text
    assert 'append_judge_semble_query_text()' in text
    assert 'render_judge_semble_prefetch_from_query_file()' in text
    assert '_append_judge_semble_query_section()' not in text
    assert '_build_judge_semble_prefetch()' not in text
    assert 'judge_semble_prefetch="$(_build_judge_semble_prefetch' not in text
    assert 'stall_judge_semble_prefetch="$(_build_judge_semble_prefetch' not in text
    assert 'RB_JUDGE_SEMBLE_PREFETCH="$(_build_judge_semble_prefetch' not in text
    assert 'JUDGE_SEMBLE_PREFETCH="$(_build_judge_semble_prefetch' not in text
    assert 'judge_semble_prefetch="$(build_semble_prefetch_block' not in text
    assert 'stall_judge_semble_prefetch="$(build_semble_prefetch_block' not in text
    assert 'RB_JUDGE_SEMBLE_PREFETCH="$(build_semble_prefetch_block' not in text
    assert 'JUDGE_SEMBLE_PREFETCH="$(build_semble_prefetch_block' not in text
    assert 'judge_semble_prefetch="$(render_judge_semble_prefetch_from_query_file "${judge_semble_query_file}" "Integration Conflict Judge Context")"' in text
    assert 'stall_judge_semble_prefetch="$(render_judge_semble_prefetch_from_query_file "${stall_judge_semble_query_file}" "Stall Judge Context")"' in text
    assert 'RB_JUDGE_SEMBLE_PREFETCH="$(render_judge_semble_prefetch_from_query_file "${RB_JUDGE_SEMBLE_QUERY_FILE}" "Review-Blocked Judge Context")"' in text
    assert 'JUDGE_SEMBLE_PREFETCH="$(render_judge_semble_prefetch_from_query_file "${JUDGE_SEMBLE_QUERY_FILE}" "Judge Context")"' in text
    assert "printf '%s\\n' \"${judge_semble_prefetch}\"" in text
    assert 'SEMBLE_PREFETCH="${stall_judge_semble_prefetch}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt' in text
    assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt' in text
    assert 'SEMBLE_PREFETCH="${JUDGE_SEMBLE_PREFETCH}" bash scripts/render_prompt.sh prompts/mode-judge.txt' in text
    assert 'bash scripts/render_prompt.sh prompts/mode-orchestrate-poll-judge.txt' not in text


def test_review_rb_judge_sources_semble_helpers_and_passes_prefetch_to_renderer() -> None:
    text = _read(REVIEW_RB_JUDGE)

    assert 'if [ -z "${SUPPORT_ROOT_DIR:-}" ]; then' in text
    assert 'if [ "$(basename "${SUPPORT_SCRIPTS_DIR}")" = "scripts" ]; then' in text
    assert 'SUPPORT_PROMPTS_DIR="${SUPPORT_PROMPTS_DIR:-${SUPPORT_ROOT_DIR}/prompts}"' in text
    assert 'if [ -f "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh" ]; then' in text
    assert 'REVIEW_RB_SEMBLE_HELPERS_AVAILABLE="false"' in text
    assert 'append_review_rb_semble_query_section()' in text
    assert 'render_review_rb_semble_prefetch()' in text
    assert 'build_review_blocked_semble_query()' not in text
    assert 'build_review_blocked_semble_prefetch()' not in text
    assert 'append_judge_semble_query_section()' not in text
    assert 'build_judge_semble_prefetch()' not in text
    assert 'RB_JUDGE_SEMBLE_PREFETCH="$(render_review_rb_semble_prefetch "${RB_JUDGE_SEMBLE_QUERY_FILE}" "Review-Blocked Judge Context")"' in text
    assert 'SEMBLE_PREFETCH="${RB_JUDGE_SEMBLE_PREFETCH}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in text


def test_unwired_orchestrate_poll_judge_prompt_remains_unconsumed() -> None:
    orchestrate = _read(ORCHESTRATE_POLL_PROCESS)

    assert "mode-orchestrate-poll-judge.txt" not in orchestrate
    assert MODE_ORCHESTRATE_POLL_JUDGE.exists()


def main() -> int:
    test_render_prompt_replaces_semble_prefetch_and_existing_placeholder()
    test_render_prompt_preserves_multiline_semble_prefetch_verbatim()
    test_render_prompt_injects_semble_prefetch_with_surrounding_whitespace()
    test_render_prompt_drops_semble_prefetch_placeholder_when_empty()
    test_render_prompt_resolves_workflow_and_semble_placeholders_together()
    test_render_prompt_drops_semble_prefetch_placeholder_when_empty_string()
    test_render_prompt_extra_env_none_unsets_inherited_workflow_flag()
    test_render_prompt_leaves_nonstandalone_semble_marker_text_unchanged()
    test_render_prompt_preserves_workflow_edit_restriction_contract()
    test_orchestrate_poll_workflow_bootstraps_optional_semble_support_for_judges()
    test_live_judge_templates_expose_semble_placeholder()
    test_orchestrate_poll_process_wires_semble_prefetch_into_live_judges()
    test_review_rb_judge_sources_semble_helpers_and_passes_prefetch_to_renderer()
    test_unwired_orchestrate_poll_judge_prompt_remains_unconsumed()
    print("OK: judge Semble prefetch contract assertions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
