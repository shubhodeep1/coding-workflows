#!/usr/bin/env python3
"""Stall recovery must not re-run the review-blocked judge on a head whose
deferred verdict only waits to merge (issue #9991, re-issued as #10243).

On 2026-11-23, 18 open PRs had a judge `merge` decision on their current head
that waited for the merge lease, and the judge was dispatched on the same head
again and again (#9573 x21, #9637 x20, #9703 x20). `_rb_judge_verdict_wait_check`
(scripts/orchestrate_poll_process.sh) skips the dispatch unless the head, the
lease, the base under the PR's files or a check result changed.

The functions run as extracted from the shipped poller against a fake `gh`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
LIB = REPO_ROOT / "scripts" / "pr_checks_lib.sh"
HEAD = "a" * 40
BASE = "b" * 40
NEW_BASE = "c" * 40
JUDGE_CTX = "ai-review/judge-merge-pending"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["FAKE_GH_DIR"])
args = sys.argv[1:]
with open(root / "calls.log", "a") as log:
	log.write(json.dumps(args) + "\n")
def fixture(name):
	path = root / name
	return path.read_text() if path.exists() else None
if args[:2] == ["workflow", "run"]:
	with open(root / "dispatches.log", "a") as log:
		log.write(" ".join(args) + "\n")
	sys.exit(0)
if args[:2] == ["api", "graphql"]:
	body = fixture("graphql.json")
	if body is None:
		sys.exit(1)
	sys.stdout.write(body)
	sys.exit(0)
if args[:2] == ["api", "user"]:
	sys.stdout.write("pipeline-bot\n")
	sys.exit(0)
endpoint = next((a for a in args[1:] if a.startswith("repos/")), "")
if "/compare/" in endpoint:
	body = fixture("compare.json")
	if body is None:
		sys.exit(1)
	sys.stdout.write(body)
	sys.exit(0)
if "/files" in endpoint:
	body = fixture("files.json")
	if body is None:
		sys.exit(1)
	sys.stdout.write(body)
	sys.exit(0)
sys.stderr.write("unexpected gh call: " + " ".join(args) + "\n")
sys.exit(1)
'''


def _extract(name: str) -> str:
	text = POLLER.read_text(encoding="utf-8")
	match = re.search(rf"^{re.escape(name)}\(\)\n\{{\n.*?^\}}\n", text, re.M | re.S)
	assert match, f"could not extract {name}"
	return match.group(0)


def _iso(seconds_ago: int) -> str:
	return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def _pr_json(number: int = 5, labels: tuple[str, ...] = ("ai:merge-lease-wait",), head: str = HEAD, auto_merge: bool = False) -> str:
	return json.dumps({"number": number, "state": "open", "head": {"sha": head, "ref": "ai/issue-1"}, "base": {"ref": "main"},
		"labels": [{"name": n} for n in labels], "auto_merge": ({"merge_method": "squash"} if auto_merge else None)})


def _verdict(ctx: str = JUDGE_CTX, *, login: str = "pipeline-bot", age: int = 3600, base: str | None = BASE, state: str = "SUCCESS") -> dict:
	desc = "Review-blocked judge decided merge; waiting for the merge lease"
	if base:
		desc += f"; base={base}"
	return {"__typename": "StatusContext", "context": ctx, "state": state, "createdAt": _iso(age),
		"description": desc, "creator": {"login": login}}


def _graphql(contexts: list[dict], *, head: str = HEAD, base: str = BASE, has_next: bool = False) -> str:
	return json.dumps({"data": {"repository": {"pullRequest": {"headRefOid": head, "baseRefOid": base,
		"commits": {"nodes": [{"commit": {"oid": head, "statusCheckRollup": {"contexts": {
			"pageInfo": {"hasNextPage": has_next}, "nodes": contexts}}}}]}}}}})


def _run(tmp_path: Path, pr_json: str, *, graphql: str | None, compare: dict | None = None, files: list | None = None,
		env: dict[str, str] | None = None, number: int = 5) -> tuple[subprocess.CompletedProcess, list[str], list[str]]:
	bin_dir = tmp_path / "bin"
	bin_dir.mkdir(exist_ok=True)
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	for name in ("graphql.json", "compare.json", "files.json", "calls.log", "dispatches.log"):
		(tmp_path / name).unlink(missing_ok=True)
	if graphql is not None:
		(tmp_path / "graphql.json").write_text(graphql, encoding="utf-8")
	if compare is not None:
		(tmp_path / "compare.json").write_text(json.dumps(compare), encoding="utf-8")
	if files is not None:
		(tmp_path / "files.json").write_text(json.dumps(files), encoding="utf-8")
	funcs = tmp_path / "funcs.sh"
	funcs.write_text("RB_JUDGE_VERDICT_WAIT_REASON=\"\"\n" + _extract("_rb_judge_verdict_wait_check") + "\n"
		+ _extract("_dispatch_rb_judge_for_pr") + "\n", encoding="utf-8")
	(tmp_path / "tracker.txt").write_text("", encoding="utf-8")
	script = f"""
set -euo pipefail
gh_retry() {{ "$@"; }}
_safe_gh_jq() {{ gh api "$@"; }}
source {str(LIB)!r}
source {str(funcs)!r}
phase_cap_can_dispatch() {{ return 0; }}
phase_cap_note_dispatch() {{ :; }}
_CONFLICT_DISPATCH_TRACKER=tracker.txt
STALL_IMPL_PR_JSON="$1"
rc=0
_dispatch_rb_judge_for_pr {number} 99 || rc=$?
echo "RC=${{rc}} REASON=${{RB_JUDGE_VERDICT_WAIT_REASON}}"
"""
	full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(tmp_path), "GITHUB_REPOSITORY": "o/r",
		"TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
	result = subprocess.run(["bash", "-c", script, "stall", pr_json], env=full_env, cwd=tmp_path,
		capture_output=True, text=True, check=False, timeout=120)
	calls = (tmp_path / "calls.log").read_text().splitlines() if (tmp_path / "calls.log").exists() else []
	dispatches = (tmp_path / "dispatches.log").read_text().splitlines() if (tmp_path / "dispatches.log").exists() else []
	return result, calls, dispatches


def _api_calls(calls: list[str]) -> list[str]:
	return [c for c in calls if not c.startswith('["workflow", "run"')]


def test_deferred_judge_merge_on_a_lease_waiter_is_skipped(tmp_path: Path) -> None:
	result, calls, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql([_verdict()]))
	assert result.returncode == 0, result.stderr
	assert "RC=4 REASON=lease_wait" in result.stdout
	assert re.search(rf"VERDICT_WAIT_SKIP source=stall pr=5 head={HEAD} verdict=judge_merge_pending reason=lease_wait waited_minutes=(59|60)", result.stdout), result.stdout
	assert dispatches == []
	# One GraphQL read plus the tick's shared login read.
	assert len(_api_calls(calls)) == 2


def test_head_gate_success_with_auto_merge_is_skipped(tmp_path: Path) -> None:
	result, _, dispatches = _run(tmp_path, _pr_json(labels=(), auto_merge=True),
		graphql=_graphql([_verdict("ai-review/head-gate", base=None)]))
	assert "RC=4 REASON=auto_merge_waiting" in result.stdout, result.stdout + result.stderr
	assert dispatches == []


@pytest.mark.parametrize("pr_json, graphql, reason", [
	(_pr_json(), _graphql([_verdict()], head="d" * 40), "head_changed"),
	(_pr_json(), _graphql([_verdict(login="someone-else")]), "no_verdict"),
	(_pr_json(), _graphql([_verdict(state="PENDING")]), "no_verdict"),
	(_pr_json(), _graphql([]), "no_verdict"),
	(_pr_json(), None, "read_failed"),
	(_pr_json(), _graphql([_verdict()], has_next=True), "checks_truncated"),
	(_pr_json(), _graphql([_verdict(age=3600), {"__typename": "CheckRun", "completedAt": _iso(60)}]), "checks_changed"),
	(_pr_json(), _graphql([_verdict(age=3600), {"__typename": "StatusContext", "context": "ci/final", "state": "SUCCESS",
		"createdAt": _iso(60), "description": "", "creator": {"login": "x"}}]), "checks_changed"),
	(_pr_json(), _graphql([_verdict(base=None)]), "base_unknown"),
])
def test_relevant_change_dispatches_the_judge(tmp_path: Path, pr_json: str, graphql: str | None, reason: str) -> None:
	result, _, dispatches = _run(tmp_path, pr_json, graphql=graphql)
	assert result.returncode == 0, result.stderr
	assert f"RC=0 REASON={reason}" in result.stdout, result.stdout + result.stderr
	assert f"VERDICT_WAIT_RECHECK source=stall pr=5 outcome=dispatch reason={reason}" in result.stdout
	assert len(dispatches) == 1 and "review_rb_judge_dispatch.yml" in dispatches[0]


def test_older_pipeline_checks_do_not_count_as_a_change(tmp_path: Path) -> None:
	contexts = [_verdict(age=3600), {"__typename": "CheckRun", "completedAt": _iso(7200)},
		{"__typename": "StatusContext", "context": "ai-review/head-gate", "state": "PENDING", "createdAt": _iso(60),
		"description": "", "creator": {"login": "pipeline-bot"}}]
	result, _, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql(contexts))
	assert "RC=4" in result.stdout, result.stdout + result.stderr
	assert dispatches == []


def test_lease_granted_label_removed_dispatches_without_a_read(tmp_path: Path) -> None:
	result, calls, dispatches = _run(tmp_path, _pr_json(labels=()), graphql=_graphql([_verdict()]))
	assert "RC=0" in result.stdout, result.stdout + result.stderr
	assert len(dispatches) == 1
	assert _api_calls(calls) == []


def test_base_moved_under_the_pr_files_dispatches(tmp_path: Path) -> None:
	compare = {"files": [{"filename": "scripts/shared.sh"}]}
	files = [[{"filename": "scripts/shared.sh"}]]
	result, calls, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql([_verdict()], base=NEW_BASE), compare=compare, files=files)
	assert "RC=0 REASON=base_moved_overlap" in result.stdout, result.stdout + result.stderr
	assert len(dispatches) == 1
	assert any(f"compare/{BASE}...{NEW_BASE}" in c for c in calls)


def test_base_moved_elsewhere_is_still_skipped(tmp_path: Path) -> None:
	compare = {"files": [{"filename": "docs/other.md"}]}
	files = [[{"filename": "scripts/shared.sh"}]]
	result, _, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql([_verdict()], base=NEW_BASE), compare=compare, files=files)
	assert "RC=4 REASON=lease_wait" in result.stdout, result.stdout + result.stderr
	assert dispatches == []


@pytest.mark.parametrize("compare, files", [
	(None, [[{"filename": "x"}]]),
	({"files": [{"filename": "x"}]}, None),
])
def test_base_check_failure_dispatches(tmp_path: Path, compare: dict | None, files: list | None) -> None:
	result, _, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql([_verdict()], base=NEW_BASE), compare=compare, files=files)
	assert "RC=0 REASON=base_check_failed" in result.stdout, result.stdout + result.stderr
	assert len(dispatches) == 1


def test_truncated_compare_counts_as_overlap(tmp_path: Path) -> None:
	compare = {"files": [{"filename": f"f{i}"} for i in range(300)]}
	result, _, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql([_verdict()], base=NEW_BASE), compare=compare,
		files=[[{"filename": "scripts/shared.sh"}]])
	assert "RC=0 REASON=base_moved_overlap" in result.stdout, result.stdout + result.stderr
	assert len(dispatches) == 1


@pytest.mark.parametrize("value", ["false", "0", "off"])
def test_switch_off_dispatches_with_no_extra_call(tmp_path: Path, value: str) -> None:
	result, calls, dispatches = _run(tmp_path, _pr_json(), graphql=_graphql([_verdict()]),
		env={"VERDICT_WAIT_DISPATCH_SKIP_ENABLED": value})
	assert "RC=0" in result.stdout, result.stdout + result.stderr
	assert len(dispatches) == 1 and _api_calls(calls) == []
	assert "VERDICT_WAIT" not in result.stdout


def test_another_prs_payload_dispatches_with_no_extra_call(tmp_path: Path) -> None:
	result, calls, dispatches = _run(tmp_path, _pr_json(number=6), graphql=_graphql([_verdict()]))
	assert "RC=0" in result.stdout, result.stdout + result.stderr
	assert len(dispatches) == 1 and _api_calls(calls) == []


def test_call_sites_treat_rc4_as_a_benign_skip() -> None:
	text = POLLER.read_text(encoding="utf-8")
	assert text.count("reason=rb_judge_verdict_wait pr=") == 3
	for var in ("_skip_redirect_rc", "rb_dispatch_rc", "_std_rb_rc"):
		assert re.search(rf'"\$\{{{var}\}}" -eq 4', text), var
	managed = text[text.index('if [ "${rb_dispatch_rc}" -eq 4 ]'):]
	managed = managed[:managed.index("fi\n")]
	assert 'STALL_RECOVERY_EFFECTIVE_ACTION="dispatch_rb_judge_skipped_verdict_wait"' in managed
	assert 'STALL_RECOVERY_SHOULD_INCREMENT="false"' in managed
	assert "tg_notify" not in managed
	std = text[text.index('elif [ "${_std_rb_rc}" -eq 4 ]'):]
	std = std[:std.index("else")]
	assert 'STALL_RECOVERY_SHOULD_INCREMENT="false"' in std


def test_poller_workflow_passes_the_switch_with_a_default() -> None:
	poll = (REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml").read_text(encoding="utf-8")
	assert poll.count("VERDICT_WAIT_DISPATCH_SKIP_ENABLED: ${{ vars.VERDICT_WAIT_DISPATCH_SKIP_ENABLED || 'true' }}") == 2
