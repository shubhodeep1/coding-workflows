#!/usr/bin/env python3
"""Contract checks for Semble and Serena wiring in the validate workflow."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import contextlib
from pathlib import Path
from tempfile import TemporaryDirectory


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
STAGE_WORKFLOW_SUPPORT = REPO_ROOT / "scripts" / "stage_workflow_support.sh"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"
SELF_HEAL_SCRIPT = REPO_ROOT / "scripts" / "self_heal_validation.sh"
VALIDATE_PROMPTS = [
	REPO_ROOT / "prompts" / "mode-validate-discover.txt",
	REPO_ROOT / "prompts" / "mode-validate-diagnose.txt",
	REPO_ROOT / "prompts" / "mode-validate-fix-harness.txt",
	REPO_ROOT / "prompts" / "mode-validate-self-heal.txt",
]
# The validate-mode prompts the support manifest stages into the isolated
# per-project validate workspace via `required_prompts`. render_prompt.py
# hydrates their {{REFERENCE_*}} placeholders from prompts/references/*.txt,
# so those reference files must be staged alongside them or render aborts.
STAGED_REQUIRED_VALIDATE_PROMPTS = (
	"prompts/mode-validate-generate.txt",
	"prompts/mode-validate-diagnose.txt",
	"prompts/mode-validate-discover.txt",
	"prompts/mode-validate-fix-harness.txt",
)
OPTIONAL_PRESERVED_VALIDATE_PROMPTS = (
	"prompts/mode-validate-self-heal.txt",
	"prompts/mode-validate-self-heal-continuation.txt",
)
REQUIRED_VALIDATE_ASSEMBLY_ASSETS = (
	"prompts/_prelude_common.txt",
	"prompts/_prelude_common_large.txt",
	"prompts/_prelude_common_xl.txt",
	"prompts/_prelude_role_persona.txt",
	"prompts/_prelude_serena.txt",
	"prompts/_prelude_output_contract.txt",
	"prompts/_templates/mode-validate-generate.txt",
	"prompts/_templates/mode-validate-diagnose.txt",
	"prompts/_templates/mode-validate-discover.txt",
	"prompts/_templates/mode-validate-fix-harness.txt",
)
OPTIONAL_VALIDATE_ASSEMBLY_ASSETS = (
	"prompts/_templates/mode-validate-self-heal.txt",
	"prompts/_templates/mode-validate-self-heal-continuation.txt",
)
STAGED_VALIDATE_WORKSPACE_PROMPTS = STAGED_REQUIRED_VALIDATE_PROMPTS + OPTIONAL_PRESERVED_VALIDATE_PROMPTS
RENDER_PROMPT_MODULE_NAME = "_validate_semble_render_prompt"
REFERENCE_PATH_RE = re.compile(r"(?P<path>[^\s'\"]*/prompts/references/[^\s:'\"]+\.txt)")


def _read(path: Path) -> str:
	return path.read_text(encoding="utf-8")


def _workflow_text() -> str:
	return _read(VALIDATE_WORKFLOW)


def _stage_support_text() -> str:
	return _read(STAGE_WORKFLOW_SUPPORT)


def _validate_process_text() -> str:
	return _read(VALIDATE_PROCESS)


def _self_heal_text() -> str:
	return _read(SELF_HEAL_SCRIPT)


def _import_render_prompt(script_path: Path = SCRIPTS_DIR / "render_prompt.py", module_name: str = RENDER_PROMPT_MODULE_NAME):
	if module_name in sys.modules:
		return sys.modules[module_name]
	spec = importlib.util.spec_from_file_location(module_name, script_path)
	if spec is None or spec.loader is None:
		raise AssertionError(f"unable to load render_prompt.py from {script_path}")
	render_prompt = importlib.util.module_from_spec(spec)
	sys.modules[module_name] = render_prompt
	try:
		spec.loader.exec_module(render_prompt)
	except Exception:
		sys.modules.pop(module_name, None)
		raise
	return render_prompt


def _manifest_string_array(key: str) -> list[str]:
	workflow_text = _workflow_text()
	key = json.dumps(key)
	key_index = workflow_text.find(key)
	if key_index == -1:
		raise AssertionError(f"validate.yml support manifest is missing a {json.loads(key)} array")
	field_start = workflow_text.find(":", key_index + len(key))
	array_start = workflow_text.find("[", field_start)
	if field_start == -1 or array_start == -1:
		raise AssertionError(f"validate.yml support manifest is missing a {json.loads(key)} array")
	try:
		values, _ = json.JSONDecoder().raw_decode(workflow_text[array_start:])
	except json.JSONDecodeError as exc:
		raise AssertionError(f"validate.yml support manifest contains an invalid {json.loads(key)} array") from exc
	if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
		raise AssertionError(f"validate.yml support manifest {json.loads(key)} must be a string array")
	return values


def _manifest_required_prompts() -> list[str]:
	return _manifest_string_array("required_prompts")


def _missing_reference_path_from_error(error_text: str) -> Path | None:
	match = REFERENCE_PATH_RE.search(error_text)
	if match is None:
		return None
	return Path(match.group("path"))


def _expected_validate_reference_files() -> set[str]:
	# Discover dependencies by exercising render_prompt.py's real reference
	# hydration path in an isolated workspace, mirroring validate's runtime.
	expected: set[str] = set()
	with TemporaryDirectory() as tmpdir:
		workspace_root = Path(tmpdir)
		render_prompt_path = workspace_root / "scripts" / "render_prompt.py"
		render_prompt_path.parent.mkdir(parents=True, exist_ok=True)
		render_prompt_path.write_text((SCRIPTS_DIR / "render_prompt.py").read_text(encoding="utf-8"), encoding="utf-8")
		isolated_module_name = f"{RENDER_PROMPT_MODULE_NAME}_isolated"
		try:
			render_prompt = _import_render_prompt(render_prompt_path, isolated_module_name)
			with contextlib.chdir(workspace_root):
				for rel in STAGED_VALIDATE_WORKSPACE_PROMPTS:
					prompt_path = workspace_root / rel
					prompt_path.parent.mkdir(parents=True, exist_ok=True)
					prompt_path.write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
					prompt_text = render_prompt.load_prompt(prompt_path)
					mode_name = render_prompt.resolve_mode_name(prompt_path, None)
					hydrated_values: dict[str, str] = {}
					while True:
						try:
							hydrated_values = render_prompt.hydrate_reference_placeholders(
								prompt_text=prompt_text,
								prompt_path=prompt_path,
								mode_name=mode_name,
								values=hydrated_values,
							)
							break
						except render_prompt.PromptLoadError as exc:
							missing_path = _missing_reference_path_from_error(str(exc))
							if missing_path is None:
								raise AssertionError(f"unexpected hydration failure for {rel}: {exc}") from exc
							expected.add(missing_path.name)
							missing_path.parent.mkdir(parents=True, exist_ok=True)
							missing_path.write_text(f"{missing_path.name}\n", encoding="utf-8")
		finally:
			sys.modules.pop(isolated_module_name, None)
	return expected


def test_validate_manifest_stages_reference_dependencies() -> None:
	# Regression guard for the infinite redispatch loop: the validate job runs
	# in an isolated workspace with no .codex-workflow-src checkout, so every
	# prompts/references/*.txt file a staged validate prompt depends on must be
	# listed in the manifest's required_prompts. If one is missing,
	# render_prompt.py aborts ("Reference file ... not found"), the validate run
	# exits before applying a result label, and the orchestrate poller keeps
	# redispatching the same cycle forever.
	required_prompts = _manifest_required_prompts()
	expected_references = _expected_validate_reference_files()
	assert expected_references, "expected at least one validate reference dependency"
	assert "validate-output-contract.txt" in expected_references, (
		"mode-validate-generate should require the validate-output-contract append reference"
	)
	for file_name in sorted(expected_references):
		manifest_entry = f"prompts/references/{file_name}"
		assert manifest_entry in required_prompts, (
			f"validate manifest required_prompts is missing {manifest_entry}; "
			"staged validate prompts depend on it at render time"
		)
		assert (REPO_ROOT / "prompts" / "references" / file_name).is_file(), (
			f"prompts/references/{file_name} is staged by the manifest but does not exist on disk"
		)


def test_validate_manifest_stages_prompt_assembly_assets() -> None:
	required_prompts = _manifest_required_prompts()
	optional_preserve = _manifest_string_array("optional_preserve_files_after_prompts")
	for repo_path in REQUIRED_VALIDATE_ASSEMBLY_ASSETS:
		assert repo_path in required_prompts, repo_path
		assert (REPO_ROOT / repo_path).is_file(), repo_path
	for repo_path in OPTIONAL_VALIDATE_ASSEMBLY_ASSETS:
		assert repo_path in optional_preserve, repo_path
		assert (REPO_ROOT / repo_path).is_file(), repo_path


def test_validate_prompts_include_serena_placeholder() -> None:
	for prompt_path in VALIDATE_PROMPTS:
		assert "{{SERENA_TOOL_HINTS}}" in _read(prompt_path), prompt_path


def test_validate_workflow_lists_semble_support_files_in_helper_manifest() -> None:
	wf = _workflow_text()
	required_snippets = [
		'helper_path="scripts/stage_workflow_support.sh"',
		'bash "${helper_path}" validate --manifest "${manifest_path}"',
		"scripts/install_semble.sh",
		"scripts/semble_helpers.sh",
		"scripts/build_semble_wrapper.sh",
	]
	for snippet in required_snippets:
		assert snippet in wf, f"validate.yml missing snippet: {snippet}"


def test_validate_workflow_lists_serena_support_files_in_helper_manifest() -> None:
	wf = _workflow_text()
	required_snippets = [
		"SERENA_ENABLED: ${{ vars.SERENA_ENABLED || 'false' }}",
		'- name: Initialize Serena runtime state',
		'echo "SERENA_AVAILABLE=false"',
		'echo "SERENA_PROJECT_PREEXISTED=${serena_project_preexisted}"',
		'echo "SERENA_PROJECT_BOOTSTRAP_HASH="',
		"scripts/setup_serena.sh",
		"scripts/serena_stats_emit.py",
		"scripts/mcp_handshake_probe.py",
		"scripts/templates/serena_project.yml.j2",
	]
	for snippet in required_snippets:
		assert snippet in wf
	assert "Consumer repo already tracks ${repo_path}; preserving caller-owned Serena template." in _stage_support_text()


def test_validate_workflow_bootstraps_and_exports_semble_state() -> None:
	wf = _workflow_text()
	assert "- name: Determine Semble bootstrap state" in wf
	assert "VALIDATION_USE_SEMBLE: ${{ vars.VALIDATION_USE_SEMBLE || 'true' }}" in wf
	assert "SERENA_ENABLED: ${{ env.SERENA_ENABLED }}" in wf
	assert 'echo "SEMBLE_AVAILABLE=false"' in wf
	assert 'echo "SEMBLE_INDEX_AVAILABLE=false"' in wf
	assert 'echo "SEMBLE_INDEX_PATH=${index_path}"' in wf
	assert 'echo "serena_enabled=${serena_enabled}"' in wf
	assert 'echo "bootstrap_enabled=${bootstrap_enabled}"' in wf
	assert "- name: setup-uv\n        if: steps.semble_gate.outputs.bootstrap_enabled == 'true'" in wf
	assert "uses: astral-sh/setup-uv@v7" in wf
	assert "- name: Install semble\n        if: steps.semble_gate.outputs.bootstrap_enabled == 'true'" in wf
	assert 'echo "::notice::VALIDATION_USE_SEMBLE is not true; skipping Semble install."' in wf
	assert "bash scripts/install_semble.sh" in wf
	assert "- name: Build semble index" in _workflow_text()
	# Inline BM25 wrapper extracted to scripts/build_semble_wrapper.sh; the
	# in-workflow body now delegates to that script via a one-liner.
	assert "bash scripts/build_semble_wrapper.sh" in wf
	assert 'SEMBLE_INDEX_PATH="${RUNTIME_DIR}/.semble-index" \\' in wf
	assert 'SEMBLE_WRAPPER_DIR="${RUNTIME_DIR}/semble/bin" \\' in wf
	assert "- name: Emit Serena stats" in wf
	assert 'serena_stat_args=(--target validate)' in wf
	assert "validate_discover.log' -o -name 'validate_diagnose.log' -o -name 'validate_self_heal.log'" in wf
	assert wf.index("- name: Emit Serena stats") < wf.index("- name: Upload validation artifacts")


def test_shared_wrapper_script_owns_bm25_implementation() -> None:
	# Assertions that previously inspected validate.yml's inline wrapper now
	# inspect the shared script. Keeping them so a regression in either
	# extraction or future renames is caught at test time.
	wrapper = (REPO_ROOT / "scripts" / "build_semble_wrapper.sh").read_text(encoding="utf-8")
	assert "MAX_INDEX_FILES = 5000" in wrapper
	assert "Semble indexing skipped after {MAX_INDEX_FILES} files" in wrapper
	assert 'write_env_kv "SEMBLE_AVAILABLE" "true"' in wrapper
	assert 'write_env_kv "SEMBLE_INDEX_AVAILABLE" "true"' in wrapper
	assert 'write_env_kv "SEMBLE_BIN"' in wrapper
	assert "def _default_index_path() -> Path:" in wrapper
	assert 'Path(os.environ.get("SEMBLE_INDEX_PATH", str(_default_index_path())))' in wrapper
	assert "payload.get('version', 'unknown')" in wrapper
	assert 'print("semble 0.1.3")' not in wrapper


def test_validate_process_includes_serena_bootstrap_and_prompt_hooks() -> None:
	text = _validate_process_text()
	assert 'SERENA_ENABLED="${SERENA_ENABLED:-false}"' in text
	assert 'SERENA_AVAILABLE="${SERENA_AVAILABLE:-false}"' in text
	assert 'SERENA_BOOTSTRAP_ATTEMPTED="${SERENA_BOOTSTRAP_ATTEMPTED:-false}"' in text
	assert 'SERENA_PROJECT_PREEXISTED="${SERENA_PROJECT_PREEXISTED:-}"' in text
	assert 'SERENA_PROJECT_BOOTSTRAP_HASH="${SERENA_PROJECT_BOOTSTRAP_HASH:-}"' in text
	assert 'write_github_env_value()' in text
	assert 'env_is_truthy()' in text
	assert 'detect_serena_project_preexisting()' in text
	assert 'clear_stale_serena_codex_config()' in text
	assert 'build_validate_serena_tool_hints()' in text
	assert 'emit_serena_fallback()' in text
	assert 'ensure_serena_bootstrap()' in text
	assert 'if ! env_is_truthy "${SERENA_ENABLED:-false}"; then\n    emit_serena_fallback "${serena_phase}" "disabled"\n    clear_stale_serena_codex_config' in text
	assert 'echo "::notice::scripts/setup_serena.sh is unavailable; validation will continue without Serena."\n    emit_serena_fallback "${serena_phase}" "setup-failure"\n    clear_stale_serena_codex_config' in text
	assert 'SERENA_FALLBACK_TARGET="validate" SERENA_FALLBACK_PHASE="${serena_phase}" GITHUB_ENV="${bootstrap_env_file}" bash scripts/setup_serena.sh' in text
	assert 'echo "::warning::scripts/setup_serena.sh exited non-zero; validation will continue without Serena."\n    emit_serena_fallback "${serena_phase}" "setup-failure"\n    clear_stale_serena_codex_config' in text
	assert 'DISCOVER_SERENA_TOOL_HINTS="$(build_validate_serena_tool_hints "discover" || true)"' in text
	assert 'SERENA_TOOL_HINTS="${DISCOVER_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh prompts/mode-validate-discover.txt' in text
	assert 'DIAGNOSE_SERENA_TOOL_HINTS="$(build_validate_serena_tool_hints "diagnose" || true)"' in text
	assert 'SERENA_TOOL_HINTS="${DIAGNOSE_SERENA_TOOL_HINTS}" bash scripts/render_prompt.sh prompts/mode-validate-diagnose.txt' in text
	assert 'ensure_serena_bootstrap "${phase}"' in text
	assert 'ensure_serena_bootstrap "discover"' in text
	assert 'ensure_serena_bootstrap "diagnose"' in text
	assert 'filter_runtime_status_noise()' in text
	assert "*' .serena/'*|*' .serena')" in text
	assert 'current_hash="$(sha256sum .serena/project.yml' in text
	assert "git status --porcelain --untracked-files=all -- . ':!validation/**' | filter_runtime_status_noise | sort > \"${PRE_GENERATE_STATUS_FILE}\"" in text
	assert "git status --porcelain --untracked-files=all -- . ':!validation/**' | filter_runtime_status_noise | sort > \"${POST_GENERATE_STATUS_FILE}\"" in text


def test_validate_process_includes_discover_and_diagnose_semble_hooks() -> None:
	text = _validate_process_text()
	assert 'if source scripts/semble_helpers.sh; then' in text
	assert 'build_validate_discover_semble_query()' in text
	assert 'build_validate_diagnose_semble_query()' in text
	assert 'append_validate_semble_context()' in text
	assert 'discover_semble_query="$(build_validate_discover_semble_query || true)"' in text
	assert 'append_validate_semble_context "${discover_semble_query}" "${VALIDATE_DISCOVER_SEMBLE_MAX_CHUNKS}" "Validate Discover Context"' in text
	assert 'diagnose_semble_query="$(build_validate_diagnose_semble_query || true)"' in text
	assert 'append_validate_semble_context "${diagnose_semble_query}" "${VALIDATE_DIAGNOSE_SEMBLE_MAX_CHUNKS}" "Validate Diagnose Context"' in text


def test_self_heal_includes_semble_and_serena_prompt_hooks() -> None:
	text = _self_heal_text()
	assert 'if source scripts/semble_helpers.sh; then' in text
	assert 'SELF_HEAL_SEMBLE_MAX_CHUNKS="${SELF_HEAL_SEMBLE_MAX_CHUNKS:-3}"' in text
	assert 'build_self_heal_semble_query()' in text
	assert 'build_self_heal_serena_tool_hints()' in text
	assert 'append_self_heal_semble_context()' in text
	assert 'if semble_query_block "${query_text}" "${SELF_HEAL_SEMBLE_MAX_CHUNKS}" "Validate Self-Heal Context"; then' in text
	assert 'self_heal_semble_query="$(build_self_heal_semble_query || true)"' in text
	assert 'self_heal_serena_tool_hints="$(build_self_heal_serena_tool_hints || true)"' in text
	assert 'SERENA_TOOL_HINTS="${self_heal_serena_tool_hints}" bash scripts/render_prompt.sh prompts/mode-validate-self-heal.txt' in text
	assert 'CURRENT VALIDATION PROMPT FILES (raw on-disk contents with any prior self-heal patches already applied)' in text
	assert 'cat "prompts/${_target}"' in text
	assert 'bash scripts/render_prompt.sh "prompts/${_target}"' not in text
	assert 'append_self_heal_semble_context "${self_heal_semble_query}"' in text


def main() -> int:
	failures: list[str] = []
	for test_fn in (
		test_validate_manifest_stages_reference_dependencies,
		test_validate_manifest_stages_prompt_assembly_assets,
		test_validate_prompts_include_serena_placeholder,
		test_validate_workflow_lists_semble_support_files_in_helper_manifest,
		test_validate_workflow_lists_serena_support_files_in_helper_manifest,
		test_validate_workflow_bootstraps_and_exports_semble_state,
		test_shared_wrapper_script_owns_bm25_implementation,
		test_validate_process_includes_serena_bootstrap_and_prompt_hooks,
		test_validate_process_includes_discover_and_diagnose_semble_hooks,
		test_self_heal_includes_semble_and_serena_prompt_hooks,
	):
		try:
			test_fn()
		except Exception as exc:
			failures.append(f"{test_fn.__name__}: {exc}")
	for failure in failures:
		print(f"::error::{failure}", file=sys.stderr)
	return 1 if failures else 0


if __name__ == "__main__":
	raise SystemExit(main())
