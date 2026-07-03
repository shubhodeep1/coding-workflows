"""Contract test for the editor capacity-fallback wiring.

Issue #3515 / run 28640359211: a sustained OpenRouter/OpenAI per-model TPM
saturation on the primary editor model (gpt-5.4) burned every retry attempt
because all attempts used the same model. The fix lets the codex retry loops
switch to a fallback editor model (MODEL_EDITOR_FALLBACK, default
openai/gpt-5.5 — a different capacity bucket) on their FINAL attempt.

This test pins the three moving parts so a future edit cannot silently drop
the fallback:
  1. openai/gpt-5.5 is declared in the model catalog (so codex can resolve
     apply_patch / verbosity for it when the loop switches --model).
  2. Each codex-driven workflow exposes MODEL_EDITOR_FALLBACK.
  3. Each retry loop actually switches to the fallback on the final attempt.
"""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG = REPO_ROOT / "scripts" / "codex_model_catalog.json"
PLAN = REPO_ROOT / ".github" / "workflows" / "plan.yml"
IMPLEMENT = REPO_ROOT / ".github" / "workflows" / "implement.yml"
REVIEW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
REVIEW_APPLY_FIXES = REPO_ROOT / "scripts" / "review_apply_fixes.sh"

FALLBACK_SLUG = "openai/gpt-5.5"
FALLBACK_ENV_DEFAULT = (
	"MODEL_EDITOR_FALLBACK: ${{ vars.WORKFLOW_EDITOR_FALLBACK_MODEL "
	"|| 'openai/gpt-5.5' }}"
)


def _catalog_entry(slug: str) -> dict:
	payload = json.loads(CATALOG.read_text(encoding="utf-8"))
	for entry in payload["models"]:
		if entry.get("slug") == slug:
			return entry
	raise AssertionError(f"{slug} not declared in {CATALOG}")


def test_fallback_model_declared_in_catalog() -> None:
	entry = _catalog_entry(FALLBACK_SLUG)
	# The loops switch --model to this slug while the config still points
	# model_catalog_json at the catalog file. If apply_patch resolution or
	# xhigh support is missing, the switched attempt regresses relative to
	# the primary. These mirror gpt-5.4 (same OpenAI generation).
	assert entry.get("apply_patch_tool_type") == "function"
	assert entry.get("support_verbosity") is True
	assert entry.get("supported_in_api") is True
	efforts = {
		level.get("effort")
		for level in entry.get("supported_reasoning_levels", [])
	}
	assert "xhigh" in efforts, (
		f"{FALLBACK_SLUG} must advertise xhigh (the repo-wide editor "
		f"reasoning level); got {sorted(efforts)}"
	)


def test_workflows_declare_fallback_env() -> None:
	for path in (PLAN, IMPLEMENT, REVIEW):
		text = path.read_text(encoding="utf-8")
		assert FALLBACK_ENV_DEFAULT in text, (
			f"{path} must declare MODEL_EDITOR_FALLBACK defaulting to "
			f"{FALLBACK_SLUG} via WORKFLOW_EDITOR_FALLBACK_MODEL"
		)


def test_plan_loop_switches_model_on_final_attempt() -> None:
	text = PLAN.read_text(encoding="utf-8")
	assert 'attempt_model="${MODEL_EDITOR}"' in text
	assert (
		'[ "${attempt}" -eq "${max_attempts}" ] '
		'&& [ -n "${MODEL_EDITOR_FALLBACK:-}" ]'
	) in text
	assert 'attempt_model="${MODEL_EDITOR_FALLBACK}"' in text
	# The codex invocation must consume the per-attempt model, not the raw
	# primary — otherwise the switch is computed but never applied.
	assert '--model "${attempt_model}"' in text
	assert '--model "${MODEL_EDITOR}"' not in text


def test_implement_loop_switches_model_on_final_attempt() -> None:
	text = IMPLEMENT.read_text(encoding="utf-8")
	assert 'attempt_model="${MODEL_EDITOR}"' in text
	assert (
		'[ "${attempt}" -eq "${max_attempts}" ] '
		'&& [ -n "${MODEL_EDITOR_FALLBACK:-}" ]'
	) in text
	assert 'CODEX_THREAD_REUSE_MODEL="${attempt_model}"' in text
	# The MAIN implement pass switches to the per-attempt model. The
	# secondary implement_repair (syntax-repair) pass intentionally keeps the
	# primary MODEL_EDITOR — it reuses a codex thread with a replace-prefix
	# transform where a mid-continuation model swap is both riskier and
	# unnecessary (it is not the token-heavy pass that hits TPM saturation).
	# Pin that boundary: exactly one raw-primary CODEX_THREAD_REUSE_MODEL
	# assignment remains (the repair pass).
	assert text.count('CODEX_THREAD_REUSE_MODEL="${MODEL_EDITOR}"') == 1


def test_review_apply_fixes_switches_model_on_final_attempt() -> None:
	text = REVIEW_APPLY_FIXES.read_text(encoding="utf-8")
	# The editor codex invocations consume the per-attempt model.
	assert '--model "${editor_attempt_model}"' in text
	assert '--model "${MODEL_EDITOR}"' not in text
	# The loop sets the fallback on the final attempt.
	assert 'EDITOR_ATTEMPT_MODEL="${MODEL_EDITOR_FALLBACK}"' in text
	assert 'local editor_attempt_model="${EDITOR_ATTEMPT_MODEL:-${MODEL_EDITOR}}"' in text
