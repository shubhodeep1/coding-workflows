#!/usr/bin/env python3
"""Rate-limit requeue for clarify and the review gate (issue #10031).

A GH_PAT primary rate limit in clarify's "Resolve integration ref" step
(resolver rc=75) or in the review gate's PR read (AUTOFIX_GATE_PR_READ_FAILED
rc=75) used to leave the item idle until a person noticed
(binance-blessings #735, #728, PR #716; tele-funtoken-msg-scoring #5698,
#5711, PR #5718). These tests drive the real step bodies and poller functions
with a fake `gh`:

- clarify.yml writes `rate_limited=true` only on the rc=75 branch, and the
  "Queue rate-limited clarify for poller" step posts the exact job-token
  marker and the `ai:reclarify-requeue` label;
- the poller's replay_failed_reclarify_commands re-enters clarify after the
  recorded reset, waits before it, escalates after the cap, and refuses
  untrusted or forged input;
- the gate's _gate_write_rate_limit_marker posts the exact job-token marker;
- run_review_gate_rate_limit_requeue_pass re-dispatches a PR whose newest
  gate marker has no later review comment, and skips / escalates otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLARIFY = REPO_ROOT / ".github" / "workflows" / "clarify.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"
GATE_SCRIPT = REPO_ROOT / "scripts" / "review_autofix_step_evaluate_gate.sh"
POLLER = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
AGENTS_FRAGMENT = REPO_ROOT / "agents.d" / "10031-rate-limit-requeue.md"
CHANGELOG_FRAGMENT = REPO_ROOT / "changelog.d" / "10031-rate-limit-requeue.md"

LOGIN = "pipeline-bot"
HEAD_A = "a" * 40
HEAD_B = "b" * 40

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_GH_LOG")
if log:
	with open(log, "a", encoding="utf-8") as fh:
		fh.write(json.dumps({"token": os.environ.get("GH_TOKEN", ""),
			"signing_post": os.environ.get("AI_COMMENT_SIGNING_POST", ""), "args": args}) + "\n")
def out(text):
	sys.stdout.write(text)
	sys.exit(0)
if args[:2] == ["api", "rate_limit"]:
	if "--jq" in args and args[args.index("--jq") + 1] == ".resources.core.remaining":
		out(os.environ.get("FAKE_BUDGET", "700") + "\n")
	out(os.environ.get("FAKE_RESET", "") + "\n")
if args[:2] == ["api", "user"]:
	out(os.environ.get("FAKE_USER_LOGIN", "owner") + "\n")
if args[:2] == ["issue", "list"]:
	out('[{"number":7}]\n')
if args[:2] == ["api", "--paginate"]:
	out(open(os.environ["FAKE_COMMENTS"], encoding="utf-8").read())
if args[:2] == ["api", "repos/o/r/issues/7"]:
	out(open(os.environ["FAKE_ISSUE"], encoding="utf-8").read())
if args[:2] == ["api", "-X"] and len(args) > 3 and args[3].endswith("/labels") and os.environ.get("FAKE_LABEL_RC"):
	sys.exit(int(os.environ["FAKE_LABEL_RC"]))
if args[:2] == ["api", "-X"]:
	sys.exit(int(os.environ.get("FAKE_WRITE_RC", "0")))
if args[:2] == ["label", "create"]:
	sys.exit(0)
sys.exit(1)
'''


def _require_jq() -> None:
	if shutil.which("jq") is None:
		raise unittest.SkipTest("jq binary not available in test environment")


def _fake_gh_dir(tmp: Path) -> Path:
	bin_dir = tmp / "bin"
	bin_dir.mkdir()
	gh = bin_dir / "gh"
	gh.write_text(FAKE_GH, encoding="utf-8")
	gh.chmod(0o755)
	return bin_dir


def _base_env(bin_dir: Path, log: Path) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if not k.startswith("AI_COMMENT_")}
	env.update({"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_LOG": str(log), "PYTHONDONTWRITEBYTECODE": "1"})
	return env


def _calls(log: Path) -> list[dict]:
	if not log.exists():
		return []
	return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _body(call: dict) -> str:
	args = call["args"]
	for i, arg in enumerate(args[:-1]):
		if arg == "-f" and args[i + 1].startswith("body="):
			return args[i + 1][len("body="):]
	return ""


def _iso(epoch: float) -> str:
	return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bash_function(script: str, signature: str) -> str:
	start = script.index(signature)
	end = script.index("\n}\n", start) + 3
	return script[start:end]


# ---------------------------------------------------------------------------
# clarify.yml
# ---------------------------------------------------------------------------

def _clarify_step(name: str) -> str:
	text = CLARIFY.read_text(encoding="utf-8")
	start = text.index(f"      - name: {name}\n")
	end = text.index("\n      - name: ", start + 1)
	return text[start:end]


def _step_run_body(step: str) -> str:
	lines = step.splitlines()
	idx = next(i for i, line in enumerate(lines) if line.strip() == "run: |")
	body = []
	for line in lines[idx + 1:]:
		if line.strip() and not line.startswith("          "):
			break
		body.append(line[10:])
	return "\n".join(body) + "\n"


def test_clarify_refctx_writes_rate_limited_only_on_rc75_branch() -> None:
	step = _clarify_step("Resolve integration ref")
	assert "id: refctx" in step
	write = 'echo "rate_limited=true" >> "$GITHUB_OUTPUT"'
	assert step.count(write) == 1
	branch_start = step.index('elif [ "${resolver_rc}" -eq 75 ] && [ "${resolver_ci_triage_routing_on}" = "true" ]; then')
	branch_end = step.index("elif", branch_start + 4)
	branch = step[branch_start:branch_end]
	assert write in branch
	assert "outcome=rate_limited" in branch and "refusing to fall back to the default branch." in branch
	assert branch.index(write) < branch.index("exit 1")
	for other in ('elif [ "${resolver_rc}" -eq 3 ]; then', 'elif [ "${resolver_rc}" -eq 2 ]'):
		other_start = step.index(other)
		other_end = step.index("elif" if "-eq 3" in other else "else", other_start + 4)
		assert "rate_limited" not in step[other_start:other_end]


def test_clarify_rate_limit_step_condition_and_env() -> None:
	text = CLARIFY.read_text(encoding="utf-8")
	assert text.index("- name: Queue failed reclarify for poller") < text.index("- name: Queue rate-limited clarify for poller")
	step = _clarify_step("Queue rate-limited clarify for poller")
	assert "if: env.CLAUDE_CAPACITY_WAIT != 'true' && failure() && steps.refctx.outputs.rate_limited == 'true'" in step
	assert "GH_TOKEN: ${{ github.token }}" in step
	assert "RATE_LIMIT_READ_TOKEN: ${{ secrets.GH_PAT }}" in step
	assert "SOURCE_COMMENT_ID: ${{ github.event.comment.id || '0' }}" in step
	body = _step_run_body(step)
	assert "${{" not in body, "values reach the body through env only"
	assert body.rstrip().endswith("exit 0")


def _run_clarify_step(tmp: Path, **overrides: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
	bin_dir = _fake_gh_dir(tmp)
	log = tmp / "gh.log"
	script = tmp / "step.sh"
	script.write_text(_step_run_body(_clarify_step("Queue rate-limited clarify for poller")), encoding="utf-8")
	env = _base_env(bin_dir, log)
	env.update({"ISSUE_NUMBER": "7", "GITHUB_RUN_ID": "44987722984", "REPOSITORY": "o/r", "EVENT_NAME": "issue_comment",
		"SOURCE_COMMENT_ID": "55", "GH_TOKEN": "job-token", "RATE_LIMIT_READ_TOKEN": "pat-token",
		"FAKE_RESET": str(int(time.time()) + 600)})
	env.update(overrides)
	result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
	return result, _calls(log)


def test_clarify_rate_limit_step_writes_marker_and_label() -> None:
	reset = int(time.time()) + 600
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_step(Path(tmp), FAKE_RESET=str(reset))
	assert result.returncode == 0, result.stderr
	rate = [c for c in calls if c["args"][:2] == ["api", "rate_limit"]]
	assert rate and rate[0]["token"] == "pat-token"
	posts = [c for c in calls if c["args"][:3] == ["api", "-X", "POST"]]
	assert [c["args"][3] for c in posts] == ["repos/o/r/issues/7/comments", "repos/o/r/issues/7/labels"]
	assert all(c["token"] == "job-token" for c in posts)
	assert _body(posts[0]) == f"<!-- ai:clarify-rate-limited:v1 run=44987722984 reset={reset} event=issue_comment source=55 -->"
	assert "labels[]=ai:reclarify-requeue" in posts[1]["args"]
	assert f"CLARIFY_RATE_LIMIT_MARKER issue=7 run=44987722984 reset={reset} outcome=written" in result.stdout


def test_clarify_rate_limit_step_clamps_reset_and_normalises_source() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		before = int(time.time())
		result, calls = _run_clarify_step(Path(tmp), FAKE_RESET=str(before + 99999), EVENT_NAME="issues", SOURCE_COMMENT_ID="0")
		after = int(time.time())
	assert result.returncode == 0, result.stderr
	marker = _body(next(c for c in calls if c["args"][:4] == ["api", "-X", "POST", "repos/o/r/issues/7/comments"]))
	m = re.fullmatch(r"<!-- ai:clarify-rate-limited:v1 run=44987722984 reset=(\d{10}) event=issues source=0 -->", marker)
	assert m, marker
	assert before + 3600 <= int(m.group(1)) <= after + 3600
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_step(Path(tmp), FAKE_RESET="garbage", EVENT_NAME="workflow_dispatch", SOURCE_COMMENT_ID="55")
	marker = _body(next(c for c in calls if c["args"][:4] == ["api", "-X", "POST", "repos/o/r/issues/7/comments"]))
	assert re.fullmatch(r"<!-- ai:clarify-rate-limited:v1 run=44987722984 reset=\d{10} event=other source=0 -->", marker), marker


def test_clarify_rate_limit_step_write_failure_exits_zero_without_label() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_step(Path(tmp), FAKE_WRITE_RC="1")
	assert result.returncode == 0, result.stderr
	assert not any("repos/o/r/issues/7/labels" in c["args"] for c in calls)
	assert "outcome=write_failed" in result.stdout
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_clarify_step(Path(tmp), ISSUE_NUMBER="")
	assert result.returncode == 0
	assert not [c for c in calls if c["args"][:2] == ["api", "-X"]]


# ---------------------------------------------------------------------------
# Poller: rate-limited clarify replay
# ---------------------------------------------------------------------------

def _user(cid: int, body: str, created: float, association: str = "OWNER", login: str = "owner") -> dict:
	return {"id": cid, "created_at": _iso(created), "body": body, "user": {"type": "User", "login": login},
		"author_association": association}


def _bot(cid: int, body: str, created: float) -> dict:
	return {"id": cid, "created_at": _iso(created), "body": body, "user": {"type": "Bot", "login": "github-actions[bot]"}}


def _rl_marker(cid: int, created: float, reset: float, run: int = 900, event: str = "issue_comment", source: int = 11) -> dict:
	return _bot(cid, f"<!-- ai:clarify-rate-limited:v1 run={run} reset={int(reset)} event={event} source={source} -->", created)


def _run_poller(rows: list[dict], issue_association: str = "OWNER", extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess, list[dict]]:
	_require_jq()
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		bin_dir = _fake_gh_dir(tmp_path)
		log = tmp_path / "gh.log"
		comments = tmp_path / "comments.json"
		comments.write_text(json.dumps([rows]), encoding="utf-8")
		issue = tmp_path / "issue.json"
		issue.write_text(json.dumps({"number": 7, "state": "open", "labels": [{"name": "ai:reclarify-requeue"}],
			"author_association": issue_association}), encoding="utf-8")
		env = _base_env(bin_dir, log)
		env.update({"GITHUB_REPOSITORY": "o/r", "RECLARIFY_REQUEUE_SWEEP_ONLY": "true",
			"RECLARIFY_UNROUTED_DETECT_ENABLED": "false", "GH_TOKEN": "test", "FAKE_COMMENTS": str(comments),
			"FAKE_ISSUE": str(issue), "FAKE_BUDGET": "700"})
		env.update(extra_env or {})
		result = subprocess.run(["bash", str(POLLER)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
		assert result.returncode == 0, result.stderr
		writes = [c for c in _calls(log) if c["args"][:2] == ["api", "-X"]]
		return result, writes


def _comment_posts(writes: list[dict]) -> list[str]:
	return [_body(c) for c in writes if c["args"][2:4] == ["POST", "repos/o/r/issues/7/comments"]]


DELETE_LABEL = ["api", "-X", "DELETE", "repos/o/r/issues/7/labels/ai%3Areclarify-requeue"]


def test_poller_replays_rate_limited_clarify_after_reset() -> None:
	now = time.time()
	source = _user(11, "/reclarify", now - 7300)
	marker = _rl_marker(12, now - 7200, now - 7200 + 600)
	result, writes = _run_poller([source, marker])
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=11 -->"]
	assert any(c["args"][:4] == DELETE_LABEL for c in writes)
	assert "CLARIFY_RATE_LIMIT_REQUEUE issue=7 run=900 outcome=requeued attempt=1/3" in result.stdout


def test_poller_waits_for_reset_and_keeps_label() -> None:
	now = time.time()
	source = _user(11, "/reclarify", now - 60)
	marker = _rl_marker(12, now - 30, now + 600)
	result, writes = _run_poller([source, marker])
	assert writes == []
	assert "CLARIFY_RATE_LIMIT_REQUEUE issue=7 run=900 reset=" in result.stdout and "outcome=waiting_for_reset" in result.stdout


def test_poller_clamps_far_future_reset_to_marker_time() -> None:
	now = time.time()
	source = _user(11, "/reclarify", now - 7300)
	marker = _rl_marker(12, now - 7200, now + 999999)
	result, writes = _run_poller([source, marker])
	assert _comment_posts(writes), result.stdout


def test_poller_escalates_after_three_replays() -> None:
	now = time.time()
	rows = [_user(11, "/reclarify", now - 9000)]
	for idx, run in enumerate((801, 802, 803)):
		rows.append(_user(13 + idx, f"/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run={run} -->", now - 8000 + idx))
	rows.append(_rl_marker(20, now - 7200, now - 7000))
	result, writes = _run_poller(rows)
	labels = [c for c in writes if c["args"][2:4] == ["POST", "repos/o/r/issues/7/labels"]]
	assert labels and "labels[]=ai:needs-human" in labels[0]["args"]
	posts = _comment_posts(writes)
	assert len(posts) == 1 and posts[0].endswith("<!-- ai:clarify-rate-limit-escalated:v1 run=900 -->")
	assert "rate limit" in posts[0]
	assert not any(line.startswith("/") for line in posts[0].splitlines())
	assert any(c["args"][:4] == DELETE_LABEL for c in writes)
	assert "outcome=escalated attempts=3/3" in result.stdout


def test_poller_cap_resets_after_successful_clarify() -> None:
	now = time.time()
	rows = [_user(11, "/reclarify", now - 9000)]
	for idx, run in enumerate((801, 802, 803)):
		rows.append(_user(13 + idx, f"/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run={run} -->", now - 8000 + idx))
	rows.append(_user(17, "/answer [auto-answered-by-clarify]", now - 7900))
	rows.append(_user(18, "/reclarify", now - 7300))
	rows.append(_rl_marker(20, now - 7200, now - 7000, source=18))
	result, writes = _run_poller(rows)
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=18 -->"]
	assert "outcome=requeued attempt=1/3" in result.stdout


def test_poller_marks_rate_limited_clarify_obsolete_after_success() -> None:
	now = time.time()
	rows = [_user(11, "/reclarify", now - 7300), _rl_marker(12, now - 7200, now - 7000),
		_user(13, "/answer [auto-answered-by-clarify]", now - 7100)]
	result, writes = _run_poller(rows)
	assert _comment_posts(writes) == []
	assert any(c["args"][:4] == DELETE_LABEL for c in writes)
	assert "outcome=obsolete" in result.stdout
	rows = [_user(11, "/reclarify", now - 7300), _rl_marker(12, now - 7200, now - 7000),
		_user(13, "/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 -->", now - 7100)]
	result, writes = _run_poller(rows)
	assert _comment_posts(writes) == [] and "outcome=already_requeued" in result.stdout


def test_poller_refuses_untrusted_source_or_forged_marker() -> None:
	now = time.time()
	# Untrusted source comment.
	rows = [_user(11, "/reclarify", now - 7300, association="NONE", login="stranger"), _rl_marker(12, now - 7200, now - 7000)]
	result, writes = _run_poller(rows)
	assert _comment_posts(writes) == [] and any(c["args"][:4] == DELETE_LABEL for c in writes)
	assert "reason=untrusted_source" in result.stdout
	# issues event: the live issue author decides.
	rows = [_rl_marker(12, now - 7200, now - 7000, event="issues", source=0)]
	result, writes = _run_poller(rows, issue_association="NONE")
	assert _comment_posts(writes) == [] and "reason=untrusted_source" in result.stdout
	result, writes = _run_poller(rows, issue_association="MEMBER")
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=0 -->"]
	# A forged non-Bot marker is never read.
	forged = {**_rl_marker(12, now - 7200, now - 7000), "user": {"type": "User", "login": "attacker"}}
	result, writes = _run_poller([_user(11, "/reclarify", now - 7300), forged])
	assert writes == []
	# A marker that is not the whole body is never read either.
	inline = _bot(12, f"x <!-- ai:clarify-rate-limited:v1 run=900 reset={int(now - 7000)} event=issue_comment source=11 -->", now - 7200)
	result, writes = _run_poller([_user(11, "/reclarify", now - 7300), inline])
	assert writes == []


def test_poller_escalation_label_failure_keeps_requeue_label_and_posts_no_marker() -> None:
	now = time.time()
	rows = [_user(11, "/reclarify", now - 9000)]
	for idx, run in enumerate((801, 802, 803)):
		rows.append(_user(13 + idx, f"/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run={run} source=11 -->", now - 8000 + idx))
	rows.append(_rl_marker(20, now - 7200, now - 7000))
	result, writes = _run_poller(rows, extra_env={"FAKE_LABEL_RC": "1"})
	assert _comment_posts(writes) == []
	assert not any(c["args"][:4] == DELETE_LABEL for c in writes)
	assert "outcome=escalated attempts=3/3 label=failed marker=skipped" in result.stdout


def test_poller_replays_automated_release_source_as_source_zero() -> None:
	now = time.time()
	for body in ("/reclarify\n<!-- ai:pr-dependency-released:7 -->", "/reclarify\n<!-- ai:partial-delivery-restart:7 -->"):
		rows = [_user(11, body, now - 7300, login=LOGIN), _rl_marker(12, now - 7200, now - 7000)]
		result, writes = _run_poller(rows, extra_env={"FAKE_USER_LOGIN": LOGIN})
		assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=0 -->"], result.stdout
	# The poller's replay of a release is automated too; a human source keeps its id.
	rows = [_user(10, "/reclarify\n<!-- ai:pr-dependency-released:7 -->", now - 7400, login=LOGIN),
		_user(11, "/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=10 -->", now - 7300, login=LOGIN),
		_rl_marker(12, now - 7200, now - 7000)]
	result, writes = _run_poller(rows, extra_env={"FAKE_USER_LOGIN": LOGIN})
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=0 -->"], result.stdout
	rows = [_user(11, "/reclarify\n<!-- ai:pr-dependency-released:7 -->", now - 7300), _rl_marker(12, now - 7200, now - 7000)]
	result, writes = _run_poller(rows, extra_env={"FAKE_USER_LOGIN": LOGIN})
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=11 -->"], result.stdout


def test_poller_chained_automated_rate_limit_replay_keeps_source_zero() -> None:
	now = time.time()
	# Replay 1 (source=0) was itself rate-limited: the new marker names it.
	rows = [_user(11, "/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=800 source=0 -->", now - 7300, login=LOGIN),
		_rl_marker(12, now - 7200, now - 7000)]
	result, writes = _run_poller(rows, extra_env={"FAKE_USER_LOGIN": LOGIN})
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=0 -->"], result.stdout
	assert "outcome=requeued attempt=2/3" in result.stdout
	# A chained replay of a human source keeps human semantics.
	rows = [_user(10, "/reclarify", now - 7400),
		_user(11, "/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=800 source=10 -->", now - 7300, login=LOGIN),
		_rl_marker(12, now - 7200, now - 7000)]
	result, writes = _run_poller(rows, extra_env={"FAKE_USER_LOGIN": LOGIN})
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:clarify-rate-limit-replay:v1 run=900 source=11 -->"], result.stdout


def test_clarify_route_classifies_rate_limit_replay() -> None:
	text = CLARIFY.read_text(encoding="utf-8")
	block = text[text.index("# BEGIN rate-limit replay classification (#10031)"):text.index("# END rate-limit replay classification (#10031)")]
	assert '"/reclarify [auto-requeued-by-poller]"' in block
	assert "ai:clarify-rate-limit-replay:v1" in block and "source=(0|[1-9][0-9]{0,19})" in block
	assert '"${_rel_login,,}" = "${COMMENT_USER_LOGIN,,}"' in block
	assert '[ "${_rl_reason}" = "verified" ] && [ "${_rl_source}" = "0" ]' in block
	assert 'IS_FORCED_RECLARIFY="false"' in block and 'IS_AUTOMATED_RELEASE="true"' in block
	assert "gate=rate_limit_replay" in block
	# Classified before the orchestrator fast-path defer reads IS_FORCED_RECLARIFY.
	assert text.index("# END rate-limit replay classification (#10031)") < text.index("reason=orchestrator_fast_path outcome=defer")


def test_poller_newer_human_request_keeps_label() -> None:
	now = time.time()
	rows = [_user(11, "/reclarify", now - 7300), _rl_marker(12, now - 7200, now - 7000), _user(13, "/reclarify please", now - 7100)]
	result, writes = _run_poller(rows)
	assert writes == [] and "reason=newer_request" in result.stdout


def test_poller_newest_marker_kind_decides_path() -> None:
	now = time.time()
	rows = [_user(11, "/reclarify", now - 7300), _rl_marker(12, now - 7200, now - 7000),
		_bot(13, "<!-- ai:reclarify-requeue:v1 source=11 -->", now - 7100)]
	result, writes = _run_poller(rows)
	assert _comment_posts(writes) == ["/reclarify [auto-requeued-by-poller]\n<!-- ai:reclarify-replay:v1 source=11 -->"]
	assert "CLARIFY_RATE_LIMIT_REQUEUE" not in result.stdout


def test_poller_constants() -> None:
	script = POLLER.read_text(encoding="utf-8")
	assert re.search(r"^RATE_LIMIT_REQUEUE_MAX_ATTEMPTS=3$", script, re.MULTILINE)
	assert re.search(r"^RATE_LIMIT_REQUEUE_GRACE_SECS=60$", script, re.MULTILINE)
	assert re.search(r"^REVIEW_GATE_RATE_LIMIT_REQUEUE_MAX_PER_CYCLE=5$", script, re.MULTILINE)
	# Defined before the reclarify-only early exit.
	assert script.index("RATE_LIMIT_REQUEUE_MAX_ATTEMPTS=3") < script.index('if [ "${RECLARIFY_REQUEUE_SWEEP_ONLY:-false}" = "true" ]; then')


# ---------------------------------------------------------------------------
# Review gate marker
# ---------------------------------------------------------------------------

def _run_gate_marker(tmp: Path, **overrides: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
	bin_dir = _fake_gh_dir(tmp)
	log = tmp / "gh.log"
	func = _bash_function(GATE_SCRIPT.read_text(encoding="utf-8"), "_gate_write_rate_limit_marker()\n{")
	script = tmp / "gate.sh"
	script.write_text("set -euo pipefail\n" + func + "_gate_write_rate_limit_marker\n", encoding="utf-8")
	env = _base_env(bin_dir, log)
	env.update({"PR_NUMBER": "716", "GITHUB_RUN_ID": "44983158668", "PR_HEAD_SHA": HEAD_A, "REPOSITORY": "o/r",
		"GH_TOKEN": "pat-token", "AI_COMMENT_JOB_TOKEN": "job-token", "FAKE_RESET": str(int(time.time()) + 600)})
	env.update(overrides)
	result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
	return result, _calls(log)


def test_gate_marker_posts_with_job_token() -> None:
	reset = int(time.time()) + 600
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_gate_marker(Path(tmp), FAKE_RESET=str(reset))
	assert result.returncode == 0, result.stderr
	posts = [c for c in calls if c["args"][:3] == ["api", "-X", "POST"]]
	assert len(posts) == 1 and posts[0]["args"][3] == "repos/o/r/issues/716/comments"
	assert posts[0]["token"] == "job-token" and posts[0]["signing_post"] == "false"
	body = _body(posts[0])
	assert body.splitlines()[-1] == f"<!-- ai:review-gate-rate-limited:v1 head={HEAD_A} reset={reset} run=44983158668 -->"
	assert "rate limit" in body.splitlines()[0]
	assert "REVIEW_GATE_RATE_LIMIT_MARKER pr=716 run=44983158668" in result.stdout and "outcome=written" in result.stdout


def test_gate_marker_unknown_head_and_missing_token() -> None:
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_gate_marker(Path(tmp), PR_HEAD_SHA="", FAKE_RESET="bogus")
	body = _body(next(c for c in calls if c["args"][:3] == ["api", "-X", "POST"]))
	assert re.search(r"\n<!-- ai:review-gate-rate-limited:v1 head=unknown reset=\d{10} run=44983158668 -->$", body), body
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_gate_marker(Path(tmp), AI_COMMENT_JOB_TOKEN="")
	assert result.returncode == 0
	assert calls == []
	assert "REVIEW_GATE_RATE_LIMIT_MARKER pr=716 run=44983158668 outcome=skipped reason=no_job_token" in result.stdout
	with tempfile.TemporaryDirectory() as tmp:
		result, calls = _run_gate_marker(Path(tmp), FAKE_WRITE_RC="1")
	assert result.returncode == 0 and "outcome=write_failed" in result.stdout


def test_gate_marker_runs_only_on_rc75() -> None:
	script = GATE_SCRIPT.read_text(encoding="utf-8")
	assert script.count("_gate_write_rate_limit_marker || true") == 1
	block = '''    if [ "${_pr_gate_rc}" -eq 75 ]; then
      _gate_write_rate_limit_marker || true
    fi
    echo "::error::AUTOFIX_GATE_PR_READ_FAILED pr=${PR_NUMBER} rc=${_pr_gate_rc}"
    exit 1'''
	assert block in script
	assert script.index("_gate_write_rate_limit_marker()\n{") < script.index(block)


def test_review_gate_step_has_job_token_and_file_size() -> None:
	text = REVIEW_WORKFLOW.read_text(encoding="utf-8")
	start = text.index("      - name: Evaluate review gate\n")
	end = text.index("\n        run:", start)
	assert "AI_COMMENT_JOB_TOKEN: ${{ github.token }}" in text[start:end]
	assert len(REVIEW_WORKFLOW.read_bytes()) < 480_000


# ---------------------------------------------------------------------------
# Poller: review gate rate-limit requeue pass
# ---------------------------------------------------------------------------

BATCH_START = "declare -A _STANDALONE_CLEAN_PRS=()"
BATCH_END = "for (( sidx=0; sidx<STANDALONE_COUNT; sidx++ )); do"


def _gate_body(run: int, reset: float, head: str = HEAD_A) -> str:
	return ("The AI review gate could not read this pull request because the GitHub API rate limit was reached.\n\n"
		f"<!-- ai:review-gate-rate-limited:v1 head={head} reset={int(reset)} run={run} -->")


def _requeue_body(run: int, head: str = HEAD_A) -> str:
	return f"Review gate rate-limit requeue: re-dispatched.\n\n<!-- ai:review-gate-rate-limit-requeue:v1 run={run} head={head} -->"


def _row(number: int, head: str = HEAD_A) -> dict:
	return {"number": number, "headRefName": f"feature/{number}", "headRefOid": head, "baseRefName": "main", "isDraft": False,
		"isCrossRepository": False, "headRepositoryOwner": {"login": "o"}}


def _node(number: int, comments: list[tuple[str, str]], head: str = HEAD_A, labels: tuple[str, ...] = (),
		closing: tuple[tuple[int, tuple[str, ...]], ...] = ()) -> dict:
	return {
		"number": number, "state": "OPEN", "headRefName": f"feature/{number}", "headRefOid": head, "isDraft": False,
		"mergeStateStatus": "BLOCKED", "updatedAt": _iso(time.time()),
		"labels": {"nodes": [{"name": name} for name in labels]},
		"closingIssuesReferences": {"totalCount": len(closing),
			"nodes": [{"number": n, "labels": {"nodes": [{"name": x} for x in names]}} for n, names in closing]},
		"comments": {"pageInfo": {"hasPreviousPage": False},
			"nodes": [{"body": body, "databaseId": i + 1, "author": {"login": author}} for i, (author, body) in enumerate(comments)]},
	}


def _run_pass(rows: list[dict], nodes: list[dict], dispatch_rc: dict[int, int] | None = None, dump_map: bool = False, fail_label: bool = False) -> dict:
	_require_jq()
	script = POLLER.read_text(encoding="utf-8")
	batch = script[script.index(BATCH_START):script.index(BATCH_END)]
	constants = "\n".join(re.findall(r"^(?:ISSUELESS_REVIEW_RETRY_[A-Z_]+=(?:'[^'\n]*'|\"[^\"\n]*\")|RATE_LIMIT_REQUEUE_[A-Z_]+=[0-9]+|REVIEW_GATE_RATE_LIMIT_REQUEUE_[A-Z_]+=[0-9]+)$", script, re.MULTILINE))
	functions = (_bash_function(script, "_review_gate_rate_limit_requeue_decide() {")
		+ _bash_function(script, "run_review_gate_rate_limit_requeue_pass() {"))
	dispatch_rc = dispatch_rc or {}
	with tempfile.TemporaryDirectory() as tmp:
		tmp_path = Path(tmp)
		prs_file = tmp_path / "prs.json"
		prs_file.write_text(json.dumps(rows), encoding="utf-8")
		graphql_file = tmp_path / "graphql.json"
		graphql_file.write_text(json.dumps({"data": {"repository": {f"p{n['number']}": n for n in nodes}}}), encoding="utf-8")
		dispatch_log = tmp_path / "dispatch.log"
		post_dir = tmp_path / "posts"
		post_dir.mkdir()
		cases = "".join(f"{n}) return {rc} ;; " for n, rc in dispatch_rc.items())
		lines = [
			"set -euo pipefail",
			"GITHUB_REPOSITORY=o/r",
			constants,
			"STANDALONE_CONFLICT_ATTEMPTS_MARKER='<!-- ai:standalone-conflict-attempts:v1 '",
			f"STANDALONE_PRS=\"$(cat {shlex.quote(str(prs_file))})\"",
			"STANDALONE_COUNT=\"$(printf '%s' \"${STANDALONE_PRS}\" | jq 'length')\"",
			"_conflict_sweep_list_failed=false",
			f"GRAPHQL_FILE={shlex.quote(str(graphql_file))}",
			f"DISPATCH_LOG={shlex.quote(str(dispatch_log))}",
			f"POST_DIR={shlex.quote(str(post_dir))}",
			"gh() {",
			"  if [ \"${2:-}\" = graphql ]; then cat \"${GRAPHQL_FILE}\"; return 0; fi",
			f"  if [ {1 if fail_label else 0} = 1 ] && [[ \"${{2:-}}\" == */labels ]]; then return 1; fi",
			"  local prev='' arg n",
			"  n=\"$(ls \"${POST_DIR}\" | wc -l | tr -d ' ')\"",
			"  for arg in \"$@\"; do",
			"    if [ \"${prev}\" = -f ]; then printf '%s' \"${arg}\" > \"${POST_DIR}/${n}.field\"; fi",
			"    prev=\"${arg}\"",
			"  done",
			"  printf '%s\\n' \"$2\" > \"${POST_DIR}/${n}.path\"",
			"  return 0",
			"}",
			"gh_retry() { \"$@\"; }",
			"UNBLOCK_TRUSTED_LOGIN=''",
			f"unblock_trusted_login() {{ UNBLOCK_TRUSTED_LOGIN={shlex.quote(LOGIN)}; printf '%s' \"${{UNBLOCK_TRUSTED_LOGIN}}\"; }}",
			f"_dispatch_review_for_conflicts() {{ printf '%s %s\\n' \"$1\" \"$2\" >> \"${{DISPATCH_LOG}}\"; case \"$1\" in {cases}esac; return 0; }}",
			batch,
			functions,
		]
		if dump_map:
			lines.append('for _k in "${!_STANDALONE_ISSUELESS_RETRY_STATE[@]}"; do printf "ENTRY %s %s\\n" "${_k}" "${_STANDALONE_ISSUELESS_RETRY_STATE[${_k}]}"; done')
		lines.append('run_review_gate_rate_limit_requeue_pass || echo "REVIEW_GATE_RATE_LIMIT_REQUEUE outcome=skipped reason=error rc=$?"')
		lines.append('echo "SLICE_DONE"')
		harness = tmp_path / "harness.sh"
		harness.write_text("\n".join(lines) + "\n", encoding="utf-8")
		result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=120)
		posts = []
		for path_file in sorted(post_dir.glob("*.path"), key=lambda p: int(p.stem)):
			field = post_dir / f"{path_file.stem}.field"
			posts.append((path_file.read_text(encoding="utf-8").strip(), field.read_text(encoding="utf-8") if field.exists() else ""))
		entries = {}
		for line in result.stdout.splitlines():
			if line.startswith("ENTRY "):
				_, num, payload = line.split(" ", 2)
				entries[int(num)] = json.loads(payload)
		assert result.returncode == 0, result.stderr
		assert "SLICE_DONE" in result.stdout, result.stderr
		return {
			"stdout": result.stdout,
			"lines": [line for line in result.stdout.splitlines() if line.startswith("REVIEW_GATE_RATE_LIMIT_REQUEUE ")],
			"dispatches": dispatch_log.read_text(encoding="utf-8").splitlines() if dispatch_log.exists() else [],
			"posts": posts,
			"entries": entries,
		}


def test_review_requeue_entry_carries_gate_fields_only_when_present() -> None:
	past = time.time() - 3600
	run = _run_pass([_row(1)], [_node(1, [("github-actions", _gate_body(7, past)), (LOGIN, _requeue_body(7)), ("x", "hi")])], dump_map=True)
	comments = run["entries"][1]["comments"]
	assert comments[0]["gate"] == {"head": HEAD_A, "reset": str(int(past)), "run": "7"}
	assert comments[1]["gate_retry"] == {"run": "7", "head": HEAD_A}
	assert comments[2] == {"a": "x", "fail": None, "retry": None}


def test_review_requeue_dispatches_eligible_pr() -> None:
	past = time.time() - 3600
	run = _run_pass([_row(716)], [_node(716, [("github-actions", _gate_body(44983158668, past))])])
	assert run["dispatches"] == ["716 feature/716"]
	assert len(run["posts"]) == 1
	path, field = run["posts"][0]
	assert path == "repos/o/r/issues/716/comments"
	assert field.endswith(f"\n<!-- ai:review-gate-rate-limit-requeue:v1 run=44983158668 head={HEAD_A} -->")
	assert "attempt 1 of 3" in field
	assert len(run["lines"]) == 1 and "outcome=requeued pr=716 repo=o/r" in run["lines"][0] and "attempt=1/3" in run["lines"][0]


def test_review_requeue_skips_and_waits() -> None:
	past = time.time() - 3600
	future = time.time() + 600
	rows = [_row(n) for n in range(1, 8)]
	nodes = [
		_node(1, [("github-actions", _gate_body(1, future))]),
		_node(2, [("github-actions", _gate_body(2, past)), (LOGIN, "AI review summary")]),
		_node(3, [("github-actions", _gate_body(3, past, head=HEAD_B))]),
		_node(4, [("github-actions", _gate_body(4, past))], labels=("ai:needs-human",)),
		_node(5, [("github-actions", _gate_body(5, past)), (LOGIN, _requeue_body(5))]),
		_node(6, [("someone", _gate_body(6, past))]),
		_node(7, [("github-actions", _gate_body(7, past, head="unknown"))], closing=((70, ("ai:implementing",)),)),
	]
	run = _run_pass(rows, nodes)
	by_pr = {int(re.search(r" pr=(\d+) ", line).group(1)): line for line in run["lines"]}
	assert "outcome=waiting_for_reset" in by_pr[1]
	assert "outcome=skipped" in by_pr[2] and "reason=later_review" in by_pr[2]
	assert "reason=head_moved" in by_pr[3]
	assert "reason=excluded_label" in by_pr[4]
	assert "outcome=already_requeued" in by_pr[5]
	assert 6 not in by_pr, "a gate marker from another author is never read"
	assert "outcome=requeued" in by_pr[7], "unknown gate head and a linked pipeline issue still requeue"
	assert run["dispatches"] == ["7 feature/7"]


def test_review_requeue_escalates_after_cap() -> None:
	past = time.time() - 3600
	comments: list[tuple[str, str]] = []
	for r in (1, 2, 3):
		comments += [("github-actions", _gate_body(r, past)), (LOGIN, _requeue_body(r))]
	comments.append(("github-actions", _gate_body(4, past)))
	run = _run_pass([_row(9)], [_node(9, comments)])
	assert run["dispatches"] == []
	paths = [p for p, _ in run["posts"]]
	assert paths == ["repos/o/r/issues/9/labels", "repos/o/r/issues/9/comments"]
	assert run["posts"][0][1] == "labels[]=ai:needs-human"
	body = run["posts"][1][1]
	assert body.endswith("\n<!-- ai:review-gate-rate-limit-escalated:v1 run=4 -->") and "rate limit" in body
	assert not any(line.startswith("/") for line in body.removeprefix("body=").splitlines())
	assert "outcome=escalated" in run["lines"][0] and "attempts=3/3" in run["lines"][0]
	run = _run_pass([_row(9)], [_node(9, comments + [(LOGIN, "stopped\n\n<!-- ai:review-gate-rate-limit-escalated:v1 run=4 -->")])])
	assert run["posts"] == [] and "outcome=already_escalated" in run["lines"][0]
	# Requeues on another head do not count toward this head's cap.
	moved = [(a, b.replace(HEAD_A, HEAD_B)) for a, b in comments[:-1]] + [comments[-1]]
	run = _run_pass([_row(9)], [_node(9, moved)])
	assert run["dispatches"] == ["9 feature/9"]
	# A failed ai:needs-human label write posts no terminal marker.
	run = _run_pass([_row(9)], [_node(9, comments)], fail_label=True)
	assert run["posts"] == [] and run["dispatches"] == []
	assert "outcome=escalated" in run["lines"][0] and "label=failed marker=skipped" in run["lines"][0]


def test_review_requeue_counts_gate_reruns_without_requeue_markers() -> None:
	past = time.time() - 3600
	# Three requeues whose marker writes failed still leave three later gate markers.
	comments = [("github-actions", _gate_body(r, past)) for r in (1, 2, 3, 4)]
	run = _run_pass([_row(9)], [_node(9, comments)])
	assert run["dispatches"] == []
	assert "outcome=escalated" in run["lines"][0] and "attempts=3/3" in run["lines"][0]


def test_review_requeue_counts_unknown_head_reruns_without_requeue_markers() -> None:
	past = time.time() - 3600
	# Re-dispatched (workflow_dispatch) gate runs have no PR head SHA and write
	# head=unknown; with every requeue marker write failed they still count.
	comments = [("github-actions", _gate_body(1, past))] + [("github-actions", _gate_body(r, past, head="unknown")) for r in (2, 3, 4)]
	run = _run_pass([_row(9)], [_node(9, comments)])
	assert run["dispatches"] == []
	assert "outcome=escalated" in run["lines"][0] and "attempts=3/3" in run["lines"][0]
	# Unknown-head markers before a marker that names another head do not count.
	older = [("github-actions", _gate_body(r, past, head="unknown")) for r in (1, 2, 3)]
	comments = older + [("github-actions", _gate_body(4, past, head=HEAD_B)), ("github-actions", _gate_body(5, past, head="unknown"))]
	run = _run_pass([_row(9)], [_node(9, comments)])
	assert run["dispatches"] == ["9 feature/9"]
	assert "attempts=0/3" in run["lines"][0]


def test_review_requeue_completed_review_resets_unknown_head_count() -> None:
	past = time.time() - 3600
	# Unknown-head gate markers from episodes a later review resolved do not
	# count against the current head.
	comments: list[tuple[str, str]] = []
	for r in (1, 2, 3):
		comments += [("github-actions", _gate_body(r, past, head="unknown")), (LOGIN, "AI review summary")]
	comments.append(("github-actions", _gate_body(4, past)))
	run = _run_pass([_row(9)], [_node(9, comments)])
	assert run["dispatches"] == ["9 feature/9"]
	assert "outcome=requeued" in run["lines"][0] and "attempts=0/3" in run["lines"][0]
	# Requeue markers on this head still count across a completed review.
	comments = []
	for r in (1, 2, 3):
		comments += [("github-actions", _gate_body(r, past)), (LOGIN, _requeue_body(r)), (LOGIN, "AI review summary")]
	comments.append(("github-actions", _gate_body(4, past)))
	run = _run_pass([_row(9)], [_node(9, comments)])
	assert run["dispatches"] == []
	assert "outcome=escalated" in run["lines"][0] and "attempts=3/3" in run["lines"][0]


def test_review_requeue_ignores_out_of_range_reset() -> None:
	far = time.time() + 999999
	run = _run_pass([_row(5)], [_node(5, [("github-actions", _gate_body(5, far))])])
	assert run["dispatches"] == ["5 feature/5"]


def test_review_requeue_dispatch_outcomes_and_cycle_cap() -> None:
	past = time.time() - 3600
	run = _run_pass([_row(1), _row(2)], [_node(1, [("github-actions", _gate_body(1, past))]), _node(2, [("github-actions", _gate_body(2, past))])],
		dispatch_rc={1: 2, 2: 1})
	assert run["posts"] == []
	assert "outcome=in_flight pr=1 " in run["lines"][0]
	assert "outcome=skipped pr=2 " in run["lines"][1] and "reason=dispatch_failed" in run["lines"][1]
	rows = [_row(n) for n in range(1, 8)]
	nodes = [_node(n, [("github-actions", _gate_body(n, past))]) for n in range(1, 8)]
	run = _run_pass(rows, nodes)
	assert len(run["dispatches"]) == 5
	assert sum("reason=cycle_cap" in line for line in run["lines"]) == 2


def test_review_requeue_pass_wiring() -> None:
	script = POLLER.read_text(encoding="utf-8")
	issueless = script.index('\nrun_issueless_review_retry_pass || echo "ISSUELESS_REVIEW_RETRY outcome=skipped reason=error rc=$?"\n')
	call = script.index('\nrun_review_gate_rate_limit_requeue_pass || echo "REVIEW_GATE_RATE_LIMIT_REQUEUE outcome=skipped reason=error rc=$?"\n')
	restore = script.rindex("\n_standalone_sweep_alert_level_restore")
	assert issueless < call < restore
	body = _bash_function(script, "run_review_gate_rate_limit_requeue_pass() {")
	assert '_dispatch_review_for_conflicts "${pr}" "${head_ref}"' in body
	assert "retrigger_review" not in body


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

def test_fragments() -> None:
	agents = AGENTS_FRAGMENT.read_text(encoding="utf-8")
	assert '<!-- agents: section="Workflow architecture" -->' in agents
	assert '<!-- agents: section="Stable log prefixes (contractual)" -->' in agents
	for prefix in ("CLARIFY_RATE_LIMIT_MARKER", "CLARIFY_RATE_LIMIT_REQUEUE", "REVIEW_GATE_RATE_LIMIT_MARKER", "REVIEW_GATE_RATE_LIMIT_REQUEUE"):
		assert f"\nLOG_PREFIX.name={prefix}\n" in agents + "\n", prefix
	changelog = CHANGELOG_FRAGMENT.read_text(encoding="utf-8")
	assert changelog.startswith("<!-- changelog: fixed -->")
	assert "Refs #10031." in changelog
