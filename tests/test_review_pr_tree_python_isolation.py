"""PR-checkout Python probes must not execute checkout-controlled modules."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
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
	_poison_module(pr_tree / "openrouter_prompt_cache.py", marker)
	reviewer_script = (SCRIPTS / "review_run_reviewers.sh").read_text(encoding="utf-8")
	reviewer_function = "normalize_openrouter_usage() {" + reviewer_script.split("normalize_openrouter_usage() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONPATH", "PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env.update({"SUPPORT_SCRIPTS_DIR": str(SCRIPTS), "PYTHONPATH": str(pr_tree), "PYTHONDONTWRITEBYTECODE": "1"})
	result = subprocess.run(
		["bash", "-c", reviewer_function + 'normalize_openrouter_usage "$1" review probe model',
			"reviewer probe", str(tmp_path / "usage.log")],
		cwd=pr_tree, env=child_env, capture_output=True, text=True, timeout=15, check=False,
	)
	assert result.returncode == 0, result.stderr
	assert "INFO: openrouter usage phase=review call=probe model=model" in result.stdout
	assert not marker.exists()


def test_consolidator_probe_never_imports_pr_json(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "json.py", marker)
	consolidator_script = (SCRIPTS / "review_consolidate.sh").read_text(encoding="utf-8")
	consolidator_function = "first_linked_issue_number()\n{" + consolidator_script.split("first_linked_issue_number()\n{", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONPATH", "PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env["LINKED_ISSUES_JSON"] = '[{"number":123}]'
	result = subprocess.run(
		["bash", "-c", consolidator_function + "first_linked_issue_number"],
		cwd=pr_tree, env=child_env, capture_output=True, text=True, timeout=15, check=False,
	)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "123"
	assert not marker.exists()


def test_rejection_verifier_never_imports_pr_json(tmp_path: Path) -> None:
	pr_tree = tmp_path / "pr"
	pr_tree.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(pr_tree / "json.py", marker)
	result = _run("review_reject_verify.sh", pr_tree, {
		"RUNTIME_DIR": str(tmp_path), "PR_NUMBER": "123", "ROUND_NUMBER": "0",
	})
	assert result.returncode == 0, result.stderr
	assert not marker.exists()
	assert (pr_tree / ".ai/review_runtime/pr-123/round-1/verified_rejections.json").is_file()


def test_pre_review_python_invocations_are_safe_path_scoped() -> None:
	shared = (
		"build_static_context.sh", "review_collect_pr_metadata.sh", "memory_helpers.sh", "opencode_helpers.sh",
		"workspace_init.sh", "gh_helpers.sh", "transcript_archive.sh",
		"write_opencode_config.sh", "review_filter_uninteresting_files.sh",
		"review_agents_md_materiality.sh", "review_run_reviewers.sh",
		"review_apply_fixes.sh", "review_consolidate.sh", "review_reject_verify.sh",
		"codex_heartbeat.sh", "codex_stall_guard.sh", "review_commit_changes.sh",
	)
	# Only interpreter option/inline invocations need the cwd removed from sys.path.
	for filename in shared:
		text = (SCRIPTS / filename).read_text(encoding="utf-8").replace("\\\n", "")
		for match in re.finditer(r"\bpython3\s+(?:-c|-m|-)\s", text):
			line = text[text.rfind("\n", 0, match.start()) + 1:match.start()]
			assert "PYTHONSAFEPATH=1" in line or (filename == "transcript_archive.sh" and "PYTHONSAFEPATH=1" in text[match.start() - 120:match.start()]), (filename, line)
	sandbox = (SCRIPTS / "review_untrusted_sandbox.sh").read_text(encoding="utf-8")
	assert 'if ! PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 python3 - "${config}"' in sandbox
	reviewer = (SCRIPTS / "review_run_reviewers.sh").read_text(encoding="utf-8")
	assert '"${SUPPORT_ROOT_DIR:-}" != /* || "${SUPPORT_SCRIPTS_DIR:-}" != /*' in reviewer
	assert 'PYTHONPATH="${SUPPORT_ROOT_DIR:-.}' not in reviewer
	assert 'PYTHONPATH="${SUPPORT_SCRIPTS_DIR:-scripts}' not in reviewer
	assert '${PYTHONPATH:+:$PYTHONPATH}' not in reviewer
	editor = (SCRIPTS / "review_apply_fixes.sh").read_text(encoding="utf-8")
	assert editor.count('[[ "${SUPPORT_SCRIPTS_DIR:-}" == /* ]] || return 0') >= 2
	assert 'PYTHONPATH="${SUPPORT_SCRIPTS_DIR:-scripts}' not in editor

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
	# The server keeps the project cwd for --project-from-cwd, but only the
	# trusted probe needs safe-path; server scripts may import sibling modules.
	probe = serena.split("probe_mcp_handshake()", 1)[1].split("\nmain()", 1)[0]
	assert '"${SERENA_UV_PYTHON_BIN}" "${SCRIPT_DIR}/mcp_handshake_probe.py"' in probe
	assert "env -u PYTHONPATH PYTHONSAFEPATH=1" in probe
	assert '-- /usr/bin/env -u PYTHONSAFEPATH "${serena_bin}"' in probe
	assert 'cd -- "${serena_neutral_dir}"' not in probe

	workflow = (ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	pre_review = workflow.split("      - name: Checkout repo", 1)[1].split("      - name: Run reviewer models", 1)[0]
	guard = pre_review.index("      - name: Require safe-path Python before PR-tree helpers")
	assert guard < pre_review.index("      - name: Initialize runtime workspace")
	assert 'install -d -m 0700 "${RUNTIME_DIR}" "${PREVIOUS_REVIEWS_DIR}" "${RUNTIME_CONTEXT_DIR}"' in pre_review
	assert "sys.flags.safe_path" in pre_review
	assert "PYTHONSAFEPATH=1 python3 -c" in pre_review
	for match in re.finditer(r"\bpython3\s+(?:-c|-m|-)\s", pre_review):
		line = pre_review[pre_review.rfind("\n", 0, match.start()) + 1:match.start()]
		assert "PYTHONSAFEPATH=1" in line, line
	assert "PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 python3 -c 'from pathlib import Path; import sys, yaml;" in workflow


def test_partial_finalize_timeout_probe_never_imports_checkout_yaml(tmp_path: Path) -> None:
	checkout = tmp_path / "pr"
	checkout.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(checkout / "yaml.py", marker)
	trusted_modules = tmp_path / "trusted"
	trusted_modules.mkdir()
	(trusted_modules / "yaml.py").write_text(
		'def safe_load(_text):\n'
		'\treturn {"jobs": {"codex-agent": {"timeout-minutes": 240, "steps": '
		'[{"name": "Run Codex resolver, validate, stage, commit", "timeout-minutes": 170}]}}}\n',
		encoding="utf-8",
	)
	workflow_path = ROOT / ".github/workflows/review_autofix.yml"
	timeout_line = next(line for line in workflow_path.read_text(encoding="utf-8").splitlines()
		if "python3 -c 'from pathlib import Path; import sys, yaml;" in line)
	timeout_command = timeout_line.split("< <(", 1)[1].rsplit("); then", 1)[0]
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env.update({"PYTHONPATH": str(trusted_modules), "workflow_timeout_source_path": str(workflow_path), "PYTHONDONTWRITEBYTECODE": "1"})
	result = subprocess.run(["bash", "-c", timeout_command], cwd=checkout, env=child_env,
		capture_output=True, text=True, timeout=15, check=False)
	assert result.returncode == 0, result.stderr
	assert result.stdout.strip() == "240 170"
	assert not marker.exists()


def test_break_glass_scan_never_imports_checkout_json(tmp_path: Path) -> None:
	checkout = tmp_path / "pr"
	checkout.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(checkout / "json.py", marker)
	comments = tmp_path / "comments.json"
	comments.write_text('[{"user":{"type":"User","login":"owner"},"body":"@codex break-glass"}]', encoding="utf-8")
	github_env = tmp_path / "github.env"
	workflow = (ROOT / ".github/workflows/review_autofix.yml").read_text(encoding="utf-8")
	break_glass_step = workflow.split("      - name: Detect review-blocked break-glass override", 1)[1]
	break_glass_command = break_glass_step.split("          PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY' >> \"$GITHUB_ENV\"", 1)[1].split("\n          PY\n", 1)[0]
	break_glass_command = "          PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY' >> \"$GITHUB_ENV\"" + break_glass_command + "\n          PY\n"
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONPATH", "PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env.update({"GITHUB_ENV": str(github_env), "PR_ISSUE_COMMENTS_FILE": str(comments), "PYTHONDONTWRITEBYTECODE": "1"})
	result = subprocess.run(["bash", "-c", textwrap.dedent(break_glass_command)], cwd=checkout,
		env=child_env, capture_output=True, text=True, timeout=15, check=False)
	assert result.returncode == 0, result.stderr
	assert "REVIEW_BREAK_GLASS=true" in github_env.read_text(encoding="utf-8")
	assert not marker.exists()


def test_conflict_retry_never_imports_checkout_re(tmp_path: Path) -> None:
	checkout = tmp_path / "pr"
	checkout.mkdir()
	marker = tmp_path / "poisoned"
	_poison_module(checkout / "re.py", marker)
	prelude_dir = tmp_path / "preludes"
	prelude_dir.mkdir()
	(prelude_dir / "integration-sync-conflict-resolver-retry-prelude.txt").write_text("Previous: {{PREVIOUS_ATTEMPT_NUMBER}}\n", encoding="utf-8")
	original_prompt = tmp_path / "original.txt"
	original_prompt.write_text("Original prompt\n", encoding="utf-8")
	retry_prompt = tmp_path / "retry.txt"
	source = (SCRIPTS / "review_conflict_resolve.sh").read_text(encoding="utf-8")
	function_start = source.index("_build_retry_prompt() {")
	retry_function = source[function_start:source.index("\n}\n", function_start) + 2]
	child_env = os.environ.copy()
	for inherited_name in ("PYTHONPATH", "PYTHONSAFEPATH", "BASH_ENV", "ENV", "WORKSPACE_PATH"):
		child_env.pop(inherited_name, None)
	child_env.update({"SUPPORT_PROMPTS_DIR": str(prelude_dir), "IS_INTEGRATION_SYNC": "true",
		"CONFLICT_RESOLVER_PROMPT_FILE": str(original_prompt), "RESOLVER_RETRY_PROMPT_FILE": str(retry_prompt)})
	result = subprocess.run(["bash", "-c", retry_function + "\n_build_retry_prompt 1 /dev/null /dev/null validation"],
		cwd=checkout, env=child_env, capture_output=True, text=True, timeout=15, check=False)
	assert result.returncode == 0, result.stderr
	assert retry_prompt.read_text(encoding="utf-8") == "Previous: 1\nOriginal prompt\n"
	assert not marker.exists()


def test_conflict_prepare_uses_safe_path() -> None:
	prepare = (SCRIPTS / "review_conflict_prepare.sh").read_text(encoding="utf-8")
	assert 'PYTHONSAFEPATH=1 python3 -c "import os,sys; tpl=' in prepare
