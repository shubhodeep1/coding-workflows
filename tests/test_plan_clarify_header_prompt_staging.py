#!/usr/bin/env python3
"""Regression guard: prompts/header.txt is staged before it is rendered.

Reported from consumer run 28221091844 (shubhodeep1/binance-blessings,
issue #220): the AI Plan phase failed before posting the implementation plan
with

  Prompt file not found: prompts/header.txt
  ##[error]Process completed with exit code 1.

plan.yml's staged runner and clarify.yml assemble the Codex prompt with

  REPO_LEARNINGS="$(cat "${RUNTIME_DIR}/repo_learnings.txt")" \
    bash scripts/render_prompt.sh prompts/header.txt

render_prompt.sh resolves the bare ``prompts/header.txt`` path relative to the
working tree and runs ``[ -f prompts/header.txt ]`` *before* delegating to
render_prompt.py, so the fragment must be staged into ./prompts/ alongside the
staged scripts/. PR #3411 added the render invocation to both workflows but the
"Stage workflow support files" step never copied prompts/header.txt out of the
.codex-workflow-src support checkout, so the first executing plan run after the
@stable wrapper bump failed deterministically at the static file-existence
check. header.txt carries only the {{REPO_LEARNINGS}} placeholder (resolved
from the REPO_LEARNINGS env var) and has no contract, so staging the single
file is sufficient.

Two kinds of tests pin the fix:

1. A static contract: every reusable workflow or staged runner that renders
   ``render_prompt.sh prompts/header.txt`` has workflow staging for
   prompts/header.txt with the standard .codex-workflow-src ->
   .codex-workflow-src-main fallback and a hard error when it is unavailable.

2. A behavioural test that reproduces the runtime layout (scripts/ staged,
   prompts/ absent from the working tree), runs the staging block, and asserts
   the header renders with {{REPO_LEARNINGS}} hydrated -- with a negative
   control proving the exact consumer error fires when staging is skipped.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
PLAN_WORKFLOW = WORKFLOW_DIR / "plan.yml"
PLAN_RUNNER = REPO_ROOT / "scripts" / "run_plan_codex.sh"
HEADER_PROMPT = REPO_ROOT / "prompts" / "header.txt"

# The bare-path render invocation that requires prompts/header.txt on disk.
HEADER_RENDER_RE = re.compile(
	r"\bbash\s+scripts/render_prompt\.sh\s+prompts/header\.txt\b"
)


def _workflow_has_header_render_invocation(workflow_text: str) -> bool:
	"""True when a workflow executes, not merely comments about, the render."""
	for line in workflow_text.splitlines():
		if line.lstrip().startswith("#"):
			continue
		if HEADER_RENDER_RE.search(line):
			return True
	return False


def _workflows_rendering_header() -> list[Path]:
	"""Reusable workflows that render the bare prompts/header.txt path."""
	matches: list[Path] = []
	for yml in sorted(WORKFLOW_DIR.glob("*.yml")):
		if _workflow_has_header_render_invocation(
			yml.read_text(encoding="utf-8")
		):
			matches.append(yml)
	return matches


def test_header_prompt_exists() -> None:
	"""The header fragment the workflows stage must exist in the repo."""
	assert HEADER_PROMPT.is_file(), "missing prompts/header.txt"
	assert "{{REPO_LEARNINGS}}" in HEADER_PROMPT.read_text(encoding="utf-8"), (
		"prompts/header.txt no longer carries the {{REPO_LEARNINGS}} placeholder"
	)


def test_clarify_sandbox_support_has_main_snapshot_fallback() -> None:
	"""The sandbox must be staged from the same support refs as the runner."""
	clarify = (WORKFLOW_DIR / "clarify.yml").read_text(encoding="utf-8")
	assert 'src=".codex-workflow-src/scripts/${f}"' in clarify
	assert '.codex-workflow-src-main/scripts/${f}' in clarify
	assert 'sandbox_src=".codex-workflow-src/scripts/clarify_sandbox/Dockerfile"' in clarify
	assert '.codex-workflow-src-main/scripts/clarify_sandbox/Dockerfile' in clarify
	assert 'echo "::error::Missing clarification sandbox Dockerfile"' in clarify
	assert 'install -m 0644 "${sandbox_src}" scripts/clarify_sandbox/Dockerfile' in clarify


def test_clarify_respond_isolates_every_model_call() -> None:
	respond = (WORKFLOW_DIR / "orchestrate_clarify_respond.yml").read_text(encoding="utf-8")
	assert "orchestrate_parse_and_post_answer.sh clarify_isolated_run.sh clarify_openrouter_broker.py ai_engine.sh claude_engine.py claude_anthropic_relay.py claude_settings.json.tmpl auto_decisions.py clarify_github_facts.py clarify_data_provision_guard.py; do" in respond
	assert 'sandbox_src=".codex-workflow-src/scripts/clarify_sandbox/Dockerfile"' in respond
	assert '.codex-workflow-src-main/scripts/clarify_sandbox/Dockerfile' in respond
	assert 'install -m 0644 "${sandbox_src}" scripts/clarify_sandbox/Dockerfile' in respond
	assert 'printf \'%s\\n\' "${_fetched_scripts[@]}" clarify_sandbox/ .gitignore' in respond
	assert "--sandbox danger-full-access" not in respond
	assert "codex --ask-for-approval" not in respond
	# Each of the three model calls has a Claude branch (Phase 5a) and the
	# unchanged codex call it falls back to (exit 75, plan D1).
	assert respond.count('bash scripts/clarify_isolated_run.sh ') == 6
	for args, rc in (
		('"${CODEX_PROMPT_FILE}" "${CODEX_OUTPUT_FILE}" "${RUNTIME_DIR}/codex_log.txt"', "rc"),
		('"${RUNTIME_DIR}/critic_prompt.txt" "${RUNTIME_DIR}/critic_output.txt" "${RUNTIME_DIR}/codex_log.txt"', "critic_rc"),
		('"${CODEX_PROMPT_FILE}.v2" "${CODEX_OUTPUT_FILE}" "${RUNTIME_DIR}/codex_log.txt"', "revise_rc"),
	):
		assert f"bash scripts/clarify_isolated_run.sh {args} claude CLARIFY_RESPOND || {rc}=$?" in respond
		assert f"bash scripts/clarify_isolated_run.sh {args} || {rc}=$?" in respond
		assert f'if [ "${{{rc}}}" -eq 75 ]; then' in respond
	assert respond.count("CLARIFY_CODEX_VERSION: ${{ vars.CODEX_VERSION || 'v0.114.0' }}") == 2


def test_clarify_respond_critique_uses_the_answer_step_fallback() -> None:
	respond = (WORKFLOW_DIR / "orchestrate_clarify_respond.yml").read_text(encoding="utf-8")
	answer_output = 'echo "engine=${CLARIFY_RESPOND_ENGINE}" >> "$GITHUB_OUTPUT"'
	assert answer_output in respond
	assert respond.index(answer_output) < respond.index("      - name: Self-critique pass for non-letter decisions")
	assert "CLARIFY_RESPOND_ENGINE: ${{ steps.run_codex.outputs.engine || steps.ai_engine.outputs.engine || 'codex' }}" in respond


def test_isolated_runner_checks_role_support_and_timeout_before_docker() -> None:
	with tempfile.TemporaryDirectory(prefix="clarify-isolation-preflight-") as td:
		tmp_path = Path(td)
		runner = REPO_ROOT / "scripts" / "clarify_isolated_run.sh"
		prompt = tmp_path / "prompt.txt"
		prompt.write_text("prompt", encoding="utf-8")
		base_env = dict(os.environ, MODEL_EDITOR="openai/gpt-6-sol", MODEL_REASONING_EFFORT="high")
		for engine, role, extra_env, error in (
			# PLAN is accepted since the heal planner runs here (#6463); an
			# unsupported role is still refused before any Docker work.
			("codex", "IMPLEMENT", {}, "Invalid clarify engine role"),
			("claude", "IMPLEMENT", {}, "Invalid clarify engine role"),
			("codex", "UNBLOCK_JUDGE", {"CLARIFY_ISOLATION_SUPPORT_DIR": "relative"}, "Invalid clarify isolation support directory"),
			("codex", "UNBLOCK_JUDGE", {"CLARIFY_ISOLATION_SUPPORT_DIR": str(tmp_path / "missing")}, "Invalid clarify isolation support directory"),
			("codex", "UNBLOCK_JUDGE", {"CLARIFY_ISOLATION_TIMEOUT_SECS": "0"}, "Invalid clarify isolation timeout"),
			("codex", "UNBLOCK_JUDGE", {"CLARIFY_ISOLATION_TIMEOUT_SECS": "abc"}, "Invalid clarify isolation timeout"),
			("claude", "UNBLOCK_JUDGE", {"CLARIFY_ISOLATION_TIMEOUT_SECS": "abc"}, "Invalid clarify isolation timeout"),
		):
			result = subprocess.run(["bash", str(runner), str(prompt), str(tmp_path / "out"), str(tmp_path / "log"), engine, role],
				cwd=REPO_ROOT, env={**base_env, **extra_env}, text=True, capture_output=True, check=False)
			assert result.returncode == 1 and error in result.stderr, result.stderr


def test_isolated_runner_support_override_and_optional_in_container_timeout() -> None:
	runner = (REPO_ROOT / "scripts" / "clarify_isolated_run.sh").read_text(encoding="utf-8")
	assert 'support="scripts"' in runner  # Existing clarify call sites keep their relative support paths.
	assert '[[ "${engine_role}" =~ ^(CLARIFY|CLARIFY_RESPOND|PLAN|UNBLOCK_JUDGE)$ ]]' in runner
	assert '[ "${engine}" != claude ] || [[ "${engine_role}" =~ ^(CLARIFY|CLARIFY_RESPOND|PLAN|UNBLOCK_JUDGE)$ ]]' in runner
	assert '"${support}/clarify_sandbox/Dockerfile" "${support}/clarify_sandbox"' in runner
	assert 'install -D -m 0644 "${support}/write_codex_config.sh"' in runner
	assert 'install -D -m 0644 "${support}/codex_model_catalog.json"' in runner
	assert '--env "CLARIFY_ISOLATION_TIMEOUT_SECS=${isolation_timeout}"' in runner
	assert 'timeout "${CLARIFY_ISOLATION_TIMEOUT_SECS}" claude -p' in runner
	assert 'if [ -n "${CLARIFY_ISOLATION_TIMEOUT_SECS}" ]; then' in runner
	assert 'timeout "${CLARIFY_ISOLATION_TIMEOUT_SECS}" codex ' in runner
	assert '\n\t\telse\n\t\t\tcodex ' in runner


def test_claude_image_build_failure_triggers_codex_fallback() -> None:
	runner_text = (REPO_ROOT / "scripts" / "clarify_isolated_run.sh").read_text(encoding="utf-8")
	claude_build = runner_text[runner_text.index('\tif ! image='):runner_text.index('\tfor account in "${claude_accounts[@]}"; do')]
	build_script = 'set -euo pipefail\nenv() { return 1; }\nai_engine_fallback() { printf "AI_ENGINE_FALLBACK role=%s reason=%s\\n" "$1" "$2" >&2; }\nengine_role=CLARIFY\nversion=v0.114.0\nclaude_version=1.0.0\nsupport=scripts\n' + claude_build
	build_result = subprocess.run(["bash", "-c", build_script], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)
	assert build_result.returncode == 75, build_result.stderr
	assert "AI_ENGINE_FALLBACK role=CLARIFY reason=image_build_failed" in build_result.stderr


def test_render_callers_stage_header_prompt() -> None:
	"""Every workflow rendering prompts/header.txt must stage it first."""
	callers = _workflows_rendering_header()
	# clarify.yml remains an inline caller; plan.yml delegates to its staged
	# runner, which is checked separately below.
	assert callers, (
		"no workflow renders prompts/header.txt -- discovery regex is stale"
	)
	caller_names = {p.name for p in callers}
	assert "clarify.yml" in caller_names, (
		f"expected clarify.yml to render prompts/header.txt, "
		f"found {sorted(caller_names)}"
	)
	for yml in callers:
		text = yml.read_text(encoding="utf-8")
		assert "mkdir -p prompts" in text, (
			f"{yml.name}: renders prompts/header.txt but never `mkdir -p prompts`"
		)
		assert 'install -m 0644 "${src}" prompts/header.txt' in text, (
			f"{yml.name}: renders prompts/header.txt but never installs it into "
			f"the working tree"
		)
		assert 'src=".codex-workflow-src/prompts/header.txt"' in text, (
			f"{yml.name}: header staging is missing the primary support-source path"
		)
		assert '.codex-workflow-src-main/prompts/header.txt' in text, (
			f"{yml.name}: header staging is missing the main-snapshot fallback"
		)
		assert (
			"::error::Failed to stage required file prompts/header.txt" in text
		), (
			f"{yml.name}: header staging must hard-fail when the fragment is "
			f"unavailable (it is required for prompt assembly)"
		)
		assert re.search(
			r'echo "::error::Failed to stage required file prompts/header\.txt"\n\s+exit 1',
			text,
		), (
			f"{yml.name}: header staging must exit immediately when neither "
			f"support checkout carries the fragment"
		)

	plan_workflow_text = PLAN_WORKFLOW.read_text(encoding="utf-8")
	plan_runner_text = PLAN_RUNNER.read_text(encoding="utf-8")
	assert "for f in gh_helpers.sh run_plan_codex.sh render_prompt.sh" in plan_workflow_text
	assert 'install -m 0644 "${src}" prompts/header.txt' in plan_workflow_text
	assert 'src=".codex-workflow-src/prompts/header.txt"' in plan_workflow_text
	assert '.codex-workflow-src-main/prompts/header.txt' in plan_workflow_text
	assert "::error::Failed to stage required file prompts/header.txt" in plan_workflow_text
	assert HEADER_RENDER_RE.search(plan_runner_text)


def _render_header(
	*,
	stage_header: bool,
	prefer_main_snapshot: bool = False,
	support_header_available: bool = True,
) -> subprocess.CompletedProcess[str]:
	"""Render prompts/header.txt the way plan.yml / clarify.yml do.

	Reproduces the consumer runtime layout: scripts/ staged into the working
	tree, the support checkout under .codex-workflow-src, and prompts/ absent
	until the staging block runs. When ``stage_header`` is true the exact
	staging snippet from the workflows runs before the render. When
	``prefer_main_snapshot`` is true the header exists only in the
	.codex-workflow-src-main fallback checkout. When
	``support_header_available`` is false, neither support checkout carries the
	header fragment and the staging block must hard-fail.
	"""
	with tempfile.TemporaryDirectory(prefix="plan-header-staging-") as td:
		root = Path(td)
		scripts_dir = root / "scripts"
		support_prompts = root / ".codex-workflow-src" / "prompts"
		support_prompts_main = root / ".codex-workflow-src-main" / "prompts"
		support_scripts = root / ".codex-workflow-src" / "scripts"
		runtime_dir = root / "rt"
		for d in (
			scripts_dir,
			support_prompts,
			support_prompts_main,
			support_scripts,
			runtime_dir,
		):
			d.mkdir(parents=True)

		# Stage the renderer (shim + python backend) as the workflows do.
		for src in (RENDER_PROMPT_PY, RENDER_PROMPT_SH):
			(scripts_dir / src.name).write_text(
				src.read_text(encoding="utf-8"), encoding="utf-8"
			)
		(scripts_dir / "render_prompt.sh").chmod(0o755)
		# render_prompt.py must also be resolvable from the support checkout.
		(support_scripts / RENDER_PROMPT_PY.name).write_text(
			RENDER_PROMPT_PY.read_text(encoding="utf-8"), encoding="utf-8"
		)
		# The header fragment exists only in the support checkout, mirroring a
		# consumer repo that ships none of these files.
		if support_header_available:
			header_prompt_dir = (
				support_prompts_main if prefer_main_snapshot else support_prompts
			)
			(header_prompt_dir / "header.txt").write_text(
				HEADER_PROMPT.read_text(encoding="utf-8"), encoding="utf-8"
			)
		(runtime_dir / "repo_learnings.txt").write_text(
			"Learned: prefer batched GraphQL.\n", encoding="utf-8"
		)

		stage_snippet = (
			'mkdir -p prompts\n'
			'if [ ! -f prompts/header.txt ]; then\n'
			'  src=".codex-workflow-src/prompts/header.txt"\n'
			'  if [ ! -f "${src}" ] && [ -f ".codex-workflow-src-main/prompts/header.txt" ]; then\n'
			'    src=".codex-workflow-src-main/prompts/header.txt"\n'
			'  fi\n'
			'  if [ ! -f "${src}" ]; then\n'
			'    echo "::error::Failed to stage required file prompts/header.txt"\n'
			'    exit 1\n'
			'  fi\n'
			'  install -m 0644 "${src}" prompts/header.txt\n'
			'fi\n'
			if stage_header
			else ""
		)
		script = (
			"set -euo pipefail\n"
			+ stage_snippet
			+ 'REPO_LEARNINGS="$(cat rt/repo_learnings.txt)" '
			+ "bash scripts/render_prompt.sh prompts/header.txt\n"
		)

		env = os.environ.copy()
		env["PYTHONDONTWRITEBYTECODE"] = "1"
		for key in ("BASH_ENV", "ENV", "WORKSPACE_PATH"):
			env.pop(key, None)
		env["PWD"] = str(root)
		env.pop("OLDPWD", None)
		return subprocess.run(
			["bash", "-c", script],
			cwd=str(root),
			env=env,
			capture_output=True,
			text=True,
			timeout=120,
		)


def test_header_renders_when_staged() -> None:
	"""With the staging block, the header renders and {{REPO_LEARNINGS}} hydrates."""
	for prefer_main_snapshot in (False, True):
		result = _render_header(
			stage_header=True, prefer_main_snapshot=prefer_main_snapshot
		)
		assert result.returncode == 0, (
			f"render failed unexpectedly (prefer_main_snapshot={prefer_main_snapshot}): "
			f"rc={result.returncode}\nstderr={result.stderr}"
		)
		assert "Prompt file not found" not in result.stderr, (
			f"staged render still hit the missing-header path "
			f"(prefer_main_snapshot={prefer_main_snapshot})"
		)
		assert "{{REPO_LEARNINGS}}" not in result.stdout, (
			f"placeholder left unhydrated in rendered header "
			f"(prefer_main_snapshot={prefer_main_snapshot})"
		)
		assert "Learned: prefer batched GraphQL." in result.stdout, (
			f"REPO_LEARNINGS env value not injected into the rendered header "
			f"(prefer_main_snapshot={prefer_main_snapshot})"
		)


def test_header_staging_hard_fails_without_any_support_copy() -> None:
	"""When neither support checkout has header.txt, staging must stop first."""
	result = _render_header(stage_header=True, support_header_available=False)
	assert result.returncode != 0
	assert "::error::Failed to stage required file prompts/header.txt" in result.stdout
	assert "Prompt file not found: prompts/header.txt" not in result.stderr


def test_header_render_fails_without_staging() -> None:
	"""Negative control: the exact consumer error fires when staging is skipped.

	Proves the staging block is load-bearing -- a regression that drops it
	reproduces ``Prompt file not found: prompts/header.txt`` and exit 1.
	"""
	result = _render_header(stage_header=False)
	assert result.returncode != 0
	assert "Prompt file not found: prompts/header.txt" in result.stderr


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	passed = 0
	failed = 0
	for test in tests:
		name = test.__name__
		try:
			test()
			print(f"  PASS  {name}")
			passed += 1
		except AssertionError as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1
		except Exception as exc:
			print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
			failed += 1
	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
