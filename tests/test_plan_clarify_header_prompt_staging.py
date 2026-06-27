#!/usr/bin/env python3
"""Regression guard: prompts/header.txt is staged before it is rendered.

Reported from consumer run 28221091844 (shubhodeep1/binance-blessings,
issue #220): the AI Plan phase failed before posting the implementation plan
with

  Prompt file not found: prompts/header.txt
  ##[error]Process completed with exit code 1.

plan.yml and clarify.yml assemble the Codex prompt with

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

1. A static contract: every reusable workflow that renders
   ``render_prompt.sh prompts/header.txt`` must also stage prompts/header.txt
   into the working tree with the standard .codex-workflow-src ->
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


def test_render_callers_stage_header_prompt() -> None:
	"""Every workflow rendering prompts/header.txt must stage it first."""
	callers = _workflows_rendering_header()
	# plan.yml and clarify.yml are the known callers; the discovery must not be
	# empty (an empty match would silently pass and rot).
	assert callers, (
		"no workflow renders prompts/header.txt -- discovery regex is stale"
	)
	caller_names = {p.name for p in callers}
	assert {"plan.yml", "clarify.yml"} <= caller_names, (
		f"expected plan.yml and clarify.yml to render prompts/header.txt, "
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
