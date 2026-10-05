#!/usr/bin/env python3
"""Host-side static prompt assembly must not follow checkout symlinks."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_autofix_step_scripts import REPO_ROOT, expanded_review_autofix_text  # noqa: E402


def _safe_env() -> dict[str, str]:
	env = os.environ.copy()
	for key in (
		"BASH_ENV", "ENV", "WORKSPACE_PATH", "GIT_DIR", "GIT_WORK_TREE",
		"GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
		"GIT_ALTERNATE_OBJECT_DIRECTORIES",
	):
		env.pop(key, None)
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _review_step_run() -> str:
	workflow = yaml.safe_load(expanded_review_autofix_text())
	for job in workflow["jobs"].values():
		for step in job.get("steps", []):
			if step.get("name") == "Pre-assemble static context (cacheable across runs)":
				return step["run"]
	raise AssertionError("review static-context step not found")


def test_review_static_context_skips_symlink_and_preserves_regular_readme() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		instructions = root / "instructions.txt"
		instructions.write_text("trusted instructions\n", encoding="utf-8")
		agents = root / "trusted_agents.txt"
		agents.write_text("trusted agents\n", encoding="utf-8")
		secret = root / "secret_config"
		secret.write_text("x-access-token:SENTINEL\n", encoding="utf-8")
		readme = root / "README.md"
		readme.symlink_to(secret)
		env = _safe_env()
		env.update(SUPPORT_INSTRUCTIONS_FILE=str(instructions), SUPPORT_AGENTS_FILE=str(agents))
		for symlinked in (True, False):
			if not symlinked:
				readme.unlink()
				readme.write_text(
					"public overview\n### 2. Create wrapper workflows\nprivate runbook\n",
					encoding="utf-8",
				)
			result = subprocess.run(
				["bash", "-c", _review_step_run()], cwd=root, env=env,
				capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			context = (root / "pre_assembled_static.txt").read_text(encoding="utf-8")
			assert "SENTINEL" not in context
			assert "trusted agents" in context
			if symlinked:
				assert "=== README.MD" not in context
				assert "::warning::README.md is a symbolic link" in result.stdout + result.stderr
			else:
				assert "=== README.MD (trimmed) ===\npublic overview\n" in context
				assert "private runbook" not in context


def test_phase_static_context_skips_symlinks_but_keeps_canonical_agents() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "unattended_system_instructions.md").write_text("trusted instructions\n", encoding="utf-8")
		(root / "ai_pipeline.md").write_text(
			"## Shared Runtime Behavior\nshared\n## Phase 1\nclarify\n"
			"## Phase 2\nplan\n## Phase 3\nimplement\n",
			encoding="utf-8",
		)
		secret = root / "secret_config"
		secret.write_text("x-access-token:SENTINEL\n", encoding="utf-8")
		(root / "README.md").symlink_to(secret)
		(root / "agents.md").symlink_to(secret)
		runtime = root / "runtime"
		runtime.mkdir()
		(runtime / "agents_canonical.md").write_text("canonical agents\n", encoding="utf-8")
		env = _safe_env()
		env["RUNTIME_DIR"] = str(runtime)
		for phase in ("clarify", "plan", "implement"):
			output = root / f"{phase}.txt"
			result = subprocess.run(
				["bash", str(REPO_ROOT / "scripts" / "build_static_context.sh"), phase, str(output)],
				cwd=root, env=env, capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			context = output.read_text(encoding="utf-8")
			assert "SENTINEL" not in context
			assert "=== README.MD" not in context
			assert "=== REPO-SPECIFIC AGENTS.MD" not in context
			assert "::warning::agents.md is a symbolic link" in result.stderr
			if phase == "implement":
				assert "=== AGENTS.MD ===\ncanonical agents\n" in context
			else:
				assert "::warning::README.md is a symbolic link" in result.stderr
				assert "=== AGENTS.MD" not in context

		(root / "README.md").unlink()
		(root / "agents.md").unlink()
		(root / "README.md").write_text(
			"public overview\n### 2. Create wrapper workflows\nprivate runbook\n", encoding="utf-8",
		)
		(root / "agents.md").write_text("local agents\n", encoding="utf-8")
		for phase in ("clarify", "plan", "implement"):
			output = root / f"{phase}-regular.txt"
			result = subprocess.run(
				["bash", str(REPO_ROOT / "scripts" / "build_static_context.sh"), phase, str(output)],
				cwd=root, env=env, capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			context = output.read_text(encoding="utf-8")
			if phase == "implement":
				assert "=== REPO-SPECIFIC AGENTS.MD ===\nlocal agents\n" in context
			else:
				assert "public overview" in context
			assert "private runbook" not in context
			assert "local agents" in context
			assert "::warning::" not in result.stderr

		for required_name in ("unattended_system_instructions.md", "ai_pipeline.md"):
			required_path = root / required_name
			original_content = required_path.read_text(encoding="utf-8")
			required_path.unlink()
			required_path.symlink_to(secret)
			for phase in ("clarify", "plan", "implement"):
				output = root / f"{phase}-{required_name}-symlink.txt"
				result = subprocess.run(
					["bash", str(REPO_ROOT / "scripts" / "build_static_context.sh"), phase, str(output)],
					cwd=root, env=env, capture_output=True, text=True, check=False,
				)
				assert result.returncode != 0
				assert "Required static context input is a symbolic link" in result.stderr
				assert "SENTINEL" not in result.stdout + result.stderr
				assert not output.exists()
			required_path.unlink()
			required_path.write_text(original_content, encoding="utf-8")


def main() -> int:
	for test in (
		test_review_static_context_skips_symlink_and_preserves_regular_readme,
		test_phase_static_context_skips_symlinks_but_keeps_canonical_agents,
	):
		test()
	print("OK: static-context symlink guard checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
