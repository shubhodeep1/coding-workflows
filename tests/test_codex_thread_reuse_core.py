#!/usr/bin/env python3
"""Focused contract tests for scripts/codex_thread_reuse.sh wiring."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "codex_thread_reuse.sh"
RENDER_PROMPT_PY = REPO_ROOT / "scripts" / "render_prompt.py"
RENDER_PROMPT_SH = REPO_ROOT / "scripts" / "render_prompt.sh"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "implement.yml"
VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
VALIDATE_PROCESS = REPO_ROOT / "scripts" / "validate_process.sh"


def _base_env() -> dict[str, str]:
	env = os.environ.copy()
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	return env


def _write_fake_codex(tmp_path: Path) -> Path:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(parents=True, exist_ok=True)
	fake_codex = bin_dir / "codex"
	fake_codex.write_text(
		"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def parse_exec(args: list[str]) -> tuple[str, list[str]]:
    if "exec" not in args:
        return "", []
    exec_index = args.index("exec")
    rest = args[exec_index + 1 :]
    if rest and rest[0] == "resume":
        return "resume", rest[1:]
    return "exec", rest


args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.114.0")
    raise SystemExit(0)

if args == ["exec", "resume", "--help"]:
    if os.environ.get("FAKE_CODEX_SUPPORT_RESUME", "true") == "true":
        print("Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]")
        raise SystemExit(0)
    print("resume not supported", file=sys.stderr)
    raise SystemExit(2)

mode, rest = parse_exec(args)
if not mode:
    raise SystemExit(0)

stdin_text = sys.stdin.read()
log_path = Path(os.environ["FAKE_CODEX_LOG"])
counter_path = Path(os.environ["FAKE_CODEX_COUNTER"])
counter = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
counter += 1
counter_path.write_text(str(counter), encoding="utf-8")

session_id = ""
if mode == "resume" and len(rest) >= 2 and rest[-1] == "-":
    session_id = rest[-2]

record = {"mode": mode, "args": args, "stdin": stdin_text, "session_id": session_id}
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")

if mode == "resume" and os.environ.get("FAKE_CODEX_FAIL_RESUME", "false") == "true":
    print("resume failed", file=sys.stderr)
    raise SystemExit(42)

if mode == "exec" and os.environ.get("FAKE_CODEX_FAIL_EXEC", "false") == "true":
    print("exec failed", file=sys.stderr)
    raise SystemExit(43)

session_root = Path(os.environ["CODEX_THREAD_REUSE_SESSION_ROOT"])
session_root.mkdir(parents=True, exist_ok=True)
new_session_id = session_id or f"session-{counter}"
session_file = session_root / f"session-{counter}.jsonl"
payload = {"type": "session_meta", "payload": {"id": new_session_id, "cwd": os.getcwd()}}
session_file.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
print(f"mode={mode} session_id={new_session_id}")
""",
		encoding="utf-8",
	)
	fake_codex.chmod(0o755)
	return fake_codex


def _helper_env(tmp_path: Path, *, support_resume: bool = True) -> dict[str, str]:
	_write_fake_codex(tmp_path)
	runtime_dir = tmp_path / "runtime"
	session_root = tmp_path / "sessions"
	log_file = tmp_path / "fake_codex_log.jsonl"
	env = _base_env()
	env.update(
		{
			"PATH": f"{tmp_path / 'bin'}:{env.get('PATH', '')}",
			"CODEX_THREAD_REUSE_RUNTIME_DIR": str(runtime_dir),
			"CODEX_THREAD_REUSE_SESSION_ROOT": str(session_root),
			"FAKE_CODEX_LOG": str(log_file),
			"FAKE_CODEX_COUNTER": str(tmp_path / "fake_codex_counter.txt"),
			"FAKE_CODEX_SUPPORT_RESUME": "true" if support_resume else "false",
			"CODEX_THREAD_REUSE_ENABLED": "true",
		}
	)
	runtime_dir.mkdir(parents=True, exist_ok=True)
	session_root.mkdir(parents=True, exist_ok=True)
	return env


def _read_fake_codex_log(env: dict[str, str]) -> list[dict[str, object]]:
	log_path = Path(env["FAKE_CODEX_LOG"])
	if not log_path.exists():
		return []
	return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_helper(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["bash", str(HELPER), *args],
		cwd=str(REPO_ROOT),
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def _run_direct(
	env: dict[str, str],
	*,
	state_key: str,
	prompt_text: str,
	phase: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
	runtime_dir = Path(env["CODEX_THREAD_REUSE_RUNTIME_DIR"])
	prompt_file = runtime_dir / f"{state_key}.prompt.txt"
	output_file = runtime_dir / f"{state_key}.output.txt"
	prompt_file.write_text(prompt_text, encoding="utf-8")
	cmd_env = env.copy()
	cmd_env.update(
		{
			"CODEX_THREAD_REUSE_STATE_KEY": state_key,
			"CODEX_THREAD_REUSE_PROMPT_FILE": str(prompt_file),
			"CODEX_THREAD_REUSE_OUTPUT_FILE": str(output_file),
			"CODEX_THREAD_REUSE_PHASE": phase,
			"CODEX_THREAD_REUSE_MODEL": "openai/gpt-5.4",
		}
	)
	proc = _run_helper(["direct-run"], env=cmd_env)
	output_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
	return proc, output_text


def _load_render_prompt_module():
	spec = importlib.util.spec_from_file_location("render_prompt_module", RENDER_PROMPT_PY)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def test_probe_supported_and_unsupported() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_probe_") as td:
		tmp_path = Path(td)
		supported_env = _helper_env(tmp_path / "supported", support_resume=True)
		supported = _run_helper(["probe-supported"], env=supported_env)
		assert supported.returncode == 0, supported.stderr
		assert "supported=true" in (Path(supported_env["CODEX_THREAD_REUSE_RUNTIME_DIR"]) / "codex-thread-reuse" / "probe.env").read_text(encoding="utf-8")

		unsupported_env = _helper_env(tmp_path / "unsupported", support_resume=False)
		unsupported = _run_helper(["probe-supported"], env=unsupported_env)
		assert unsupported.returncode != 0
		assert "supported=false" in (Path(unsupported_env["CODEX_THREAD_REUSE_RUNTIME_DIR"]) / "codex-thread-reuse" / "probe.env").read_text(encoding="utf-8")


def test_extract_session_id_uses_session_meta_payload_id() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_extract_") as td:
		tmp_path = Path(td)
		session_file = tmp_path / "session.jsonl"
		session_file.write_text(
			'{"type":"other","payload":{}}\n'
			'{"type":"session_meta","payload":{"id":"session-123","cwd":"/tmp/example"}}\n',
			encoding="utf-8",
		)
		proc = _run_helper(["extract-session-id", str(session_file)], env=_base_env())
		assert proc.returncode == 0, proc.stderr
		assert proc.stdout.strip() == "session-123"


def test_begin_capture_uses_portable_marker_name() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_marker_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		proc = _run_helper(["begin-capture", "portable"], env=env)
		assert proc.returncode == 0, proc.stderr
		marker_path = Path(proc.stdout.strip())
		assert marker_path.name.startswith("portable.")
		assert "%N" not in marker_path.name
		assert marker_path.read_text(encoding="utf-8").strip().isdigit()


def test_runtime_root_scopes_fallback_per_shell_session() -> None:
	base_env = _base_env()
	same_shell = subprocess.run(
		[
			"bash",
			"-lc",
			f'source "{HELPER}" && unset CODEX_THREAD_REUSE_RUNTIME_DIR RUNTIME_DIR && '\
			'printf "%s\\n%s\\n" "$(codex_thread_reuse_runtime_root)" "$(codex_thread_reuse_runtime_root)"',
		],
		cwd=str(REPO_ROOT),
		env=base_env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert same_shell.returncode == 0, same_shell.stderr
	first, second = [line for line in same_shell.stdout.splitlines() if line.strip()]
	assert first == second
	assert "codex-thread-reuse-default" not in first

	first_shell = subprocess.run(
		[
			"bash",
			"-lc",
			f'source "{HELPER}" && unset CODEX_THREAD_REUSE_RUNTIME_DIR RUNTIME_DIR && codex_thread_reuse_runtime_root',
		],
		cwd=str(REPO_ROOT),
		env=base_env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert first_shell.returncode == 0, first_shell.stderr
	second_shell = subprocess.run(
		[
			"bash",
			"-lc",
			f'source "{HELPER}" && unset CODEX_THREAD_REUSE_RUNTIME_DIR RUNTIME_DIR && codex_thread_reuse_runtime_root',
		],
		cwd=str(REPO_ROOT),
		env=base_env,
		capture_output=True,
		text=True,
		check=False,
	)
	assert second_shell.returncode == 0, second_shell.stderr
	assert first_shell.stdout.strip() != second_shell.stdout.strip()


def test_record_session_skips_null_cwd_metadata_without_crashing() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_null_cwd_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		marker = _run_helper(["begin-capture", "null-cwd"], env=env)
		assert marker.returncode == 0, marker.stderr
		session_root = Path(env["CODEX_THREAD_REUSE_SESSION_ROOT"])
		(session_root / "null-cwd.jsonl").write_text(
			json.dumps({"type": "session_meta", "payload": {"id": "session-null", "cwd": None}}) + "\n",
			encoding="utf-8",
		)
		proc = _run_helper(["record-session", "null-cwd", marker.stdout.strip()], env=env)
		assert proc.returncode != 0
		assert "TypeError" not in proc.stderr


def test_direct_run_disabled_does_not_store_session_state_or_markers() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_disabled_clean_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		env["CODEX_THREAD_REUSE_ENABLED"] = "false"
		proc, _ = _run_direct(env, state_key="disabled-clean", prompt_text="prompt\n", phase="implement")
		assert proc.returncode == 0, proc.stderr
		session_get = _run_helper(["session-get", "disabled-clean"], env=env)
		assert session_get.returncode != 0
		markers_dir = Path(env["CODEX_THREAD_REUSE_RUNTIME_DIR"]) / "codex-thread-reuse" / "markers"
		assert not markers_dir.exists() or not any(markers_dir.iterdir())


def test_direct_run_uses_exec_when_feature_disabled_even_with_saved_session() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_disabled_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		first_proc, _ = _run_direct(env, state_key="disabled", prompt_text="first prompt\n", phase="validate_discover")
		assert first_proc.returncode == 0, first_proc.stderr

		disabled_env = env.copy()
		disabled_env["CODEX_THREAD_REUSE_ENABLED"] = "false"
		second_proc, _ = _run_direct(disabled_env, state_key="disabled", prompt_text="second prompt\n", phase="validate_discover")
		assert second_proc.returncode == 0, second_proc.stderr

		modes = [entry["mode"] for entry in _read_fake_codex_log(env)]
		assert modes == ["exec", "exec"]


def test_direct_run_stores_session_then_resumes_on_later_attempt() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_resume_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		first_proc, _ = _run_direct(env, state_key="resume", prompt_text="first prompt\n", phase="implement")
		assert first_proc.returncode == 0, first_proc.stderr

		session_get = _run_helper(["session-get", "resume"], env=env)
		assert session_get.returncode == 0
		assert session_get.stdout.strip() == "session-1"

		second_proc, _ = _run_direct(env, state_key="resume", prompt_text="second prompt\n", phase="implement")
		assert second_proc.returncode == 0, second_proc.stderr

		modes = [entry["mode"] for entry in _read_fake_codex_log(env)]
		assert modes == ["exec", "resume"]


def test_resume_failure_falls_back_to_exec() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_fallback_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		first_proc, _ = _run_direct(env, state_key="fallback", prompt_text="first prompt\n", phase="implement")
		assert first_proc.returncode == 0, first_proc.stderr

		resume_fail_env = env.copy()
		resume_fail_env["FAKE_CODEX_FAIL_RESUME"] = "true"
		second_proc, _ = _run_direct(resume_fail_env, state_key="fallback", prompt_text="second prompt\n", phase="implement")
		assert second_proc.returncode == 0, second_proc.stderr
		session_get = _run_helper(["session-get", "fallback"], env=env)
		assert session_get.returncode == 0
		assert session_get.stdout.strip() == "session-3"

		modes = [entry["mode"] for entry in _read_fake_codex_log(env)]
		assert modes == ["exec", "resume", "exec"]


def test_resume_and_fallback_exec_failure_clears_saved_session() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_clear_stale_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		first_proc, _ = _run_direct(env, state_key="stale", prompt_text="first prompt\n", phase="implement")
		assert first_proc.returncode == 0, first_proc.stderr

		failed_env = env.copy()
		failed_env["FAKE_CODEX_FAIL_RESUME"] = "true"
		failed_env["FAKE_CODEX_FAIL_EXEC"] = "true"
		second_proc, _ = _run_direct(failed_env, state_key="stale", prompt_text="second prompt\n", phase="implement")
		assert second_proc.returncode != 0

		session_get = _run_helper(["session-get", "stale"], env=env)
		assert session_get.returncode != 0 or not session_get.stdout.strip()

		third_proc, _ = _run_direct(env, state_key="stale", prompt_text="third prompt\n", phase="implement")
		assert third_proc.returncode == 0, third_proc.stderr

		modes = [entry["mode"] for entry in _read_fake_codex_log(env)]
		assert modes == ["exec", "resume", "exec", "exec"]


def test_invalid_saved_session_id_falls_back_to_exec() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_invalid_state_") as td:
		tmp_path = Path(td)
		env = _helper_env(tmp_path)
		state_dir = Path(env["CODEX_THREAD_REUSE_RUNTIME_DIR"]) / "codex-thread-reuse" / "states"
		state_dir.mkdir(parents=True, exist_ok=True)
		(state_dir / "invalid.session_id").write_text("bad id\n", encoding="utf-8")

		proc, _ = _run_direct(env, state_key="invalid", prompt_text="prompt\n", phase="implement")
		assert proc.returncode == 0, proc.stderr

		modes = [entry["mode"] for entry in _read_fake_codex_log(env)]
		assert modes == ["exec"]

		session_get = _run_helper(["session-get", "invalid"], env=env)
		assert session_get.returncode == 0
		assert session_get.stdout.strip() == "session-1"


def test_transform_prompt_replace_prefix_preserves_repair_context() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_prefix_") as td:
		tmp_path = Path(td)
		source = tmp_path / "source.txt"
		continuation = tmp_path / "continuation.txt"
		output = tmp_path / "output.txt"
		source.write_text(
			"full intro\nmore intro\n=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ===\ndiag\nallow-list\n",
			encoding="utf-8",
		)
		continuation.write_text("compact intro\n", encoding="utf-8")
		proc = _run_helper(
			[
				"transform-prompt",
				"replace-prefix",
				str(source),
				str(continuation),
				str(output),
				"=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ===",
			],
			env=_base_env(),
		)
		assert proc.returncode == 0, proc.stderr
		assert output.read_text(encoding="utf-8") == "compact intro\n=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ===\ndiag\nallow-list\n"


def test_transform_prompt_replace_between_preserves_diagnose_evidence() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_between_") as td:
		tmp_path = Path(td)
		source = tmp_path / "source.txt"
		continuation = tmp_path / "continuation.txt"
		output = tmp_path / "output.txt"
		source.write_text(
			"prefix\n=== IMPLEMENT FAILURE DIAGNOSIS TASK ===\nold task\n=== SOURCE ISSUE BODY ===\nissue body\ndiff tail\n",
			encoding="utf-8",
		)
		continuation.write_text("new task\n", encoding="utf-8")
		proc = _run_helper(
			[
				"transform-prompt",
				"replace-between",
				str(source),
				str(continuation),
				str(output),
				"=== IMPLEMENT FAILURE DIAGNOSIS TASK ===",
				"=== SOURCE ISSUE BODY ===",
			],
			env=_base_env(),
		)
		assert proc.returncode == 0, proc.stderr
		assert output.read_text(encoding="utf-8") == "prefix\n=== IMPLEMENT FAILURE DIAGNOSIS TASK ===\nnew task\n=== SOURCE ISSUE BODY ===\nissue body\ndiff tail\n"


def test_transform_prompt_replace_between_preserves_self_heal_evidence() -> None:
	with tempfile.TemporaryDirectory(prefix="codex_thread_self_heal_") as td:
		tmp_path = Path(td)
		source = tmp_path / "source.txt"
		continuation = tmp_path / "continuation.txt"
		output = tmp_path / "output.txt"
		source.write_text(
			"context\n=== SELF-HEAL TASK ===\nold self-heal task\n=== SELF-HEAL ATTEMPT ===\nattempt 2\nlogs\n",
			encoding="utf-8",
		)
		continuation.write_text("new self-heal task\n", encoding="utf-8")
		proc = _run_helper(
			[
				"transform-prompt",
				"replace-between",
				str(source),
				str(continuation),
				str(output),
				"=== SELF-HEAL TASK ===",
				"=== SELF-HEAL ATTEMPT ===",
			],
			env=_base_env(),
		)
		assert proc.returncode == 0, proc.stderr
		assert output.read_text(encoding="utf-8") == "context\n=== SELF-HEAL TASK ===\nnew self-heal task\n=== SELF-HEAL ATTEMPT ===\nattempt 2\nlogs\n"


def test_render_prompt_autodiscovers_continuation_contracts_and_accepts_serena_hints() -> None:
	render_prompt = _load_render_prompt_module()
	for prompt_path, expected_contract in (
		(
			REPO_ROOT / "prompts" / "mode-implement-repair-continuation.txt",
			REPO_ROOT / "prompts" / "contracts" / "mode-implement-repair-continuation.yml",
		),
		(
			REPO_ROOT / "prompts" / "mode-implement-diagnose-continuation.txt",
			REPO_ROOT / "prompts" / "contracts" / "mode-implement-diagnose-continuation.yml",
		),
		(
			REPO_ROOT / "prompts" / "mode-validate-self-heal-continuation.txt",
			REPO_ROOT / "prompts" / "contracts" / "mode-validate-self-heal-continuation.yml",
		),
	):
		contract_path = render_prompt.discover_contract_path(prompt_path, prompt_path.stem)
		assert contract_path == expected_contract.resolve()
		proc = subprocess.run(
			[sys.executable, str(RENDER_PROMPT_PY), str(prompt_path)],
			cwd=str(REPO_ROOT),
			env={**_base_env(), "SERENA_TOOL_HINTS": "- Use Serena when helpful."},
			capture_output=True,
			text=True,
			check=False,
		)
		assert proc.returncode == 0, proc.stderr
		assert "{{SERENA_TOOL_HINTS}}" not in proc.stdout


def test_render_prompt_shell_shim_enforces_prompt_contracts() -> None:
	with tempfile.TemporaryDirectory(prefix="render_prompt_contract_") as td:
		tmp_path = Path(td)
		prompts_dir = tmp_path / "prompts"
		contracts_dir = prompts_dir / "contracts"
		contracts_dir.mkdir(parents=True, exist_ok=True)

		prompt_path = prompts_dir / "contract-check.txt"
		contract_path = contracts_dir / "contract-check.yml"
		prompt_path.write_text("Header\n{{UNDECLARED}}\n", encoding="utf-8")
		contract_path.write_text(
			"required_vars: []\noptional_vars: {}\nforbidden_vars: []\n",
			encoding="utf-8",
		)

		proc = subprocess.run(
			["bash", str(RENDER_PROMPT_SH), str(prompt_path)],
			cwd=str(REPO_ROOT),
			env=_base_env(),
			capture_output=True,
			text=True,
			check=False,
		)
		assert proc.returncode != 0
		assert "unknown_in_template" in proc.stderr


def test_implement_workflow_contains_thread_reuse_wiring() -> None:
	text = IMPLEMENT_WORKFLOW.read_text(encoding="utf-8")
	fetch_script_list = text.split("for f in ", 1)[1].split("; do", 1)[0]
	assert "CODEX_THREAD_REUSE_ENABLED: ${{ vars.CODEX_THREAD_REUSE_ENABLED || 'false' }}" in text
	assert 'echo "CODEX_THREAD_REUSE_RUNTIME_DIR=${RUNTIME_DIR}"' in text
	assert "codex_thread_reuse.sh" in fetch_script_list
	assert "mode-implement-repair-continuation.txt mode-implement-diagnose-continuation.txt mode-validate-self-heal-continuation.txt" in text
	assert "mode-implement-repair-continuation.yml mode-implement-diagnose-continuation.yml mode-validate-self-heal-continuation.yml" in text
	assert "name: Probe Codex thread-reuse support" in text
	assert "bash scripts/codex_thread_reuse.sh direct-run || cmd_rc=$?" in text
	assert 'CODEX_THREAD_REUSE_MARKER_START="=== CAPTURED SYNTAX DIAGNOSTICS (FULL) ==="' in text
	assert "codex_thread_reuse_install_wrapper" in text
	assert "=== IMPLEMENT FAILURE DIAGNOSIS TASK ===" in text
	assert "=== SOURCE ISSUE BODY ===" in text


def test_validate_process_contains_thread_reuse_wiring() -> None:
	text = VALIDATE_PROCESS.read_text(encoding="utf-8")
	assert 'CODEX_THREAD_REUSE_ENABLED="${CODEX_THREAD_REUSE_ENABLED:-false}"' in text
	assert 'CODEX_THREAD_REUSE_HELPER=""' in text
	assert '"scripts/codex_thread_reuse.sh"' in text
	assert "resolve_validate_thread_reuse_asset()" in text
	assert "validate_thread_reuse_enabled()" in text
	assert 'CODEX_THREAD_REUSE_SKIP_GIT_REPO_CHECK="true"' in text
	assert 'bash "${CODEX_THREAD_REUSE_HELPER}" direct-run' in text
	assert "prompts/mode-validate-self-heal-continuation.txt" in text
	assert "=== SELF-HEAL TASK ===" in text
	assert "=== SELF-HEAL ATTEMPT ===" in text
	assert 'PATH="${heal_path}" \\' in text
	assert 'SELF_HEAL_FAILURE_PHASE="${phase}" \\' in text


def test_validate_workflow_contains_thread_reuse_bootstrap() -> None:
	text = VALIDATE_WORKFLOW.read_text(encoding="utf-8")
	assert "CODEX_THREAD_REUSE_ENABLED: ${{ vars.CODEX_THREAD_REUSE_ENABLED || 'false' }}" in text
	assert 'helper_path="scripts/stage_workflow_support.sh"' in text
	assert 'bash "${helper_path}" validate --manifest "${manifest_path}"' in text
	assert '"scripts/codex_thread_reuse.sh"' in text
	assert '"prompts/mode-validate-self-heal-continuation.txt"' in text
	assert '"prompts/contracts/mode-validate-self-heal-continuation.yml"' in text


def main() -> int:
	test_probe_supported_and_unsupported()
	test_extract_session_id_uses_session_meta_payload_id()
	test_begin_capture_uses_portable_marker_name()
	test_runtime_root_scopes_fallback_per_shell_session()
	test_record_session_skips_null_cwd_metadata_without_crashing()
	test_direct_run_disabled_does_not_store_session_state_or_markers()
	test_direct_run_uses_exec_when_feature_disabled_even_with_saved_session()
	test_direct_run_stores_session_then_resumes_on_later_attempt()
	test_resume_failure_falls_back_to_exec()
	test_resume_and_fallback_exec_failure_clears_saved_session()
	test_invalid_saved_session_id_falls_back_to_exec()
	test_transform_prompt_replace_prefix_preserves_repair_context()
	test_transform_prompt_replace_between_preserves_diagnose_evidence()
	test_transform_prompt_replace_between_preserves_self_heal_evidence()
	test_render_prompt_autodiscovers_continuation_contracts_and_accepts_serena_hints()
	test_render_prompt_shell_shim_enforces_prompt_contracts()
	test_implement_workflow_contains_thread_reuse_wiring()
	test_validate_process_contains_thread_reuse_wiring()
	test_validate_workflow_contains_thread_reuse_bootstrap()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
