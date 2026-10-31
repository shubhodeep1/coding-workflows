#!/usr/bin/env python3
"""Reconfirm-after-failed-PATCH contract for the weekly retro tracker comment (BUG-005).

GitHub may apply a PATCH and lose the response. Both the source-repo
`Upsert weekly retro tracker comment` step and the consumer fan-out in
`scripts/workflow_retro_fanout.sh` must re-read the comment by ID before
posting a new one, and post only when that GET returns 404.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "workflow_retro_fanout.sh"
WF_PATH = REPO_ROOT / ".github" / "workflows" / "workflow-log-analysis.yml"

_HELPERS_SPEC = importlib.util.spec_from_file_location(
	"_retro_comment_reconfirm_fanout_helpers",
	REPO_ROOT / "tests" / "test_workflow_retro_fanout.py",
)
assert _HELPERS_SPEC is not None and _HELPERS_SPEC.loader is not None
_fanout_helpers = importlib.util.module_from_spec(_HELPERS_SPEC)
_HELPERS_SPEC.loader.exec_module(_fanout_helpers)

_stage_support_tree = _fanout_helpers._stage_support_tree
_install_mock_codex = _fanout_helpers._install_mock_codex
_write_exec = _fanout_helpers._write_exec
_recent_iso = _fanout_helpers._recent_iso
RETRO_BODY = _fanout_helpers.RETRO_BODY

CONSUMER_REPO = "owner/active-repo"
TRACKER_NUMBER = 9100
COMMENT_ID = 555
STALE_SENTINEL = "STALE-RETRO-BODY-SENTINEL"


MOCK_GH = r'''#!/usr/bin/env python3
import glob
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MOCK_GH_STATE_FILE"])
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
args = sys.argv[1:]
mode = os.environ["MOCK_RECONFIRM_MODE"]
comment_id = int(os.environ["MOCK_COMMENT_ID"])
tracker_number = int(os.environ["MOCK_TRACKER_NUMBER"])
stale_sentinel = os.environ["MOCK_STALE_SENTINEL"]


def save():
	state_path.write_text(json.dumps(state), encoding="utf-8")


def first_value(flag):
	for idx, arg in enumerate(args):
		if arg == flag and idx + 1 < len(args):
			return args[idx + 1]
	return ""


def week_label():
	pattern = os.path.join(os.environ["TMPDIR"], "workflow-retro-fanout.*", "*-context.json")
	for path in sorted(glob.glob(pattern)):
		data = json.loads(Path(path).read_text(encoding="utf-8"))
		return data["window"]["week_label"]
	return "unknown-week"


def stale_body():
	return f"<!-- ai:workflow-retro:{week_label()} -->\n\n{stale_sentinel}"


state.setdefault("calls", []).append(args)

if args and args[0] == "api":
	method = first_value("-X") or "GET"
	path_arg = next((a for a in args[1:] if "/" in a and not a.startswith("-")), "")
	if "actions/variables/WORKFLOW_RETRO_ENABLED" in path_arg:
		save()
		print("true")
		sys.exit(0)
	if method == "GET" and path_arg.startswith(f"repos/") and f"/issues/{tracker_number}/comments" in path_arg:
		state.setdefault("events", []).append("LIST")
		save()
		print(json.dumps([{"id": comment_id, "body": stale_body()}]))
		sys.exit(0)
	if method == "PATCH" and path_arg.endswith(f"/issues/comments/{comment_id}"):
		state.setdefault("events", []).append("PATCH")
		payload = json.loads(Path(first_value("--input")).read_text(encoding="utf-8"))
		if mode == "match":
			# The PATCH landed server-side but the response was lost.
			state["server_body"] = payload["body"]
		save()
		print("HTTP 502: Bad Gateway", file=sys.stderr)
		sys.exit(1)
	if method == "GET" and path_arg.endswith(f"/issues/comments/{comment_id}"):
		state.setdefault("events", []).append("RECONFIRM_GET")
		save()
		if mode == "match":
			print(json.dumps({"id": comment_id, "body": state.get("server_body", "")}))
			sys.exit(0)
		if mode == "stale":
			print(json.dumps({"id": comment_id, "body": stale_body()}))
			sys.exit(0)
		if mode == "404":
			print("gh: Not Found (HTTP 404)", file=sys.stderr)
			sys.exit(1)
		print("HTTP 500: Server Error", file=sys.stderr)
		sys.exit(1)
	if method == "POST" and path_arg.endswith(f"/issues/{tracker_number}/comments"):
		state.setdefault("events", []).append("POST")
		save()
		print(json.dumps({"id": comment_id + 1}))
		sys.exit(0)
	save()
	print(f"unexpected gh api args: {args}", file=sys.stderr)
	sys.exit(1)

if args[:2] == ["label", "create"]:
	save()
	sys.exit(0)

if args[:2] == ["issue", "list"]:
	save()
	print("[]")
	sys.exit(0)

if args[:2] == ["issue", "create"]:
	repo = first_value("--repo") or "owner/repo"
	save()
	print(f"https://github.com/{repo}/issues/{tracker_number}")
	sys.exit(0)

if args[:2] == ["issue", "edit"]:
	save()
	sys.exit(0)

save()
print(f"unexpected gh args: {args}", file=sys.stderr)
sys.exit(1)
'''


def _activity_report() -> dict:
	return {
		"runs": [
			{
				"repository": CONSUMER_REPO,
				"run_id": idx + 1,
				"workflow_name": f"Workflow {idx + 1}",
				"workflow_family": "review_autofix",
				"conclusion": "success",
				"duration_seconds": 60,
				"created_at": _recent_iso(days_ago=1),
			}
			for idx in range(2)
		],
		"summary": {},
		"scope": {},
		"errors": [],
	}


def _run_reconfirm_fanout(mode: str) -> tuple[subprocess.CompletedProcess[str], dict]:
	with tempfile.TemporaryDirectory(prefix="retro-reconfirm-test-") as td:
		tmp_path = Path(td)
		bin_dir = tmp_path / "bin"
		bin_dir.mkdir(parents=True, exist_ok=True)
		runtime_tmp = tmp_path / "tmp"
		runtime_tmp.mkdir()
		state_file = tmp_path / "gh-state.json"
		state_file.write_text("{}", encoding="utf-8")
		_write_exec(bin_dir / "gh", MOCK_GH)
		_install_mock_codex(bin_dir)
		support_root = tmp_path / "support"
		support_root.mkdir()
		staged_script = _stage_support_tree(support_root)
		workspace = tmp_path / "workspace"
		subprocess.run(["git", "init", "-q", str(workspace)], check=True)
		(bin_dir / "python3").symlink_to(sys.executable)

		report_file = tmp_path / "workflow_log_report.json"
		report_file.write_text(json.dumps(_activity_report()), encoding="utf-8")
		roster_file = tmp_path / "consumer_repos.json"
		roster_file.write_text(json.dumps([CONSUMER_REPO]), encoding="utf-8")

		env = os.environ.copy()
		env.update(
			{
				"GH_TOKEN": "test-token",
				"GITHUB_REPOSITORY": "owner/source",
				"GITHUB_WORKSPACE": str(workspace),
				"OPENROUTER_API_KEY": "test-openrouter-key",
				"MOCK_CODEX_OUTPUT": RETRO_BODY,
				"MOCK_GH_STATE_FILE": str(state_file),
				"MOCK_RECONFIRM_MODE": mode,
				"MOCK_COMMENT_ID": str(COMMENT_ID),
				"MOCK_TRACKER_NUMBER": str(TRACKER_NUMBER),
				"MOCK_STALE_SENTINEL": STALE_SENTINEL,
				"PATH": f"{bin_dir}:{env.get('PATH', '')}",
				"PYTHONDONTWRITEBYTECODE": "1",
				"WORKFLOW_LOG_REPORT_FILE": str(report_file),
				"CONSUMER_REPOS_FILE": str(roster_file),
				"CODEX_RETRY_BACKOFF_BASE_SECS": "1",
				"GH_RETRY_MAX_ATTEMPTS": "1",
				"TMPDIR": str(runtime_tmp),
				# Keep engine selection on the mock codex path.
				"CLAUDE_ENGINE_SUPPORT_DIR": str(tmp_path / "no-claude-engine-support"),
			}
		)
		proc = subprocess.run(
			["bash", str(staged_script)],
			cwd=REPO_ROOT,
			env=env,
			capture_output=True,
			text=True,
			encoding="utf-8",
		)
		final_state = json.loads(state_file.read_text(encoding="utf-8"))
		return proc, final_state


def _assert_reconfirm_shape(proc: subprocess.CompletedProcess[str], state: dict) -> list[str]:
	events = state.get("events", [])
	assert "PATCH" in events, (events, proc.stdout, proc.stderr)
	patch_index = events.index("PATCH")
	assert events.count("RECONFIRM_GET") == 1, (events, proc.stdout, proc.stderr)
	assert events.index("RECONFIRM_GET") > patch_index, events
	for line in (proc.stdout + proc.stderr).splitlines():
		if "::warning::" in line:
			assert STALE_SENTINEL not in line, line
	return events


def test_patch_fails_get_matches_no_post() -> None:
	proc, state = _run_reconfirm_fanout("match")
	events = _assert_reconfirm_shape(proc, state)
	assert "POST" not in events, events
	assert f"status=refreshed tracker=#{TRACKER_NUMBER}" in proc.stdout, proc.stdout + proc.stderr
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_patch_fails_get_stale_no_post_returns_failure() -> None:
	proc, state = _run_reconfirm_fanout("stale")
	events = _assert_reconfirm_shape(proc, state)
	assert "POST" not in events, events
	assert f"WORKFLOW_RETRO_FANOUT_V1: repo={CONSUMER_REPO} status=failed" in proc.stdout
	assert (
		f"retro comment #{COMMENT_ID} exists on {CONSUMER_REPO} but was not refreshed; not posting a duplicate."
	) in proc.stdout
	assert proc.returncode == 1, proc.stdout + proc.stderr


def test_patch_fails_get_404_posts_exactly_once() -> None:
	proc, state = _run_reconfirm_fanout("404")
	events = _assert_reconfirm_shape(proc, state)
	assert events.count("POST") == 1, events
	assert events.index("POST") > events.index("RECONFIRM_GET"), events
	assert f"status=posted tracker=#{TRACKER_NUMBER}" in proc.stdout, proc.stdout + proc.stderr
	assert proc.returncode == 0, proc.stdout + proc.stderr


def test_patch_fails_get_500_no_post() -> None:
	proc, state = _run_reconfirm_fanout("500")
	events = _assert_reconfirm_shape(proc, state)
	assert "POST" not in events, events
	assert f"WORKFLOW_RETRO_FANOUT_V1: repo={CONSUMER_REPO} status=failed" in proc.stdout
	assert "could not reconfirm it; not posting a duplicate." in proc.stdout
	assert proc.returncode == 1, proc.stdout + proc.stderr


def _assert_post_only_after_reconfirm(block: str, patch_line: str, get_line: str, post_line: str) -> str:
	patch_index = block.index(patch_line)
	get_index = block.index(get_line)
	post_index = block.index(post_line)
	assert patch_index < get_index < post_index, (patch_index, get_index, post_index)
	# The only POST must sit after the reconfirm GET.
	assert block.count("-X POST") == 1
	between = block[get_index:post_index]
	assert "HTTP 404|Not Found" in between
	assert "not posting a duplicate" in between
	comment_region = block[patch_index:get_index]
	assert "only on this PATCH-failure path" in comment_region
	assert "week-scoped" in comment_region
	return between


def test_workflow_retro_tracker_post_only_after_reconfirm_get() -> None:
	wf = WF_PATH.read_text(encoding="utf-8")
	start = wf.index("- name: Upsert weekly retro tracker comment")
	end = wf.index("- name: Consumer retro fan-out", start)
	block = wf[start:end]
	between = _assert_post_only_after_reconfirm(
		block,
		'gh_retry gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_comment_id}"',
		'gh_retry gh api "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_comment_id}"',
		'gh_retry gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${TRACKER_NUMBER}/comments"',
	)
	assert "exit 0" in between
	assert "exit 1" in between
	assert "will try posting a new comment." not in block


def test_fanout_script_post_only_after_reconfirm_get() -> None:
	script = SCRIPT_PATH.read_text(encoding="utf-8")
	start = script.index("run_consumer_retro() {")
	end = script.index("\nfor fanout_repo in", start)
	block = script[start:end]
	between = _assert_post_only_after_reconfirm(
		block,
		'gh_retry gh api -X PATCH "repos/${target_repo}/issues/comments/${existing_comment_id}"',
		'gh_retry gh api "repos/${target_repo}/issues/comments/${existing_comment_id}"',
		'gh_retry gh api -X POST "repos/${target_repo}/issues/${FANOUT_TRACKER_NUMBER}/comments"',
	)
	assert "return 0" in between
	assert "return 1" in between
	assert "; posting a new comment.\"\n\tfi" not in block


def main() -> int:
	for name in sorted(globals()):
		if name.startswith("test_") and callable(globals()[name]):
			globals()[name]()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
