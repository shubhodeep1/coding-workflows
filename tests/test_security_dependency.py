"""Security dependency hold (issue #4934): scripts/security_dependency.py.

The verdict rules moved unchanged from scripts/claude_issue_route.py when the
Claude session automation was retired; clarify, implement and the standalone
stall poller call the new script with the same command line.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "security_dependency.py"
REPO = "shubhodeep1/digital_pa"


def _load():
	spec = importlib.util.spec_from_file_location("security_dependency", SCRIPT)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


dep = _load()


def _issue(labels=(), body="", number=9):
	return {
		"number": number,
		"title": "Fix the thing",
		"body": body,
		"labels": [{"name": name} for name in labels],
		"repository_url": f"https://api.github.com/repos/{REPO}",
	}


def _cli(*args: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(SCRIPT), *args],
		capture_output=True,
		text=True,
		env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
		check=False,
	)


def test_security_followup_dependency_verdict_and_validation():
	target = _issue(["ai:security"], "<!-- ai:security-finding:abc -->\n- Depends on: #8")
	predecessor = {"number": 8, "repository_url": target["repository_url"], "state": "open", "labels": []}
	assert dep.security_dependency_verdict(target, predecessor)["reason"] == "dependency open"
	predecessor["state"] = "closed"
	assert dep.security_dependency_verdict(target, predecessor)["reason"] == "dependency closed without ai:merged"
	predecessor["labels"] = [{"name": "ai:merged"}]
	assert dep.security_dependency_verdict(target, predecessor)["status"] == "ready"
	for invalid in ({**predecessor, "pull_request": {}}, None, {**predecessor, "repository_url": "https://api.github.com/repos/other/repo"}):
		assert dep.security_dependency_verdict(target, invalid)["status"] == "held"
	for bad_body in (target["body"] + "\n- Depends on: #7", target["body"].replace("#8", "#9"), target["body"].replace("#8", "#0")):
		assert dep.security_dependency_verdict({**target, "body": bad_body}, predecessor)["status"] == "held"
	assert dep.security_dependency_verdict({**target, "labels": []}, predecessor)["status"] == "held"


def test_issue_without_a_dependency_is_none():
	assert dep.security_dependency_verdict(_issue(["ai:security"], "no dependency"), None) == {"status": "none", "reason": "no security dependency"}


def test_cli_none_number_only_and_identity_mismatch(tmp_path: Path):
	plain = tmp_path / "plain.json"
	plain.write_text(json.dumps(_issue(body="nothing here")), encoding="utf-8")
	out = _cli("security-dependency", "--issue-json", str(plain), "--repo", REPO, "--issue-number", "9")
	assert out.returncode == 0
	assert json.loads(out.stdout)["status"] == "none"

	dependent = tmp_path / "dependent.json"
	dependent.write_text(json.dumps(_issue(["ai:security"], "<!-- ai:security-finding:abc -->\n- Depends on: #8")), encoding="utf-8")
	out = _cli("security-dependency", "--issue-json", str(dependent), "--repo", REPO, "--issue-number", "9", "--number-only")
	assert json.loads(out.stdout) == {"status": "held", "reason": "dependency pending verification", "depends_on": 8}

	out = _cli("security-dependency", "--issue-json", str(dependent), "--repo", REPO, "--issue-number", "10")
	assert json.loads(out.stdout)["status"] == "held"
	out = _cli("security-dependency", "--issue-json", str(tmp_path / "absent.json"), "--repo", REPO, "--issue-number", "9")
	assert json.loads(out.stdout)["status"] == "held"


def test_codex_dependency_gates_and_scheduled_release_are_wired():
	clarify = (ROOT / ".github/workflows/clarify.yml").read_text(encoding="utf-8")
	implement = (ROOT / ".github/workflows/implement.yml").read_text(encoding="utf-8")
	poller = (ROOT / "scripts/orchestrate_poll_process.sh").read_text(encoding="utf-8")
	for text in (clarify, implement, poller):
		assert "scripts/security_dependency.py security-dependency" in text
		assert "claude_issue_route.py" not in text
	assert 'security-dependency --issue-json "${ISSUE_META_FILE}"' in clarify
	assert 'security-dependency --issue-json "${ISSUE_META_FILE}"' in implement
	assert "reason=security_dependency_held" in poller
	assert "ai:security-dependency-released:" in poller
	for workflow in ("clarify.yml", "implement.yml", "orchestrate_poll.yml"):
		assert "security_dependency.py" in (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8"), workflow
