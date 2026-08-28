#!/usr/bin/env python3
"""Contract tests for scripts/opencode_helpers.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPERS = REPO_ROOT / "scripts" / "opencode_helpers.sh"


def _bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
	full_env = os.environ.copy()
	# The negative-path tests below drive opencode_emit_failure_alert, which
	# sources scripts/tg_helpers.sh and sends a real Telegram message whenever
	# bot credentials are present in the environment (e.g. validate.yml exports
	# TG_BOT_SECRET into the validation harness that runs this suite). Strip
	# the credentials so synthetic fixtures can never page an operator; the
	# alert-contract test stubs tg_send_msg and is unaffected.
	for key in ("TG_BOT_SECRET", "TG_ADMIN_CHAT_ID", "TG_CHAT_ID"):
		full_env.pop(key, None)
	if env:
		full_env.update(env)
	return subprocess.run(
		["bash", "-c", script],
		cwd=REPO_ROOT,
		env=full_env,
		capture_output=True,
		check=False,
	)


def test_ansi_filter_removes_common_sequences_and_is_idempotent() -> None:
	payload = b"plain \xe2\x98\x83 \x1b[31mred\x1b[0m \x1b]0;title\x07\x1b(B\x1b#5\x1b)0done\n"
	first = subprocess.run(
		["bash", "-c", f"source {HELPERS}; opencode_strip_ansi"],
		cwd=REPO_ROOT,
		input=payload,
		capture_output=True,
		check=True,
	).stdout
	second = subprocess.run(
		["bash", "-c", f"source {HELPERS}; opencode_strip_ansi"],
		cwd=REPO_ROOT,
		input=first,
		capture_output=True,
		check=True,
	).stdout
	assert first == b"plain \xe2\x98\x83 red done\n"
	assert second == first


def test_command_argv_is_fixed_and_prompt_remains_on_stdin() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		stub = bin_dir / "opencode"
		stub.write_text(
			"#!/usr/bin/env bash\n"
			"printf '%s\\0' \"$@\" > \"${ARGS_FILE}\"\n"
			"printf '%s' \"${OPENCODE_CONFIG:-}\" > \"${CONFIG_ENV_FILE}\"\n"
			"cat > \"${STDIN_FILE}\"\n"
			"printf 'OK\\n'\n",
			encoding="utf-8",
		)
		stub.chmod(0o755)
		config = root / "config.json"
		config.write_text("{}\n", encoding="utf-8")
		args_file = root / "args"
		stdin_file = root / "stdin"
		config_env_file = root / "config-env"
		prompt = "private prompt text"
		result = _bash(
			f"source {HELPERS}; printf '%s' \"$PROMPT\" | "
			f"opencode_run_cmd writer vendor/model xhigh {config} {root} json",
			{
				"PATH": f"{bin_dir}:{os.environ['PATH']}",
				"ARGS_FILE": str(args_file),
				"STDIN_FILE": str(stdin_file),
				"CONFIG_ENV_FILE": str(config_env_file),
				"PROMPT": prompt,
			},
		)
		assert result.returncode == 0, result.stderr.decode()
		argv = args_file.read_bytes().rstrip(b"\0").decode().split("\0")
		assert argv == [
			"run",
			"--dir",
			str(root),
			"-m",
			"openrouter/vendor/model",
			"--agent",
			"writer",
			"--variant",
			"xhigh",
			"--title",
			"coding-workflows-agent-run",
			"--print-logs",
			"--log-level",
			"INFO",
			"--format",
			"json",
			"--auto",
		]
		assert prompt not in argv
		assert stdin_file.read_text(encoding="utf-8") == prompt
		assert config_env_file.read_text(encoding="utf-8") == str(config)
		assert result.stderr == (
			b"opencode_agent_start role=writer expected_provider=openrouter "
			b"expected_model=vendor/model variant=xhigh\n"
		)


def test_reviewer_command_omits_writer_auto_approval() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		stub = bin_dir / "opencode"
		stub.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
		stub.chmod(0o755)
		config = root / "config.json"
		config.write_text("{}\n", encoding="utf-8")
		result = _bash(
			f"source {HELPERS}; opencode_run_cmd reviewer vendor/model low {config} {root}",
			{"PATH": f"{bin_dir}:{os.environ['PATH']}"},
		)
		assert result.returncode == 0
		assert b"--auto" not in result.stdout
		assert b"--format" not in result.stdout


def test_command_rejects_an_invalid_output_format() -> None:
	with tempfile.TemporaryDirectory() as directory:
		config = Path(directory) / "config.json"
		config.write_text("{}\n", encoding="utf-8")
		result = _bash(f"source {HELPERS}; opencode_run_cmd reviewer vendor/model low {config} {directory} xml")
		assert result.returncode == 2
		assert b"invalid output format 'xml'" in result.stderr


def test_alert_is_single_line_sanitized_and_uses_error_level() -> None:
	with tempfile.TemporaryDirectory() as directory:
		capture = Path(directory) / "telegram"
		result = _bash(
			f"source {HELPERS}; "
			"tg_send_msg() { printf '%s\\n%s\\n' \"$1\" \"$2\" > \"$TG_CAPTURE\"; }; "
			"opencode_emit_failure_alert $'phase\\nbad' reviewer vendor/model 17 $'auth\\tfailure'",
			{"TG_CAPTURE": str(capture)},
		)
		assert result.returncode == 17
		stderr_lines = result.stderr.decode().splitlines()
		assert stderr_lines == [
			"opencode_agent_failure phase=phase_bad role=reviewer model=vendor/model rc=17 failure_class=auth_failure"
		]
		assert capture.read_text(encoding="utf-8").splitlines() == [stderr_lines[0], "ERROR"]


def test_bootstrap_fails_closed_for_missing_and_wrong_binary() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		config = root / "config.json"
		config.write_text("{}\n", encoding="utf-8")
		writer = root / "writer.sh"
		writer.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

		missing = _bash(
			f"source {HELPERS}; opencode_require_bootstrap test reviewer vendor/model {config} 1.18.23 {writer}",
			{"PATH": "/usr/bin:/bin"},
		)
		assert missing.returncode == 127
		assert b"failure_class=binary_missing" in missing.stderr

		bin_dir = root / "bin"
		bin_dir.mkdir()
		stub = bin_dir / "opencode"
		stub.write_text("#!/usr/bin/env bash\nprintf '1.18.22\\n'\n", encoding="utf-8")
		stub.chmod(0o755)
		wrong = _bash(
			f"source {HELPERS}; opencode_require_bootstrap test reviewer vendor/model {config} 1.18.23 {writer}",
			{"PATH": f"{bin_dir}:/usr/bin:/bin"},
		)
		assert wrong.returncode == 1
		assert b"failure_class=version_mismatch" in wrong.stderr


def test_bootstrap_rejects_an_invalid_expected_version() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		config = root / "config.json"
		config.write_text("{}\n", encoding="utf-8")
		writer = root / "writer.sh"
		writer.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
		result = _bash(
			f"source {HELPERS}; opencode_require_bootstrap test reviewer vendor/model {config} latest {writer}",
			{"PATH": "/usr/bin:/bin"},
		)
		assert result.returncode == 2
		assert b"failure_class=invalid_expected_version" in result.stderr


def test_bootstrap_validates_writer_and_generated_json() -> None:
	with tempfile.TemporaryDirectory() as directory:
		root = Path(directory)
		bin_dir = root / "bin"
		bin_dir.mkdir()
		stub = bin_dir / "opencode"
		stub.write_text("#!/usr/bin/env bash\nprintf '1.18.23\\n'\n", encoding="utf-8")
		stub.chmod(0o755)
		writer = root / "writer.sh"
		writer.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
		config = root / "config.json"
		config.write_text("not-json\n", encoding="utf-8")
		env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
		invalid = _bash(
			f"source {HELPERS}; opencode_require_bootstrap test writer vendor/model {config} 1.18.23 {writer}",
			env,
		)
		assert invalid.returncode == 1
		assert b"failure_class=config_invalid" in invalid.stderr

		config.write_text("{}\n", encoding="utf-8")
		valid = _bash(
			f"source {HELPERS}; opencode_require_bootstrap test writer vendor/model {config} 1.18.23 {writer}",
			env,
		)
		assert valid.returncode == 0, valid.stderr.decode()


def main() -> int:
	tests = [value for key, value in sorted(globals().items()) if key.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
