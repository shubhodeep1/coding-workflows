"""PR-checkout Python probes must not execute checkout-controlled modules."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _run(script: str, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONPATH", "PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env.update({"PYTHONDONTWRITEBYTECODE": "1", **env})
	return subprocess.run(
		["bash", str(SCRIPTS / script), *args], cwd=cwd, env=child_env,
		capture_output=True, text=True, timeout=45, check=False,
	)


def _poison_module(path: Path, marker: Path, extra: str = "") -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(f"open({str(marker)!r}, 'w').write('executed')\n{extra}", encoding="utf-8")


def test_semble_install_never_imports_pr_modules(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "semble" / "__init__.py", marker)
	_poison_module(pr_tree / "semble" / "version.py", marker, "__version__ = '0.1.3'\n")
	_poison_module(pr_tree / "pip" / "__main__.py", marker)
	github_env = tmp_path / "github.env"
	result = _run("install_semble.sh", pr_tree, {
		"SEMBLE_PYTHON_BIN": sys.executable, "PYTHONUSERBASE": str(tmp_path / "userbase"),
		"HOME": str(tmp_path), "PIP_NO_INDEX": "1", "GITHUB_ENV": str(github_env),
	})
	assert result.returncode == 0, result.stderr
	assert not marker.exists()
	assert "SEMBLE_AVAILABLE=false" in github_env.read_text(encoding="utf-8")


def test_semble_builder_never_imports_pr_modules(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "semble" / "__init__.py", marker)
	_poison_module(pr_tree / "bm25s.py", marker)
	github_env = tmp_path / "github.env"
	result = _run("build_semble_wrapper.sh", pr_tree, {
		"SEMBLE_PYTHON_BIN": sys.executable, "GITHUB_WORKSPACE": str(pr_tree),
		"SEMBLE_INDEX_PATH": str(tmp_path / "index"), "GITHUB_ENV": str(github_env),
		"PYTHONNOUSERSITE": "1",
	})
	assert result.returncode == 0, result.stderr
	assert not marker.exists()
	assert "SEMBLE_INDEX_AVAILABLE=false" in github_env.read_text(encoding="utf-8")


def test_serena_config_clear_and_uv_ignore_pr_tree(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "re.py", marker)
	(pr_tree / "uv.toml").write_text('[tool]\n', encoding="utf-8")
	uv_log = tmp_path / "uv.log"
	uv_bin = tmp_path / "bin"
	uv_bin.mkdir()
	uv_stub = uv_bin / "uv"
	uv_stub.write_text(
		'#!/usr/bin/env bash\nprintf "%s|%s|%s\\n" "$PWD" "${UV_NO_CONFIG:-}" "$*" >> "$UV_LOG"\nexit 1\n',
		encoding="utf-8",
	)
	uv_stub.chmod(0o755)
	github_env = tmp_path / "github.env"
	base_env = {
		"HOME": str(tmp_path / "home"), "GITHUB_WORKSPACE": str(pr_tree),
		"GITHUB_ENV": str(github_env), "SERENA_UV_PYTHON_BIN": sys.executable,
		"UV_LOG": str(uv_log), "PATH": f"{uv_bin}:{os.environ['PATH']}",
		# Invoke the stub through bash so this test also works on noexec temp mounts.
		"UV_STUB": str(uv_stub), "BASH_FUNC_uv%%": '() { bash "$UV_STUB" "$@"; }',
	}
	for enabled in ("false", "true"):
		result = _run("setup_serena.sh", pr_tree, {**base_env, "SERENA_ENABLED": enabled})
		assert result.returncode == 0, result.stderr
		assert not marker.exists()
	assert "SERENA_AVAILABLE=false" in github_env.read_text(encoding="utf-8")
	assert uv_log.exists(), result.stderr
	uv_calls = uv_log.read_text(encoding="utf-8").splitlines()
	assert any("tool install" in call for call in uv_calls)
	assert all(call.split("|", 2)[0] != str(pr_tree) and call.split("|", 2)[1] == "1" for call in uv_calls)


def test_diff_filter_never_imports_pr_fnmatch(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "fnmatch.py", marker)
	diff_path = tmp_path / "diff.txt"
	diff_path.write_text("diff --git a/readme.md b/readme.md\n", encoding="utf-8")
	result = _run("review_filter_uninteresting_files.sh", pr_tree, {},
		"--diff-file", str(diff_path), "--output-diff", str(tmp_path / "out.txt"),
		"--kept-paths-file", str(tmp_path / "kept.txt"),
		"--skipped-paths-file", str(tmp_path / "skipped.txt"),
		"--repo-root", str(pr_tree))
	assert result.returncode == 0, result.stderr
	assert not marker.exists()


def test_reviewer_usage_probe_never_imports_pr_json(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "json.py", marker)
	reviewer_script = (SCRIPTS / "review_run_reviewers.sh").read_text(encoding="utf-8")
	reviewer_function = "normalize_openrouter_usage() {" + reviewer_script.split("normalize_openrouter_usage() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONPATH", "PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env.update({"SUPPORT_SCRIPTS_DIR": str(SCRIPTS), "PYTHONDONTWRITEBYTECODE": "1"})
	result = subprocess.run(
		["bash", "-c", reviewer_function + 'normalize_openrouter_usage "$1" review probe model',
			"reviewer probe", str(tmp_path / "usage.log")],
		cwd=pr_tree, env=child_env, capture_output=True, text=True, timeout=15, check=False,
	)
	assert result.returncode == 0, result.stderr
	assert "INFO: openrouter usage phase=review call=probe model=model" in result.stdout
	assert not marker.exists()


def test_pre_review_python_invocations_are_safe_path_scoped() -> None:
	shared = (
		"review_collect_pr_metadata.sh", "memory_helpers.sh", "opencode_helpers.sh",
		"workspace_init.sh", "gh_helpers.sh", "transcript_archive.sh",
		"write_opencode_config.sh", "review_filter_uninteresting_files.sh",
		"review_agents_md_materiality.sh", "review_run_reviewers.sh",
		"review_apply_fixes.sh",
	)
	# Only interpreter option/inline invocations need the cwd removed from sys.path.
	for filename in shared:
		text = (SCRIPTS / filename).read_text(encoding="utf-8").replace("\\\n", "")
		for match in re.finditer(r"\bpython3\s+(?:-c|-m|-)\s", text):
			line = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
			assert "PYTHONSAFEPATH=1" in line or (filename == "transcript_archive.sh" and "PYTHONSAFEPATH=1" in text[match.start() - 120:match.start()]), (filename, line)

	installer = (SCRIPTS / "install_semble.sh").read_text(encoding="utf-8")
	builder = (SCRIPTS / "build_semble_wrapper.sh").read_text(encoding="utf-8")
	serena = (SCRIPTS / "setup_serena.sh").read_text(encoding="utf-8")
	assert "PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1" in installer
	assert "PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1" in builder
	assert "PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1" in serena
	assert installer.count("semble_python -m pip") == 2
	assert "semble_python - \"${repo_root}\"" in builder
	assert serena.count("serena_python - <<'PY'") == 2
	assert serena.count("serena_uv tool install") == 2
	assert "UV_NO_CONFIG=1 uv" in serena
	# The trusted absolute script is safe without a cwd move: its subprocess
	# must keep the project cwd for Serena's --project-from-cwd setting.
	probe = serena.split("probe_mcp_handshake()", 1)[1].split("\nmain()", 1)[0]
	assert '"${SERENA_UV_PYTHON_BIN}" "${SCRIPT_DIR}/mcp_handshake_probe.py"' in probe
	assert 'cd -- "${serena_neutral_dir}"' not in probe

	workflow = (ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	pre_review = workflow.split("      - name: Checkout repo", 1)[1].split("      - name: Run reviewer models", 1)[0]
	guard = pre_review.index("      - name: Require safe-path Python before PR-tree helpers")
	assert guard < pre_review.index("      - name: Initialize runtime workspace")
	assert "sys.flags.safe_path" in pre_review
	assert "PYTHONSAFEPATH=1 python3 -c" in pre_review
	for match in re.finditer(r"\bpython3\s+(?:-c|-m|-)\s", pre_review):
		line = pre_review[pre_review.rfind("\n", 0, match.start()) + 1:match.start()]
		assert "PYTHONSAFEPATH=1" in line, line
