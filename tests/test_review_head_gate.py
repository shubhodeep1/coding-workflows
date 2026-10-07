"""Exercise the SHA-bound status and stale auto-merge gate without GitHub."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "review_head_gate.sh"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if args[:2] == ["api", "repos/o/r/pulls/42"]:
    if os.environ.get("FAKE_FAIL") == "read": sys.exit(1)
    print(json.dumps({"state": "open", "head": {"sha": os.environ.get("FAKE_HEAD", "a" * 40)},
        "auto_merge": {"enabled_by": {"login": "bot"}} if os.environ.get("FAKE_ENABLED") == "true" else None}))
if "--disable-auto" in args and os.environ.get("FAKE_FAIL") == "disable": sys.exit(1)
if "statuses/" in " ".join(args) and os.environ.get("FAKE_FAIL") == "status": sys.exit(1)
'''


def _run(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir()
	gh_path = bin_dir / "gh"
	gh_path.write_text(FAKE_GH, encoding="utf-8")
	gh_path.chmod(0o755)
	log_path = tmp_path / "gh.log"
	env = {
		**os.environ,
		"PATH": f"{bin_dir}:{os.environ['PATH']}",
		"FAKE_GH_LOG": str(log_path),
		"GH_RETRY_MAX_ATTEMPTS": "1",
		"REPOSITORY": "o/r",
		"PR_NUMBER": "42",
		"EVENT_NAME": "pull_request",
		"EVENT_ACTION": "synchronize",
		"PR_EVENT_HEAD_SHA": HEAD,
		"GATE_HEAD_SHA": HEAD,
		"GATE_AUTO_MERGE_ENABLED": "true",
		"DETERMINISTIC_SKIP": "false",
		"REVIEW_HEAD_GATE_STATUS_ENABLED": "true",
		"REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED": "true",
		**overrides,
	}
	result = subprocess.run(["bash", str(HELPER), "gate"], env=env, capture_output=True, text=True, check=False)
	calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []
	return result, calls


def _statuses(calls: list[list[str]]) -> list[list[str]]:
	return [call for call in calls if any("/statuses/" in arg for arg in call)]


def test_synchronize_posts_pending_then_withdraws(tmp_path: Path) -> None:
	result, calls = _run(tmp_path)
	assert result.returncode == 0, result.stderr
	assert len(_statuses(calls)) == 1
	assert calls[0][:4] == ["api", "-X", "POST", f"repos/o/r/statuses/{HEAD}"]
	assert "context=ai-review/head-gate" in calls[0]
	assert "state=pending" in calls[0]
	assert calls[1] == ["pr", "merge", "42", "--repo", "o/r", "--disable-auto"]


@pytest.mark.parametrize("known,expected_read,expected_disable", [("false", False, False), ("", True, True)])
def test_cached_or_unknown_enrollment(tmp_path: Path, known: str, expected_read: bool, expected_disable: bool) -> None:
	if known == "" and shutil.which("jq") is None:
		pytest.skip("jq is required for live PR response validation")
	result, calls = _run(tmp_path, GATE_AUTO_MERGE_ENABLED=known, FAKE_ENABLED="true")
	assert result.returncode == 0
	assert any(call[:2] == ["api", "repos/o/r/pulls/42"] for call in calls) == expected_read
	assert any("--disable-auto" in call for call in calls) == expected_disable


def test_disable_failure_fails_gate(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, FAKE_FAIL="disable", DETERMINISTIC_SKIP="true")
	assert result.returncode != 0
	assert "outcome=failed reason=disable_failed" in result.stdout
	assert not any("state=success" in call for call in _statuses(calls))


@pytest.mark.parametrize("action,expected", [("opened", 1), ("reopened", 0), ("synchronize", 1)])
def test_event_action(tmp_path: Path, action: str, expected: int) -> None:
	result, calls = _run(tmp_path, EVENT_ACTION=action, GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0
	assert len(_statuses(calls)) == expected
	assert not any("--disable-auto" in call for call in calls)


def test_dispatch_skip_status_is_bound_to_gate_head(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, EVENT_NAME="workflow_dispatch", EVENT_ACTION="", DETERMINISTIC_SKIP="true", GATE_HEAD_SHA=NEW_HEAD)
	assert result.returncode == 0
	assert len(_statuses(calls)) == 1
	assert f"repos/o/r/statuses/{NEW_HEAD}" in calls[0]
	assert "state=success" in calls[0]
	assert not any("--disable-auto" in call for call in calls)


def test_changed_event_head_marks_both_heads_pending(tmp_path: Path) -> None:
	result, calls = _run(tmp_path, PR_EVENT_HEAD_SHA=NEW_HEAD, GATE_AUTO_MERGE_ENABLED="false")
	assert result.returncode == 0
	assert {call[3] for call in _statuses(calls)} == {f"repos/o/r/statuses/{HEAD}", f"repos/o/r/statuses/{NEW_HEAD}"}


@pytest.mark.parametrize("overrides,expected", [
	({"GATE_HEAD_SHA": "invalid", "PR_EVENT_HEAD_SHA": "invalid", "GATE_AUTO_MERGE_ENABLED": "false"}, 0),
	({"REVIEW_HEAD_GATE_STATUS_ENABLED": "false", "GATE_AUTO_MERGE_ENABLED": "false"}, 0),
	({"FAKE_FAIL": "status", "GATE_AUTO_MERGE_ENABLED": "false"}, 1),
	({"REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED": "false"}, 1),
])
def test_status_and_withdraw_switches(tmp_path: Path, overrides: dict[str, str], expected: int) -> None:
	result, calls = _run(tmp_path, **overrides)
	assert result.returncode == 0
	assert len(_statuses(calls)) == expected
	if overrides.get("REVIEW_STALE_AUTO_MERGE_WITHDRAW_ENABLED") == "false":
		assert not any("--disable-auto" in call for call in calls)
