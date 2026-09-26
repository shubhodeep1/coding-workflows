#!/usr/bin/env python3
"""Renderer dependency bootstrap in scripts/validate_process.sh.

The renderer runs through the isolated python launcher (python3 -I under
env -i), which ignores user site-packages, so dependencies installed with
`pip install --user` are invisible to it. ensure_validation_renderer_python_deps
installs them into a private venv when the isolated interpreter cannot import
them. These tests drive the extracted function with a fake python3 first on
PATH; the fake venv wraps the test interpreter, which already has the
dependencies, so no package index is contacted.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PROCESS_PATH = REPO_ROOT / "scripts" / "validate_process.sh"
GH_HELPERS_PATH = REPO_ROOT / "scripts" / "gh_helpers.sh"
FUNCTION_START = 'VALIDATION_RENDERER_DEPS_VENV_BIN=""\n'
FUNCTION_END = "run_template_validation_harness_renderer()\n"


def _extracted_function() -> str:
	text = VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")
	start = text.index(FUNCTION_START)
	end = text.index(FUNCTION_END, start)
	return text[start:end]


def _launcher_definition() -> str:
	text = GH_HELPERS_PATH.read_text(encoding="utf-8")
	start = text.index("_gh_helpers_run_isolated_python()\n")
	end = text.index("\n}\n", start) + 3
	return text[start:end]


def _write_fake_python(bin_dir: Path, log_path: Path, *, venv_fails: bool) -> None:
	"""A python3 without the renderer dependencies whose `-m venv` builds
	wrappers around the test interpreter (which has them)."""
	real_python = shlex.quote(sys.executable)
	fake = bin_dir / "python3"
	fake.write_text(
		"#!/usr/bin/env bash\n"
		f"log={shlex.quote(str(log_path))}\n"
		'args=" $* "\n'
		'if [[ "${args}" == *" -m venv "* ]]; then\n'
		'\tprintf "venv %s\\n" "${@: -1}" >> "${log}"\n'
		+ ('\texit 1\n' if venv_fails else "")
		+ '\tdir="${@: -1}"\n'
		'\tmkdir -p "${dir}/bin"\n'
		'\tfor name in python python3; do\n'
		f'\t\tprintf "#!/usr/bin/env bash\\nexec {real_python} \\"\\$@\\"\\n" > "${{dir}}/bin/${{name}}"\n'
		'\t\tchmod +x "${dir}/bin/${name}"\n'
		"\tdone\n"
		"\texit 0\n"
		"fi\n"
		'if [[ "${args}" == *"import yaml, jsonschema, jinja2"* ]]; then\n'
		'\tprintf "probe-missing\\n" >> "${log}"\n'
		"\texit 1\n"
		"fi\n"
		f"exec {real_python} \"$@\"\n",
		encoding="utf-8",
	)
	fake.chmod(0o755)


def _run(root: Path, script_body: str, *, venv_fails: bool = False) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
	fake_bin = root / "fake-bin"
	fake_bin.mkdir()
	log_path = root / "fake-python.log"
	_write_fake_python(fake_bin, log_path, venv_fails=venv_fails)
	generate_log = root / "validate_generate.log"
	script = (
		"set -euo pipefail\n"
		+ _launcher_definition()
		+ "validate_run_isolated_python()\n{\n\t_gh_helpers_run_isolated_python \"$@\"\n}\n"
		+ _extracted_function()
		+ script_body
	)
	env = {
		"PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
		"HOME": str(root),
		"TMPDIR": str(root),
		"RUNNER_TEMP": str(root),
		"GITHUB_RUN_ID": "7",
		"GENERATE_LOG_FILE": str(generate_log),
		"PYTHONDONTWRITEBYTECODE": "1",
	}
	proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
	return proc, log_path, generate_log


def test_missing_dependencies_are_installed_once_into_private_venv(tmp_path: Path) -> None:
	proc, log_path, generate_log = _run(
		tmp_path,
		"ensure_validation_renderer_python_deps\n"
		'first="${VALIDATION_RENDERER_DEPS_VENV_BIN}"\n'
		"ensure_validation_renderer_python_deps\n"
		'printf "first=%s\\nsecond=%s\\n" "${first}" "${VALIDATION_RENDERER_DEPS_VENV_BIN}"\n'
		'PATH="${VALIDATION_RENDERER_DEPS_VENV_BIN}:${PATH}" validate_run_isolated_python -- -c "import yaml; print(\\"renderer-deps-ok\\")"\n'
	)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	lines = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
	assert lines["first"].startswith(f"{tmp_path}/validate-renderer-deps-venv-7-")
	assert lines["first"].endswith("/bin")
	assert lines["second"] == lines["first"], "a second call must reuse the venv"
	assert "renderer-deps-ok" in proc.stdout
	assert log_path.read_text(encoding="utf-8").count("venv ") == 1
	log_text = generate_log.read_text(encoding="utf-8")
	assert "are not importable by the isolated python3" in log_text


def test_importable_dependencies_leave_the_path_unchanged(tmp_path: Path) -> None:
	proc, log_path, _generate_log = _run(
		tmp_path,
		# The dependency probe succeeds, as with a venv-provided interpreter.
		'validate_run_isolated_python() { return 0; }\n'
		"ensure_validation_renderer_python_deps\n"
		'printf "venv_bin=[%s]\\n" "${VALIDATION_RENDERER_DEPS_VENV_BIN}"\n'
	)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "venv_bin=[]" in proc.stdout
	assert not log_path.exists() or "venv " not in log_path.read_text(encoding="utf-8")


def test_unpreparable_venv_returns_failure_and_logs_it(tmp_path: Path) -> None:
	proc, _log_path, generate_log = _run(
		tmp_path,
		"if ensure_validation_renderer_python_deps; then echo unexpected-success; else echo bootstrap-failed; fi\n"
		'printf "venv_bin=[%s]\\n" "${VALIDATION_RENDERER_DEPS_VENV_BIN}"\n',
		venv_fails=True,
	)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "bootstrap-failed" in proc.stdout
	assert "venv_bin=[]" in proc.stdout
	assert "Could not prepare the renderer dependency venv" in generate_log.read_text(encoding="utf-8")


def test_renderer_runs_with_bootstrapped_venv_on_path() -> None:
	text = VALIDATE_PROCESS_PATH.read_text(encoding="utf-8")
	renderer = text[text.index("run_template_validation_harness_renderer()\n"):]
	renderer = renderer[: renderer.index("\n}\n")]
	assert "ensure_validation_renderer_python_deps || true" in renderer
	assert 'renderer_path="${VALIDATION_RENDERER_DEPS_VENV_BIN}:${PATH}"' in renderer
	assert 'renderer_summary="$(PATH="${renderer_path}" validate_run_isolated_python -- "${renderer_script}"' in renderer
	# The venv lives outside RUNTIME_DIR, which is uploaded as the artifact.
	assert '"${RUNNER_TEMP:-/tmp}/validate-renderer-deps-venv-' in _extracted_function()
	# printf must not parse the probe's closing marker as an option.
	assert "printf -- '--- end python3 environment probe ---\\n'" in renderer
