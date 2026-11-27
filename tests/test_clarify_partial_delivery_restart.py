#!/usr/bin/env python3
"""Clarify treats the partial-delivery restart as an automated run (#7657).

issue_pr_status.yml posts "/reclarify" + "<!-- ai:partial-delivery-restart:<n> -->"
with GH_PAT after a merged PR delivered only part of an issue. The route step's
#7328 release block must verify it like the PR-dependency release, so no
human-answer marker is added and clarify-respond answers the follow-up
questions unattended. The block is read from the shipped workflow text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLARIFY = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
BEGIN = "# BEGIN pr-dependency release classification (#7328)"
END = "# END pr-dependency release classification (#7328)"
RESTART_BODY = "/reclarify\n<!-- ai:partial-delivery-restart:42 -->"
RELEASE_BODY = "/reclarify\n<!-- ai:pr-dependency-released:42 -->"

GH_STUB = """gh_retry()
{
	if [ "${STUB_LOGIN_FAIL:-}" = "1" ]; then
		return 1
	fi
	case "$*" in
		*issues/comments/*)
			[ -n "${STUB_SOURCE_COMMENT:-}" ] || return 1
			printf '%s\\n' "${STUB_SOURCE_COMMENT}"
			;;
		*) printf '%s\\n' "${STUB_LOGIN}" ;;
	esac
}
"""


def _release_block() -> str:
	text = CLARIFY.read_text(encoding="utf-8")
	begin = text.index(BEGIN)
	line_start = text.rfind("\n", 0, begin) + 1
	end = text.index(END, begin)
	return textwrap.dedent(text[line_start:end])


def _run(body: str, association: str, comment_login: str, *, login_fail: bool = False, source_comment: str = "") -> dict:
	with tempfile.TemporaryDirectory() as workdir:
		scripts_dir = Path(workdir) / "scripts"
		scripts_dir.mkdir()
		(scripts_dir / "gh_helpers.sh").write_text(GH_STUB, encoding="utf-8")
		script = (
			"set -euo pipefail\nIS_FORCED_RECLARIFY=true\n"
			+ _release_block()
			+ '\nprintf "release=%s forced=%s trusted=%s\\n" "${IS_AUTOMATED_RELEASE}" "${IS_FORCED_RECLARIFY}" "${COMMENT_ROUTE_TRUSTED}"\n'
		)
		env = {
			"PATH": os.environ["PATH"],
			"EVENT_NAME": "issue_comment",
			"ISSUE_NUMBER": "42",
			"COMMENT_BODY": body,
			"COMMENT_AUTHOR_ASSOCIATION": association,
			"COMMENT_USER_LOGIN": comment_login,
			"STUB_LOGIN": "pipeline-bot",
			"STUB_LOGIN_FAIL": "1" if login_fail else "",
			"STUB_SOURCE_COMMENT": source_comment,
			"GITHUB_REPOSITORY": "o/r",
		}
		result = subprocess.run(["bash", "-c", script], env=env, cwd=workdir, capture_output=True, text=True, check=True)
	values = dict(part.split("=", 1) for part in result.stdout.strip().splitlines()[-1].split())
	values["stdout"] = result.stdout
	return values


def test_restart_from_the_pipeline_login_is_automated() -> None:
	for association in ("OWNER", "NONE"):
		verified = _run(RESTART_BODY, association, "Pipeline-Bot")
		assert (verified["release"], verified["forced"], verified["trusted"]) == ("true", "false", "true"), association
		assert "gate=partial_delivery_restart reason=verified outcome=restart issue=42" in verified["stdout"]
	crlf = _run(RESTART_BODY.replace("\n", "\r\n") + "\r\n", "NONE", "pipeline-bot")
	assert crlf["release"] == "true"


def test_restart_from_another_author_keeps_human_rules() -> None:
	owner = _run(RESTART_BODY, "OWNER", "maintainer")
	assert (owner["release"], owner["forced"], owner["trusted"]) == ("false", "true", "true")
	assert "gate=partial_delivery_restart reason=author_mismatch outcome=human_reclarify" in owner["stdout"]
	outsider = _run(RESTART_BODY, "NONE", "outsider")
	assert (outsider["release"], outsider["trusted"]) == ("false", "false")
	assert "gate=partial_delivery_restart reason=author_mismatch outcome=skip" in outsider["stdout"]
	failed = _run(RESTART_BODY, "NONE", "pipeline-bot", login_fail=True)
	assert (failed["release"], failed["trusted"]) == ("false", "false")
	assert "reason=identity_unavailable" in failed["stdout"]


def test_restart_for_another_issue_or_with_extra_text_is_not_a_restart() -> None:
	for body in (
		"/reclarify\n<!-- ai:partial-delivery-restart:43 -->",
		RESTART_BODY + "\nextra",
		"text\n" + RESTART_BODY,
	):
		other = _run(body, "NONE", "pipeline-bot")
		assert (other["release"], other["trusted"]) == ("false", "false"), body
		assert "partial_delivery_restart" not in other["stdout"], body


def test_pr_dependency_release_keeps_its_gate_name() -> None:
	verified = _run(RELEASE_BODY, "NONE", "pipeline-bot")
	assert verified["release"] == "true"
	assert "gate=pr_dependency_release reason=verified outcome=restart issue=42" in verified["stdout"]


def test_poller_replay_of_a_restart_is_verified_against_its_source() -> None:
	assert shutil.which("jq") is not None, "jq is required for the replay case"
	replay = "/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=777 -->"
	source = json.dumps({"user": {"login": "Pipeline-Bot"}, "issue_url": "https://api.github.com/repos/o/r/issues/42", "body": RESTART_BODY})
	verified = _run(replay, "OWNER", "pipeline-bot", source_comment=source)
	assert (verified["release"], verified["forced"]) == ("true", "false")
	assert "gate=partial_delivery_restart reason=verified_replay outcome=restart" in verified["stdout"]
	release_source = source.replace("partial-delivery-restart", "pr-dependency-released")
	release = _run(replay, "OWNER", "pipeline-bot", source_comment=release_source)
	assert "gate=pr_dependency_release reason=verified_replay outcome=restart" in release["stdout"]


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
	for test in tests:
		test()
	print(f"OK: {len(tests)} clarify partial-delivery restart tests passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
