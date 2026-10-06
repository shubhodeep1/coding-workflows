"""The heal editor's output may cross into the credentialed host only by scope."""

import subprocess
import sys
import json
import os
import shutil
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import review_untrusted_workspace as workspace  # noqa: E402


def test_heal_transfer_rejects_out_of_scope_before_writing(tmp_path: Path) -> None:
	host = tmp_path / "host"
	copy = tmp_path / "copy"
	host.mkdir()
	copy.mkdir()
	(host / "scripts").mkdir()
	(host / "scripts" / "fix.py").write_text("old\n")
	subprocess.run(["git", "init", "-q", str(host)], check=True)
	subprocess.run(["git", "add", "--all"], cwd=host, check=True)
	manifest = tmp_path / "manifest.json"
	workspace.snapshot(host, copy, manifest)
	(copy / "scripts" / "fix.py").write_text("new\n")
	(copy / "scripts" / "outside.py").write_text("bad\n")
	with pytest.raises(ValueError, match="out of heal scope"):
		workspace.transfer(host, copy, manifest, ["tests/**", "changelog.d/*.md"])
	assert (host / "scripts" / "fix.py").read_text() == "old\n"
	(copy / "scripts" / "outside.py").unlink()
	workspace.transfer(host, copy, manifest, ["scripts/fix.py", "tests/**", "changelog.d/*.md"])
	assert (host / "scripts" / "fix.py").read_text() == "new\n"


def test_editor_container_and_post_editor_host_contract() -> None:
	runner = (ROOT / "scripts/heal_isolated_implement.sh").read_text()
	workflow = (ROOT / ".github/workflows/implement.yml").read_text()
	for option in ("--rm --init", "--network none", "--read-only", "--scope-file", "docker rm -f", "container_survived", "env -i"):
		assert option in runner
	assert "docker.sock" not in runner
	assert 'HEAL_ROUTE:-false}" = true' in workflow
	assert 'source "${HEAL_TRUSTED_SUPPORT_DIR:-scripts}/write_guard.sh"' in workflow
	assert 'env.HEAL_ROUTE != \'true\'' in workflow


@pytest.mark.parametrize("changed_path", [".git/config", ".github/ai/extra.json", ".gitattributes"])
def test_heal_transfer_rejects_editor_configuration(tmp_path: Path, changed_path: str) -> None:
	host = tmp_path / "host"
	copy = tmp_path / "copy"
	host.mkdir()
	copy.mkdir()
	(host / "scripts").mkdir()
	(host / "scripts/fix.py").write_text("old\n")
	subprocess.run(["git", "init", "-q", str(host)], check=True)
	subprocess.run(["git", "add", "--all"], cwd=host, check=True)
	manifest = tmp_path / "manifest.json"
	workspace.snapshot(host, copy, manifest)
	(copy / "scripts/fix.py").write_text("new\n")
	target = copy / changed_path
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text("malicious\n")
	with pytest.raises(ValueError):
		workspace.transfer(host, copy, manifest, ["scripts/fix.py", "tests/**", "changelog.d/*.md"])
	assert (host / "scripts/fix.py").read_text() == "old\n"


def test_missing_trusted_scope_refuses_before_editor(tmp_path: Path) -> None:
	issue = tmp_path / "issue.json"
	issue.write_text(json.dumps({"body": "files_touched:\n - scripts/**", "labels": [{"name": "ai:workflow-heal"}]}))
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh_calls = tmp_path / "gh-calls"
	live = {"data": {"repository": {"issue": {"body": "files_touched:", "lastEditedAt": None, "author": {"login": "pipeline"}, "labels": {"nodes": [{"name": "ai:workflow-heal"}]}}}}}
	gh.write_text("#!/bin/bash\necho \"$*\" >> '" + str(gh_calls) + "'\nif [ \"$1 $2\" = 'api user' ]; then echo pipeline; elif [ \"$1 $2\" = 'api graphql' ]; then echo '" + json.dumps(live) + "'; fi\n")
	gh.chmod(0o755)
	env_file = tmp_path / "env"
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", ISSUE_META_FILE=str(issue), ISSUE_NUMBER="42", GITHUB_REPOSITORY="owner/repo", GITHUB_ENV=str(env_file), WORKFLOW_HEAL_PY=str(ROOT / "scripts/workflow_failure_heal.py"), PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run(["bash", str(ROOT / "scripts/implement_heal_preflight.sh")], env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stderr
	assert "reason=missing" in result.stdout
	assert "SKIP_IMPLEMENT=true" in env_file.read_text()
	assert "HEAL_ROUTE=true" not in env_file.read_text()
	assert gh_calls.read_text().index("issue comment") < gh_calls.read_text().index("issue edit")


@pytest.mark.parametrize("repair_needed", [False, True])
def test_isolated_editor_does_not_pass_pat_to_container_or_run_host_edits(tmp_path: Path, repair_needed: bool) -> None:
	host = tmp_path / "host"
	host.mkdir()
	(host / "scripts").mkdir()
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
	trusted = tmp_path / ".codex-workflow-src/scripts/clarify_sandbox"
	trusted.mkdir(parents=True)
	(trusted / "Dockerfile").write_text("FROM scratch\n")
	if repair_needed:
		prompt_dir = tmp_path / ".codex-workflow-src/prompts"
		prompt_dir.mkdir()
		(prompt_dir / "mode-implement-repair-syntax.txt").write_text("Repair syntax without touching credentials.\n")
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	commands = tmp_path / "docker-commands"
	validation_marker = tmp_path / "validator-failed-once"
	docker = bin_dir / "docker"
	docker.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "__DOCKER_COMMANDS__"
[ -z "${GH_PAT:-}" ] && [ -z "${GH_TOKEN:-}" ] || exit 19
case "$1" in
  build) echo fake-image ;;
  run)
    for arg in "$@"; do
      case "$arg" in
        type=bind,src=*,dst=/results)
          dest="${arg#type=bind,src=}"
          printf 'isolated result\\n' > "${dest%%,dst=*}/output"
          ;;
        type=bind,src=*,dst=/source)
          dest="${arg#type=bind,src=}"
          printf 'new\\n' > "${dest%%,dst=*}/scripts/fix.py"
          ;;
      esac
    done
    if [ "__REPAIR_NEEDED__" = true ] && [[ "$*" == *"/validator.sh"* ]] && [ ! -e "__VALIDATION_MARKER__" ]; then
      touch "__VALIDATION_MARKER__"
      exit 1
    fi
    ;;
  ps|rm) ;;
  *) exit 1 ;;
esac
""".replace("__DOCKER_COMMANDS__", str(commands)).replace("__REPAIR_NEEDED__", "true" if repair_needed else "false").replace("__VALIDATION_MARKER__", str(validation_marker)))
	docker.chmod(0o755)
	prompt = tmp_path / "prompt"
	prompt.write_text("Edit the file")
	scope = tmp_path / "scope"
	scope.write_text("scripts/fix.py\ntests/**\nchangelog.d/*.md\n")
	output = tmp_path / "output"
	env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GH_PAT="fake-private-pat", GH_TOKEN="fake-private-pat", OPENROUTER_API_KEY="fake-model-key", HEAL_SCOPE_FILE=str(scope), HEAL_TRUSTED_SUPPORT_DIR=str(support), RUNNER_TEMP=str(tmp_path), GITHUB_WORKSPACE=str(tmp_path), WORKSPACE_PATH=str(host), PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run(["bash", str(ROOT / "scripts/heal_isolated_implement.sh"), str(prompt), str(output)], env=env, capture_output=True, text=True, check=False)
	assert result.returncode == 0, result.stdout + result.stderr
	assert (host / "scripts/fix.py").read_text() == "new\n"
	assert output.read_text() == "isolated result\n"
	assert "rm -f heal-editor-" in commands.read_text()
	assert "--network none" in commands.read_text()
	if repair_needed:
		assert validation_marker.exists()
		assert commands.read_text().count("--network none") >= 4


def test_real_docker_kills_background_editor_child_when_available() -> None:
	if not shutil.which("docker") or subprocess.run(["docker", "image", "inspect", "busybox:1.36"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0:
		pytest.skip("busybox:1.36 is not cached locally")
	name = "heal-child-test-" + str(os.getpid())
	container = subprocess.check_output(["docker", "run", "--rm", "--init", "-d", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL", "busybox:1.36", "sh", "-c", "setsid sleep 600 & wait"], text=True).strip()
	try:
		children = []
		for _ in range(25):
			top = subprocess.check_output(["docker", "top", container, "-eo", "pid,comm"], text=True)
			children = [int(row.split()[0]) for row in top.splitlines()[1:] if row.split()[-1] == "sleep"]
			if children:
				break
			time.sleep(0.2)
		assert children
	finally:
		subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, check=False)
	for _ in range(25):
		if all(not (Path("/proc") / str(pid) / "comm").exists() or (Path("/proc") / str(pid) / "comm").read_text().strip() != "sleep" for pid in children):
			break
		time.sleep(0.2)
	else:
		raise AssertionError("editor child outlived container")
