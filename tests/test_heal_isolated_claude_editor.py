"""The workflow-heal editor on the Claude engine (scripts/heal_isolated_implement.sh).

With AI_ENGINE_RESOLVED_IMPLEMENT=claude the heal editor runs through
ai_engine.sh claude_run on the script's own snapshot, then goes through the
same syntax validation and scoped transfer as a codex edit. Claude
unavailable (exit 75) runs the isolated codex editor; any other Claude
failure fails the run. The harness replaces docker and claude_run with fakes.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FAKE_AI_ENGINE = """\
# Fake ai_engine.sh: claude_run edits the workdir snapshot like the real
# isolated write container would, records how it was called, and exits with
# FAKE_CLAUDE_RC.
claude_run()
{
	printf 'role=%s workdir=%s root=%s mode=%s\\n' "$1" "$4" "${CODEX_ISOLATED_ROOT:-unset}" "${CODEX_ISOLATED_MODE:-unset}" >> "${FAKE_CLAUDE_CALLS}"
	if [ "${FAKE_CLAUDE_RC:-0}" -eq 0 ]; then
		printf 'claude\\n' > "$4/scripts/fix.py"
		printf 'claude result\\n' > "$3"
	elif [ "${FAKE_CLAUDE_RC}" -eq 75 ]; then
		echo "AI_ENGINE_FALLBACK role=$1 reason=all_accounts_failed" >&2
	fi
	return "${FAKE_CLAUDE_RC:-0}"
}
"""

FAKE_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >> "__DOCKER_COMMANDS__"
[ -z "${GH_PAT:-}" ] && [ -z "${GH_TOKEN:-}" ] || exit 19
case "$1" in
  build) echo fake-image ;;
  run)
    for arg in "$@"; do
      case "$arg" in
        type=bind,src=*,dst=/results)
          dest="${arg#type=bind,src=}"
          printf 'codex result\\n' > "${dest%%,dst=*}/output"
          ;;
        type=bind,src=*,dst=/source)
          dest="${arg#type=bind,src=}"
          [[ "$*" == *"/validator.sh"* ]] || printf 'codex\\n' > "${dest%%,dst=*}/scripts/fix.py"
          ;;
      esac
    done
    ;;
  ps|rm) ;;
  *) exit 1 ;;
esac
"""


def _run(tmp_path: Path, *, engine: str = "claude", claude_rc: int = 0, with_ai_engine: bool = True) -> dict[str, object]:
	host = tmp_path / "host"
	(host / "scripts").mkdir(parents=True)
	(host / "scripts/fix.py").write_text("old\n")
	subprocess.run(["git", "init", "-q", str(host)], check=True)
	subprocess.run(["git", "add", "--all"], cwd=host, check=True)
	support = tmp_path / "support"
	support.mkdir()
	for name in ("review_untrusted_workspace.py", "files_touched_scope_guard.py"):
		shutil.copy(ROOT / "scripts" / name, support / name)
	(support / "validate_changed_files_syntax.sh").write_text("#!/bin/bash\nexit 0\n")
	(support / "clarify_openrouter_broker.py").write_text(
		"import socket,sys,time\ns=socket.socket(socket.AF_UNIX)\ns.bind(sys.argv[2])\ns.listen(1)\ntime.sleep(30)\n"
	)
	if with_ai_engine:
		(support / "ai_engine.sh").write_text(FAKE_AI_ENGINE)
	trusted = tmp_path / ".codex-workflow-src/scripts/clarify_sandbox"
	trusted.mkdir(parents=True)
	(trusted / "Dockerfile").write_text("FROM scratch\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	commands = tmp_path / "docker-commands"
	docker = bin_dir / "docker"
	docker.write_text(FAKE_DOCKER.replace("__DOCKER_COMMANDS__", str(commands)))
	docker.chmod(0o755)
	prompt = tmp_path / "prompt"
	prompt.write_text("Edit the file")
	scope = tmp_path / "scope"
	scope.write_text("scripts/fix.py\n")
	output = tmp_path / "output"
	claude_calls = tmp_path / "claude-calls"
	env = dict(
		os.environ,
		PATH=f"{bin_dir}:{os.environ['PATH']}",
		GH_PAT="fake-private-pat",
		GH_TOKEN="fake-private-pat",
		OPENROUTER_API_KEY="fake-model-key",
		HEAL_SCOPE_FILE=str(scope),
		HEAL_TRUSTED_SUPPORT_DIR=str(support),
		RUNNER_TEMP=str(tmp_path),
		GITHUB_WORKSPACE=str(tmp_path),
		WORKSPACE_PATH=str(host),
		PYTHONDONTWRITEBYTECODE="1",
		AI_ENGINE_RESOLVED_IMPLEMENT=engine,
		FAKE_CLAUDE_RC=str(claude_rc),
		FAKE_CLAUDE_CALLS=str(claude_calls),
		# The implement job's persistent workspace sandbox must not leak in.
		CODEX_ISOLATED_ROOT=str(tmp_path / "job-sandbox"),
		CODEX_ISOLATED_MODE="workspace",
	)
	result = subprocess.run(
		["bash", str(ROOT / "scripts/heal_isolated_implement.sh"), str(prompt), str(output)],
		env=env, capture_output=True, text=True, check=False,
	)
	docker_log = commands.read_text() if commands.exists() else ""
	return {
		"rc": result.returncode,
		"stderr": result.stderr,
		"stdout": result.stdout,
		"host_file": (host / "scripts/fix.py").read_text(),
		"output": output.read_text() if output.exists() else None,
		"claude_calls": claude_calls.read_text().splitlines() if claude_calls.exists() else [],
		"editor_docker_runs": [line for line in docker_log.splitlines() if line.startswith("run ") and "/validator.sh" not in line],
		"validator_runs": [line for line in docker_log.splitlines() if "/validator.sh" in line],
	}


def test_claude_editor_edits_the_snapshot_then_validates_and_transfers(tmp_path: Path) -> None:
	result = _run(tmp_path)
	assert result["rc"] == 0, result["stderr"]
	assert result["host_file"] == "claude\n"
	assert result["output"] == "claude result\n"
	assert len(result["claude_calls"]) == 1
	call = result["claude_calls"][0]
	assert call.startswith("role=IMPLEMENT workdir=")
	assert "/heal-isolated." in call and call.split("workdir=", 1)[1].split()[0].endswith("/source")
	# The job's persistent workspace sandbox is never reused for the snapshot.
	assert "root=unset mode=unset" in call
	# No codex editor container ran; syntax validation still did.
	assert result["editor_docker_runs"] == []
	assert len(result["validator_runs"]) == 1
	assert "HEAL_ISOLATED_EDITOR phase=run engine=claude outcome=success" in result["stderr"]
	assert "HEAL_ISOLATED_EDITOR phase=transfer engine=claude outcome=success reason=validated" in result["stdout"]
	assert "isolated_claude_unavailable" not in result["stderr"]


def test_claude_unavailable_runs_the_isolated_codex_editor(tmp_path: Path) -> None:
	result = _run(tmp_path, claude_rc=75)
	assert result["rc"] == 0, result["stderr"]
	assert result["host_file"] == "codex\n"
	assert result["output"] == "codex result\n"
	assert len(result["claude_calls"]) == 1
	assert len(result["editor_docker_runs"]) == 1
	assert "engine=claude outcome=unavailable action=codex" in result["stderr"]
	assert "HEAL_ISOLATED_EDITOR phase=transfer engine=codex outcome=success" in result["stdout"]


@pytest.mark.parametrize("claude_rc", [1, 76, 124])
def test_claude_failure_fails_without_transfer_or_codex_rerun(tmp_path: Path, claude_rc: int) -> None:
	# The real claude_run returns 0, 1, 75 or 124 (it maps its internal 76 to
	# 75). 76 is kept as an unexpected status: every exit other than 0 and 75
	# must fail closed, never rerun codex.
	result = _run(tmp_path, claude_rc=claude_rc)
	assert result["rc"] not in (0, 42), result["stderr"]
	assert result["host_file"] == "old\n"
	assert result["output"] is None
	assert result["editor_docker_runs"] == []
	assert f"engine=claude outcome=failed reason=editor_exit_{claude_rc}" in result["stderr"]


def test_codex_engine_never_calls_claude(tmp_path: Path) -> None:
	result = _run(tmp_path, engine="codex")
	assert result["rc"] == 0, result["stderr"]
	assert result["claude_calls"] == []
	assert result["host_file"] == "codex\n"
	assert "HEAL_ISOLATED_EDITOR phase=transfer engine=codex outcome=success" in result["stdout"]


def test_missing_engine_support_falls_back_to_codex(tmp_path: Path) -> None:
	result = _run(tmp_path, with_ai_engine=False)
	assert result["rc"] == 0, result["stderr"]
	assert result["claude_calls"] == []
	assert result["host_file"] == "codex\n"
	assert "AI_ENGINE_FALLBACK role=IMPLEMENT reason=support_missing" in result["stderr"]
