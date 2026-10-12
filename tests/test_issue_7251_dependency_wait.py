"""Issue #7251 dependency wait (unblock fix-up #7257).

#7251 waits for PR #6667 to merge into the default branch. The helpers live
in scripts/gh_helpers.sh, the poller handler in
scripts/orchestrate_poll_process.sh, and the clarify / plan holds in their
workflows. Everything is behind ISSUE_7251_DEPENDENCY_WAIT_ENABLED (on by
default) and applies only to #7251 in shubhodeep1/coding-workflows.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GH_HELPERS = ROOT / "scripts" / "gh_helpers.sh"
POLLER = ROOT / "scripts" / "orchestrate_poll_process.sh"
SOURCE_REPO = "shubhodeep1/coding-workflows"
RELEASED = "<!-- ai:issue-7251-dependency-released:6667 -->"
PREFLIGHT = "<!-- ai:issue-7251-preflight-failed:6667 -->"
CLOSED = "<!-- ai:issue-7251-dependency-closed:6667 -->"
# The helpers parse GitHub JSON with jq, which every runner has; skip those
# cases like the poller's other jq-dependent tests when it is absent.
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq binary not available in test environment")

FAKE_GH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_GH_CALLS}"
if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then
  [ "${FAKE_GRAPHQL_RC:-0}" = "0" ] || exit 1
  cat "${FAKE_SNAPSHOT_FILE}"
  exit 0
fi
if [ "$1" = "api" ] && [ "$2" = "--paginate" ]; then
  printf '%s' "${FAKE_COMMENTS:-[]}"
  exit 0
fi
if [ "$1" = "api" ]; then
  for arg in "$@"; do
    case "${arg}" in
      body=*) printf '%s\n----\n' "${arg#body=}" >> "${FAKE_GH_POSTS}"; exit "${FAKE_POST_RC:-0}" ;;
    esac
  done
  exit 0
fi
exit 0
"""


def _bash(script: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
	full_env = {k: v for k, v in os.environ.items() if not k.startswith("ISSUE_7251_")}
	full_env.update(env or {})
	return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env, cwd=cwd, check=False)


def _fake_gh(tmp_path: Path) -> dict[str, str]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	return {
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_CALLS": str(tmp_path / "calls.txt"),
		"FAKE_GH_POSTS": str(tmp_path / "posts.txt"),
		"FAKE_SNAPSHOT_FILE": str(tmp_path / "snapshot.json"),
	}


def _snapshot(pr_state="OPEN", merged=False, base="main", default="main", issue_state="OPEN", comments=(), labels=("ai:clarification",), total=None):
	nodes = [{"body": body, "authorAssociation": assoc} for body, assoc in comments]
	return {
		"data": {
			"repository": {
				"defaultBranchRef": {"name": default},
				"pr": {"state": pr_state, "merged": merged, "isDraft": pr_state == "OPEN", "baseRefName": base},
				"issue": {
					"state": issue_state,
					"labels": {"nodes": [{"name": name} for name in labels]},
					"comments": {"totalCount": len(nodes) if total is None else total, "nodes": nodes},
				},
			}
		}
	}


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
	("value", "enabled", "warns"),
	[(None, True, False), ("", True, False), ("true", True, False), ("TRUE", True, False), ("True", True, False),
	 ("false", False, False), ("FALSE", False, False), ("yes", False, True), ("0", False, True)],
)
def test_flag_parse(value, enabled, warns):
	env = {} if value is None else {"ISSUE_7251_DEPENDENCY_WAIT_ENABLED": value}
	result = _bash(f'source "{GH_HELPERS}"; issue_7251_dependency_wait_enabled; echo "rc=$?"', env)
	assert f"rc={0 if enabled else 1}" in result.stdout
	assert ("::warning::ISSUE_7251_DEPENDENCY_WAIT_ENABLED=" in result.stderr) is warns


def test_flag_warning_value_is_sanitized():
	result = _bash(f'source "{GH_HELPERS}"; issue_7251_dependency_wait_enabled || true', {"ISSUE_7251_DEPENDENCY_WAIT_ENABLED": "on$(id)`x`;" + "a" * 60})
	line = [ln for ln in result.stderr.splitlines() if "::warning::" in ln][0]
	value = line.split("=", 1)[1].split(" ", 1)[0]
	assert re.fullmatch(r"[A-Za-z0-9_-]{0,32}", value), value


@pytest.mark.parametrize(
	("repo", "issue", "flag", "expected"),
	[(SOURCE_REPO, "7251", None, 0), ("other/repo", "7251", None, 1), (SOURCE_REPO, "7250", None, 1), (SOURCE_REPO, "7251", "false", 1)],
)
def test_applies_makes_no_api_call(tmp_path, repo, issue, flag, expected):
	env = _fake_gh(tmp_path)
	if flag is not None:
		env["ISSUE_7251_DEPENDENCY_WAIT_ENABLED"] = flag
	result = _bash(f'source "{GH_HELPERS}"; issue_7251_dependency_wait_applies "{repo}" "{issue}"; echo "rc=$?"', env)
	assert f"rc={expected}" in result.stdout
	assert not (tmp_path / "calls.txt").exists()


@needs_jq
@pytest.mark.parametrize(
	("snapshot", "expected"),
	[
		(_snapshot(), "open"),
		(_snapshot(merged=True, pr_state="MERGED"), "merged"),
		(_snapshot(merged=True, pr_state="MERGED", base="release"), "closed_unmerged"),
		(_snapshot(merged=True, pr_state="MERGED", default=""), "unverified"),
		(_snapshot(pr_state="CLOSED"), "closed_unmerged"),
		({"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pr": None}}}, "unverified"),
	],
)
def test_pr_state(snapshot, expected):
	payload = json.dumps(snapshot["data"]["repository"])
	result = subprocess.run(["bash", "-c", f'source "{GH_HELPERS}"; issue_7251_dependency_pr_state "$1"', "_", payload], capture_output=True, text=True, check=False)
	assert result.stdout.strip() == expected


@needs_jq
def test_pr_state_malformed_is_unverified():
	result = subprocess.run(["bash", "-c", f'source "{GH_HELPERS}"; issue_7251_dependency_pr_state "$1"', "_", "not json"], capture_output=True, text=True, check=False)
	assert result.stdout.strip() == "unverified"


@needs_jq
def test_gate_state_reads_one_snapshot_and_fails_unverified(tmp_path):
	env = _fake_gh(tmp_path)
	Path(env["FAKE_SNAPSHOT_FILE"]).write_text(json.dumps(_snapshot()), encoding="utf-8")
	script = f'source "{GH_HELPERS}"; gh_retry() {{ "$@"; }}; issue_7251_dependency_gate_state "{SOURCE_REPO}"'
	assert _bash(script, env).stdout.strip() == "open"
	assert len((tmp_path / "calls.txt").read_text().splitlines()) == 1
	env["FAKE_GRAPHQL_RC"] = "1"
	assert _bash(script, env).stdout.strip() == "unverified"


# ---------------------------------------------------------------- poller


def _poller_functions() -> str:
	text = POLLER.read_text(encoding="utf-8")
	start = text.index("# Issue #7251 dependency wait (unblock fix-up #7257)\n# ----")
	end = text.index("run_standalone_stall_recovery() {", start)
	return text[start:end]


def _origin_repo(tmp_path: Path, engine_text: str) -> Path:
	origin = tmp_path / "origin.git"
	work = tmp_path / "seed"
	checkout = tmp_path / "checkout"
	git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
	subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
	subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
	(work / "scripts").mkdir()
	(work / "scripts" / "ai_engine.sh").write_text(engine_text, encoding="utf-8")
	subprocess.run([*git, "-C", str(work), "add", "-A"], check=True)
	subprocess.run([*git, "-C", str(work), "commit", "-q", "-m", "seed"], check=True)
	subprocess.run(["git", "-C", str(work), "push", "-q", str(origin), "main"], check=True)
	subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
	return checkout


def _run_handler(tmp_path, snapshot, *, engine_text="_AI_ENGINE_EXIT_REFUSED\nai_engine_fallback_class\n", repo=SOURCE_REPO, extra_env=None):
	env = _fake_gh(tmp_path)
	env.update({"GITHUB_REPOSITORY": repo, "RUNTIME_DIR": str(tmp_path), "TG_LOG": str(tmp_path / "tg.txt")})
	env.update(extra_env or {})
	Path(env["FAKE_SNAPSHOT_FILE"]).write_text(json.dumps(snapshot), encoding="utf-8")
	checkout = _origin_repo(tmp_path, engine_text)
	script = "\n".join([
		f'source "{GH_HELPERS}"',
		'gh_retry() { "$@"; }',
		'ensure_label_exists() { :; }',
		'tg_notify_issue() { printf "%s|%s\\n" "$2" "$3" >> "${TG_LOG}"; }',
		_poller_functions(),
		"run_issue_7251_dependency_wait",
		'echo "HOLD=$(cat "${RUNTIME_DIR}/issue_7251_dependency_wait_hold")"',
		"issue_7251_dependency_wait_held && echo HELD=yes || echo HELD=no",
	])
	result = _bash(script, env, cwd=checkout)
	posts = (tmp_path / "posts.txt").read_text() if (tmp_path / "posts.txt").exists() else ""
	calls = (tmp_path / "calls.txt").read_text() if (tmp_path / "calls.txt").exists() else ""
	tg = (tmp_path / "tg.txt").read_text() if (tmp_path / "tg.txt").exists() else ""
	return result, posts, calls, tg


@needs_jq
def test_open_holds_and_posts_nothing(tmp_path):
	result, posts, calls, tg = _run_handler(tmp_path, _snapshot())
	assert "STALL_SKIP issue=7251 reason=dependency_wait detail=open action=none" in result.stdout
	assert "HELD=yes" in result.stdout
	assert posts == "" and tg == ""
	assert calls.count("api graphql") == 1


@needs_jq
def test_snapshot_failure_is_unverified_and_holds(tmp_path):
	result, posts, _calls, _tg = _run_handler(tmp_path, _snapshot(), extra_env={"FAKE_GRAPHQL_RC": "1"})
	assert "detail=unverified action=none" in result.stdout
	assert "HELD=yes" in result.stdout
	assert posts == ""


@needs_jq
def test_merged_with_preflight_passing_posts_one_reclarify(tmp_path):
	result, posts, _calls, tg = _run_handler(tmp_path, _snapshot(pr_state="MERGED", merged=True))
	assert "detail=merged action=reclarify" in result.stdout
	assert "HELD=no" in result.stdout
	assert posts == f"/reclarify\n{RELEASED}\n----\n"
	assert tg == ""


@needs_jq
def test_merged_with_preflight_failing_reports_once(tmp_path):
	result, posts, _calls, tg = _run_handler(tmp_path, _snapshot(pr_state="MERGED", merged=True), engine_text="_AI_ENGINE_EXIT_REFUSED\n")
	assert "detail=merged action=none preflight=failed missing=ai_engine_fallback_class" in result.stdout
	assert posts.startswith(PREFLIGHT)
	assert "`ai_engine_fallback_class`" in posts
	assert "/reclarify" not in posts
	assert tg.count("|WARNING") == 1
	assert "HELD=no" in result.stdout


@needs_jq
def test_merged_preflight_report_failure_sends_no_alert(tmp_path):
	result, _posts, _calls, tg = _run_handler(tmp_path, _snapshot(pr_state="MERGED", merged=True), engine_text="nothing\n", extra_env={"FAKE_POST_RC": "1"})
	assert "report=failed" in result.stdout
	assert tg == ""
	assert "HELD=yes" in result.stdout


@needs_jq
def test_closed_unmerged_reblocks_once(tmp_path):
	result, posts, calls, _tg = _run_handler(tmp_path, _snapshot(pr_state="CLOSED"))
	assert "detail=closed_unmerged action=reblocked" in result.stdout
	assert "labels[]=ai:blocked" in calls
	assert "--remove-label ai:clarification" in calls
	assert posts.startswith(CLOSED)
	assert "HELD=no" in result.stdout


@needs_jq
@pytest.mark.parametrize(
	("snapshot", "marker"),
	[
		(_snapshot(pr_state="MERGED", merged=True), f"/reclarify\n{RELEASED}"),
		(_snapshot(pr_state="MERGED", merged=True), f"{PREFLIGHT}\nreport"),
		(_snapshot(pr_state="CLOSED"), f"{CLOSED}\nreblocked"),
	],
)
def test_trusted_marker_dedups_and_untrusted_is_ignored(tmp_path, snapshot, marker):
	trusted = json.loads(json.dumps(snapshot))
	trusted["data"]["repository"]["issue"]["comments"] = {"totalCount": 1, "nodes": [{"body": marker, "authorAssociation": "MEMBER"}]}
	(tmp_path / "trusted").mkdir()
	result, posts, calls, _tg = _run_handler(tmp_path / "trusted", trusted)
	assert posts == ""
	assert "labels[]=ai:blocked" not in calls
	assert "action=none" in result.stdout
	untrusted = json.loads(json.dumps(snapshot))
	untrusted["data"]["repository"]["issue"]["comments"] = {"totalCount": 1, "nodes": [{"body": marker, "authorAssociation": "NONE"}]}
	(tmp_path / "untrusted").mkdir()
	result, posts, _calls, _tg = _run_handler(tmp_path / "untrusted", untrusted)
	assert posts != ""


@needs_jq
def test_long_history_reads_paginated_comments(tmp_path):
	snapshot = _snapshot(pr_state="MERGED", merged=True, total=150)
	comments = json.dumps([{"body": f"/reclarify\n{RELEASED}", "author_association": "OWNER"}])
	result, posts, calls, _tg = _run_handler(tmp_path, snapshot, extra_env={"FAKE_COMMENTS": comments})
	assert "api --paginate repos/shubhodeep1/coding-workflows/issues/7251/comments" in calls
	assert posts == ""
	assert "detail=merged action=none" in result.stdout


@needs_jq
def test_closed_issue_is_released(tmp_path):
	result, posts, _calls, _tg = _run_handler(tmp_path, _snapshot(issue_state="CLOSED"))
	assert "detail=issue_closed action=none" in result.stdout
	assert posts == "" and "HELD=no" in result.stdout


@needs_jq
@pytest.mark.parametrize("extra", [{"GITHUB_REPOSITORY": "other/repo"}, {"ISSUE_7251_DEPENDENCY_WAIT_ENABLED": "false"}])
def test_other_repo_or_flag_off_makes_no_call(tmp_path, extra):
	repo = extra.get("GITHUB_REPOSITORY", SOURCE_REPO)
	result, posts, calls, _tg = _run_handler(tmp_path, _snapshot(), repo=repo, extra_env={k: v for k, v in extra.items() if k != "GITHUB_REPOSITORY"})
	assert calls == "" and posts == ""
	assert "HELD=no" in result.stdout


def test_poller_call_sites_and_skip_points():
	text = POLLER.read_text(encoding="utf-8")
	sweep = text[text.index('if _is_truthy "${UNBLOCK_SCAN_SWEEP_ONLY:-false}"; then'):]
	assert sweep.index("run_issue_7251_dependency_wait || true") < sweep.index("run_unblock_scan ||")
	main_tail = text[text.rindex("\nrun_standalone_stall_recovery\n") - 200:]
	assert "run_issue_7251_dependency_wait || true\n\nrun_standalone_stall_recovery" in main_tail
	stall = text[text.index("run_standalone_stall_recovery() {"):]
	assert stall.index("issue_7251_dependency_wait_held") < stall.index("security_dependency.py security-dependency")
	assert stall.index("issue_7251_dependency_wait_held") < stall.index('if [ "${has_pipeline_label}" -eq 0 ]')
	scan = text[text.index("run_unblock_scan() {"):text.index("run_unblock_scan() {") + 6000]
	assert "outcome=skip reason=issue_7251_dependency_wait" in scan


# ---------------------------------------------------------------- workflows


def _step(workflow: str, name: str) -> str:
	text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
	start = text.index(f"- name: {name}\n")
	end = text.index("\n      - name:", start + 1)
	return text[start:end]


def test_clarify_gate_contract():
	step = _step("clarify.yml", "Decide clarify route")
	assert "ISSUE_7251_DEPENDENCY_WAIT_ENABLED: ${{ vars.ISSUE_7251_DEPENDENCY_WAIT_ENABLED || 'true' }}" in step
	assert "AI_PHASE_GATE_V1 phase=clarify gate=issue_7251_dependency_wait reason=pr_6667_unmerged outcome=skip" in step
	gate = step[step.index('ISSUE_7251_HELD="false"'):step.index("DEPENDENCY_VERDICT=")]
	assert "labels" not in gate.replace("No labels are written", "")
	assert '[ "${ISSUE_7251_HELD}" != "true" ]' in step[step.index("orchestrator_fast_path=true") - 400:]


def test_plan_gate_contract():
	step = _step("plan.yml", "Validate planning phase label")
	assert "ISSUE_7251_DEPENDENCY_WAIT_ENABLED: ${{ vars.ISSUE_7251_DEPENDENCY_WAIT_ENABLED || 'true' }}" in step
	gate_line = "AI_PHASE_GATE_V1 phase=plan gate=issue_7251_dependency_wait reason=pr_6667_unmerged outcome=skip"
	assert gate_line in step
	assert step.index(gate_line) < step.index("--add-label 'ai:planning'")
	assert step.index("reason=issue_closed") < step.index(gate_line)


def test_poll_workflow_forwards_flag_to_both_poller_steps():
	for name in ("Process each tracking issue", "Run unblock scan without active projects"):
		assert "ISSUE_7251_DEPENDENCY_WAIT_ENABLED: ${{ vars.ISSUE_7251_DEPENDENCY_WAIT_ENABLED || 'true' }}" in _step("orchestrate_poll.yml", name), name
