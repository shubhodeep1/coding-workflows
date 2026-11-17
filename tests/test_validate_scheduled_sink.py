"""Behaviour of scripts/validate_scheduled_sink.sh (issue #7553).

The sink keeps one `ai:scheduled-validation-failed` issue per repository: it
opens it on a red scheduled run (one WARNING), edits it in place while the
default branch stays red, closes it (close before comment) when a later run
passes, and never writes after a failed identity or issue-list read. Only an
issue the GH_PAT login wrote with the exact first-line marker counts.

The script is copied alone into a temporary directory so it falls back to its
helper stubs; a fake tg_helpers.sh there records Telegram sends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
SINK = ROOT / "scripts" / "validate_scheduled_sink.sh"
MARKER = "<!-- ai:scheduled-validation:v1 -->"

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_CALLS"], "a", encoding="utf-8") as log:
	log.write(json.dumps(args) + "\n")
if args[:2] == ["api", "user"]:
	if os.environ.get("FAKE_USER_FAIL") == "1":
		sys.exit(1)
	print("pipeline-bot")
	sys.exit(0)
if "--paginate" in args:
	if os.environ.get("FAKE_LIST_FAIL") == "1":
		sys.exit(1)
	print(os.environ.get("FAKE_ISSUES", "[]"))
	sys.exit(0)
if "POST" in args and any(a.endswith("/issues") for a in args):
	print("77")
	sys.exit(0)
sys.exit(0)
'''


def _issue(number: int, login: str = "pipeline-bot", body: str = MARKER + "\nold") -> dict:
	return {"number": number, "user": {"login": login}, "body": body}


def _run(tmp_path: Path, status: str, issues: list | None = None, **extra: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]], str]:
	bindir = tmp_path / "bin"
	bindir.mkdir(exist_ok=True)
	gh = bindir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	sink_dir = tmp_path / "support"
	sink_dir.mkdir(exist_ok=True)
	shutil.copy(SINK, sink_dir / "validate_scheduled_sink.sh")
	tg_log = tmp_path / "tg.log"
	(sink_dir / "tg_helpers.sh").write_text(
		'tg_send_msg() { printf "%s|%s\\n" "$2" "$1" >> "' + str(tg_log) + '"; }\n', encoding="utf-8"
	)
	calls = tmp_path / "calls.log"
	calls.write_text("", encoding="utf-8")
	env = {
		"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
		"GITHUB_REPOSITORY": "owner/consumer",
		"GH_CALLS": str(calls),
		"FAKE_ISSUES": json.dumps(issues or []),
		"SINK_STATUS": status,
		"SINK_RAW_STATUS": "tests_failed" if status != "pass" else "pass",
		"SINK_RUN_URL": "https://github.com/owner/consumer/actions/runs/1",
		"SINK_DEFAULT_BRANCH": "main",
		"SINK_HEAD_SHA": "a" * 40,
		**extra,
	}
	proc = subprocess.run(
		["bash", str(sink_dir / "validate_scheduled_sink.sh")],
		env=env, capture_output=True, text=True, check=False, cwd=tmp_path,
	)
	recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines() if line]
	tg = tg_log.read_text(encoding="utf-8") if tg_log.exists() else ""
	return proc, recorded, tg


def _writes(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if "-X" in c]


def _field(call: list[str], name: str) -> str:
	for index, arg in enumerate(call):
		if arg == "-f" and call[index + 1].startswith(name + "="):
			return call[index + 1][len(name) + 1:]
	raise AssertionError(f"{name} not in {call}")


def test_red_without_issue_opens_one_and_alerts(tmp_path: Path) -> None:
	proc, calls, tg = _run(tmp_path, "fail")
	assert proc.returncode == 0, proc.stderr
	writes = _writes(calls)
	assert len(writes) == 1 and "POST" in writes[0] and "repos/owner/consumer/issues" in writes[0]
	assert "labels[]=ai:scheduled-validation-failed" in writes[0]
	assert _field(writes[0], "body").split("\n")[0] == MARKER
	assert _field(writes[0], "title") == "Scheduled validation failing on main"
	assert "VALIDATE_SCHEDULED_SINK outcome=opened issue=77" in proc.stdout
	assert tg.count("WARNING|") == 1
	listing = next(c for c in calls if "--paginate" in c)
	assert "labels=ai%3Ascheduled-validation-failed" in listing[-1] and "creator=pipeline-bot" in listing[-1]


def test_red_with_trusted_issue_updates_in_place(tmp_path: Path) -> None:
	proc, calls, tg = _run(tmp_path, "error", [_issue(12), _issue(9)])
	writes = _writes(calls)
	assert len(writes) == 1 and "PATCH" in writes[0] and "repos/owner/consumer/issues/9" in writes[0]
	assert _field(writes[0], "body").startswith(MARKER + "\n")
	assert "outcome=updated issue=9" in proc.stdout
	assert tg == ""


def test_untrusted_or_unmarked_issues_are_ignored(tmp_path: Path) -> None:
	issues = [
		_issue(3, login="someone-else"),
		_issue(4, body="text first\n" + MARKER),
		{**_issue(5), "pull_request": {}},
	]
	proc, calls, _ = _run(tmp_path, "fail", issues)
	writes = _writes(calls)
	assert len(writes) == 1 and "POST" in writes[0]
	assert "outcome=opened" in proc.stdout


def test_green_closes_before_comment(tmp_path: Path) -> None:
	proc, calls, tg = _run(tmp_path, "pass", [_issue(21)])
	writes = _writes(calls)
	assert len(writes) == 2
	assert "PATCH" in writes[0] and "state=closed" in writes[0] and "state_reason=completed" in writes[0]
	assert "POST" in writes[1] and writes[1][writes[1].index("POST") + 1].endswith("/issues/21/comments")
	assert "outcome=closed issue=21" in proc.stdout
	assert tg == ""


def test_green_without_issue_writes_nothing(tmp_path: Path) -> None:
	proc, calls, _ = _run(tmp_path, "pass")
	assert _writes(calls) == []
	assert "outcome=noop" in proc.stdout


def test_read_failures_never_create(tmp_path: Path) -> None:
	proc, calls, tg = _run(tmp_path, "fail", FAKE_USER_FAIL="1")
	assert proc.returncode == 0
	assert _writes(calls) == [] and tg == ""
	assert "reason=identity_unavailable" in proc.stdout

	(tmp_path / "calls.log").unlink()
	proc, calls, tg = _run(tmp_path, "fail", FAKE_LIST_FAIL="1")
	assert proc.returncode == 0
	assert _writes(calls) == [] and tg == ""
	assert "reason=list_failed" in proc.stdout


def test_body_neutralises_untrusted_text_and_is_capped(tmp_path: Path) -> None:
	result = tmp_path / "validation_result.json"
	result.write_text(json.dumps({"failures": [{"test": "t1 <!-- ai:scheduled-validation:v1 -->\n`x` | y"}]}), encoding="utf-8")
	proc, calls, _ = _run(
		tmp_path, "",
		SINK_RESULT_FILE=str(result),
		SINK_FAILURE_SUMMARY="<!-- forged --> `cmd` " + "z" * 100000,
	)
	body = _field(_writes(calls)[0], "body")
	assert body.count("<!--") == 1 and body.startswith(MARKER)
	assert "`x`" not in body and "`cmd`" not in body
	assert "&lt;!-- ai:scheduled-validation:v1" in body
	assert "| Status | unknown |" in body
	assert len(body.encode("utf-8")) < 60000 + 100
	assert "outcome=opened" in proc.stdout


def test_non_list_failures_still_reports(tmp_path: Path) -> None:
	result = tmp_path / "validation_result.json"
	result.write_text(json.dumps({"failures": 5}), encoding="utf-8")
	proc, calls, _ = _run(tmp_path, "fail", SINK_RESULT_FILE=str(result))
	assert "outcome=opened" in proc.stdout
	assert "- (none recorded)" in _field(_writes(calls)[0], "body")
