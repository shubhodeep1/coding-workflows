"""The shared read sandbox must have its relay in every staged support copy."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_relay_staged_for_read_profiles() -> None:
	implement = (ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	poller = (ROOT / ".github/workflows/orchestrate_poll.yml").read_text(encoding="utf-8")
	stager = (ROOT / "scripts/stage_workflow_support.sh").read_text(encoding="utf-8")
	assert "ai_engine.sh claude_engine.py claude_read_isolated_run.sh claude_read_snapshot.py claude_anthropic_relay.py review_untrusted_workspace.py; do" in implement
	assert 'for _staged_support_runtime_script in "${_fetched_scripts[@]}"; do' in implement
	assert "ai_engine.sh claude_engine.py claude_read_isolated_run.sh claude_read_snapshot.py claude_anthropic_relay.py review_untrusted_workspace.py claude_settings.json.tmpl codex_stall_guard.sh clarify_isolated_run.sh clarify_openrouter_broker.py codex_model_catalog.json; do" in poller
	assert "OPTIONAL_BOOTSTRAP_SCRIPTS=" in stager
	assert "claude_engine.py claude_anthropic_relay.py" in stager


def test_smoke_accepts_filtered_history_snapshot() -> None:
	smoke = (ROOT / ".github/workflows/claude-engine-smoke.yml").read_text(encoding="utf-8")
	assert "reason=(none|alternates|filtered_history) files=[0-9]+" in smoke


def test_heal_passes_only_created_worktrees_to_read_snapshot() -> None:
	heal = (ROOT / "scripts/workflow_failure_heal_intake.sh").read_text(encoding="utf-8")
	assert 'RUNTIME_DIR="${RUNTIME_DIR}"' in heal
	assert 'AI_ENGINE_ISOLATED_READ_PATHS="${heal_read_extra_dirs}"' in heal
	assert '"${HEAL_SOURCE_NOTE:-unavailable (diagnose against the working directory)}" != "unavailable (diagnose against the working directory)"' in heal
	assert '"${HEAL_BRANCH_TIP_NOTE:-unavailable}" != "unavailable"' in heal
	assert heal.index('AI_ENGINE_ISOLATED_READ_PATHS="${heal_read_extra_dirs}"') < heal.index("claude_run_selected WORKFLOW_HEAL")
