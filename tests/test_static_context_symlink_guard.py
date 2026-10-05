#!/usr/bin/env python3
"""Host-side static prompt assembly must not follow checkout symlinks."""

from __future__ import annotations

import os
import shutil
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
		overflow = root / "probably_unnecessary_but_read_if_stuck.md"
		overflow.symlink_to(secret)
		env = _safe_env()
		runtime = root / "runtime"
		runtime.mkdir()
		env.update(SUPPORT_INSTRUCTIONS_FILE=str(instructions), SUPPORT_AGENTS_FILE=str(agents),
			SUPPORT_SCRIPTS_DIR=str(REPO_ROOT / "scripts"), RUNTIME_DIR=str(runtime))
		for symlinked in (True, False):
			if not symlinked:
				readme.unlink()
				overflow.unlink()
				overflow.write_text("public runbook\n", encoding="utf-8")
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
				assert "=== OVERFLOW REFERENCE ===" not in context
				assert "read ./probably_unnecessary_but_read_if_stuck.md" not in context
				assert "reason=symlink_path" in result.stdout + result.stderr
				assert "::warning::Review static README omitted" in result.stdout + result.stderr
				assert "::warning::probably_unnecessary_but_read_if_stuck.md is a symbolic link" in result.stdout + result.stderr
			else:
				assert "public overview" not in context
				assert (runtime / "static_readme_trimmed.txt").read_text() == "public overview\n"
				assert "=== OVERFLOW REFERENCE ===" in context
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
		overflow = root / "probably_unnecessary_but_read_if_stuck.md"
		overflow.symlink_to(secret)
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
			assert "=== OVERFLOW REFERENCE ===" not in context
			assert "read ./probably_unnecessary_but_read_if_stuck.md" not in context
			assert "=== REPO-SPECIFIC AGENTS.MD" not in context
			assert "::warning::agents.md is a symbolic link" in result.stderr
			assert "::warning::probably_unnecessary_but_read_if_stuck.md is a symbolic link" in result.stderr
			if phase == "implement":
				assert "=== AGENTS.MD ===\ncanonical agents\n" in context
			else:
				assert "STATIC_CONTEXT_README outcome=rejected reason=symlink_path" in result.stderr
				assert "::warning::Static context README omitted" in result.stderr
				assert "=== AGENTS.MD" not in context

		(root / "README.md").unlink()
		(root / "agents.md").unlink()
		overflow.unlink()
		overflow.write_text("public runbook\n", encoding="utf-8")
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
				assert "UNTRUSTED_DATA: public overview" in context
				assert "=== BEGIN UNTRUSTED README.MD (trimmed) ===" in context
			assert "private runbook" not in context
			assert "=== OVERFLOW REFERENCE ===" in context
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


def test_orchestrator_static_context_guards_symlinks() -> None:
	for workflow_name, step_name, agent_name in (
		("orchestrate.yml", "Pre-assemble static context", "agents.md"),
		("orchestrate_clarify_respond.yml", "Pre-assemble static context (cacheable across runs)", "AGENTS.md"),
	):
		workflow = yaml.safe_load((REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
		step_run = next(
			step["run"]
			for job in workflow["jobs"].values()
			for step in job.get("steps", [])
			if step.get("name") == step_name
		)
		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			(root / "scripts").mkdir()
			shutil.copy2(REPO_ROOT / "scripts/build_static_context.sh", root / "scripts/build_static_context.sh")
			(root / "unattended_system_instructions.md").write_text("trusted instructions\n", encoding="utf-8")
			(root / "ai_pipeline.md").write_text("trusted pipeline\n", encoding="utf-8")
			(root / ".git").mkdir()
			secret = root / ".git/config"
			secret.write_text("x-access-token:SENTINEL\n", encoding="utf-8")
			for name in ("agents.md", "AGENTS.md", "README.md", "probably_unnecessary_but_read_if_stuck.md"):
				(root / name).symlink_to(secret)
			workflow_env = _safe_env()
			workflow_env["RUNNER_TEMP"] = str(root)
			result = subprocess.run(
				["bash", "-c", step_run], cwd=root, env=workflow_env,
				capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			context = (root / "pre_assembled_static.txt").read_text(encoding="utf-8")
			assert "trusted instructions" in context
			assert "SENTINEL" not in context
			assert "=== AGENTS.MD ===" not in context
			assert "=== README.MD ===" not in context
			assert "read ./probably_unnecessary_but_read_if_stuck.md" not in context
			assert f"::warning::{agent_name} is a symbolic link" in result.stderr
			assert "STATIC_CONTEXT_README outcome=rejected reason=symlink_path" in result.stderr
			assert "::warning::Static context README omitted" in result.stderr
			assert "::warning::probably_unnecessary_but_read_if_stuck.md is a symbolic link" in result.stderr

			if agent_name == "AGENTS.md":
				(root / "agents.md").unlink()
				(root / "agents.md").write_text("safe fallback agents\n", encoding="utf-8")
				result = subprocess.run(
					["bash", "-c", step_run], cwd=root, env=workflow_env,
					capture_output=True, text=True, check=False,
				)
				assert result.returncode == 0, result.stderr
				assert "safe fallback agents" in (root / "pre_assembled_static.txt").read_text(encoding="utf-8")
				assert "SENTINEL" not in (root / "pre_assembled_static.txt").read_text(encoding="utf-8")

			for name in ("agents.md", "AGENTS.md", "README.md", "probably_unnecessary_but_read_if_stuck.md"):
				(root / name).unlink()
				(root / name).write_text(f"regular {name}\n", encoding="utf-8")
			result = subprocess.run(
				["bash", "-c", step_run], cwd=root, env=workflow_env,
				capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			context = (root / "pre_assembled_static.txt").read_text(encoding="utf-8")
			assert f"regular {agent_name}" in context
			assert "UNTRUSTED_DATA: regular README.md" in context
			assert "=== OVERFLOW REFERENCE ===" in context
			assert "SENTINEL" not in context
			assert "::warning::" not in result.stderr

			for required_name in ("unattended_system_instructions.md", "ai_pipeline.md"):
				required_path = root / required_name
				required_path.unlink()
				required_path.symlink_to(secret)
				(root / "pre_assembled_static.txt").unlink(missing_ok=True)
				result = subprocess.run(
					["bash", "-c", step_run], cwd=root, env=workflow_env,
					capture_output=True, text=True, check=False,
				)
				assert result.returncode != 0
				assert "Required static context input is a symbolic link" in result.stderr
				assert "SENTINEL" not in result.stdout + result.stderr
				assert not (root / "pre_assembled_static.txt").exists()
				required_path.unlink()
				required_path.write_text("trusted input\n", encoding="utf-8")

			output = root / "pre_assembled_static.txt"
			output.unlink(missing_ok=True)
			output.symlink_to(secret)
			result = subprocess.run(
				["bash", "-c", step_run], cwd=root, env=workflow_env,
				capture_output=True, text=True, check=False,
			)
			assert result.returncode != 0
			assert "non-regular pre_assembled_static.txt" in result.stderr
			assert secret.read_text(encoding="utf-8") == "x-access-token:SENTINEL\n"


def test_shared_readme_context_rejects_non_regular_inputs_and_output() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		(root / "unattended_system_instructions.md").write_text("trusted instructions\n", encoding="utf-8")
		(root / "ai_pipeline.md").write_text("trusted pipeline\n", encoding="utf-8")
		secret = root / "secret"
		secret.write_text("SENTINEL\n", encoding="utf-8")
		readme = root / "README.md"
		output = root / "readme-context.txt"
		for kind in ("symlink", "fifo", "directory", "oversize"):
			if readme.exists() or readme.is_symlink():
				if readme.is_dir() and not readme.is_symlink():
					readme.rmdir()
				else:
					readme.unlink()
			if kind == "symlink":
				readme.symlink_to(secret)
			elif kind == "fifo":
				os.mkfifo(readme)
			elif kind == "directory":
				readme.mkdir()
			else:
				readme.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
			result = subprocess.run(
				["bash", str(REPO_ROOT / "scripts/build_static_context.sh"), "readme", str(output)],
				cwd=root, env=_safe_env(), capture_output=True, text=True, check=False,
			)
			assert result.returncode == 0, result.stderr
			assert output.read_text(encoding="utf-8") == ""
			assert "STATIC_CONTEXT_README outcome=rejected" in result.stderr
			assert "SENTINEL" not in result.stdout + result.stderr

		if readme.is_dir():
			readme.rmdir()
		else:
			readme.unlink()
		outside = root / "outside"
		outside.write_text("unchanged\n", encoding="utf-8")
		output.unlink()
		output.symlink_to(outside)
		result = subprocess.run(
			["bash", str(REPO_ROOT / "scripts/build_static_context.sh"), "readme", str(output)],
			cwd=root, env=_safe_env(), capture_output=True, text=True, check=False,
		)
		assert result.returncode == 1
		assert outside.read_text(encoding="utf-8") == "unchanged\n"
		assert "Static context output is not a regular file" in result.stderr


def test_shared_readme_only_requires_readme_and_bounds_prompt_size() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		root = Path(tmp)
		readme = root / "README.md"
		output = root / "readme-context.txt"
		readme.write_text("safe overview\n", encoding="utf-8")
		result = subprocess.run(
			["bash", str(REPO_ROOT / "scripts/build_static_context.sh"), "readme", str(output)],
			cwd=root, env=_safe_env(), capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert "UNTRUSTED_DATA: safe overview" in output.read_text(encoding="utf-8")
		readme.write_bytes(b"x\n" * 12000)
		result = subprocess.run(
			["bash", str(REPO_ROOT / "scripts/build_static_context.sh"), "readme", str(output)],
			cwd=root, env=_safe_env(), capture_output=True, text=True, check=False,
		)
		assert result.returncode == 0, result.stderr
		assert output.read_text(encoding="utf-8") == ""
		assert "reason=prompt_size" in result.stderr


def test_all_static_readme_assemblers_use_the_shared_reader() -> None:
	workflow_expectations = {
		"orchestrate.yml": "orchestrate_readme_context",
		"orchestrate_clarify_respond.yml": "clarify_respond_readme_context",
		"orchestrate_poll.yml": "build_static_context.sh",
		"validate.yml": "scripts/build_static_context.sh",
	}
	for workflow_name, expected_text in workflow_expectations.items():
		workflow_text = (REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
		assert expected_text in workflow_text
	assert "cat README.md" not in (REPO_ROOT / ".github/workflows/orchestrate.yml").read_text(encoding="utf-8")
	assert "cat README.md" not in (REPO_ROOT / ".github/workflows/orchestrate_clarify_respond.yml").read_text(encoding="utf-8")
	for script_name in ("orchestrate_poll_process.sh", "validate_process.sh"):
		script_text = (REPO_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
		assert "build_static_context.sh readme" in script_text
		assert "cat README.md" not in script_text


def main() -> int:
	for test in (
		test_review_static_context_skips_symlink_and_preserves_regular_readme,
		test_phase_static_context_skips_symlinks_but_keeps_canonical_agents,
		test_orchestrator_static_context_guards_symlinks,
		test_shared_readme_context_rejects_non_regular_inputs_and_output,
		test_shared_readme_only_requires_readme_and_bounds_prompt_size,
		test_all_static_readme_assemblers_use_the_shared_reader,
	):
		test()
	readme_suite = subprocess.run(
		[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_review_static_context_readme.py"],
		cwd=REPO_ROOT, env=_safe_env(), check=False,
	)
	if readme_suite.returncode != 0:
		return readme_suite.returncode
	print("OK: static-context symlink guard and review README checks passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
