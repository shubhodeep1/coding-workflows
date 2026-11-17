"""Pull-request dependency hold (issue #7228): scripts/pr_dependency.py.

Mirrors tests/test_security_dependency.py. Clarify, plan, both implement
gates and the standalone stall poller hold a trusted standalone issue that
declares `- Depends on PR: #N` and `- Requires on main: <path> contains <s>`
until PR #N is merged into the default branch and the string is on it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pr_dependency.py"
SECURITY_SCRIPT = ROOT / "scripts" / "security_dependency.py"
REPO = "shubhodeep1/digital_pa"
DECLARATION = "- Depends on PR: #8\n- Requires on main: scripts/ai_engine.sh contains _AI_ENGINE_EXIT_REFUSED\n"


def _load(path: Path, name: str):
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


dep = _load(SCRIPT, "pr_dependency")


def _issue(body=DECLARATION, labels=(), number=9, association="OWNER", login="maintainer"):
	return {
		"number": number,
		"title": "Feature track",
		"body": body,
		"labels": [{"name": name} for name in labels],
		"user": {"login": login, "type": "User"},
		"author_association": association,
		"repository_url": f"https://api.github.com/repos/{REPO}",
	}


def _pull(number=8, merged=True, ref="main", default_branch="main", full_name=REPO):
	return {
		"number": number,
		"merged": merged,
		"merged_at": "2026-10-12T00:00:00Z" if merged else None,
		"base": {"ref": ref, "repo": {"full_name": full_name, "default_branch": default_branch}},
	}


def _decl():
	return dep.pr_dependency_declaration(_issue())


def _cli(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, "-I", "-B", str(SCRIPT), *args],
		capture_output=True,
		text=True,
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
		check=False,
	)


def test_declaration_parses_plain_and_backticked_forms():
	assert _decl() == {"pr": 8, "path": "scripts/ai_engine.sh", "needle": "_AI_ENGINE_EXIT_REFUSED"}
	quoted = "- Depends on PR: #8\n- Requires on main: `scripts/ai_engine.sh` contains `_AI_ENGINE_EXIT_REFUSED`\n"
	assert dep.pr_dependency_declaration(_issue(quoted)) == _decl()
	assert dep.pr_dependency_declaration(_issue("nothing declared")) is None
	assert dep.pr_dependency_declaration(_issue(DECLARATION.replace("\n", "\r\n"))) == _decl()
	assert dep.pr_dependency_declaration(_issue("- Depends on: #8")) is None


@pytest.mark.parametrize(
	("body", "reason"),
	[
		("- Depends on PR: #8\n", "malformed_declaration"),
		("- Requires on main: a.sh contains x\n", "malformed_declaration"),
		(DECLARATION + "- Depends on PR: #7\n", "repeated_or_malformed_declaration"),
		(DECLARATION + "- Requires on main: b.sh contains y\n", "repeated_or_malformed_declaration"),
		(DECLARATION.replace("#8", "#0"), "repeated_or_malformed_declaration"),
		(DECLARATION.replace("#8", "#abc"), "repeated_or_malformed_declaration"),
		(DECLARATION.replace("#8", "#9"), "self_reference"),
		(DECLARATION.replace("scripts/ai_engine.sh", "../x"), "invalid_path"),
		(DECLARATION.replace("scripts/ai_engine.sh", "/abs"), "invalid_path"),
		(DECLARATION.replace("scripts/ai_engine.sh", "a//b"), "invalid_path"),
		(DECLARATION.replace("scripts/ai_engine.sh", ".git/config"), "invalid_path"),
		(DECLARATION.replace("scripts/ai_engine.sh", "x/.GIT/y"), "invalid_path"),
		(DECLARATION.replace("_AI_ENGINE_EXIT_REFUSED", "<!-- ai:x -->"), "invalid_required_string"),
		(DECLARATION.replace("_AI_ENGINE_EXIT_REFUSED", "café"), "invalid_required_string"),
		(DECLARATION.replace("_AI_ENGINE_EXIT_REFUSED", "x" * 201), "invalid_required_string"),
	],
)
def test_malformed_declarations_fail_closed(body, reason):
	with pytest.raises(ValueError, match=f"^{reason}$"):
		dep.pr_dependency_declaration(_issue(body))


def test_trust_rules():
	assert dep.is_trusted_author(_issue(association="MEMBER"), None) is True
	assert dep.is_trusted_author(_issue(association="NONE", login="outsider"), None) is None
	assert dep.is_trusted_author(_issue(association="NONE", login="outsider"), "pipeline-bot") is False
	assert dep.is_trusted_author(_issue(association="NONE", login="pipeline-bot"), "pipeline-bot") is True


def test_verdict_only_ready_when_every_check_passes():
	decl = _decl()
	ok = b"line\n_AI_ENGINE_EXIT_REFUSED=76\n"
	assert dep.pr_dependency_verdict(_issue(), decl, _pull(), ok, REPO, True) == {"status": "ready", "reason": "dependency_satisfied", "depends_on_pr": 8}
	cases = [
		(None, ok, None, "identity_unavailable"),
		(None, ok, False, "untrusted_author"),
		(None, ok, True, "pr_unreadable"),
		(_pull(number=7), ok, True, "pr_missing_or_not_pr"),
		(_pull(full_name="other/repo"), ok, True, "pr_repository_mismatch"),
		(_pull(merged=False), ok, True, "pr_unmerged"),
		(_pull(ref="release"), ok, True, "pr_wrong_base"),
		(_pull(), None, True, "content_unreadable"),
		(_pull(), b"\xff\xfe", True, "content_unreadable"),
		(_pull(), dep.CONTENT_TOO_LARGE, True, "content_too_large"),
		(_pull(), b"nothing", True, "required_string_missing"),
	]
	for pull, content, trusted, reason in cases:
		verdict = dep.pr_dependency_verdict(_issue(), decl, pull if pull is not None else None, content, REPO, trusted)
		assert verdict == {"status": "held", "reason": reason, "depends_on_pr": 8}, reason


def test_evaluate_reads_content_only_after_pr_checks(monkeypatch):
	calls = []
	monkeypatch.setattr(dep, "fetch_pull", lambda repo, n: calls.append(("pull", n)) or _pull(merged=False))
	monkeypatch.setattr(dep, "fetch_default_branch_content", lambda *a: calls.append(("content",) + a) or b"")
	assert dep.evaluate(_issue(), _decl(), REPO, "")["reason"] == "pr_unmerged"
	assert calls == [("pull", 8)]

	calls.clear()
	monkeypatch.setattr(dep, "fetch_pull", lambda repo, n: _pull())
	monkeypatch.setattr(dep, "fetch_default_branch_content", lambda repo, path, ref: calls.append((path, ref)) or b"_AI_ENGINE_EXIT_REFUSED")
	assert dep.evaluate(_issue(), _decl(), REPO, "")["status"] == "ready"
	assert calls == [("scripts/ai_engine.sh", "main")]

	monkeypatch.setattr(dep, "fetch_login", lambda: "")
	assert dep.evaluate(_issue(association="NONE", login="outsider"), _decl(), REPO, "")["reason"] == "identity_unavailable"


def test_security_matcher_ignores_pr_declaration():
	security = _load(SECURITY_SCRIPT, "security_dependency_for_pr_test")
	assert security.security_dependency_number(_issue(labels=("ai:security",))) is None


def test_cli_outcomes(tmp_path: Path):
	def run(issue, *extra, number="9"):
		path = tmp_path / "issue.json"
		path.write_text(json.dumps(issue), encoding="utf-8")
		out = _cli("pr-dependency", "--issue-json", str(path), "--repo", REPO, "--issue-number", number, *extra)
		assert out.returncode == 0, out.stderr
		return json.loads(out.stdout)

	assert run(_issue("plain body"))["status"] == "none"
	assert run(_issue(labels=("ai:orchestrator-managed",))) == {"status": "none", "reason": "orchestrator_managed"}
	assert run(_issue(), "--number-only") == {"status": "held", "reason": "pending verification", "depends_on_pr": 8}
	assert run(_issue(), number="10") == {"status": "held", "reason": "issue_identity_mismatch"}
	assert run({**_issue(), "pull_request": {}}) == {"status": "held", "reason": "issue_identity_mismatch"}
	assert run(_issue("- Depends on PR: #8\n")) == {"status": "held", "reason": "malformed_declaration"}
	bad = tmp_path / "bad.json"
	bad.write_text("{not json", encoding="utf-8")
	out = _cli("pr-dependency", "--issue-json", str(bad), "--repo", REPO, "--issue-number", "9")
	assert json.loads(out.stdout)["status"] == "held"
	out = _cli("pr-dependency", "--issue-json", str(tmp_path / "absent.json"), "--repo", REPO, "--issue-number", "9")
	assert json.loads(out.stdout)["status"] == "held"


def test_gates_are_wired_with_trusted_isolated_copies():
	wf = ROOT / ".github" / "workflows"
	clarify = (wf / "clarify.yml").read_text(encoding="utf-8")
	plan = (wf / "plan.yml").read_text(encoding="utf-8")
	implement = (wf / "implement.yml").read_text(encoding="utf-8")
	poll = (wf / "orchestrate_poll.yml").read_text(encoding="utf-8")
	poller = (ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	for text in (clarify, plan, implement, poll):
		assert "for f in gh_helpers.sh " in text and " pr_dependency.py " in text
	assert "pr_dependency.py|load_workflow_overlay.py|security_dependency.py|" in clarify
	assert 'python3 -I -B "${CLARIFY_HOST_PYTHON_DIR}/pr_dependency.py" pr-dependency' in clarify
	assert "gate=pr_dependency" in clarify
	assert '/labels/ai:blocked' in clarify
	assert 'python3 -I -B scripts/pr_dependency.py pr-dependency' in plan
	plan_step = plan[plan.index("- name: Validate planning phase label"):]
	assert plan_step.index("gate=pr_dependency") < plan_step.index("--add-label 'ai:planning'")
	implement_calls = [line for line in implement.splitlines() if "pr_dependency.py" in line and "pr-dependency" in line]
	assert len(implement_calls) == 2
	for line in implement_calls:
		assert 'python3 -I -B "${IMPLEMENT_STAGED_SUPPORT_RUN_DIR}/pr_dependency.py"' in line
	assert implement.count("gate=pr_dependency reason=trusted_helper_unavailable") == 2
	assert "python3 -I -B scripts/pr_dependency.py pr-dependency" in poller
	assert "<!-- ai:pr-dependency-released:${issue_num} -->" in poller
	assert "reason=pr_dependency_held" in poller
	assert "reason=pr_dependency_comments_unavailable" in poller
	assert '((.user.login // "") == $login)' in poller
	scan = poller[poller.index("run_unblock_scan() {"):poller.index("\n}\n", poller.index("run_unblock_scan() {"))]
	assert scan.index("reason=pr_dependency_held") < scan.index("gh workflow run unblock_judge_dispatch.yml")
	# The hold is applied before selection, which picks at most one item per tick.
	assert scan.index("reason=pr_dependency_held") < scan.index("python3 scripts/unblock_scan.py select")
	assert "python3 -I -B scripts/pr_dependency.py pr-dependency" in scan


def test_helper_imports_only_the_standard_library(tmp_path: Path):
	"""Like security_dependency.py: a branch-added json.py never runs under -I."""
	(tmp_path / "json.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
	out = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False)
	assert out.returncode == 0, out.stderr


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
state = os.environ["FAKE_GH_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as fh:
	fh.write(json.dumps(args) + "\n")
def load(name):
	with open(os.path.join(state, name), encoding="utf-8") as fh:
		return fh.read()
if args[:1] == ["workflow"] and args[1:2] == ["run"]:
	sys.exit(0)
if args[:1] == ["api"]:
	if "search/issues" in args:
		print(load("search.json"))
		sys.exit(0)
	if args[1:2] == ["graphql"]:
		print(load("graphql.json"))
		sys.exit(0)
	if args[-1].endswith("/pulls/801"):
		print(load("pull.json"))
		sys.exit(0)
sys.exit(1)
"""


def _run_unblock_scan(tmp_path: Path, *, with_helper: bool = True) -> tuple[str, list[list[str]]]:
	"""Run the poller's real run_unblock_scan against a fake gh (issue #7228)."""
	repo = tmp_path / "repo"
	(repo / "scripts").mkdir(parents=True)
	for name in ("unblock_scan.py", "unblock_ledger.py") + (("pr_dependency.py",) if with_helper else ()):
		shutil.copy(ROOT / "scripts" / name, repo / "scripts" / name)
	state = tmp_path / "state"
	state.mkdir()
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	(bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
	(bin_dir / "gh").chmod(0o755)
	# Issue 700 is the oldest block, so without the hold it would take the
	# selector's single dispatch slot; issue 701 is an ordinary blocked issue.
	(state / "search.json").write_text(json.dumps([
		{"number": 700, "labels": ["ai:blocked"], "pull_request": False, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"},
		{"number": 701, "labels": ["ai:blocked"], "pull_request": False, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z"},
	]), encoding="utf-8")

	def node(body: str, labeled_at: str) -> dict:
		return {
			"body": body,
			"author": {"login": "maintainer"},
			"authorAssociation": "OWNER",
			"timelineItems": {"nodes": [{"createdAt": labeled_at, "label": {"name": "ai:blocked"}}]},
			"comments": {"totalCount": 0, "nodes": []},
		}

	(state / "graphql.json").write_text(json.dumps({"data": {"repository": {
		"i700": node("Feature track.\n- Depends on PR: #801\n- Requires on main: scripts/ai_engine.sh contains _AI_ENGINE_EXIT_REFUSED\n", "2026-01-02T00:00:00Z"),
		"i701": node("Plain blocked issue.", "2026-01-03T00:00:00Z"),
	}}}), encoding="utf-8")
	(state / "pull.json").write_text(json.dumps({
		"number": 801, "merged": False, "merged_at": None,
		"base": {"ref": "main", "repo": {"full_name": "owner/repo", "default_branch": "main"}},
	}), encoding="utf-8")
	(state / "failed.txt").write_text("", encoding="utf-8")
	poller = (ROOT / "scripts" / "orchestrate_poll_process.sh").read_text(encoding="utf-8")
	start = poller.index("run_unblock_scan() {")
	(tmp_path / "scan.sh").write_text(poller[start:poller.index("\n}\n", start) + 3], encoding="utf-8")
	driver = tmp_path / "driver.sh"
	driver.write_text(
		"set -uo pipefail\n"
		f"source '{tmp_path / 'scan.sh'}'\n"
		'gh_retry() { "$@"; }\n'
		"_safe_gh_jq() { echo '[]'; }\n"
		# The needs-human digest upkeep that shares the scan's tick slot is
		# covered by its own tests; stub it so this scan sees no parked items.
		"needs_human_digest_prune() { :; }\n"
		'unblock_trusted_login() { UNBLOCK_TRUSTED_LOGIN="pipeline-bot"; printf \'%s\' "${UNBLOCK_TRUSTED_LOGIN}"; }\n'
		'run_unblock_scan || echo "UNBLOCK_SCAN rc=$?"\n',
		encoding="utf-8",
	)
	env = dict(
		os.environ,
		PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
		FAKE_GH_STATE=str(state),
		GITHUB_REPOSITORY="owner/repo",
		RUNNER_TEMP=str(tmp_path / "runner"),
		UNBLOCK_FAILED_PROJECTS_FILE=str(state / "failed.txt"),
		NEEDS_HUMAN_PARKED_ITEMS_FILE=str(state / "parked.json"),
		UNBLOCK_JUDGE_ENABLED="true",
		PYTHONDONTWRITEBYTECODE="1",
	)
	(tmp_path / "runner").mkdir()
	out = subprocess.run(["bash", str(driver)], cwd=repo, env=env, capture_output=True, text=True, check=False, timeout=120)
	calls_file = state / "calls.jsonl"
	lines = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
	calls = [json.loads(line) for line in lines if line]
	return out.stdout + out.stderr, calls


@pytest.mark.skipif(shutil.which("jq") is None, reason="run_unblock_scan needs jq")
@pytest.mark.parametrize("with_helper", [True, False])
def test_unblock_scan_skips_held_issue_without_starving_others(tmp_path: Path, with_helper: bool):
	log, calls = _run_unblock_scan(tmp_path, with_helper=with_helper)
	dispatched = [c for c in calls if c[:2] == ["workflow", "run"]]
	assert "UNBLOCK_SCAN item=700 kind=issue outcome=skip reason=pr_dependency_held" in log, log
	assert dispatched == [["workflow", "run", "unblock_judge_dispatch.yml", "--repo", "owner/repo", "-f", "item=701"]], log
	assert "UNBLOCK_SCAN item=701 kind=issue outcome=dispatched" in log, log
	# The held issue is read once (pulls/801) and never reaches the contents read.
	assert not any(c and c[-1].startswith("repos/owner/repo/contents/") for c in calls)
