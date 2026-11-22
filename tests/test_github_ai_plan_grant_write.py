#!/usr/bin/env python3
"""Plan-granted `.github/ai/<file>` writes (issue #9920, plan Phase 5 / R3).

Covers the implement commit staging block, the review/heal sandbox grant
(review_untrusted_workspace.py), the review prepare helper, the review commit
filter, the GITHUB_AI_PLAN_GRANT_WRITE_ENABLED switch (default on, `false`
restores the old behaviour) and the workflow env forwarding.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import files_touched_scope_guard as guard  # noqa: E402

COMMIT_SCRIPT = SCRIPTS / "implement_commit_changes.sh"
WORKSPACE_HELPER = SCRIPTS / "review_untrusted_workspace.py"
SANDBOX_SCRIPT = SCRIPTS / "review_untrusted_sandbox.sh"
REVIEW_COMMIT = SCRIPTS / "review_commit_changes.sh"
HEAL_SCRIPT = SCRIPTS / "heal_isolated_implement.sh"
COLLECT_SCRIPT = SCRIPTS / "review_collect_pr_metadata.sh"
FLAG = "GITHUB_AI_PLAN_GRANT_WRITE_ENABLED"
GRANTED = ".github/ai/x.json"


def _load_workspace_module():
	spec = importlib.util.spec_from_file_location("review_untrusted_workspace_grant_test", WORKSPACE_HELPER)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _clean_env(**extra: str) -> dict[str, str]:
	env = {key: value for key, value in os.environ.items()
		if not key.startswith(("GIT_", "GITHUB_")) and key not in (FLAG, "ALLOW_WORKFLOW_EDITS", "RUNTIME_DIR")}
	env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONDONTWRITEBYTECODE": "1"})
	env.update(extra)
	return env


def _git(cwd: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
		env=_clean_env()).stdout


def _init_repo(path: Path) -> Path:
	path.mkdir(parents=True, exist_ok=True)
	_git(path, "init", "-q")
	_git(path, "config", "user.name", "t")
	_git(path, "config", "user.email", "t@invalid")
	(path / "README.md").write_text("readme\n", encoding="utf-8")
	(path / ".github" / "ai").mkdir(parents=True)
	(path / ".github" / "ai" / "claude_engine.json").write_text("{}\n", encoding="utf-8")
	_git(path, "add", "-A")
	_git(path, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
	return path


def _grant(paths: list[str], *, author: str = "maintainer", association: str = "OWNER") -> dict:
	body = "files_touched:\n" + "".join(f"  - {entry}\n" for entry in paths) if paths else "no block"
	meta = {"number": 42, "user": {"login": author}, "author_association": association, "body": body}
	return guard.build_automation_grant(meta, "pipeline-bot")


# --- implement_commit_changes.sh ---------------------------------------------


def _run_commit(repo: Path, tmp: Path, grant: dict | None, **env_extra: str) -> tuple[subprocess.CompletedProcess, str]:
	runtime = tmp / "runtime"
	runtime.mkdir(exist_ok=True)
	output = tmp / "github_output"
	output.write_text("", encoding="utf-8")
	grant_file = runtime / "automation_path_grant.json"
	if grant is not None:
		grant_file.write_text(json.dumps(grant), encoding="utf-8")
	env = _clean_env(
		GITHUB_OUTPUT=str(output), GITHUB_REPOSITORY="acme/consumer", ISSUE_NUMBER="42",
		RUNTIME_DIR=str(runtime), AUTOMATION_PATH_GRANT_FILE=str(grant_file),
		IMPLEMENT_STAGED_SUPPORT_RUN_DIR=str(SCRIPTS), ENFORCE_FILES_TOUCHED="false",
		ALLOW_WORKFLOW_EDITS="true")
	env.update(env_extra)
	proc = subprocess.run(["bash", str(COMMIT_SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)
	return proc, output.read_text(encoding="utf-8")


def _committed(repo: Path) -> list[str]:
	return _git(repo, "show", "--name-only", "--format=", "HEAD").split()


def test_implement_stages_granted_github_ai_file_and_reports_absent(tmp_path: Path) -> None:
	repo = _init_repo(tmp_path / "repo")
	(repo / GRANTED).write_text("{\"a\": 1}\n", encoding="utf-8")
	(repo / ".github/ai/y.json").write_text("{}\n", encoding="utf-8")
	proc, output = _run_commit(repo, tmp_path, _grant([GRANTED, ".github/ai/missing.json"]))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert f"IMPLEMENT_GITHUB_AI_GRANT path={GRANTED} outcome=staged" in proc.stdout
	assert "IMPLEMENT_GITHUB_AI_GRANT path=.github/ai/missing.json outcome=absent" in proc.stdout
	assert "did_commit=true" in output
	committed = _committed(repo)
	assert GRANTED in committed
	assert ".github/ai/y.json" not in committed


def test_implement_does_not_stage_ungranted_or_open_grant(tmp_path: Path) -> None:
	repo = _init_repo(tmp_path / "repo")
	(repo / ".github/ai/y.json").write_text("{}\n", encoding="utf-8")
	proc, output = _run_commit(repo, tmp_path, _grant([GRANTED]))
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "did_commit=false" in output
	open_grant = _grant([], author="pipeline-bot", association="NONE")
	assert open_grant["open"] is True and open_grant["paths"] == []
	(repo / GRANTED).write_text("{}\n", encoding="utf-8")
	proc, output = _run_commit(repo, tmp_path, open_grant)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "outcome=staged" not in proc.stdout and "did_commit=false" in output
	# An untrusted author's list grants nothing.
	proc, output = _run_commit(repo, tmp_path, _grant([GRANTED], association="CONTRIBUTOR"))
	assert "outcome=staged" not in proc.stdout and "did_commit=false" in output


def test_implement_skips_when_workflow_edits_disabled(tmp_path: Path) -> None:
	repo = _init_repo(tmp_path / "repo")
	(repo / GRANTED).write_text("{}\n", encoding="utf-8")
	proc, output = _run_commit(repo, tmp_path, _grant([GRANTED]), ALLOW_WORKFLOW_EDITS="false")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "IMPLEMENT_GITHUB_AI_GRANT outcome=skipped reason=workflow_edits_disabled" in proc.stdout
	assert "did_commit=false" in output


def test_implement_refuses_symlink_and_rejects_glob_entries(tmp_path: Path) -> None:
	repo = _init_repo(tmp_path / "repo")
	(tmp_path / "target.json").write_text("{}\n", encoding="utf-8")
	os.symlink(tmp_path / "target.json", repo / GRANTED)
	grant = _grant([GRANTED])
	grant["paths"] += [".github/ai/*.json", ".github/ai/sub/z.json"]
	(repo / ".github/ai/sub").mkdir()
	(repo / ".github/ai/sub/z.json").write_text("{}\n", encoding="utf-8")
	proc, output = _run_commit(repo, tmp_path, grant)
	assert f"IMPLEMENT_GITHUB_AI_GRANT path={GRANTED} outcome=refused" in proc.stdout
	assert "outcome=staged" not in proc.stdout
	assert "did_commit=false" in output


@pytest.mark.parametrize("value", ["false", "FALSE", "bogus"])
def test_implement_switch_off_restores_old_behaviour(tmp_path: Path, value: str) -> None:
	repo = _init_repo(tmp_path / "repo")
	(repo / GRANTED).write_text("{}\n", encoding="utf-8")
	proc, output = _run_commit(repo, tmp_path, _grant([GRANTED]), **{FLAG: value})
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert "IMPLEMENT_GITHUB_AI_GRANT" not in proc.stdout
	assert "did_commit=false" in output
	if value == "bogus":
		assert "::warning::GITHUB_AI_PLAN_GRANT_WRITE_ENABLED='bogus' is not true/false" in proc.stdout


def test_implement_switch_default_and_empty_are_on(tmp_path: Path) -> None:
	repo = _init_repo(tmp_path / "repo")
	(repo / GRANTED).write_text("{}\n", encoding="utf-8")
	proc, _ = _run_commit(repo, tmp_path, _grant([GRANTED]), **{FLAG: ""})
	assert "outcome=staged" in proc.stdout, proc.stdout + proc.stderr


# --- review_untrusted_workspace.py -------------------------------------------


@pytest.fixture()
def workspace_env(monkeypatch: pytest.MonkeyPatch):
	monkeypatch.delenv(FLAG, raising=False)
	monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
	for key in [key for key in os.environ if key.startswith("GIT_")]:
		monkeypatch.delenv(key, raising=False)
	return monkeypatch


def _snapshot(module, tmp: Path, granted, *, pre=None):
	host = _init_repo(tmp / "host")
	if pre:
		pre(host)
	workspace = tmp / "ws"
	workspace.mkdir()
	manifest = tmp / "manifest.json"
	module.snapshot(host, workspace, manifest, granted=granted)
	return host, workspace, manifest


def test_workspace_granted_path_round_trip(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()
	host, workspace, manifest = _snapshot(module, tmp_path, frozenset({GRANTED}))
	assert json.loads(module.granted_paths_path(manifest).read_text(encoding="utf-8")) == [GRANTED]
	(workspace / GRANTED).write_text("{\"new\": true}\n", encoding="utf-8")
	(workspace / ".github/ai/evil.json").write_text("{}\n", encoding="utf-8")
	module.transfer(host, workspace, manifest)
	assert (host / GRANTED).read_text(encoding="utf-8") == "{\"new\": true}\n"
	assert not (host / ".github/ai/evil.json").exists()


def test_workspace_existing_granted_file_enters_snapshot(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()

	def pre(host: Path) -> None:
		(host / GRANTED).write_text("old\n", encoding="utf-8")
		(host / ".github/ai/other.json").write_text("other\n", encoding="utf-8")

	host, workspace, manifest = _snapshot(module, tmp_path, frozenset({GRANTED}), pre=pre)
	assert (workspace / GRANTED).read_text(encoding="utf-8") == "old\n"
	assert not (workspace / ".github/ai/other.json").exists()
	baseline = json.loads(manifest.read_text(encoding="utf-8"))
	assert GRANTED in baseline and ".github/ai/other.json" not in baseline


def test_workspace_symlink_under_github_ai_is_refused(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()

	def pre(host: Path) -> None:
		os.symlink("/etc/passwd", host / GRANTED)

	host, workspace, manifest = _snapshot(module, tmp_path, frozenset({GRANTED}), pre=pre)
	assert not (workspace / GRANTED).exists() and not (workspace / GRANTED).is_symlink()
	assert GRANTED not in json.loads(manifest.read_text(encoding="utf-8"))
	(workspace / GRANTED).write_text("x\n", encoding="utf-8")
	with pytest.raises(ValueError):
		module.transfer(host, workspace, manifest)
	assert (host / GRANTED).is_symlink()


def test_workspace_rejects_invalid_grant_lists(tmp_path: Path) -> None:
	module = _load_workspace_module()
	cases = {
		"glob": [GRANTED, ".github/ai/*.json"],
		"dotdot": [".github/ai/.."],
		"nested": [".github/ai/sub/x.json"],
		"prefix": ["scripts/x.sh"],
		"trailing": [".github/ai/"],
		"backslash": [".github/ai/a\\b"],
		"newline": [".github/ai/a\nb"],
		"nonstring": [1],
	}
	for name, entries in cases.items():
		path = tmp_path / f"{name}.json"
		path.write_text(json.dumps(entries), encoding="utf-8")
		assert module.load_granted_paths(path) == frozenset(), name
		assert module._parse_granted_paths(path)[1] == "rejected", name
	path = tmp_path / "ok.json"
	path.write_text(json.dumps([GRANTED]), encoding="utf-8")
	assert module.load_granted_paths(path) == frozenset({GRANTED})
	assert module.load_granted_paths(tmp_path / "missing.json") == frozenset()
	assert not module.allowed(GRANTED)
	assert module.allowed(GRANTED, granted=frozenset({GRANTED}))
	# The secret/credential/.env filters run before the grant.
	for name in (".github/ai/secret.json", ".github/ai/.env", ".github/ai/credentials.json"):
		assert not module.allowed(name, granted=frozenset({name}))


def test_workspace_heal_scope_transfer(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()
	host, workspace, manifest = _snapshot(module, tmp_path, frozenset({GRANTED}))
	(workspace / GRANTED).write_text("{}\n", encoding="utf-8")
	with pytest.raises(ValueError, match="out of heal scope"):
		module.transfer(host, workspace, manifest, scope=["tests/**"])
	assert not (host / GRANTED).exists()
	module.transfer(host, workspace, manifest, scope=[GRANTED, "tests/**"])
	assert (host / GRANTED).exists()


def test_workspace_heal_scope_refuses_ungranted_github_ai(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()
	host, workspace, manifest = _snapshot(module, tmp_path, frozenset())
	(workspace / GRANTED).write_text("{}\n", encoding="utf-8")
	with pytest.raises(ValueError, match="out of heal scope"):
		module.transfer(host, workspace, manifest, scope=[GRANTED])


def test_workspace_refresh_discards_pre_editor_granted_write(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()
	host, workspace, manifest = _snapshot(module, tmp_path, frozenset({GRANTED}))
	(workspace / GRANTED).write_text("build-backend write\n", encoding="utf-8")
	module.refresh(host, workspace, manifest)
	assert not (workspace / GRANTED).exists()


def test_workspace_switch_off_ignores_frozen_grant(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()
	host, workspace, manifest = _snapshot(module, tmp_path, frozenset({GRANTED}))
	workspace_env.setenv(FLAG, "false")
	(workspace / GRANTED).write_text("{}\n", encoding="utf-8")
	module.transfer(host, workspace, manifest)
	assert not (host / GRANTED).exists()


def test_workspace_snapshot_without_grant_writes_no_sidecar(tmp_path: Path, workspace_env) -> None:
	module = _load_workspace_module()
	_, _, manifest = _snapshot(module, tmp_path, None)
	assert not module.granted_paths_path(manifest).exists()


def _cli_snapshot(tmp: Path, extra: list[str], **env_extra: str) -> tuple[subprocess.CompletedProcess, Path]:
	host = _init_repo(tmp / "host")
	workspace = tmp / "ws"
	workspace.mkdir()
	manifest = tmp / "manifest.json"
	env = _clean_env(**env_extra)
	proc = subprocess.run([sys.executable, str(WORKSPACE_HELPER), "snapshot", str(host), str(workspace), str(manifest), *extra],
		env=env, capture_output=True, text=True)
	return proc, manifest


def test_cli_snapshot_granted_paths_file(tmp_path: Path) -> None:
	grants = tmp_path / "grants.json"
	grants.write_text(json.dumps([GRANTED]), encoding="utf-8")
	proc, manifest = _cli_snapshot(tmp_path, ["--granted-paths-file", str(grants)])
	assert proc.returncode == 0, proc.stderr
	assert "REVIEW_GITHUB_AI_GRANT phase=snapshot outcome=granted paths=1" in proc.stderr
	assert GRANTED not in proc.stderr  # a count only
	assert json.loads((tmp_path / "manifest.json.github_ai_grants.json").read_text(encoding="utf-8")) == [GRANTED]


def test_cli_snapshot_with_host_git_dir_and_grant(tmp_path: Path) -> None:
	grants = tmp_path / "grants.json"
	grants.write_text(json.dumps([GRANTED]), encoding="utf-8")
	host_git_dir = tmp_path / "host" / ".git"
	proc, manifest = _cli_snapshot(tmp_path, [str(host_git_dir), "--granted-paths-file", str(grants)])
	assert proc.returncode == 0, proc.stderr
	assert "outcome=granted paths=1" in proc.stderr


def test_cli_snapshot_rejected_list(tmp_path: Path) -> None:
	grants = tmp_path / "grants.json"
	grants.write_text(json.dumps([".github/ai/*"]), encoding="utf-8")
	proc, manifest = _cli_snapshot(tmp_path, ["--granted-paths-file", str(grants)])
	assert proc.returncode == 0, proc.stderr
	assert "REVIEW_GITHUB_AI_GRANT phase=snapshot outcome=rejected paths=0" in proc.stderr
	assert json.loads((tmp_path / "manifest.json.github_ai_grants.json").read_text(encoding="utf-8")) == []


def test_cli_snapshot_old_shapes_still_parse(tmp_path: Path) -> None:
	proc, manifest = _cli_snapshot(tmp_path, [])
	assert proc.returncode == 0, proc.stderr
	assert "REVIEW_GITHUB_AI_GRANT" not in proc.stderr
	assert json.loads((tmp_path / "manifest.json.github_ai_grants.json").read_text(encoding="utf-8")) == []
	bad = subprocess.run([sys.executable, str(WORKSPACE_HELPER), "transfer", "a", "b", "c", "--granted-paths-file", "d"],
		env=_clean_env(), capture_output=True, text=True)
	assert bad.returncode == 2


def test_cli_snapshot_switch_off_ignores_flag(tmp_path: Path) -> None:
	grants = tmp_path / "grants.json"
	grants.write_text(json.dumps([GRANTED]), encoding="utf-8")
	proc, manifest = _cli_snapshot(tmp_path, ["--granted-paths-file", str(grants)], **{FLAG: "false"})
	assert proc.returncode == 0, proc.stderr
	assert "outcome=none reason=switch_off paths=0" in proc.stderr
	assert not (tmp_path / "manifest.json.github_ai_grants.json").exists()


def test_workspace_switch_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
	module = _load_workspace_module()
	for value, expected in ((None, True), ("", True), ("TRUE", True), ("false", False), ("False", False), ("nope", False)):
		if value is None:
			monkeypatch.delenv(FLAG, raising=False)
		else:
			monkeypatch.setenv(FLAG, value)
		assert module.github_ai_grant_enabled() is expected, value


# --- review_untrusted_sandbox.sh prepare helper -------------------------------


def _sandbox_helper_source() -> str:
	text = SANDBOX_SCRIPT.read_text(encoding="utf-8")
	start = text.index("_review_github_ai_grant_none()\n")
	end = text.index("# Only the trusted prepare step may select a root")
	return text[start:end]


def _run_prepare_helper(tmp: Path, issue: dict | None, *, status: str = "ok", login: str = "pipeline-bot",
	**env_extra: str) -> tuple[subprocess.CompletedProcess, Path]:
	stub_dir = tmp / "bin"
	stub_dir.mkdir(exist_ok=True)
	gh_log = tmp / "gh_calls.txt"
	(stub_dir / "gh").write_text(f"#!/usr/bin/env bash\necho \"$*\" >> '{gh_log}'\necho '{login}'\n", encoding="utf-8")
	(stub_dir / "gh").chmod(0o755)
	raw = tmp / "linked_issues_raw.json"
	raw.write_text(json.dumps([issue] if issue is not None else []), encoding="utf-8")
	(tmp / "linked_issues_raw.json.status").write_text(status + "\n", encoding="utf-8")
	runner_temp = tmp / "runner_temp"
	runner_temp.mkdir(exist_ok=True)
	github_env = tmp / "github_env"
	github_env.write_text("", encoding="utf-8")
	script = tmp / "helper.sh"
	script.write_text("set -euo pipefail\nsupport=\"$1\"\n" + _sandbox_helper_source()
		+ "out=\"$(_review_github_ai_grant_prepare)\" || out=\"\"\nprintf 'OUT=%s\\n' \"${out}\"\n", encoding="utf-8")
	env = _clean_env(PATH=f"{stub_dir}:{os.environ['PATH']}", LINKED_ISSUES_RAW_FILE=str(raw),
		RUNNER_TEMP=str(runner_temp), GITHUB_ENV=str(github_env), GITHUB_REPOSITORY="acme/consumer")
	env.update(env_extra)
	proc = subprocess.run(["bash", str(script), str(SCRIPTS)], env=env, capture_output=True, text=True)
	return proc, github_env


def _issue(body: str, *, login: str = "maintainer", association: str = "OWNER") -> dict:
	return {"number": 42, "title": "t", "body": body, "labels": [], "author_login": login, "author_association": association}


def test_prepare_helper_grants_trusted_exact_paths(tmp_path: Path) -> None:
	proc, github_env = _run_prepare_helper(tmp_path, _issue(f"files_touched:\n  - {GRANTED}\n  - scripts/a.sh\n  - .github/ai/sub/y.json\n"))
	assert proc.returncode == 0, proc.stderr
	assert "REVIEW_GITHUB_AI_GRANT phase=prepare outcome=granted reason=granted paths=1" in proc.stderr
	out = re.search(r"^OUT=(.*)$", proc.stdout, re.M).group(1)
	assert out.startswith(str(tmp_path / "runner_temp"))
	assert json.loads(Path(out).read_text(encoding="utf-8")) == [GRANTED]
	# The snapshot parser accepts the list (a nested entry would reject it all).
	assert _load_workspace_module().load_granted_paths(Path(out)) == frozenset({GRANTED})
	# The export waits for a successful prepare (next to REVIEW_SANDBOX_ROOT).
	assert github_env.read_text(encoding="utf-8") == ""


def test_prepare_helper_fails_open_without_api_call(tmp_path: Path) -> None:
	proc, _ = _run_prepare_helper(tmp_path, _issue("files_touched:\n  - scripts/a.sh\n"))
	assert "outcome=none reason=no_github_ai_path" in proc.stderr and "OUT=\n" in proc.stdout
	assert not (tmp_path / "gh_calls.txt").exists()
	proc, _ = _run_prepare_helper(tmp_path, _issue(f"files_touched:\n  - {GRANTED}\n"), status="failed")
	assert "reason=linked_issues_incomplete" in proc.stderr and "OUT=\n" in proc.stdout
	proc, _ = _run_prepare_helper(tmp_path, None)
	assert "reason=linked_issue_count" in proc.stderr
	proc, _ = _run_prepare_helper(tmp_path, _issue(f"files_touched:\n  - {GRANTED}\n", association="CONTRIBUTOR"))
	assert "outcome=none reason=untrusted_author" in proc.stderr and "OUT=\n" in proc.stdout
	proc, _ = _run_prepare_helper(tmp_path, _issue(f"files_touched:\n  - {GRANTED}\n"), login="", GH_TOKEN="stub")
	assert "reason=identity_unavailable" in proc.stderr
	# No precomputed login and no token: the reason names the missing credential.
	proc, _ = _run_prepare_helper(tmp_path, _issue(f"files_touched:\n  - {GRANTED}\n"), login="", GH_TOKEN="",
		GITHUB_TOKEN="", REVIEW_GITHUB_AI_GRANT_PIPELINE_LOGIN="")
	assert "reason=no_credentials" in proc.stderr and "OUT=\n" in proc.stdout
	# An open pipeline grant (no list) grants no .github/ai path.
	proc, _ = _run_prepare_helper(tmp_path, _issue("mentions .github/ai/x.json only", login="pipeline-bot",
		association="NONE"))
	assert "outcome=none" in proc.stderr and "OUT=\n" in proc.stdout


def test_prepare_helper_uses_the_precomputed_login_without_gh(tmp_path: Path) -> None:
	"""The prepare step has no GitHub token: the metadata step's login is used, and gh is not called."""
	issue = _issue(f"files_touched:\n  - {GRANTED}\n", login="pipeline-pat", association="NONE")
	proc, _ = _run_prepare_helper(tmp_path, issue, login="", GH_TOKEN="",
		REVIEW_GITHUB_AI_GRANT_PIPELINE_LOGIN="pipeline-pat")
	assert proc.returncode == 0, proc.stderr
	assert "REVIEW_GITHUB_AI_GRANT phase=prepare outcome=granted reason=granted paths=1" in proc.stderr
	assert not (tmp_path / "gh_calls.txt").exists()


def test_review_login_is_resolved_with_the_metadata_token_and_passed_to_prepare() -> None:
	workflow = (REPO_ROOT / ".github" / "workflows" / "review_autofix.yml").read_text(encoding="utf-8")
	metadata = "      - name: Collect PR metadata\n        id: collect_pr_metadata\n        env:\n          GH_TOKEN: ${{ secrets.GH_PAT }}\n"
	assert workflow.count(metadata) == 1
	deps_start = workflow.index("        id: deps_prepare\n")
	deps_run = workflow.index("        run: |\n", deps_start)
	login_env = "          REVIEW_GITHUB_AI_GRANT_PIPELINE_LOGIN: ${{ steps.collect_pr_metadata.outputs.github_ai_grant_pipeline_login || '' }}\n"
	assert deps_start < workflow.index(login_env) < deps_run
	# The PAT is never bound to the step that installs PR dependencies.
	assert "GH_TOKEN" not in workflow[deps_start:deps_run]
	collect = COLLECT_SCRIPT.read_text(encoding="utf-8")
	assert "printf 'github_ai_grant_pipeline_login=%s\\n' \"${_github_ai_grant_login}\" >> \"${GITHUB_OUTPUT}\"" in collect
	assert "github_ai_grant_pipeline_login=%s\\n' \"${_github_ai_grant_login}\" >> \"${GITHUB_ENV" not in collect
	assert 'pipeline_login="${REVIEW_GITHUB_AI_GRANT_PIPELINE_LOGIN:-}"' in SANDBOX_SCRIPT.read_text(encoding="utf-8")


def test_prepare_helper_switch_off(tmp_path: Path) -> None:
	proc, github_env = _run_prepare_helper(tmp_path, _issue(f"files_touched:\n  - {GRANTED}\n"), **{FLAG: "false"})
	assert "REVIEW_GITHUB_AI_GRANT" not in proc.stderr and "OUT=\n" in proc.stdout
	assert github_env.read_text(encoding="utf-8") == ""
	assert not (tmp_path / "gh_calls.txt").exists()


# --- contract greps -----------------------------------------------------------


def test_scripts_default_the_switch_on() -> None:
	commit = COMMIT_SCRIPT.read_text(encoding="utf-8")
	assert 'github_ai_grant_flag_raw="${GITHUB_AI_PLAN_GRANT_WRITE_ENABLED-}"' in commit
	assert "''|true) github_ai_grant_enabled=\"true\" ;;" in commit
	# The new block sits after the self-repo block and keeps ':!.github/ai'.
	assert commit.index("IMPLEMENT_SELF_REPO_GITHUB_AI_ADD path=") < commit.index("IMPLEMENT_GITHUB_AI_GRANT outcome=skipped")
	assert commit.index("IMPLEMENT_GITHUB_AI_GRANT outcome=skipped") < commit.index("# >>> automation-path grant guard (commit) >>>")
	assert "':!.github/ai'" in commit
	heal = HEAL_SCRIPT.read_text(encoding="utf-8")
	assert 'heal_github_ai_grant_switch="${GITHUB_AI_PLAN_GRANT_WRITE_ENABLED:-true}"' in heal
	assert 'snapshot_args+=(--granted-paths-file "${root}/github_ai_grants.json")' in heal
	assert heal.index("--granted-paths-file") < heal.index('review_untrusted_workspace.py" snapshot')
	sandbox = SANDBOX_SCRIPT.read_text(encoding="utf-8")
	assert 'local switch="${GITHUB_AI_PLAN_GRANT_WRITE_ENABLED-}"' in sandbox
	assert 'if [ "${action}" = prepare ]; then\n\t\treview_github_ai_grant_list="$(_review_github_ai_grant_prepare)"' in sandbox
	assert '"${snapshot_git_dir[@]}" "${review_github_ai_grant_args[@]}"' in sandbox
	root_export = sandbox.index("printf 'REVIEW_SANDBOX_ROOT=%s\\n' \"${root}\" >> \"${GITHUB_ENV:?}\"")
	grant_export = sandbox.index("printf 'REVIEW_GITHUB_AI_GRANT_PATHS_FILE=%s\\n' \"${review_github_ai_grant_args[1]:-}\"")
	assert root_export < grant_export < sandbox.index("trap - EXIT", root_export)
	assert sandbox.count("REVIEW_GITHUB_AI_GRANT_PATHS_FILE=%s") == 1
	review_commit = REVIEW_COMMIT.read_text(encoding="utf-8")
	assert review_commit.count('review_github_ai_grant_switch="${GITHUB_AI_PLAN_GRANT_WRITE_ENABLED-}"') == 2
	assert 'grep -Fxq -- "${created_file}"' in review_commit
	helper = WORKSPACE_HELPER.read_text(encoding="utf-8")
	assert 'GITHUB_AI_GRANT_FLAG = "GITHUB_AI_PLAN_GRANT_WRITE_ENABLED"' in helper


def test_collect_metadata_persists_author_fields() -> None:
	text = COLLECT_SCRIPT.read_text(encoding="utf-8")
	assert "closingIssuesReferences(first:50){nodes{number title body author{login} authorAssociation labels(first:50){nodes{name}}}}" in text
	assert text.count("author { login }\n            authorAssociation") == 2
	assert "author_login: (if (.author | type) == \"object\"" in text
	assert "author_association: (if (.authorAssociation | type) == \"string\"" in text


def test_collect_metadata_projection_is_additive() -> None:
	text = COLLECT_SCRIPT.read_text(encoding="utf-8")
	start = text.index("if printf '%s' \"${_linked_context_raw}\" | jq -c '")
	program_start = text.index("jq -c '", start) + len("jq -c '")
	program = text[program_start:text.index("' > \"${LINKED_ISSUES_RAW_FILE}\"", start)]
	data = [{"number": 7, "title": "t", "body": "b", "author": {"login": "octo"}, "authorAssociation": "OWNER",
		"labels": {"nodes": [{"name": "x"}]}},
		{"number": 8, "title": "t", "body": "b", "author": None, "labels": ["y"]}]
	result = json.loads(subprocess.run(["jq", "-c", program], input=json.dumps(data), capture_output=True,
		text=True, check=True).stdout)
	assert result == [
		{"number": 7, "title": "t", "body": "b", "labels": ["x"], "author_login": "octo", "author_association": "OWNER"},
		{"number": 8, "title": "t", "body": "b", "labels": ["y"], "author_login": "", "author_association": ""},
	]


def test_workflows_forward_the_switch_with_default() -> None:
	# GitHub forbids configuration variable names starting with GITHUB_ (actionlint
	# rejects them), so the env key reads the AI_PLAN_GRANT_WRITE_ENABLED variable.
	line = "  GITHUB_AI_PLAN_GRANT_WRITE_ENABLED: ${{ vars.AI_PLAN_GRANT_WRITE_ENABLED || 'true' }}\n"
	for name in ("implement.yml", "review_autofix.yml"):
		text = (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
		assert text.count(line) == 1, name
		assert "vars.GITHUB_" not in text, name
		env_start = text.index("\nenv:\n")
		assert env_start < text.index(line) < text.index("\njobs:\n"), name


def test_remedy_text_mentions_granted_github_ai_paths() -> None:
	text = (SCRIPTS / "workflow_failure_heal.py").read_text(encoding="utf-8")
	assert "named exactly in the linked issue's trusted `files_touched` grant is now admitted" in text


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq required")
def test_heal_jq_filter_drops_open_and_untrusted_grants(tmp_path: Path) -> None:
	heal = HEAL_SCRIPT.read_text(encoding="utf-8")
	program = re.search(r"jq -c '(\[select\(\.trusted == true[^']*)'", heal).group(1)
	for grant, expected in (
		(_grant([GRANTED, "scripts/a.sh"]), [GRANTED]),
		# A nested entry is dropped, not allowed to reject the whole list.
		(_grant([GRANTED, ".github/ai/sub/y.json"]), [GRANTED]),
		(_grant([GRANTED], association="CONTRIBUTOR"), []),
		(_grant([], author="pipeline-bot", association="NONE"), []),
	):
		out = subprocess.run(["jq", "-c", program], input=json.dumps(grant), capture_output=True, text=True, check=True)
		assert json.loads(out.stdout) == expected


# --- review_commit_changes.sh created-file filter ------------------------------


def _review_cleanup_block() -> str:
	text = REVIEW_COMMIT.read_text(encoding="utf-8")
	match = re.search(
		r'NEW_FILES_BEFORE_COMMIT_FILE="\$\(mktemp\)"\n.*?\nfi\nrm -f "\$\{NEW_FILES_BEFORE_COMMIT_FILE\}"\n',
		text, re.DOTALL)
	assert match, "new-file cleanup block not found"
	return match.group(0)


@pytest.mark.parametrize("switch", [None, "false"])
def test_review_commit_keeps_only_granted_github_ai_creation(tmp_path: Path, switch: str | None) -> None:
	repo = tmp_path / "repo"
	repo.mkdir()
	env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
	subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
	(repo / ".github" / "ai").mkdir(parents=True)
	(repo / GRANTED).write_text("{}\n", encoding="utf-8")
	(repo / ".github/ai/y.json").write_text("{}\n", encoding="utf-8")
	runtime = tmp_path / "runtime"
	runtime.mkdir()
	snapshot = runtime / "pre_editor_untracked.txt"
	snapshot.write_bytes(b"")
	grants = runtime / "grants.json"
	grants.write_text(json.dumps([GRANTED]), encoding="utf-8")
	env.update({"IS_WORKFLOW_SOURCE_REPO": "false", "RUNTIME_DIR": str(runtime),
		"PRE_EDITOR_UNTRACKED_FILE": str(snapshot), "REVIEW_GITHUB_AI_GRANT_PATHS_FILE": str(grants)})
	if switch is not None:
		env[FLAG] = switch
	proc = subprocess.run(["bash", "-c", "set -euo pipefail\n" + _review_cleanup_block()], cwd=repo,
		capture_output=True, text=True, env=env)
	assert proc.returncode == 0, proc.stdout + proc.stderr
	assert not (repo / ".github/ai/y.json").exists()
	assert "- .github/ai/y.json (pipeline artifact)" in proc.stdout
	if switch is None:
		assert (repo / GRANTED).exists(), proc.stdout
		assert f"REVIEW_GITHUB_AI_GRANT phase=commit outcome=kept path={GRANTED}" in proc.stdout
	else:
		assert not (repo / GRANTED).exists(), proc.stdout
		assert "REVIEW_GITHUB_AI_GRANT" not in proc.stdout
