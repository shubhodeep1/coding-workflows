#!/usr/bin/env python3
"""Contract tests for judge-family Semble prefetch wiring."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_PROMPT = REPO_ROOT / "scripts" / "render_prompt.sh"
ORCHESTRATE_POLL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-judge.txt"
REVIEW_BLOCKED_PROMPT = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
STALL_PROMPT = REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt"
ORCHESTRATE_POLL_JUDGE_PROMPT = REPO_ROOT / "prompts" / "mode-orchestrate-poll-judge.txt"
_UNSET = object()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _run_render(prompt_text: str, *, semble_prefetch: object = _UNSET) -> subprocess.CompletedProcess[str]:
	with tempfile.TemporaryDirectory(prefix="judge_semble_contract_") as td:
		root = Path(td)
		prompt_file = root / "prompt.txt"
		prompt_file.write_text(prompt_text, encoding="utf-8")
		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		if semble_prefetch is _UNSET:
			env.pop("SEMBLE_PREFETCH", None)
		else:
			env["SEMBLE_PREFETCH"] = str(semble_prefetch)
		return subprocess.run(
			["bash", str(RENDER_PROMPT), str(prompt_file)],
			cwd=str(REPO_ROOT),
			env=env,
			capture_output=True,
			text=True,
			timeout=60,
		)


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


def test_render_prompt_replaces_multiline_semble_prefetch() -> None:
    result = _run_render(
        "Role: judge\n{{SEMBLE_PREFETCH}}\nTask body\n",
        semble_prefetch="=== SEMBLE: Judge Context ===\nchunk 1\nchunk 2\n=== END SEMBLE ===",
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "{{SEMBLE_PREFETCH}}" not in result.stdout
    assert "=== SEMBLE: Judge Context ===\nchunk 1\nchunk 2\n=== END SEMBLE ===\n" in result.stdout


def test_render_prompt_replaces_semble_prefetch_and_guards_placeholder() -> None:
    rendered = _render_prompt(
        "Header\n{{SEMBLE_PREFETCH}}\nFooter\n",
        "=== SEMBLE: Judge Context ===\nchunk one\n=== END SEMBLE ===\n",
    )

    assert rendered == (
        "Header\n"
        "=== SEMBLE: Judge Context ===\n"
        "chunk one\n"
        "=== END SEMBLE ===\n"
        "Footer\n"
    )

    render_text = _read(RENDER_PROMPT)
    assert '"{{SEMBLE_PREFETCH}}")' in render_text
    assert "Unresolved SEMBLE_PREFETCH placeholder" in render_text


def test_render_prompt_rejects_unresolved_semble_placeholder() -> None:
	result = _run_render("Role: judge\n{{SEMBLE_PREFETCH}}\nTask body\n")
	assert result.returncode != 0
	assert "Unresolved SEMBLE_PREFETCH placeholder" in result.stderr


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


def test_render_prompt_allows_empty_semble_prefetch() -> None:
	result = _run_render("Role: judge\n{{SEMBLE_PREFETCH}}\nTask body\n", semble_prefetch="")
	assert result.returncode == 0, result.stderr
	assert result.stderr == ""
	assert result.stdout == "Role: judge\n\nTask body\n"


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
	result = _run_render(
		"Before {{SEMBLE_PREFETCH}} After\n",
		semble_prefetch="=== SEMBLE: Judge Context ===\nchunk\n=== END SEMBLE ===",
	)
	assert result.returncode == 0, result.stderr
	assert result.stderr == ""
	assert result.stdout == "Before {{SEMBLE_PREFETCH}} After\n"

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


def test_orchestrate_poll_workflow_bootstraps_semble_for_judges() -> None:
	workflow = _read(ORCHESTRATE_POLL_WORKFLOW)
	stage_block = _step_block(workflow, "Stage workflow support files")
	init_block = _step_block(workflow, "Create runtime workspace")

	assert "SEMBLE_ENABLED: ${{ vars.SEMBLE_ENABLED || 'false' }}" in workflow
	assert (
		'OPTIONAL_BOOTSTRAP_SCRIPTS="install_semble.sh semble_helpers.sh"' in stage_block
		or "for f in install_semble.sh semble_helpers.sh; do" in stage_block
	)
	assert "Optional Semble support script ${f} is unavailable" in stage_block
	assert 'echo "SEMBLE_AVAILABLE=false"' in init_block
	assert 'echo "SEMBLE_BIN="' in init_block
	assert 'echo "SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index"' in init_block
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in init_block


def test_orchestrate_poll_workflow_adds_gated_setup_install_and_index_steps() -> None:
	workflow = _read(ORCHESTRATE_POLL_WORKFLOW)
	install_block = _step_block(workflow, "Install semble")

	assert "astral-sh/setup-uv@v3" in workflow
	assert "steps.find_tracking.outputs.has_work == 'true' && env.SEMBLE_ENABLED == 'true'" in workflow
	assert 'scripts/install_semble.sh' in workflow
	assert 'binary_matches_pin' in install_block
	assert 'current_semble_version' in install_block
	assert 'echo "SEMBLE_AVAILABLE=true" >> "$GITHUB_ENV"' in workflow
	assert 'echo "SEMBLE_INDEX_AVAILABLE=true" >> "$GITHUB_ENV"' in workflow
	assert (
		'"${SEMBLE_BIN_PATH}" index . --out "${SEMBLE_INDEX_PATH}"' in workflow
		or 'if "${semble_bin}" index . --out "${semble_index_path}"; then' in workflow
	)


def test_live_judge_templates_expose_semble_placeholder() -> None:
	for prompt in (JUDGE_PROMPT, REVIEW_BLOCKED_PROMPT, STALL_PROMPT):
		text = _read(prompt)
		assert "{{SEMBLE_PREFETCH}}" in text, f"Missing Semble placeholder in {prompt}"
		assert text.count("{{SEMBLE_PREFETCH}}") == 1, f"Expected exactly one Semble placeholder in {prompt}"


def test_orchestrate_poll_process_wires_semble_for_all_judge_builders() -> None:
	text = _read(ORCHESTRATE_POLL_PROCESS)
	assert 'source scripts/semble_helpers.sh' in text
	assert 'SEMBLE_HELPERS_AVAILABLE="true"' in text
	assert 'append_semble_query_text()' in text
	assert 'append_semble_query_json()' in text
	assert 'build_judge_semble_prefetch "${integration_semble_query_file}" "${integration_semble_context_file}" "Integration Conflict Judge Context"' in text
	assert 'build_judge_semble_prefetch "${stall_semble_query_file}" "${stall_semble_context_file}" "Stall Recovery Judge Context"' in text
	assert 'build_judge_semble_prefetch "${RB_JUDGE_SEMBLE_QUERY_FILE}" "${RB_JUDGE_SEMBLE_CONTEXT_FILE}" "Review-Blocked Judge Context"' in text
	assert 'build_judge_semble_prefetch "${JUDGE_SEMBLE_QUERY_FILE}" "${JUDGE_SEMBLE_CONTEXT_FILE}" "Judge Context"' in text
	assert 'render_prompt_with_semble_prefetch()' in text
	assert 'render_prompt_with_semble_prefetch prompts/mode-judge.txt "${JUDGE_SEMBLE_CONTEXT_FILE}"' in text
	assert 'render_prompt_with_semble_prefetch prompts/mode-judge-stall-recovery.txt "${stall_semble_context_file}"' in text
	assert 'render_prompt_with_semble_prefetch prompts/mode-judge-review-blocked.txt "${RB_JUDGE_SEMBLE_CONTEXT_FILE}"' in text
	assert 'Integration Conflict Judge Context' in text
	assert 'Review-Blocked Judge Context' in text
	assert 'Stall Recovery Judge Context' in text
	assert 'Judge Context' in text


def test_review_rb_judge_wires_semble_prefetch() -> None:
	text = _read(REVIEW_RB_JUDGE)
	assert 'source "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh"' in text
	assert 'SEMBLE_HELPERS_AVAILABLE="true"' in text
	assert 'RB_JUDGE_SEMBLE_QUERY_FILE="${RUNTIME_DIR}/rb_judge_semble_query.txt"' in text
	assert 'RB_JUDGE_SEMBLE_CONTEXT_FILE="${RUNTIME_DIR}/rb_judge_semble_context.txt"' in text
	assert 'append_semble_query_section' in text
	assert 'append_semble_query_json_section' in text
	assert 'if [ "${SEMBLE_HELPERS_AVAILABLE}" = "true" ] \\' in text
	assert '&& [ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ] \\' in text
	assert 'semble_query_block \\' in text
	assert '"$(cat "${RB_JUDGE_SEMBLE_QUERY_FILE}")" \\' in text
	assert '> "${RB_JUDGE_SEMBLE_CONTEXT_FILE}" || true' in text
	assert 'Review-Blocked Judge Context' in text
	assert "    \"4\" \\" in text
	assert 'SEMBLE_PREFETCH="$(cat "${RB_JUDGE_SEMBLE_CONTEXT_FILE}" 2>/dev/null || true)" \\' in text
	assert 'bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in text


def test_unwired_orchestrate_poll_judge_prompt_remains_unconsumed() -> None:
	orchestrate = _read(ORCHESTRATE_POLL_PROCESS)
	assert "mode-orchestrate-poll-judge.txt" not in orchestrate
	assert ORCHESTRATE_POLL_JUDGE_PROMPT.exists()


def main() -> int:
    test_render_prompt_replaces_semble_prefetch_and_guards_placeholder()
    test_render_prompt_replaces_multiline_semble_prefetch()
    test_render_prompt_rejects_unresolved_semble_placeholder()
    test_render_prompt_preserves_multiline_semble_prefetch_verbatim()
    test_render_prompt_injects_semble_prefetch_with_surrounding_whitespace()
    test_render_prompt_allows_empty_semble_prefetch()
    test_render_prompt_resolves_workflow_and_semble_placeholders_together()
    test_render_prompt_drops_semble_prefetch_placeholder_when_empty_string()
    test_render_prompt_extra_env_none_unsets_inherited_workflow_flag()
    test_render_prompt_leaves_nonstandalone_semble_marker_text_unchanged()
    test_render_prompt_preserves_workflow_edit_restriction_contract()
    test_orchestrate_poll_workflow_bootstraps_semble_for_judges()
    test_orchestrate_poll_workflow_adds_gated_setup_install_and_index_steps()
    test_live_judge_templates_expose_semble_placeholder()
    test_orchestrate_poll_process_wires_semble_for_all_judge_builders()
    test_review_rb_judge_wires_semble_prefetch()
    test_unwired_orchestrate_poll_judge_prompt_remains_unconsumed()
    print("OK: judge Semble prefetch contract assertions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
