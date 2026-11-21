"""Tests for scripts/heal_port_to_default.sh and `workflow_failure_heal.py heal-port`
(P6 step 2, decision D4, of docs/plans/heal-evidence-reproduction-and-trends-plan.md; Refs #8434).

The script runs against a fake `gh` that serves one GraphQL lookup from a JSON
state file and records every call and created issue, and against a temporary
bare "origin" repository with `main` and `stable` branches.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "heal_port_to_default.sh"
HEAL_PY = REPO_ROOT / "scripts" / "workflow_failure_heal.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"
REMOVAL_REGISTRY = REPO_ROOT / "docs" / "scripts-pending-removal.md"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import workflow_failure_heal as heal  # noqa: E402

SELF_REPO = "shubhodeep1/coding-workflows"
LOGIN = "workflow-bot"
HEAL_ISSUE = 41
PR = 77
ROOT = "e" * 64
PARENT_FP = "f" * 64
AUTO_CLOSE_RE = re.compile(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#[0-9]")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
	json.dump(state, open(state_path, "w"))
def fail():
	save()
	sys.stderr.write("HTTP 500\n")
	sys.exit(1)
fails = set(state.get("fail", []))
if args[:2] == ["api", "graphql"]:
	if "graphql" in fails:
		fail()
	response = json.loads(json.dumps(state["graphql"]))
	nodes = list(state.get("search_nodes", []))
	for created in state.get("created", []):
		nodes.append({"number": created["number"], "body": created["body"], "author": {"login": state["login"]}})
	response["data"]["search"] = {"nodes": nodes}
	save()
	print(json.dumps(response))
	sys.exit(0)
if args[:3] == ["api", "-X", "POST"]:
	if "create" in fails:
		fail()
	fields = {}
	labels = []
	index = 3
	while index < len(args):
		if args[index] in ("-f", "-F"):
			key, _, value = args[index + 1].partition("=")
			if args[index] == "-F" and value.startswith("@"):
				value = open(value[1:]).read()
			if key == "labels[]":
				labels.append(value)
			else:
				fields[key] = value
			index += 2
		else:
			index += 1
	number = 123 + len(state.get("created", []))
	state.setdefault("created", []).append({"number": number, "title": fields.get("title"), "body": fields.get("body"), "labels": labels, "path": args[3]})
	save()
	print(number)
	sys.exit(0)
fail()
'''


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
	return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _git_env(tmp_path: Path) -> dict:
	env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "WORKFLOW_HEAL", "GH_RETRY", "HEAL_"))}
	config = tmp_path / "gitconfig"
	config.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
	env.update({"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"})
	return env


def _write(root: Path, rel: str, text: str) -> None:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text)


def _origin(tmp_path: Path, *, conflict: bool = False, empty: bool = False) -> tuple[Path, str]:
	"""Bare origin with main and stable; return (work repo, stable merge sha)."""
	env = _git_env(tmp_path)
	seed = tmp_path / "seed"
	seed.mkdir()
	_git(seed, "init", "-q", "-b", "main", env=env)
	_write(seed, "scripts/a.sh", "one\ntwo\nthree\n")
	_write(seed, "scripts/b.py", "x = 1\n")
	_write(seed, "docs/c.md", "doc\n")
	_git(seed, "add", "-A", env=env)
	_git(seed, "commit", "-q", "-m", "base", env=env)
	_git(seed, "checkout", "-q", "-b", "stable", env=env)
	if empty:
		_git(seed, "commit", "-q", "--allow-empty", "-m", "empty heal", env=env)
	else:
		_write(seed, "scripts/a.sh", "one\nTWO\nthree\n")
		_write(seed, "scripts/b.py", "x = 2\n")
		_write(seed, "tests/test_a.py", "def test_a():\n\tassert True\n")
		_git(seed, "add", "-A", env=env)
		_git(seed, "commit", "-q", "-m", "heal (squash)", env=env)
	merge_sha = _git(seed, "rev-parse", "HEAD", env=env)
	_git(seed, "checkout", "-q", "main", env=env)
	if conflict:
		_write(seed, "scripts/a.sh", "one\nzwei\nthree\n")
		_git(seed, "commit", "-qam", "main moved", env=env)
	else:
		_write(seed, "docs/c.md", "doc 2\n")
		_git(seed, "commit", "-qam", "main moved elsewhere", env=env)
	origin = tmp_path / "origin.git"
	_git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin), env=env)
	_git(origin, "config", "uploadpack.allowAnySHA1InWant", "true", env=env)
	work = tmp_path / "work"
	work.mkdir()
	_git(work, "init", "-q", "-b", "main", env=env)
	_git(work, "remote", "add", "origin", f"file://{origin}", env=env)
	return work, merge_sha


def _parent_body(*, scope: bool = True, fp_marker: bool = True) -> str:
	lines = []
	if fp_marker:
		lines += [
			f"<!-- workflow-failure-heal:fp={PARENT_FP} -->",
			"<!-- workflow-failure-heal:gen=2 -->",
			f"<!-- workflow-failure-heal:root={ROOT} -->",
			f"<!-- workflow-failure-heal:source={SELF_REPO}#run -->",
			"<!-- workflow-failure-heal:classification=workflow-defect -->",
		]
	lines += ["", "## Workflow failure", "", "- **Target branch:** `stable`", ""]
	if scope:
		lines.append(f"<!-- ai:workflow-heal-scope:v1 paths=scripts/a.sh,tests/**,changelog.d/*.md runs={SELF_REPO}:777 -->")
	return "\n".join(lines) + "\n"


def _state(*, labels: list[str] | None = None, body: str | None = None, title: str = "[workflow-heal] CI: Run tests failed",
	search_nodes: list | None = None, issue_present: bool = True, **extra) -> dict:
	issue = {
		"number": HEAL_ISSUE, "title": title, "body": _parent_body() if body is None else body,
		"lastEditedAt": None, "author": {"login": LOGIN},
		"labels": {"nodes": [{"name": name} for name in (["ai:workflow-heal"] if labels is None else labels)]},
	}
	state = {
		"login": LOGIN,
		"graphql": {"data": {"viewer": {"login": LOGIN}, "repository": {"issue": issue if issue_present else None}}},
		"search_nodes": search_nodes or [],
	}
	state.update(extra)
	return state


def _execute(tmp_path: Path, state: dict, work: Path | None = None, merge_sha: str = "a" * 40, **env_overrides):
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	(bin_dir / "gh").write_text(FAKE_GH)
	(bin_dir / "gh").chmod(0o755)
	state_file = tmp_path / "state.json"
	if not state_file.exists():
		state_file.write_text(json.dumps(state))
	if work is None:
		work = tmp_path / "empty-work"
		work.mkdir(exist_ok=True)
		_git(work, "init", "-q", env=_git_env(tmp_path))
	env = _git_env(tmp_path)
	env.update({
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_STATE": str(state_file),
		"WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED": "true",
		"REPOSITORY": SELF_REPO, "GITHUB_REPOSITORY": SELF_REPO,
		"GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "4242",
		"PR_NUMBER": str(PR), "PR_BASE_REF": "stable", "PR_HEAD_REF": f"ai/issue-{HEAL_ISSUE}",
		"PR_HEAD_REPO": SELF_REPO, "PR_MERGE_COMMIT_SHA": merge_sha, "PR_DEFAULT_BRANCH": "main",
		"RUNNER_TEMP": str(tmp_path), "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1",
		"GH_RETRY_MAX_ATTEMPTS": "1",
	})
	env.update(env_overrides)
	for key in [key for key, value in env.items() if value is None]:
		env.pop(key)
	result = subprocess.run(["bash", str(SCRIPT)], cwd=work, env=env, capture_output=True, text=True, timeout=120)
	return result, json.loads(state_file.read_text())


def _graphql_calls(state: dict) -> list:
	return [call for call in state.get("calls", []) if call[:2] == ["api", "graphql"]]


def _post_calls(state: dict) -> list:
	return [call for call in state.get("calls", []) if call[:3] == ["api", "-X", "POST"]]


def test_applying_patch_files_exactly_one_port_issue(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(), work, merge_sha)
	assert result.returncode == 0, result.stderr
	assert f"WORKFLOW_HEAL port outcome=filed heal={HEAL_ISSUE} pr={PR} issue=123" in result.stdout
	assert len(_graphql_calls(state)) == 1
	assert len(_post_calls(state)) == 1
	created = state["created"]
	assert len(created) == 1
	issue = created[0]
	assert issue["path"] == f"repos/{SELF_REPO}/issues"
	assert issue["labels"] == ["ai:workflow-heal"]
	assert issue["title"].startswith("[port to main] ")
	body = issue["body"]
	fp = heal.heal_port_fingerprint(SELF_REPO, HEAL_ISSUE)
	assert body.split("\n")[0] == f"<!-- workflow-failure-heal:fp={fp} -->"
	assert "<!-- workflow-failure-heal:gen=2 -->" in body
	assert f"<!-- workflow-failure-heal:root={ROOT} -->" in body
	assert f"<!-- workflow-failure-heal:source={SELF_REPO}#{HEAL_ISSUE} -->" in body
	assert f"- **Merge commit:** `{merge_sha}`" in body
	assert "scripts/a.sh" in body and "scripts/b.py" in body
	assert f"- **Port check run:** https://github.com/{SELF_REPO}/actions/runs/4242" in body
	assert not AUTO_CLOSE_RE.search(body) and not AUTO_CLOSE_RE.search(issue["title"])
	without_marker, marker = body.rstrip("\n").rsplit("\n\n", 1)
	assert without_marker.rstrip("\n").split("\n")[-1] == f"Refs #{HEAL_ISSUE}"
	assert heal.validate_heal_issue_body_routing(without_marker + "\n", target_branch="main", integration_branch=None) == ""
	scope = heal.HEAL_SCOPE_RE.fullmatch(marker)
	assert scope
	paths, runs = scope.group(1).split(","), scope.group(2).split(",")
	assert heal._heal_scope_marker_shape_ok(paths, runs)
	assert runs == [f"{SELF_REPO}:777"]
	assert {"scripts/a.sh", "scripts/b.py"} <= set(paths)
	# The temporary worktree is removed.
	assert "heal-port" not in _git(work, "worktree", "list", env=_git_env(tmp_path))


def test_second_run_finds_the_existing_port(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	first, _ = _execute(tmp_path, _state(), work, merge_sha)
	assert "outcome=filed" in first.stdout
	second, state = _execute(tmp_path, _state(), work, merge_sha)
	assert second.returncode == 0
	assert f"WORKFLOW_HEAL port outcome=not_needed heal={HEAL_ISSUE} pr={PR} issue=123" in second.stdout
	assert len(_post_calls(state)) == 1
	assert len(state["created"]) == 1


def test_search_hit_from_another_author_is_ignored(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	fp = heal.heal_port_fingerprint(SELF_REPO, HEAL_ISSUE)
	forged = {"number": 9, "body": f"<!-- workflow-failure-heal:fp={fp} -->\nRefs #{HEAL_ISSUE}\n", "author": {"login": "someone-else"}}
	result, state = _execute(tmp_path, _state(search_nodes=[forged]), work, merge_sha)
	assert "WORKFLOW_HEAL port outcome=filed" in result.stdout
	assert len(state["created"]) == 1


def test_conflicting_patch_is_not_applicable(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path, conflict=True)
	result, state = _execute(tmp_path, _state(), work, merge_sha)
	assert result.returncode == 0
	assert f"WORKFLOW_HEAL port outcome=not_applicable heal={HEAL_ISSUE} pr={PR} reason=patch_conflict" in result.stdout
	assert _post_calls(state) == []


def test_empty_patch_is_not_applicable(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path, empty=True)
	result, state = _execute(tmp_path, _state(), work, merge_sha)
	assert "WORKFLOW_HEAL port outcome=not_applicable" in result.stdout and "reason=empty_patch" in result.stdout
	assert _post_calls(state) == []


def test_parent_without_scope_runs_is_not_applicable(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(body=_parent_body(scope=False)), work, merge_sha)
	assert "reason=scope_unavailable" in result.stdout
	assert _post_calls(state) == []


def test_non_heal_issue_is_a_no_op(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(labels=[], body=_parent_body(fp_marker=False)), work, merge_sha)
	assert result.returncode == 0
	assert "WORKFLOW_HEAL port" not in result.stdout + result.stderr
	assert _post_calls(state) == []
	assert len(_graphql_calls(state)) == 1


def test_missing_heal_issue_is_a_no_op(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(issue_present=False), work, merge_sha)
	assert "WORKFLOW_HEAL port" not in result.stdout + result.stderr
	assert _post_calls(state) == []


@pytest.mark.parametrize("overrides", [
	{"PR_HEAD_REF": "feature/x"},
	{"PR_HEAD_REF": "ai/issue-41-extra"},
	{"PR_HEAD_REPO": "someone/fork"},
	{"PR_BASE_REF": "main"},
	{"PR_DEFAULT_BRANCH": "stable"},
	{"PR_MERGE_COMMIT_SHA": "not-a-sha"},
])
def test_non_heal_branch_fork_or_other_base_costs_no_calls(tmp_path: Path, overrides) -> None:
	result, state = _execute(tmp_path, _state(), **overrides)
	assert result.returncode == 0
	assert state.get("calls", []) == []
	assert "WORKFLOW_HEAL port" not in result.stdout + result.stderr


@pytest.mark.parametrize("value", [None, "false", "TRUE ", "1", "yes"])
def test_switch_off_does_nothing(tmp_path: Path, value) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(), work, merge_sha, WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED=value)
	assert result.returncode == 0
	assert result.stdout == "" and result.stderr == ""
	assert state.get("calls", []) == []
	assert not (work / ".git" / "FETCH_HEAD").exists()


def test_switch_is_case_insensitive(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, _ = _execute(tmp_path, _state(), work, merge_sha, WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED="TRUE")
	assert "WORKFLOW_HEAL port outcome=filed" in result.stdout


def test_lookup_failure_warns_and_exits_zero(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(fail=["graphql"]), work, merge_sha)
	assert result.returncode == 0
	assert "::warning::WORKFLOW_HEAL port error reason=lookup_failed" in result.stdout
	assert _post_calls(state) == []


def test_create_failure_warns_and_exits_zero(tmp_path: Path) -> None:
	work, merge_sha = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(fail=["create"]), work, merge_sha)
	assert result.returncode == 0
	assert "::warning::WORKFLOW_HEAL port error reason=create_failed" in result.stdout
	assert len(_post_calls(state)) == 1


def test_fetch_failure_warns_and_exits_zero(tmp_path: Path) -> None:
	work, _ = _origin(tmp_path)
	result, state = _execute(tmp_path, _state(), work, "b" * 40)
	assert result.returncode == 0
	assert "::warning::WORKFLOW_HEAL port error reason=fetch_failed" in result.stdout
	assert _post_calls(state) == []


# --- heal-port helper -------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run([sys.executable, "-B", str(HEAL_PY), "heal-port", *args], capture_output=True, text=True,
		env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def test_fingerprint_is_stable_hex() -> None:
	first = _cli("fingerprint", "--repo", SELF_REPO, "--heal-issue", "41")
	second = _cli("fingerprint", "--repo", SELF_REPO, "--heal-issue", "41")
	assert first.returncode == 0
	assert re.fullmatch(r"[0-9a-f]{64}\n", first.stdout)
	assert first.stdout == second.stdout
	assert first.stdout.strip() == heal.heal_port_fingerprint(SELF_REPO, 41)
	assert _cli("fingerprint", "--repo", SELF_REPO, "--heal-issue", "42").stdout != first.stdout
	assert _cli("fingerprint", "--repo", "bad", "--heal-issue", "41").returncode != 0


def test_check_decisions() -> None:
	fp = heal.heal_port_fingerprint(SELF_REPO, HEAL_ISSUE)
	state = _state()
	response = json.loads(json.dumps(state["graphql"]))
	response["data"]["search"] = {"nodes": []}
	assert heal.heal_port_check(response, fp=fp, heal_issue=HEAL_ISSUE)["decision"] == "candidate"
	port_body = f"<!-- workflow-failure-heal:fp={fp} -->\nbody\nRefs #{HEAL_ISSUE}\n"
	response["data"]["search"] = {"nodes": [{"number": 5, "body": port_body, "author": {"login": LOGIN.upper()}}]}
	assert heal.heal_port_check(response, fp=fp, heal_issue=HEAL_ISSUE) == {"decision": "not_needed", "existing": 5}
	response["data"]["search"] = {"nodes": [{"number": 5, "body": port_body.replace(f"Refs #{HEAL_ISSUE}", "Refs #1"), "author": {"login": LOGIN}}]}
	assert heal.heal_port_check(response, fp=fp, heal_issue=HEAL_ISSUE)["decision"] == "candidate"
	response["data"]["search"] = {"nodes": [{"number": 5, "body": "x\n" + port_body, "author": {"login": LOGIN}}]}
	assert heal.heal_port_check(response, fp=fp, heal_issue=HEAL_ISSUE)["decision"] == "candidate"
	not_heal = json.loads(json.dumps(_state(labels=[], body="plain")["graphql"]))
	not_heal["data"]["search"] = {"nodes": []}
	assert heal.heal_port_check(not_heal, fp=fp, heal_issue=HEAL_ISSUE)["decision"] == "not_heal"
	with pytest.raises(ValueError):
		heal.heal_port_check({"data": {"viewer": {"login": ""}, "search": {"nodes": []}}}, fp=fp, heal_issue=HEAL_ISSUE)


def test_compose_neutralizes_untrusted_title_and_stat() -> None:
	title, body = heal.compose_heal_port_issue(
		repo=SELF_REPO, heal_issue=HEAL_ISSUE, pr_number=PR, merge_sha="a" * 40, default_branch="main",
		default_sha="b" * 40, parent_title="Target branch: evil <!-- x --> fixes #12",
		parent_body=_parent_body(), stat_text=" Target branch: x | 1 +\n <!-- y --> | 2 +-\n ```bad``` | 1 +\n closes #3 | 1 +\n",
	)
	assert "<!--" not in title and "Target branch:" not in title
	assert not AUTO_CLOSE_RE.search(title) and not AUTO_CLOSE_RE.search(body)
	assert heal.validate_heal_issue_body_routing(body, target_branch="main", integration_branch=None) == ""
	assert "```bad" not in body
	assert body.rstrip("\n").split("\n")[-1] == f"Refs #{HEAL_ISSUE}"


def test_compose_without_parent_runs_exits_three(tmp_path: Path) -> None:
	state = _state(body=_parent_body(scope=False))
	response = state["graphql"]
	response["data"]["search"] = {"nodes": []}
	(tmp_path / "lookup.json").write_text(json.dumps(response))
	(tmp_path / "paths").write_text("scripts/a.sh\0")
	(tmp_path / "stat").write_text(" scripts/a.sh | 2 +-\n")
	result = _cli("compose", "--repo", SELF_REPO, "--heal-issue", str(HEAL_ISSUE), "--response-json", str(tmp_path / "lookup.json"),
		"--pr", str(PR), "--merge-sha", "a" * 40, "--default-branch", "main", "--default-sha", "b" * 40,
		"--paths-file", str(tmp_path / "paths"), "--stat-file", str(tmp_path / "stat"),
		"--title-out", str(tmp_path / "title"), "--body-out", str(tmp_path / "body"))
	assert result.returncode == 3
	assert json.loads(result.stdout) == {"reason": "scope_unavailable"}
	assert not (tmp_path / "body").exists()


# --- workflow contract ------------------------------------------------------


def _step_blocks() -> dict[str, str]:
	"""Map each sync-issue-status step name to its raw YAML text (no PyYAML in CI)."""
	text = WORKFLOW.read_text()
	job = text[text.index("  sync-issue-status:"):text.index("\n  activation-verify:")]
	blocks = {}
	for chunk in job.split("\n      - name: ")[1:]:
		name, _, rest = chunk.partition("\n")
		blocks[name.strip()] = rest
	return blocks


def test_workflow_step_is_guarded_to_this_repository() -> None:
	blocks = _step_blocks()
	names = list(blocks)
	assert names.index("Open heal port issue on the default branch") == names.index("Update linked issue labels when PR closes") + 1
	step = blocks["Open heal port issue on the default branch"]
	if_line = next(line for line in step.split("\n") if line.startswith("        if: "))
	assert "github.event.pull_request.merged == true" in if_line
	assert "github.repository == 'shubhodeep1/coding-workflows'" in if_line
	assert "\n        continue-on-error: true\n" in step
	assert "          WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED: ${{ vars.WORKFLOW_HEAL_CODE_REF_ROUTING_ENABLED || 'false' }}\n" in step
	assert "          PR_MERGE_COMMIT_SHA: ${{ github.event.pull_request.merge_commit_sha || '' }}\n" in step
	run = step[step.index("        run: |\n"):]
	assert "${{" not in run
	assert "HEAL_PORT_READY" in run and "bash scripts/heal_port_to_default.sh" in run


def test_workflow_fetch_is_optional_and_repository_guarded() -> None:
	fetch = _step_blocks()["Fetch memory helper scripts"]
	block_start = fetch.index('if [ "${{ github.repository }}" = "${wf_source}" ]; then\n            HEAL_PORT_READY=1')
	block = fetch[block_start:fetch.index("fi\n", fetch.index("done", block_start)) + 3]
	assert 'for heal_port_file in heal_port_to_default.sh workflow_failure_heal.py; do' in block
	assert 'fetch_from_ref_or_local "scripts/${heal_port_file}" "scripts/${heal_port_file}" "false"' in block
	assert "::warning::scripts/${heal_port_file} unavailable; the heal port step will skip." in block
	assert 'echo "HEAL_PORT_READY=${HEAL_PORT_READY}" >> "$GITHUB_ENV"' in block
	fetched = re.findall(r'_fetched_scripts\+=\((.*?)\)', fetch)
	assert not any("heal_port" in entry or "workflow_failure_heal" in entry for entry in fetched)


def test_removal_registry_entry() -> None:
	text = REMOVAL_REGISTRY.read_text()
	entry = text[text.index("### `scripts/heal_port_to_default.sh`"):]
	assert "- **Type:** long-running" in entry
	assert "permanent — review annually" in entry
	assert "grep -rn heal_port_to_default .github/workflows scripts" in entry
	assert "- **Owner:** @shubhodeep1" in entry
