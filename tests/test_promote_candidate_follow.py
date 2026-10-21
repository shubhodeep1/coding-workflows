#!/usr/bin/env python3
"""scripts/promote_candidate_follow.sh and its wiring (Phase 2 of
docs/plans/pin-promote-cycle-to-candidate-plan.md).

The follower keeps `promote-candidate` at the main tip unless the
`promote-cycle-frozen` tag marks a frozen promote cycle. Each case runs the
real script against a stub `gh` (canned status and body per method + path),
a stub `date` (fixed epoch and UTC hour) and a stub `curl` (Telegram).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "promote_candidate_follow.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "promote-candidate-follow.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = REPO_ROOT / "workflow-templates"
REPO = "shubhodeep1/coding-workflows"
MAIN_SHA = "a" * 40
OLD_SHA = "b" * 40
TAG_OBJ = "c" * 40
NOW = 1_800_000_000
INTERNAL_WRAPPERS = [
	"internal-cancel-on-pr-close.yml",
	"internal-check-failure-triage.yml",
	"internal-clarify.yml",
	"internal-implement.yml",
	"internal-issue-pr-status.yml",
	"internal-memory-maintenance.yml",
	"internal-orchestrate-clarify-respond.yml",
	"internal-orchestrate-poll.yml",
	"internal-orchestrate.yml",
	"internal-plan.yml",
	"internal-review.yml",
	"internal-validate.yml",
	"internal-workflow-failure-heal.yml",
]

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
	log.write(json.dumps(args) + "\n")
if args[:1] != ["api"]:
	sys.exit(1)
method, path, include = "GET", "", False
i = 1
while i < len(args):
	arg = args[i]
	if arg in ("-X", "--method"):
		method = args[i + 1]; i += 2; continue
	if arg in ("-f", "-F", "--jq"):
		i += 2; continue
	if arg == "-i":
		include = True; i += 1; continue
	path = arg; i += 1
routes = json.loads(Path(os.environ["FAKE_GH_ROUTES"]).read_text())
status, body = routes.get(f"{method} {path}", [404, {"message": "Not Found"}])
text = json.dumps(body)
if include:
	sys.stdout.write(f"HTTP/2.0 {status} X\r\ncontent-type: application/json\r\n\r\n{text}")
else:
	sys.stdout.write(text)
if status >= 300:
	sys.stderr.write(f"gh: {body.get('message', 'error')} (HTTP {status})\n")
	sys.exit(1)
'''

FAKE_DATE = r'''#!/bin/sh
for a in "$@"; do
	case "$a" in
		+%H) echo "${FAKE_HOUR:-12}"; exit 0 ;;
		+%s) echo "${FAKE_NOW}"; exit 0 ;;
	esac
done
exec /bin/date "$@"
'''

FAKE_CURL = r'''#!/bin/sh
printf '%s\n' "$*" >> "${FAKE_CURL_LOG}"
printf '{"ok":true,"result":{"message_id":7}}'
'''


def _branch(sha: str = OLD_SHA, protected: bool = True) -> list:
	return [200, {"name": "promote-candidate", "commit": {"sha": sha}, "protected": protected}]


def _routes(*, branch: list | None = None, tag: list | None = None, tag_object: list | None = None, main_sha: str = MAIN_SHA, writes: dict | None = None) -> dict:
	routes = {
		f"GET repos/{REPO}/branches/promote-candidate": branch if branch is not None else _branch(),
		f"GET repos/{REPO}/git/ref/heads/main": [200, {"ref": "refs/heads/main", "object": {"sha": main_sha, "type": "commit"}}],
		f"PATCH repos/{REPO}/git/refs/heads/promote-candidate": [200, {"ref": "refs/heads/promote-candidate"}],
		f"POST repos/{REPO}/git/refs": [201, {"ref": "refs/heads/promote-candidate"}],
		f"DELETE repos/{REPO}/git/refs/tags/promote-cycle-frozen": [204, {}],
	}
	if tag is not None:
		routes[f"GET repos/{REPO}/git/ref/tags/promote-cycle-frozen"] = tag
	if tag_object is not None:
		routes[f"GET repos/{REPO}/git/tags/{TAG_OBJ}"] = tag_object
	routes.update(writes or {})
	return routes


def _annotated_tag() -> list:
	return [200, {"ref": "refs/tags/promote-cycle-frozen", "object": {"sha": TAG_OBJ, "type": "tag"}}]


def _lightweight_tag() -> list:
	return [200, {"ref": "refs/tags/promote-cycle-frozen", "object": {"sha": MAIN_SHA, "type": "commit"}}]


def _message(text: str) -> list:
	return [200, {"sha": TAG_OBJ, "message": text}]


def _run(routes: dict, *, event: str = "push", hour: str = "12", **extra: str) -> tuple[subprocess.CompletedProcess, list[list[str]], str]:
	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		bin_dir = tmp / "bin"
		bin_dir.mkdir()
		for name, body in (("gh", FAKE_GH), ("date", FAKE_DATE), ("curl", FAKE_CURL)):
			(bin_dir / name).write_text(body, encoding="utf-8")
			(bin_dir / name).chmod(0o755)
		(tmp / "routes.json").write_text(json.dumps(routes), encoding="utf-8")
		env = {
			"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
			"HOME": str(tmp),
			"TMPDIR": str(tmp),
			"FAKE_GH_ROUTES": str(tmp / "routes.json"),
			"FAKE_GH_LOG": str(tmp / "gh.log"),
			"FAKE_CURL_LOG": str(tmp / "curl.log"),
			"FAKE_NOW": str(NOW),
			"FAKE_HOUR": hour,
			"GH_TOKEN": "synthetic-token-value",
			"GITHUB_REPOSITORY": REPO,
			"GITHUB_EVENT_NAME": event,
			"GH_RETRY_MAX_ATTEMPTS": "1",
			"GH_RATE_LIMIT_BREAKER_FILE": str(tmp / "breaker"),
			"TG_BOT_SECRET": "synthetic-bot",
			"TG_CHAT_ID": "123",
			"ALERT_MSG_LEVEL": "WARNING",
			"PYTHONDONTWRITEBYTECODE": "1",
			**extra,
		}
		result = subprocess.run(["bash", str(SCRIPT)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
		gh_log = tmp / "gh.log"
		calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines()] if gh_log.exists() else []
		curl_log = tmp / "curl.log"
		curl = curl_log.read_text(encoding="utf-8") if curl_log.exists() else ""
	return result, calls, curl


def _follow_lines(result: subprocess.CompletedProcess) -> list[str]:
	lines = [line for line in result.stdout.splitlines() if line.startswith("PROMOTE_CANDIDATE_FOLLOW ")]
	assert len(lines) == 1, (result.stdout, result.stderr)
	assert "synthetic-token-value" not in result.stdout + result.stderr
	return lines


def _writes(calls: list[list[str]]) -> list[list[str]]:
	return [c for c in calls if "-X" in c]


def _method(call: list[str]) -> str:
	return call[call.index("-X") + 1]


def _assert_patches_never_force(calls: list[list[str]]) -> None:
	for call in calls:
		if "-X" in call and _method(call) == "PATCH":
			assert "force=false" in call, call
			assert "force=true" not in call, call


def test_advanced_fast_forwards_with_force_false() -> None:
	result, calls, curl = _run(_routes())
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=advanced from={OLD_SHA} to={MAIN_SHA} protected=true"]
	writes = _writes(calls)
	assert [_method(c) for c in writes] == ["PATCH"]
	assert f"sha={MAIN_SHA}" in writes[0]
	_assert_patches_never_force(calls)
	assert curl == ""


def test_current_writes_nothing() -> None:
	result, calls, _ = _run(_routes(branch=_branch(MAIN_SHA)))
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == ["PROMOTE_CANDIDATE_FOLLOW outcome=current protected=true"]
	assert _writes(calls) == []
	# 3 reads: branch, tag ref, main ref.
	assert len(calls) == 3


def test_created_when_branch_missing() -> None:
	result, calls, _ = _run(_routes(branch=[404, {"message": "Branch not found"}]))
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=created to={MAIN_SHA} protected=unknown"]
	writes = _writes(calls)
	assert [_method(c) for c in writes] == ["POST"]
	assert "ref=refs/heads/promote-candidate" in writes[0] and f"sha={MAIN_SHA}" in writes[0]


def test_created_branch_alerts_on_first_scheduled_run_after_midnight() -> None:
	routes = _routes(branch=[404, {"message": "Branch not found"}])
	result, calls, curl = _run(routes, event="schedule", hour="00")
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=unprotected action=created to={MAIN_SHA} protected=unknown"]
	assert [_method(c) for c in _writes(calls)] == ["POST"]
	assert "CRITICAL" in curl and "ruleset" in curl


def test_frozen_by_fresh_annotated_tag() -> None:
	routes = _routes(tag=_annotated_tag(), tag_object=_message(f"frozen_at={NOW - 3600} cycle_run=42\n"))
	result, calls, _ = _run(routes)
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == ["PROMOTE_CANDIDATE_FOLLOW outcome=frozen protected=true"]
	assert _writes(calls) == []
	assert len(calls) == 4


def test_lightweight_or_unparsable_or_future_tag_counts_as_fresh() -> None:
	for routes in (
		_routes(tag=_lightweight_tag()),
		_routes(tag=_annotated_tag(), tag_object=_message("no timestamp here")),
		_routes(tag=_annotated_tag(), tag_object=_message("xfrozen_at=1 cycle_run=1")),
		_routes(tag=_annotated_tag(), tag_object=_message("frozen_at=1oops")),
		_routes(tag=_annotated_tag(), tag_object=_message(f"frozen_at={NOW + 10_000_000}")),
	):
		result, calls, _ = _run(routes)
		assert result.returncode == 0, result.stderr
		assert _follow_lines(result) == ["PROMOTE_CANDIDATE_FOLLOW outcome=frozen protected=true"]
		assert _writes(calls) == []


def test_disabled_switch_ignores_the_tag() -> None:
	routes = _routes(tag=_annotated_tag(), tag_object=_message(f"frozen_at={NOW - 60}"))
	result, calls, _ = _run(routes, PROMOTE_CYCLE_PIN_ENABLED="FALSE")
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=unfrozen_disabled from={OLD_SHA} to={MAIN_SHA} protected=true"]
	assert [_method(c) for c in _writes(calls)] == ["PATCH"]
	# The tag object is not read and the tag is not deleted.
	assert not any(f"git/tags/{TAG_OBJ}" in c for call in calls for c in call)
	_assert_patches_never_force(calls)


def test_stale_tag_is_deleted_with_a_critical_alert() -> None:
	routes = _routes(tag=_annotated_tag(), tag_object=_message(f"frozen_at={NOW - 172_801}"))
	result, calls, curl = _run(routes)
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=stale_tag_deleted from={OLD_SHA} to={MAIN_SHA} protected=true"]
	assert [_method(c) for c in _writes(calls)] == ["DELETE", "PATCH"]
	assert "CRITICAL" in curl and "promote-cycle-frozen" in curl
	_assert_patches_never_force(calls)


def test_freeze_max_secs_override_and_invalid_value() -> None:
	routes = _routes(tag=_annotated_tag(), tag_object=_message(f"frozen_at={NOW - 120}"))
	result, _, _ = _run(routes, PROMOTE_CYCLE_FREEZE_MAX_SECS="60")
	assert "outcome=stale_tag_deleted" in _follow_lines(result)[0]
	result, _, _ = _run(routes, PROMOTE_CYCLE_FREEZE_MAX_SECS="abc")
	assert "outcome=frozen" in _follow_lines(result)[0]
	assert "PROMOTE_CYCLE_FREEZE_MAX_SECS is not a positive integer" in result.stderr


def test_unprotected_alert_only_on_first_scheduled_run_after_midnight() -> None:
	routes = _routes(branch=_branch(protected=False))
	result, calls, curl = _run(routes, event="schedule", hour="00")
	assert result.returncode == 0, result.stderr
	assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=unprotected action=advanced from={OLD_SHA} to={MAIN_SHA} protected=false"]
	assert "CRITICAL" in curl and "ruleset" in curl
	_assert_patches_never_force(calls)
	for event, hour in (("schedule", "01"), ("push", "00"), ("workflow_dispatch", "00")):
		result, _, curl = _run(routes, event=event, hour=hour)
		assert _follow_lines(result) == [f"PROMOTE_CANDIDATE_FOLLOW outcome=advanced from={OLD_SHA} to={MAIN_SHA} protected=false"], (event, hour)
		assert curl == "", (event, hour)
	# A protected branch never alerts.
	result, _, curl = _run(_routes(), event="schedule", hour="00")
	assert "outcome=advanced" in _follow_lines(result)[0] and curl == ""


def test_rejected_patch_is_an_error_without_further_writes() -> None:
	routes = _routes(writes={f"PATCH repos/{REPO}/git/refs/heads/promote-candidate": [422, {"message": "Update is not a fast forward"}]})
	result, calls, _ = _run(routes)
	assert result.returncode == 1
	assert _follow_lines(result) == ["PROMOTE_CANDIDATE_FOLLOW outcome=error reason=patch_rejected protected=true"]
	assert [_method(c) for c in _writes(calls)] == ["PATCH"]
	_assert_patches_never_force(calls)


def test_failed_reads_fail_closed() -> None:
	for routes, reason in (
		(_routes(tag=[401, {"message": "Bad credentials"}]), "tag_read_failed"),
		(_routes(branch=[401, {"message": "Bad credentials"}]), "branch_read_failed"),
		(_routes(tag=_annotated_tag(), tag_object=[401, {"message": "Bad credentials"}]), "tag_object_read_failed"),
		(_routes(main_sha="not-a-sha"), "main_read_invalid"),
	):
		result, calls, _ = _run(routes)
		assert result.returncode == 1, reason
		assert f"outcome=error reason={reason}" in _follow_lines(result)[0]
		assert _writes(calls) == [], reason


def test_missing_token_or_repository_is_an_error() -> None:
	result, calls, _ = _run(_routes(), GH_TOKEN="")
	assert result.returncode == 1 and "reason=gh_token_missing" in _follow_lines(result)[0]
	assert calls == []
	result, calls, _ = _run(_routes(), GITHUB_REPOSITORY="bad repo")
	assert result.returncode == 1 and "reason=invalid_repository" in _follow_lines(result)[0]
	assert calls == []


def test_script_is_executable_and_defaults_live_in_the_script() -> None:
	assert os.access(SCRIPT, os.X_OK)
	text = SCRIPT.read_text(encoding="utf-8")
	assert 'PROMOTE_CYCLE_PIN_ENABLED:-true' in text
	assert "FOLLOW_DEFAULT_FREEZE_MAX_SECS=172800" in text
	assert "force=true" not in text


_USES_RE = re.compile(r"^\s*uses:\s*(\S+)", re.MULTILINE)


def test_internal_wrappers_use_promote_candidate_and_nothing_else_does() -> None:
	internal = sorted(p.name for p in WORKFLOWS_DIR.glob("internal-*.yml"))
	assert internal == sorted(INTERNAL_WRAPPERS)
	for name in INTERNAL_WRAPPERS:
		uses = [u for u in _USES_RE.findall((WORKFLOWS_DIR / name).read_text(encoding="utf-8")) if u.startswith("shubhodeep1/coding-workflows/")]
		assert uses, name
		for ref in uses:
			assert ref.endswith("@promote-candidate"), (name, ref)
	others = [p for p in WORKFLOWS_DIR.glob("*.yml") if p.name not in INTERNAL_WRAPPERS] + list(TEMPLATES_DIR.glob("*.yml"))
	for path in others:
		for ref in _USES_RE.findall(path.read_text(encoding="utf-8")):
			assert not ref.endswith("@promote-candidate"), (path, ref)


def test_follower_workflow_contract() -> None:
	import yaml

	workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
	on = workflow.get("on", workflow.get(True))
	assert on["push"] == {"branches": ["main"]}
	assert on["schedule"] == [{"cron": "17 * * * *"}]
	assert "workflow_dispatch" in on
	assert workflow["permissions"] == {"contents": "write"}
	assert workflow["concurrency"] == {"group": "promote-candidate-follow-${{ github.repository }}", "cancel-in-progress": False}
	job = workflow["jobs"]["follow"]
	assert job["if"] == "github.repository == 'shubhodeep1/coding-workflows' && github.ref == 'refs/heads/main'"
	checkout, follow = job["steps"]
	assert checkout["with"] == {"ref": "main", "fetch-depth": 1, "persist-credentials": False}
	env = follow["env"]
	assert env["GH_TOKEN"] == "${{ secrets.GH_PAT }}"
	assert env["PROMOTE_CYCLE_PIN_ENABLED"] == "${{ vars.PROMOTE_CYCLE_PIN_ENABLED || 'true' }}"
	assert env["PROMOTE_CYCLE_FREEZE_MAX_SECS"] == "${{ vars.PROMOTE_CYCLE_FREEZE_MAX_SECS || '172800' }}"
	assert follow["run"].strip() == "bash scripts/promote_candidate_follow.sh"
